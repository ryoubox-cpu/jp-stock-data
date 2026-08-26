#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ユニバース銘柄の日足OHLCVを取得して保存する。

初回は6年分を一括取得（FULL=1）、以降は差分のみ取得して追記する。

出力:
  data/prices.parquet     全期間・全銘柄（列: ticker,date,open,high,low,close,volume）
  data/prices_recent.csv  直近300営業日ぶん（人もAIも読みやすい素のCSV）
  data/manifest.json      銘柄数・期間・更新日時などのメタ情報
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import pandas as pd

PARQUET = "data/prices.parquet"
PARQUET_DIR = "data/prices"          # 100MB制限を避けるため年ごとに分割
RECENT_CSV = "data/prices_recent.csv"
MANIFEST = "data/manifest.json"
UNIVERSE = "data/universe.csv"

FULL = os.environ.get("FULL", "0") == "1"
PERIOD = os.environ.get("PERIOD", "6y" if FULL else "3mo")
CHUNK = int(os.environ.get("CHUNK", "25"))
PAUSE = float(os.environ.get("PAUSE", "1.5"))
RECENT_DAYS = int(os.environ.get("RECENT_DAYS", "300"))
JST = timezone(timedelta(hours=9))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)

COLS = ["ticker", "date", "open", "high", "low", "close", "volume"]


