#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指数・セクターETFの日足を全期間取得する。

個別株(fetch_prices.py)と違い、こちらは period="max" で取れるだけ遡る。
S&P500は1927年、米セクターETFは1998年、日本のTOPIX-17は2009年ごろから。

出力:
  data/assets.parquet   全銘柄の日足（ticker,date,open,high,low,close,volume）
  data/assets_coverage.csv  銘柄ごとの取得期間・本数（どこまで遡れたかの確認用）
  data/assets_manifest.json
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

ASSETS = "assets.csv"
PARQUET = "data/assets.parquet"
COVERAGE = "data/assets_coverage.csv"
MANIFEST = "data/assets_manifest.json"

FULL = os.environ.get("FULL", "0") == "1"
PERIOD = os.environ.get("PERIOD", "max" if FULL else "3mo")
CHUNK = int(os.environ.get("CHUNK", "15"))
PAUSE = float(os.environ.get("PAUSE", "2.0"))
JST = timezone(timedelta(hours=9))
COLS = ["ticker", "date", "open", "high", "low", "close", "volume"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)


def normalize(raw, ticker):
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=COLS)
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = {str(c) for c in df.columns.get_level_values(0)}
        df.columns = (df.columns.get_level_values(0) if {"Open", "Close"} & lv0
                      else df.columns.get_level_values(-1))
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    df = df.loc[:, ~pd.Index(df.columns).duplicated()]
    if "close" not in df.columns:
        return pd.DataFrame(columns=COLS)
    for c in ("open", "high", "low"):
        if c not in df.columns:
            df[c] = df["close"]
    if "volume" not in df.columns:
        df["volume"] = pd.NA          # 指数は出来高がないことがある
    df = df[["open", "high", "low", "close", "volume"]].reset_index()
    df = df.rename(columns={df.columns[0]: "date"})
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert(None).dt.normalize()
    df["ticker"] = ticker
    return df.dropna(subset=["close"])[COLS]


def fetch(tickers, period):
    import yfinance as yf

    frames, failed = [], []
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        raw = None
        for a in range(3):
            try:
                raw = yf.download(chunk, period=period, interval="1d", auto_adjust=True,
                                  progress=False, group_by="ticker", threads=True)
                break
            except Exception as e:
                logging.warning("取得失敗(試行%d) %s: %s", a + 1, chunk[:3], e)
                time.sleep(5 * (a + 1))
        if raw is None or len(raw) == 0:
            failed += chunk
            continue
        for t in chunk:
            try:
                if isinstance(raw.columns, pd.MultiIndex) and t in set(raw.columns.get_level_values(0)):
                    sub = raw[t]
                elif len(chunk) == 1:
                    sub = raw
                else:
                    failed.append(t)
                    continue
                d = normalize(sub, t)
                frames.append(d) if len(d) else failed.append(t)
            except Exception as e:
                logging.warning("整形失敗 %s: %s", t, e)
                failed.append(t)
        logging.info("進捗 %d/%d", min(i + CHUNK, len(tickers)), len(tickers))
        time.sleep(PAUSE)
    if failed:
        logging.warning("取得できず %d件: %s", len(failed), failed)
    return (pd.concat(frames, ignore_index=True) if frames
            else pd.DataFrame(columns=COLS)), failed


def main():
    if not os.path.exists(ASSETS):
        logging.error("%s がありません", ASSETS)
        return 1
    uni = pd.read_csv(ASSETS)
    uni.columns = [c.strip().lower() for c in uni.columns]
    tickers = uni["ticker"].astype(str).str.strip().tolist()
    logging.info("対象 %d 資産 / period=%s", len(tickers), PERIOD)

    new, failed = fetch(tickers, PERIOD)
    if new.empty:
        logging.error("1件も取得できませんでした")
        return 1

    if os.path.exists(PARQUET) and not FULL:
        new = pd.concat([pd.read_parquet(PARQUET), new], ignore_index=True)
    new["date"] = pd.to_datetime(new["date"])
    new = (new.drop_duplicates(subset=["ticker", "date"], keep="last")
              .sort_values(["ticker", "date"]).reset_index(drop=True))

    os.makedirs("data", exist_ok=True)
    new.to_parquet(PARQUET, index=False, compression="snappy")

    cov = (new.groupby("ticker")
              .agg(本数=("close", "size"), 開始=("date", "min"), 終了=("date", "max"))
              .reset_index())
    cov["年数"] = ((cov["終了"] - cov["開始"]).dt.days / 365.25).round(1)
    cov = uni.merge(cov, on="ticker", how="left").sort_values("年数", ascending=False)
    cov.to_csv(COVERAGE, index=False, encoding="utf-8")

    got = cov["本数"].notna().sum()
    manifest = {
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "assets_requested": len(tickers), "assets_fetched": int(got),
        "failed": failed, "rows": int(len(new)),
        "date_min": str(new["date"].min().date()), "date_max": str(new["date"].max().date()),
        "parquet_mb": round(os.path.getsize(PARQUET) / 1e6, 2),
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
    }
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)

    logging.info("取得 %d/%d 資産 / %d行 / %s〜%s / %.1fMB",
                 got, len(tickers), len(new), manifest["date_min"],
                 manifest["date_max"], manifest["parquet_mb"])
    print("\n--- 遡れた年数（上位20） ---")
    print(cov.head(20)[["ticker", "name", "年数", "開始", "本数"]].to_string(index=False))
    short = cov[(cov["年数"].notna()) & (cov["年数"] < 5)]
    if len(short):
        print(f"\n--- 5年未満（検証には不十分） {len(short)}件 ---")
        print(short[["ticker", "name", "年数"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
