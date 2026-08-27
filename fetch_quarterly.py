#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四半期のEPS・BPSを取得する（過去のPER/PBRを復元するため）。

設計の考え方:
  PER = 株価 ÷ EPS、PBR = 株価 ÷ BPS。
  株価は既に27年分あるので、EPSとBPSの四半期データさえあれば
  任意の時点のPER/PBRを計算できる。

先読みバイアスの防ぎ方（point-in-time）:
  決算は期末から45日以内に発表される。
  そこで「1四半期前まで」の確定値だけを使う（約90日のラグ）。
  こうすると発表日の精度に依存せず、構造的に先読みが起きない。
  実際のトレーダーが画面で見る実績PERと同じ考え方でもある。

出力:
  data/quarterly.csv  ticker, period_end, net_income, equity, shares, eps_q, bps
  → 別途、株価と突き合わせてPER/PBRの時系列を作る
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
OUT = "data/quarterly.csv"
CACHE = "data/quarterly_cache.json"
JST = timezone(timedelta(hours=9))
PAUSE = float(os.environ.get("PAUSE", "2.0"))
MAX_TICKERS = int(os.environ.get("MAX_TICKERS", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)


def pick(df, names):
    """財務諸表から該当する行を探す（表記ゆれに対応）。"""
    if df is None or len(df) == 0:
        return None
    idx = {str(i).strip().lower(): i for i in df.index}
    for n in names:
        k = n.strip().lower()
        if k in idx:
            return df.loc[idx[k]]
    for n in names:
        k = n.strip().lower()
        for key, orig in idx.items():
            if k in key:
                return df.loc[orig]
    return None


def fetch_one(t):
    """1銘柄の四半期データを取り出す。"""
    import yfinance as yf

    tk = yf.Ticker(t)
    inc = tk.quarterly_income_stmt
    bal = tk.quarterly_balance_sheet
    if inc is None or len(inc) == 0:
        return None
    ni = pick(inc, ["Net Income", "Net Income Common Stockholders",
                    "Net Income From Continuing Operation Net Minority Interest"])
    eq = pick(bal, ["Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"])
    sh = pick(bal, ["Ordinary Shares Number", "Share Issued", "Common Stock"])
    if ni is None:
        return None
    out = []
    for col in inc.columns:
        try:
            d = pd.Timestamp(col).date()
        except Exception:
            continue
        rec = {"period_end": str(d),
               "net_income": float(ni[col]) if col in ni.index and pd.notna(ni[col]) else None}
        for key, src in (("equity", eq), ("shares", sh)):
            v = None
            if src is not None and col in src.index and pd.notna(src[col]):
                v = float(src[col])
            rec[key] = v
        out.append(rec)
    return out or None


def load_cache():
    if not os.path.exists(CACHE):
        return {}
    try:
        with open(CACHE, encoding="utf-8") as f:
            c = json.load(f)
        if time.time() - c.get("saved_at", 0) < 14 * 24 * 3600:   # 2週間有効
            logging.info("キャッシュ再利用 %d銘柄", len(c.get("data", {})))
            return c.get("data", {})
    except Exception as e:
        logging.warning("キャッシュ読み込み失敗: %s", e)
    return {}


def save_cache(data):
    try:
        os.makedirs("data", exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.time(), "data": data}, f)
    except Exception as e:
        logging.warning("キャッシュ保存失敗: %s", e)


def main():
    if not os.path.exists(UNIVERSE):
        logging.error("%s がありません", UNIVERSE)
        return 1
    uni = pd.read_csv(UNIVERSE)
    tickers = uni["ticker"].astype(str).tolist()
    if MAX_TICKERS:
        tickers = tickers[:MAX_TICKERS]

    data = load_cache()
    todo = [t for t in tickers if t not in data]
    logging.info("対象 %d銘柄 / 未取得 %d銘柄（間隔 %.1f秒）", len(tickers), len(todo), PAUSE)

    fails, cur, t0 = 0, PAUSE, time.time()
    for i, t in enumerate(todo, 1):
        rec = None
        for attempt in range(3):
            try:
                rec = fetch_one(t)
                break
            except Exception as e:
                msg = str(e).lower()
                wait = 30.0 if ("429" in msg or "rate" in msg or "too many" in msg) else 3.0
                time.sleep(wait * (attempt + 1))
        data[t] = rec
        if rec:
            fails = 0
            cur = max(PAUSE, cur * 0.9)
        else:
            fails += 1
            cur = min(cur * 1.4, 10.0)
            if fails >= 10:
                logging.warning("連続%d件失敗。60秒休止", fails)
                time.sleep(60)
                fails = 0
        if i % 50 == 0 or i == len(todo):
            got = sum(1 for v in data.values() if v)
            logging.info("%d/%d（成功 %d / 間隔 %.1f秒 / 残り約%.0f分）",
                         i, len(todo), got, cur,
                         (time.time() - t0) / i * (len(todo) - i) / 60)
            save_cache(data)
        time.sleep(cur)
    save_cache(data)

    rows = []
    for t in tickers:
        for r in (data.get(t) or []):
            rows.append({"ticker": t, **r})
    if not rows:
        logging.error("1件も取得できませんでした")
        return 1
    df = pd.DataFrame(rows)
    df["period_end"] = pd.to_datetime(df["period_end"])
    df = df.sort_values(["ticker", "period_end"])
    # 1株当たりに換算
    df["eps_q"] = df["net_income"] / df["shares"]
    df["bps"] = df["equity"] / df["shares"]
    df = df[["ticker", "period_end", "net_income", "equity", "shares", "eps_q", "bps"]]
    os.makedirs("data", exist_ok=True)
    df.to_csv(OUT, index=False, encoding="utf-8")

    n_ok = df.groupby("ticker").size()
    manifest = {
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "tickers_requested": len(tickers),
        "tickers_with_data": int(df.ticker.nunique()),
        "rows": len(df),
        "period_min": str(df.period_end.min().date()),
        "period_max": str(df.period_end.max().date()),
        "eps_available": int(df.eps_q.notna().sum()),
        "bps_available": int(df.bps.notna().sum()),
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
    }
    with open("data/quarterly_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\n取得 {df.ticker.nunique()}/{len(tickers)}銘柄 / {len(df):,}行")
    print(f"期間 {manifest['period_min']} 〜 {manifest['period_max']}")
    print(f"EPS計算可 {manifest['eps_available']:,}行 / BPS計算可 {manifest['bps_available']:,}行")
    print(f"1銘柄あたりの四半期数: 中央値 {int(n_ok.median())} / 最大 {int(n_ok.max())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
