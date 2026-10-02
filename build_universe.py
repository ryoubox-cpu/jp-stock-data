#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""時価総額でユニバースを構築する（月1回実行を想定）。

手順:
  1. JPXの「東証上場銘柄一覧」(data_j.xls) を自動で探して取得
  2. 内国株（既定: プライム・スタンダード・グロース）に絞る
  3. yfinance で時価総額を取得し、閾値以上を残す
  4. 前回ユニバースにいた銘柄は、閾値を割っても上場中なら残す（データが途切れないように）
  5. data/universe.csv に保存

2026-10 変更点:
  ・既定の下限を300億円に引き下げ、スタンダード・グロースも対象に
  ・英字入りの新コード（285A など）を取りこぼしていた不具合を修正
  ・銘柄数が前回の半分未満になったら保存しない安全装置を追加

JPXが取れない場合は universe_seed.txt（1行1コード）にフォールバックする。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import logging

import pandas as pd
import requests

MIN_MARKET_CAP = float(os.environ.get("MIN_MARKET_CAP", 30e9))    # 下限（既定300億円）
# 対象市場（カンマ区切り）。JPX一覧の「市場・商品区分」に含まれる語で判定する
MARKETS = [m.strip() for m in os.environ.get("MARKETS", "プライム,スタンダード,グロース").split(",") if m.strip()]
KEEP_EXISTING = os.environ.get("KEEP_EXISTING", "1") == "1"       # 前回の銘柄を残すか
CODE_RE = r"\d[0-9A-Z]\d[0-9A-Z]"     # 1301 も 285A も通す（2024年以降の英字入りコード対応）
MAX_TICKERS = int(os.environ.get("MAX_TICKERS", 0))               # 0なら無制限（時価総額上位N銘柄に絞る）
JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
OUT = "data/universe.csv"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)


def find_jpx_xls_url() -> str | None:
    """JPXのページから data_j.xls のリンクを探す（URLは時々変わるため固定しない）。"""
    try:
        r = requests.get(JPX_PAGE, headers=UA, timeout=30)
        r.raise_for_status()
        m = re.search(r'href="([^"]*data_j\.xls)"', r.text)
        if not m:
            logging.warning("JPXページ内に data_j.xls のリンクが見つかりません")
            return None
        href = m.group(1)
        return href if href.startswith("http") else "https://www.jpx.co.jp" + href
    except Exception as e:
        logging.warning("JPXページ取得失敗: %s", e)
        return None


def load_jpx_listing() -> pd.DataFrame | None:
    url = find_jpx_xls_url()
    if not url:
        return None
    try:
        logging.info("JPX銘柄一覧を取得: %s", url)
        r = requests.get(url, headers=UA, timeout=60)
        r.raise_for_status()
        with open("/tmp/data_j.xls", "wb") as f:
            f.write(r.content)
        df = pd.read_excel("/tmp/data_j.xls")
        df.columns = [str(c).strip() for c in df.columns]
        code_col = next((c for c in df.columns if "コード" in c), None)
        name_col = next((c for c in df.columns if "銘柄名" in c), None)
        mkt_col = next((c for c in df.columns if "市場" in c and "区分" in c), None)
        if not code_col or not name_col:
            logging.warning("想定した列が見つかりません: %s", list(df.columns))
            return None
        out = pd.DataFrame({
            "code": df[code_col].astype(str).str.strip().str.upper().str.replace(r"\.0$", "", regex=True),
            "meigara": df[name_col].astype(str).str.strip(),
            "market": df[mkt_col].astype(str) if mkt_col else "",
        })
        # 内国株のみ（4桁コード。英字入りの新コードも含む）
        out = out[out["code"].str.fullmatch(CODE_RE)]
        if mkt_col:
            is_domestic = out["market"].str.contains("内国", na=False)
            in_market = out["market"].apply(lambda s: any(m in s for m in MARKETS))
            out = out[is_domestic & in_market].copy()
            # 市場名を短く（例: 「プライム（内国株式）」→「プライム」）
            out["market"] = out["market"].str.replace(r"（.*?）|\(.*?\)", "", regex=True).str.strip()
            logging.info("市場別: %s", out["market"].value_counts().to_dict())
        logging.info("JPX一覧から %d 銘柄", len(out))
        return out.reset_index(drop=True)
    except Exception as e:
        logging.warning("JPX一覧の読み込み失敗: %s", e)
        return None


def load_seed() -> pd.DataFrame | None:
    if not os.path.exists("universe_seed.txt"):
        return None
    codes = []
    with open("universe_seed.txt", encoding="utf-8") as f:
        for line in f:
            c = line.strip().split(",")[0].strip()
            if re.fullmatch(CODE_RE, c.upper()):
                codes.append(c)
    if not codes:
        return None
    logging.info("シードファイルから %d 銘柄", len(codes))
    return pd.DataFrame({"code": codes, "meigara": codes, "market": "seed"})


