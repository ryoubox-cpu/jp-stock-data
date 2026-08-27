#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""決算発表日つきの実績EPSを取得する（過去のPERを先読みなしで復元するため）。

診断で判明した事実にもとづく実装:
  ・get_earnings_dates(limit=90) で最大90期（約22年）返る
  ・列名は 'EPS Estimate' / 'Reported EPS' / 'Surprise(%)'
    （'actual' ではない。前回の診断はここを取り違えた）
  ・インデックスは 'Earnings Date'（タイムゾーン付き、米国時間で入っている）
  ・最新行の Reported EPS は NaN（未発表のため）
  ・年次の income_stmt / balance_sheet は4〜5期ぶん取れる（純資産・株数用）

先読みバイアスの防ぎ方:
  各EPSに発表日が紐づいているので、「その日以降でないと使わない」を厳守できる。
  発表日はタイムゾーン付きなので、日本時間の日付に正規化して保存する。
  保守的に、発表日の翌営業日から有効とみなす想定（利用側で実装）。

出力:
  data/eps_history.csv   ticker, earnings_date, eps_estimate, eps_reported, surprise
  data/annual_bs.csv     ticker, period_end, net_income, equity, shares, eps_annual
  data/eps_manifest.json
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

UNIVERSE = "data/universe.csv"
OUT_EPS = "data/eps_history.csv"
OUT_BS = "data/annual_bs.csv"
CACHE = "data/eps_cache.json"
MANIFEST = "data/eps_manifest.json"
JST = timezone(timedelta(hours=9))

PAUSE = float(os.environ.get("PAUSE", "1.5"))
MAX_TICKERS = int(os.environ.get("MAX_TICKERS", "0"))
LIMIT = int(os.environ.get("EPS_LIMIT", "100"))
SKIP_BS = os.environ.get("SKIP_BS", "0") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)


def find_col(cols, *keywords):
    """列名を柔軟に探す（表記ゆれ・大小文字に対応）。"""
    low = {str(c).strip().lower(): c for c in cols}
    for kw in keywords:
        k = kw.strip().lower()
        if k in low:
            return low[k]
    for kw in keywords:
        k = kw.strip().lower()
        for lc, orig in low.items():
            if k in lc:
                return orig
    return None


def fetch_eps(tk):
    """決算発表日つきEPSを取り出す。"""
    ed = tk.get_earnings_dates(limit=LIMIT)
    if ed is None or len(ed) == 0:
        return None
    df = ed.reset_index()
    date_col = find_col(df.columns, "Earnings Date", "earnings date", "date")
    est_col = find_col(df.columns, "EPS Estimate", "estimate")
    rep_col = find_col(df.columns, "Reported EPS", "reported", "actual")
    sur_col = find_col(df.columns, "Surprise(%)", "surprise")
    if date_col is None or rep_col is None:
        return None
    out = pd.DataFrame({"earnings_date_raw": df[date_col]})
    out["eps_reported"] = pd.to_numeric(df[rep_col], errors="coerce") if rep_col else None
    out["eps_estimate"] = pd.to_numeric(df[est_col], errors="coerce") if est_col else None
    out["surprise"] = pd.to_numeric(df[sur_col], errors="coerce") if sur_col else None
    return out


def fetch_bs(tk):
    """年次の純利益・純資産・株数を取り出す（BPS計算用）。"""
    try:
        inc = tk.income_stmt
        bal = tk.balance_sheet
    except Exception:
        return None
    if (inc is None or len(inc) == 0) and (bal is None or len(bal) == 0):
        return None
    rows = []
    cols = set()
    for d in (inc, bal):
        if d is not None and len(d):
            cols |= set(d.columns)
    for c in cols:
        rec = {"period_end": str(pd.Timestamp(c).date())}
        for df_, key, name in [(inc, "Net Income", "net_income"),
                               (inc, "Basic EPS", "eps_annual"),
                               (bal, "Stockholders Equity", "equity"),
                               (bal, "Ordinary Shares Number", "shares")]:
            v = None
            if df_ is not None and len(df_) and key in df_.index and c in df_.columns:
                x = df_.loc[key, c]
                if pd.notna(x):
                    v = float(x)
            rec[name] = v
        rows.append(rec)
    return rows or None


def load_cache():
    if not os.path.exists(CACHE):
        return {}
    try:
        with open(CACHE, encoding="utf-8") as f:
            c = json.load(f)
        age = time.time() - c.get("saved_at", 0)
        if age < 14 * 24 * 3600:
            logging.info("キャッシュ再利用 %d銘柄（%.1f時間前）",
                         len(c.get("data", {})), age / 3600)
            return c.get("data", {})
    except Exception as e:
        logging.warning("キャッシュ読み込み失敗: %s", e)
    return {}


def save_cache(data):
    try:
        os.makedirs("data", exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.time(), "data": data}, f)
        os.replace(tmp, CACHE)
    except Exception as e:
        logging.warning("キャッシュ保存失敗: %s", e)