def normalize(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """yfinance の DataFrame を縦持ちに正規化する。"""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=COLS)
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = {str(c) for c in df.columns.get_level_values(0)}
        df.columns = (df.columns.get_level_values(0) if {"Open", "Close"} & lv0
                      else df.columns.get_level_values(-1))
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    df = df.loc[:, ~pd.Index(df.columns).duplicated()]
    need = ["open", "high", "low", "close", "volume"]
    for c in need:
        if c not in df.columns:
            return pd.DataFrame(columns=COLS)
    df = df[need].reset_index()
    date_col = df.columns[0]
    df = df.rename(columns={date_col: "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    df["ticker"] = ticker
    df = df.dropna(subset=["close"])
    return df[COLS]


def fetch(tickers: list[str], period: str) -> pd.DataFrame:
    import yfinance as yf

    frames, failed = [], []
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        raw = None
        for attempt in range(3):
            try:
                raw = yf.download(chunk, period=period, interval="1d", auto_adjust=True,
                                  progress=False, group_by="ticker", threads=True)
                break
            except Exception as e:
                logging.warning("取得失敗(試行%d) %s: %s", attempt + 1, chunk[:3], e)
                time.sleep(5 * (attempt + 1))
        if raw is None or len(raw) == 0:
            failed.extend(chunk)
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
                if len(d):
                    frames.append(d)
                else:
                    failed.append(t)
            except Exception as e:
                logging.warning("整形失敗 %s: %s", t, e)
                failed.append(t)
        logging.info("進捗 %d/%d 銘柄", min(i + CHUNK, len(tickers)), len(tickers))
        time.sleep(PAUSE)

    if failed:
        logging.warning("取得できなかった銘柄 %d 件: %s", len(failed), failed[:10])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS)


def sanity_check(df: pd.DataFrame) -> list[str]:
    """明らかにおかしいデータを検出する（分割の未調整・流動性不足など）。

    中小型株を含めると、株式分割の未調整・出来高ゼロの日・
    極端に薄い売買代金といった問題が増えるため検査を強化している。
    """
    warns = []
    if df.empty:
        return ["データが空です"]
    for t, g in df.groupby("ticker"):
        g = g.sort_values("date")
        if len(g) < 5:
            continue
        # 流動性: 直近60日の平均売買代金と、出来高ゼロの日の割合
        tail = g.tail(60)
        turnover = float((tail["volume"] * tail["close"]).mean())
        if turnover < 1e8:
            warns.append(f"{t} 売買代金が薄い(60日平均 {turnover/1e8:.2f}億円)")
        zero_ratio = float((g["volume"].fillna(0) == 0).mean())
        if zero_ratio > 0.02:
            warns.append(f"{t} 出来高ゼロの日が{zero_ratio*100:.0f}%")
        r = g["close"].pct_change().dropna()
        # 1日で-60%以下 or +150%以上は株式分割の未調整を疑う
        bad = r[(r <= -0.6) | (r >= 1.5)]
        for idx, v in bad.items():
            d = g.loc[idx, "date"]
            warns.append(f"{t} {d.date()} 前日比 {v*100:+.0f}% — 分割未調整の疑い")
        if (g["close"] <= 0).any():
            warns.append(f"{t} 終値に0以下の値あり")
        if g["date"].duplicated().any():
            warns.append(f"{t} 日付の重複あり")
    return warns


def main() -> int:
    if not os.path.exists(UNIVERSE):
        logging.error("%s がありません。先に build_universe.py を実行してください", UNIVERSE)
        return 1
    uni = pd.read_csv(UNIVERSE)
    tickers = uni["ticker"].astype(str).tolist()
    logging.info("対象 %d 銘柄 / period=%s / FULL=%s", len(tickers), PERIOD, FULL)

    new = fetch(tickers, PERIOD)
    if new.empty:
        logging.error("1件も取得できませんでした。既存データは変更しません")
        return 1
    logging.info("取得 %d 行 / %d 銘柄", len(new), new["ticker"].nunique())

    old = None
    if not FULL:
        if os.path.isdir(PARQUET_DIR):
            fs = [os.path.join(PARQUET_DIR, f) for f in sorted(os.listdir(PARQUET_DIR))
                  if f.endswith(".parquet")]
            if fs:
                old = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
        elif os.path.exists(PARQUET):
            old = pd.read_parquet(PARQUET)
    if old is not None and len(old):
        old["ticker"] = old["ticker"].astype(str)
        merged = pd.concat([old, new], ignore_index=True)
    else:
        merged = new

    merged["date"] = pd.to_datetime(merged["date"])
    merged = (merged.drop_duplicates(subset=["ticker", "date"], keep="last")
                    .sort_values(["ticker", "date"])
                    .reset_index(drop=True))

    warns = sanity_check(merged)
    for w in warns[:20]:
        logging.warning("整合性: %s", w)

    os.makedirs("data", exist_ok=True)
    os.makedirs(PARQUET_DIR, exist_ok=True)

    # 容量削減: 価格はfloat32、銘柄コードはcategory、圧縮はzstd
    slim = merged.copy()
    for c in ["open", "high", "low", "close"]:
        if c in slim:
            slim[c] = slim[c].astype("float32")
    if "volume" in slim:
        slim["volume"] = slim["volume"].astype("float32")
    slim["ticker"] = slim["ticker"].astype("category")

    # GitHubの100MB制限を避けるため年ごとに分割して保存
    for old in os.listdir(PARQUET_DIR):
        if old.endswith(".parquet"):
            os.remove(os.path.join(PARQUET_DIR, old))
    parts = []
    for yr, g in slim.groupby(slim["date"].dt.year):
        path = os.path.join(PARQUET_DIR, f"{int(yr)}.parquet")
        g.to_parquet(path, index=False, compression="zstd")
        mb = os.path.getsize(path) / 1e6
        parts.append({"year": int(yr), "file": path, "rows": len(g), "mb": round(mb, 1)})
        if mb > 90:
            logging.warning("%s が %.1fMB。100MB制限に接近しています", path, mb)
    logging.info("分割保存: %d ファイル / 合計 %.1fMB",
                 len(parts), sum(p["mb"] for p in parts))
    # 旧形式の単一ファイルが残っていれば削除（100MB超でpushが失敗するため）
    if os.path.exists(PARQUET):
        os.remove(PARQUET)
        logging.info("旧 %s を削除しました", PARQUET)

    cutoff = merged["date"].max() - pd.Timedelta(days=int(RECENT_DAYS * 1.5))
    recent = merged[merged["date"] >= cutoff]
    recent.to_csv(RECENT_CSV + ".gz", index=False, encoding="utf-8", compression="gzip")
    if os.path.exists(RECENT_CSV):
        os.remove(RECENT_CSV)

    manifest = {
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "tickers": int(merged["ticker"].nunique()),
        "rows": int(len(merged)),
        "date_min": str(merged["date"].min().date()),
        "date_max": str(merged["date"].max().date()),
        "parts": parts,
        "parquet_mb": round(sum(p["mb"] for p in parts), 2),
        "recent_csv_mb": round(os.path.getsize(RECENT_CSV + ".gz") / 1e6, 2),
        "warnings": warns[:50],
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
    }
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    logging.info("保存完了: %s", json.dumps(manifest, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
