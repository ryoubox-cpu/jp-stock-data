#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PER・PBRなどのファンダメンタル指標を取得する。

注意（重要）:
  yfinanceから取れるのは「現在時点」の値であり、過去の時系列ではない。
  そのため厳密なバックテストには使えない（先読みバイアスが入る）。
  直近期間での粗い検証と、日々のスクリーニングでの参考値として使う前提。

対策:
  取得日を必ず記録し、日を追って蓄積することで、
  将来的には自前の時系列データになる（今日から先は正しく使える）。

レート制限対策は build_universe.py と同じ:
  間隔を空ける / 失敗で自動減速 / 途中経過をキャッシュして再開可能
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
OUT_LATEST = "data/fundamentals.csv"
HIST_DIR = "data/fundamentals"          # 日付ごとに蓄積する
CACHE = "data/fundamentals_cache.json"
JST = timezone(timedelta(hours=9))

PAUSE = float(os.environ.get("PAUSE", "1.5"))
MAX_TICKERS = int(os.environ.get("MAX_TICKERS", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)

# 取りたい項目（yfinance の info キー → 出力名）
FIELDS = {
    "trailingPE": "per",              # 実績PER
    "forwardPE": "per_forward",       # 予想PER
    "priceToBook": "pbr",             # PBR
    "dividendYield": "dividend_yield",
    "returnOnEquity": "roe",
    "profitMargins": "profit_margin",
    "debtToEquity": "debt_to_equity",
    "trailingEps": "eps",
    "bookValue": "bps",
    "marketCap": "market_cap",
    "sector": "sector",
    "industry": "industry",
}


def load_cache():
    if not os.path.exists(CACHE):
        return {}
    try:
        with open(CACHE, encoding="utf-8") as f:
            c = json.load(f)
        age = time.time() - c.get("saved_at", 0)
        if age < 3 * 24 * 3600:          # 3日以内なら再利用
            logging.info("キャッシュを再利用: %d銘柄（%.1f時間前）",
                         len(c.get("data", {})), age / 3600)
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


def fetch(tickers):
    import yfinance as yf

    data = load_cache()
    todo = [t for t in tickers if t not in data]
    logging.info("取得対象 %d銘柄（間隔 %.1f秒）", len(todo), PAUSE)
    fails, cur, t0 = 0, PAUSE, time.time()

    for i, t in enumerate(todo, 1):
        rec = None
        for attempt in range(3):
            try:
                info = yf.Ticker(t).get_info()
                if info:
                    rec = {out: info.get(key) for key, out in FIELDS.items()}
                break
            except Exception as e:
                msg = str(e).lower()
                wait = 30.0 if ("429" in msg or "rate" in msg or "too many" in msg) else 3.0
                time.sleep(wait * (attempt + 1))
        data[t] = rec
        if rec and rec.get("per") is not None:
            fails = 0
            cur = max(PAUSE, cur * 0.9)
        else:
            fails += 1
            cur = min(cur * 1.4, 8.0)
            if fails >= 10:
                logging.warning("連続%d件失敗。60秒休止", fails)
                time.sleep(60)
                fails = 0
        if i % 50 == 0 or i == len(todo):
            got = sum(1 for v in data.values() if v and v.get("per") is not None)
            el = time.time() - t0
            logging.info("%d/%d（PER取得 %d / 間隔 %.1f秒 / 残り約%.0f分）",
                         i, len(todo), got, cur, el / i * (len(todo) - i) / 60)
            save_cache(data)
        time.sleep(cur)
    save_cache(data)
    return data


def main():
    if not os.path.exists(UNIVERSE):
        logging.error("%s がありません", UNIVERSE)
        return 1
    uni = pd.read_csv(UNIVERSE)
    tickers = uni["ticker"].astype(str).tolist()
    if MAX_TICKERS:
        tickers = tickers[:MAX_TICKERS]
    logging.info("ユニバース %d銘柄", len(tickers))

    data = fetch(tickers)
    rows = []
    for t in tickers:
        r = data.get(t) or {}
        rows.append({"ticker": t, **{v: r.get(v) for v in FIELDS.values()}})
    df = pd.DataFrame(rows)
    now = datetime.now(JST)
    df.insert(1, "as_of", now.strftime("%Y-%m-%d"))
    df = uni[["ticker", "meigara"]].merge(df, on="ticker", how="right")

    got = df["per"].notna().sum()
    logging.info("PER取得できたのは %d/%d 銘柄", got, len(df))
    if got < len(df) * 0.2:
        logging.error("取得率が低すぎます。既存ファイルを維持して終了")
        return 1

    os.makedirs("data", exist_ok=True)
    os.makedirs(HIST_DIR, exist_ok=True)
    df.to_csv(OUT_LATEST, index=False, encoding="utf-8")
    # 日付つきで蓄積（今日から先は正しい時系列になる）
    df.to_csv(f"{HIST_DIR}/{now.strftime('%Y%m%d')}.csv", index=False, encoding="utf-8")

    manifest = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S JST"),
        "tickers": len(df), "per_available": int(got),
        "pbr_available": int(df["pbr"].notna().sum()),
        "snapshots": len([f for f in os.listdir(HIST_DIR) if f.endswith(".csv")]),
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
    }
    with open("data/fundamentals_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("\n--- PERの分布 ---")
    per = df["per"].dropna()
    per = per[(per > 0) & (per < 200)]
    print(f"  件数 {len(per)} / 中央値 {per.median():.1f}倍 / "
          f"15倍以下 {(per <= 15).sum()}銘柄 ({(per<=15).mean()*100:.0f}%)")
    for lo, hi in [(0, 10), (10, 15), (15, 20), (20, 30), (30, 50), (50, 200)]:
        n = ((per > lo) & (per <= hi)).sum()
        print(f"  {lo:>3}〜{hi:>3}倍: {n:>4}銘柄")
    pbr = df["pbr"].dropna()
    pbr = pbr[(pbr > 0) & (pbr < 30)]
    print("\n--- PBRの分布 ---")
    print(f"  件数 {len(pbr)} / 中央値 {pbr.median():.2f}倍 / "
          f"1倍割れ {(pbr < 1).sum()}銘柄 ({(pbr<1).mean()*100:.0f}%)")
    if "sector" in df:
        print(f"\n--- セクター数 {df['sector'].nunique()} ---")
        print(df["sector"].value_counts().head(8).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