def fetch_market_caps(tickers: list[str], pause: float = None) -> dict:
    """時価総額を取得する。

    yfinanceは1銘柄ずつの問い合わせになるためレート制限に弱い。
    対策:
      - 間隔を広めに取る（既定1.2秒）
      - 失敗が続いたら自動で減速し、長めに休む
      - 途中経過をキャッシュに保存し、再実行時は続きから
    """
    import yfinance as yf

    pause = float(os.environ.get("PAUSE", 1.2)) if pause is None else pause
    cache_path = "data/market_cap_cache.json"
    caps = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            age = time.time() - cached.get("saved_at", 0)
            if age < 7 * 24 * 3600:              # 1週間以内なら再利用
                caps = {k: v for k, v in cached.get("caps", {}).items() if v}
                logging.info("キャッシュから %d 銘柄を再利用（%.1f時間前）", len(caps), age / 3600)
        except Exception as e:
            logging.warning("キャッシュ読み込み失敗: %s", e)

    todo = [t for t in tickers if t not in caps]
    logging.info("時価総額の取得対象 %d 銘柄（間隔 %.1f秒）", len(todo), pause)

    consecutive_fail = 0
    cur_pause = pause
    t0 = time.time()
    for i, t in enumerate(todo, 1):
        cap = None
        for attempt in range(3):
            try:
                cap = yf.Ticker(t).fast_info.market_cap
                break
            except Exception as e:
                msg = str(e).lower()
                # レート制限らしき応答は長めに待つ
                wait = 30.0 if ("429" in msg or "rate" in msg or "too many" in msg) else 3.0
                time.sleep(wait * (attempt + 1))
        caps[t] = float(cap) if cap else None

        if cap:
            consecutive_fail = 0
            cur_pause = max(pause, cur_pause * 0.9)      # 順調なら少しずつ戻す
        else:
            consecutive_fail += 1
            cur_pause = min(cur_pause * 1.5, 10.0)       # 失敗したら減速
            if consecutive_fail >= 10:
                logging.warning("連続%d件失敗。60秒休止して減速します", consecutive_fail)
                time.sleep(60)
                consecutive_fail = 0

        if i % 50 == 0 or i == len(todo):
            got = sum(1 for v in caps.values() if v)
            el = time.time() - t0
            eta = el / i * (len(todo) - i) / 60
            logging.info("%d/%d 処理（成功 %d / 間隔 %.1f秒 / 残り約%.0f分）",
                         i, len(todo), got, cur_pause, eta)
            try:
                os.makedirs("data", exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump({"saved_at": time.time(), "caps": caps}, f)
            except Exception as e:
                logging.warning("キャッシュ保存失敗: %s", e)
        time.sleep(cur_pause)

    try:
        os.makedirs("data", exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.time(), "caps": caps}, f)
    except Exception:
        pass
    return caps


def main() -> int:
    listing = load_jpx_listing()
    if listing is None or listing.empty:
        listing = load_seed()
    if listing is None or listing.empty:
        logging.error("ユニバースの元データが取得できません")
        return 1

    listing["ticker"] = listing["code"] + ".T"
    caps = fetch_market_caps(listing["ticker"].tolist())
    listing["market_cap"] = listing["ticker"].map(caps)

    got = listing["market_cap"].notna().sum()
    logging.info("時価総額を取得できたのは %d / %d 銘柄", got, len(listing))
    if got < len(listing) * 0.3:
        logging.error("取得率が低すぎます（レート制限の可能性）。既存ユニバースを維持します")
        return 1

    listing["above_min"] = listing["market_cap"] >= MIN_MARKET_CAP
    sel = listing[listing["above_min"]].copy()
    sel = sel.sort_values("market_cap", ascending=False).reset_index(drop=True)
    logging.info("時価総額 %.0f億円以上: %d 銘柄", MIN_MARKET_CAP / 1e8, len(sel))
    if MAX_TICKERS and len(sel) > MAX_TICKERS:
        sel = sel.head(MAX_TICKERS).reset_index(drop=True)
        logging.info("上位 %d 銘柄に制限", MAX_TICKERS)

    if len(sel) == 0:
        logging.error("該当ゼロ。既存ユニバースを維持します")
        return 1

    # 前回いた銘柄は、上場中なら閾値を割っても残す。
    # 外すと株価データがそこで途切れ、上場廃止と区別がつかなくなる（生存者バイアスの一因）。
    prev = None
    if os.path.exists(OUT):
        try:
            prev = pd.read_csv(OUT)
        except Exception as e:
            logging.warning("前回ユニバースの読み込み失敗: %s", e)
    if KEEP_EXISTING and prev is not None and len(prev):
        keep = listing[listing["ticker"].isin(prev["ticker"]) & ~listing["ticker"].isin(sel["ticker"])]
        if len(keep):
            logging.info("前回からの継続（閾値未満だが上場中）: %d 銘柄", len(keep))
            sel = pd.concat([sel, keep], ignore_index=True)

    # 安全装置: 前回の半分未満なら、取得失敗の可能性が高いので保存しない
    if prev is not None and len(prev) and len(sel) < len(prev) * 0.5:
        logging.error("銘柄数が前回 %d → 今回 %d に激減。取得失敗とみなし既存ユニバースを維持します",
                      len(prev), len(sel))
        return 1

    os.makedirs("data", exist_ok=True)
    cols = ["ticker", "code", "meigara", "market_cap", "market", "above_min"]
    sel[cols].to_csv(OUT, index=False, encoding="utf-8")
    logging.info("合計 %d 銘柄（市場別 %s）", len(sel), sel["market"].value_counts().to_dict())
    logging.info("保存: %s", OUT)
    print(sel[["ticker", "meigara", "market_cap"]].head(15).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