def main():
    import yfinance as yf

    if not os.path.exists(UNIVERSE):
        logging.error("%s がありません", UNIVERSE)
        return 1
    uni = pd.read_csv(UNIVERSE)
    tickers = uni["ticker"].astype(str).str.strip().tolist()
    if MAX_TICKERS:
        tickers = tickers[:MAX_TICKERS]

    data = load_cache()
    todo = [t for t in tickers if t not in data]
    logging.info("対象 %d銘柄 / 未取得 %d銘柄 / 間隔 %.1f秒 / limit=%d / 年次BS=%s",
                 len(tickers), len(todo), PAUSE, LIMIT, "取得しない" if SKIP_BS else "取得する")

    fails, cur, t0 = 0, PAUSE, time.time()
    for i, t in enumerate(todo, 1):
        rec = {"eps": None, "bs": None}
        for attempt in range(3):
            try:
                tk = yf.Ticker(t)
                e = fetch_eps(tk)
                if e is not None and len(e):
                    rec["eps"] = e.to_dict("records")
                    # 日付をJSON化できる形に
                    for r in rec["eps"]:
                        r["earnings_date_raw"] = str(r["earnings_date_raw"])
                if not SKIP_BS:
                    rec["bs"] = fetch_bs(tk)
                break
            except Exception as ex:
                msg = str(ex).lower()
                wait = 30.0 if ("429" in msg or "rate" in msg or "too many" in msg) else 3.0
                if attempt == 2:
                    logging.debug("%s 取得失敗: %s", t, str(ex)[:80])
                time.sleep(wait * (attempt + 1))
        data[t] = rec
        if rec["eps"]:
            fails = 0
            cur = max(PAUSE, cur * 0.92)
        else:
            fails += 1
            cur = min(cur * 1.3, 8.0)
            if fails >= 15:
                logging.warning("連続%d件失敗。60秒休止して継続", fails)
                time.sleep(60)
                fails = 0
        if i % 50 == 0 or i == len(todo):
            got = sum(1 for v in data.values() if v and v.get("eps"))
            el = time.time() - t0
            logging.info("%d/%d（EPS取得 %d / 間隔 %.1f秒 / 残り約%.0f分）",
                         i, len(todo), got, cur, el / i * (len(todo) - i) / 60)
            save_cache(data)
        time.sleep(cur)
    save_cache(data)

    # ---- EPS履歴を整形 ----
    eps_rows = []
    for t in tickers:
        for r in ((data.get(t) or {}).get("eps") or []):
            eps_rows.append({"ticker": t, **r})
    if not eps_rows:
        logging.error("EPSを1件も取得できませんでした。既存ファイルを維持して終了")
        return 1
    E = pd.DataFrame(eps_rows)
    # タイムゾーン付きの日時を日本時間の日付に正規化する
    dt = pd.to_datetime(E["earnings_date_raw"], errors="coerce", utc=True)
    E["earnings_date"] = dt.dt.tz_convert("Asia/Tokyo").dt.date
    E = E.drop(columns=["earnings_date_raw"])
    E = E.dropna(subset=["earnings_date"])
    # 同一銘柄・同一日の重複を除去（最後の値を採用）
    E = (E.sort_values(["ticker", "earnings_date"])
           .drop_duplicates(subset=["ticker", "earnings_date"], keep="last")
           .reset_index(drop=True))
    E = E[["ticker", "earnings_date", "eps_estimate", "eps_reported", "surprise"]]
    os.makedirs("data", exist_ok=True)
    E.to_csv(OUT_EPS, index=False, encoding="utf-8")

    # ---- 年次の純資産・株数 ----
    n_bs = 0
    if not SKIP_BS:
        bs_rows = []
        for t in tickers:
            for r in ((data.get(t) or {}).get("bs") or []):
                bs_rows.append({"ticker": t, **r})
        if bs_rows:
            B = pd.DataFrame(bs_rows)
            B["period_end"] = pd.to_datetime(B["period_end"], errors="coerce")
            B = (B.dropna(subset=["period_end"])
                   .sort_values(["ticker", "period_end"])
                   .drop_duplicates(subset=["ticker", "period_end"], keep="last"))
            B["bps"] = B["equity"] / B["shares"]
            B.to_csv(OUT_BS, index=False, encoding="utf-8")
            n_bs = len(B)

    rep = E.dropna(subset=["eps_reported"])
    per_tk = rep.groupby("ticker").size()
    manifest = {
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "tickers_requested": len(tickers),
        "tickers_with_eps": int(E.ticker.nunique()),
        "eps_rows": int(len(E)),
        "eps_reported_rows": int(len(rep)),
        "date_min": str(rep.earnings_date.min()) if len(rep) else None,
        "date_max": str(rep.earnings_date.max()) if len(rep) else None,
        "median_quarters_per_ticker": int(per_tk.median()) if len(per_tk) else 0,
        "annual_bs_rows": n_bs,
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
    }
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("\n=== 取得結果 ===")
    print(f"EPS取得できた銘柄 {E.ticker.nunique()}/{len(tickers)}")
    print(f"実績EPSの行数 {len(rep):,} / 期間 {manifest['date_min']} 〜 {manifest['date_max']}")
    print(f"1銘柄あたりの四半期数: 中央値 {manifest['median_quarters_per_ticker']} / "
          f"最大 {int(per_tk.max()) if len(per_tk) else 0}")
    if len(rep):
        yrs = (pd.Timestamp(manifest["date_max"]) - pd.Timestamp(manifest["date_min"])).days / 365.25
        print(f"遡及年数 {yrs:.1f}年")
        by_year = rep.groupby(pd.to_datetime(rep.earnings_date).dt.year).size()
        print("\n年別の件数（抜粋）")
        print(by_year.tail(12).to_string())
    if n_bs:
        print(f"\n年次BS {n_bs:,}行（BPS計算用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
