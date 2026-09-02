#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""スイング候補の通知ツール（楽天スーパースクリーナーのCSVを使う版）。

【検証で確定した条件】
  時価総額1兆円以上 / PER15倍以下 / 25日移動平均線から-8%以上の下方乖離

  乖離の深さは単調性ρ=+1.00で効いた（訓練2010-2018・検証2019-2026の両方）。
  暴落局面を除いても有意（t=10.18）。183銘柄中161銘柄がプラスで偏りもない。

【入力】
  楽天スーパースクリーナーが出力したCSV（既定 data.csv）
  タブ区切り・日本語の列名・数値にカンマや「+20.0 (+0.52%)」形式が混在する。
  区切りと列名は自動判定するので、ファイル名を変えずにそのまま置ける。

  時価総額とPERはこのCSVから読む（楽天の最新値）。
  株価の時系列（移動平均の計算用）はYahooファイナンスから直接取得する。
  jp-stock-data リポジトリには依存しない（更新遅延の影響を受けないため）。

【場中モード（INTRADAY）】
  定時実行（GITHUB_EVENT_NAME=schedule）は終値ベース。検証と同じ条件で判定する。
  手動実行や外部から叩かれた場合は、場中なら現在値を当日終値の代わりに使う。
  暫定値なので通知に断りを入れる。売買代金の20日平均は確定足だけで計算する
  （当日の出来高は途中集計のため）。
  INTRADAY=1 で常に有効、0 で常に無効、auto（既定）は起動のされ方で切り替える。

【売買の前提】
  機械は候補出しまで。売り時は人間が判断する。
  機械的に利確+10%/損切-5%で決済すると指数を買うのとほぼ同じ成績になるが、
  値幅を取れれば個別株が大きく上回る（+10%到達 個別56% vs 指数24%）。
"""
from __future__ import annotations

import io
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))

# 楽天スーパースクリーナーのCSV。既定のファイル名をそのまま使う
UNIVERSE_CSV = os.environ.get("UNIVERSE_CSV", "data.csv")

MIN_MCAP = float(os.environ.get("MIN_MCAP", 1e12))     # 時価総額1兆円以上
MAX_PER = float(os.environ.get("MAX_PER", 15.0))       # PER15倍以下
MAX_DEV = float(os.environ.get("MAX_DEV", -0.08))      # 25日線から-8%以下
MIN_TURNOVER = float(os.environ.get("MIN_TURNOVER", 3e8))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
FORCE_RUN = os.environ.get("FORCE_RUN", "0") == "1"

# 株価が何日前まで古かったら通知を止めるか（暦日。休場を挟むので既定は4日）
MAX_DATA_LAG_DAYS = int(os.environ.get("MAX_DATA_LAG_DAYS", 4))

# 場中に現在値を使うか。auto = 定時実行のときだけ無効（＝終値ベースを守る）
_INTRADAY = os.environ.get("INTRADAY", "auto").lower()
INTRADAY = (_INTRADAY in ("1", "true", "yes") or
            (_INTRADAY == "auto" and
             os.environ.get("GITHUB_EVENT_NAME", "manual") != "schedule"))
# Yahooの同時接続数。増やすと速いがレート制限（429）を食らいやすい
YF_WORKERS = int(os.environ.get("YF_WORKERS", 4))
YF_RANGE = os.environ.get("YF_RANGE", "2y")
YF_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)


def to_num(s):
    """「3,876」「+20.0 (+0.52%)」「-」などを数値にする。"""
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return np.nan
    t = str(s).replace(",", "").replace("％", "%").strip()
    if t in ("", "-", "－", "—", "N/A", "nan"):
        return np.nan
    m = re.match(r"^([+-]?\d+\.?\d*)", t)
    return float(m.group(1)) if m else np.nan


def pick_col(cols, *keys):
    """日本語の列名を部分一致で探す。"""
    for k in keys:
        for c in cols:
            if k in str(c):
                return c
    return None


def load_screener(path):
    """楽天スーパースクリーナーのCSVを読む。区切りと列名を自動判定する。"""
    if not os.path.exists(path):
        # よくある置き場所を順に探す（楽天スーパースクリーナーの既定名を優先）
        import glob
        cands = ["data.csv", "data/data.csv", "screener.csv",
                 "data/screener.csv", "output.csv"]
        cands += sorted(glob.glob("*.csv")) + sorted(glob.glob("data/*.csv"))
        for alt in cands:
            if os.path.exists(alt):
                logging.info("%s が無いので %s を使います", path, alt)
                path = alt
                break
        else:
            raise FileNotFoundError(
                f"スクリーナーCSVが見つかりません（探した場所: {path} ほか）。"
                "環境変数 UNIVERSE_CSV でパスを指定できます")

    raw = None
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            raw = open(path, encoding=enc).read()
            logging.info("文字コード %s で読み込み", enc)
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        raise ValueError(f"{path} の文字コードを判定できません")

    sep = "\t" if raw.count("\t") > raw.count(",") else ","
    d = pd.read_csv(io.StringIO(raw), sep=sep, dtype=str)
    d.columns = [str(c).strip() for c in d.columns]
    logging.info("%s を読み込み: %d行 / 区切り=%s / 列=%s",
                 path, len(d), "タブ" if sep == "\t" else "カンマ", list(d.columns)[:8])

    c_code = pick_col(d.columns, "コード", "銘柄コード", "証券コード", "ticker")
    c_name = pick_col(d.columns, "銘柄名", "名称", "銘柄")
    c_per = pick_col(d.columns, "PER", "株価収益率")
    c_mcap = pick_col(d.columns, "時価総額")
    if c_code is None:
        raise ValueError(f"銘柄コードの列が見つかりません（列: {list(d.columns)}）")

    out = pd.DataFrame()
    code = d[c_code].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    out["ticker"] = np.where(code.str.endswith(".T"), code, code + ".T")
    out["meigara"] = d[c_name].astype(str).str.strip() if c_name else out["ticker"]
    out["per_csv"] = d[c_per].map(to_num) if c_per else np.nan
    # 時価総額は百万円単位で入っている
    if c_mcap:
        unit = 1e6 if "百万" in c_mcap else (1e8 if "億" in c_mcap else 1.0)
        out["mcap"] = d[c_mcap].map(to_num) * unit
    else:
        out["mcap"] = np.nan
    out = out[out["ticker"].str.match(r"^\d{4}\.T$")].drop_duplicates("ticker")
    logging.info("有効な銘柄 %d件 / PER取得 %d件 / 時価総額取得 %d件",
                 len(out), out.per_csv.notna().sum(), out.mcap.notna().sum())
    return out


# ---------------------------------------------------------------- Yahoo取得

def fetch_yahoo_one(ticker, rng=YF_RANGE, tries=4):
    """Yahooファイナンスのチャートから日足を取る。

    v8/finance/chart は close が分割調整済み・配当は未調整で返る。
    jp-stock-data（yfinanceのClose）と同じ基準なので検証結果と整合する。

    戻り値は (日足のDataFrame, meta)。meta には現在値・その時刻・板の状態が入る。
    """
    err = None
    for i in range(tries):
        host = YF_HOSTS[i % len(YF_HOSTS)]
        url = (f"https://{host}/v8/finance/chart/{ticker}"
               f"?range={rng}&interval=1d&includePrePost=false")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.loads(r.read().decode("utf-8"))
            chart = j.get("chart") or {}
            if chart.get("error"):
                raise ValueError(str(chart["error"]))
            res = (chart.get("result") or [None])[0]
            if not res:
                raise ValueError("結果が空")
            ts = res.get("timestamp") or []
            q = (res.get("indicators", {}).get("quote") or [{}])[0]
            close, vol = q.get("close") or [], q.get("volume") or []
            if not ts or not close:
                raise ValueError("日足が入っていない")
            d = pd.DataFrame({
                "date": (pd.to_datetime(ts, unit="s", utc=True)
                         .tz_convert("Asia/Tokyo").normalize().tz_localize(None)),
                "close": pd.to_numeric(pd.Series(close), errors="coerce"),
                "volume": pd.to_numeric(pd.Series(vol), errors="coerce")})
            d["ticker"] = ticker
            d["is_live"] = False
            d = d.dropna(subset=["close"]).drop_duplicates("date", keep="last")
            m = res.get("meta") or {}
            meta = {"price": m.get("regularMarketPrice"),
                    "time": m.get("regularMarketTime"),
                    "state": str(m.get("marketState", "")).upper()}
            return d.sort_values("date").reset_index(drop=True), meta
        except Exception as e:  # noqa: BLE001 リトライして最後に警告を出す
            err = e
            if i < tries - 1:
                time.sleep(1.5 * (i + 1) + random.random())
    logging.warning("%s の株価取得に失敗: %s", ticker, err)
    return None, None


def apply_live(d, meta, today):
    """場中なら当日の行を現在値で作り直す。is_live=Trueを立てて後段で区別する。

    寄付前（PRE）は現在値が前日終値のままなので触らない。
    引け後（POST/CLOSED）は当日足がすでに確定値なので触らない。
    """
    if not meta or meta.get("state") != "REGULAR":
        return d, None
    px = meta.get("price")
    try:
        px = float(px)
    except (TypeError, ValueError):
        return d, None
    if not np.isfinite(px) or px <= 0:
        return d, None
    tk = d["ticker"].iloc[0]
    d = d[d["date"] != today]
    row = {"date": today, "close": px, "volume": np.nan,
           "ticker": tk, "is_live": True}
    d = pd.concat([d, pd.DataFrame([row])], ignore_index=True)
    return d, meta.get("time")


def load_prices(tickers, now, intraday=False):
    """対象銘柄の日足をYahooから並列で取得して1本のDataFrameにする。"""
    tickers = sorted(tickers)
    today = pd.Timestamp(now.date())
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=YF_WORKERS) as ex:
        got = list(ex.map(fetch_yahoo_one, tickers))

    dfs, failed, live_times = [], [], []
    for t, (d, meta) in zip(tickers, got):
        if d is None or not len(d):
            failed.append(t)
            continue
        if intraday:
            d, lt = apply_live(d, meta, today)
            if lt:
                live_times.append(lt)
        elif now.hour * 60 + now.minute < 15 * 60 + 10:
            # 終値ベースで判定する回は、当日の未確定バーを落とす
            d = d[d["date"] != today]
        dfs.append(d)
    if not dfs:
        raise RuntimeError("Yahooから1銘柄も取得できませんでした（レート制限の可能性）")

    df = pd.concat(dfs, ignore_index=True)
    cutoff = df["date"].max() - pd.Timedelta(days=400)
    df = df[df["date"] >= cutoff]
    logging.info("Yahooから取得: %d銘柄 / 失敗 %d銘柄 / %.1f秒",
                 df.ticker.nunique(), len(failed), time.time() - t0)
    if failed:
        logging.warning("取得できなかった銘柄: %s", ", ".join(failed))

    live_at = None
    if live_times:
        live_at = (datetime.fromtimestamp(max(live_times), JST).strftime("%H:%M")
                   + " JST")
        logging.info("場中モード: %d銘柄を現在値で計算（%s時点）", len(live_times), live_at)
    elif intraday:
        logging.info("場中モードだが立会時間外のため終値で計算")
    return df, failed, live_at


def build(df, uni):
    rows = []
    for t, g in df.groupby("ticker", sort=False):
        g = g.sort_values("date")
        if len(g) < 80:
            continue
        p = g["close"].astype(float)
        cur = float(p.iloc[-1])
        # 売買代金は確定足だけで見る（当日の出来高は場中だと途中集計のため）
        c = g[~g["is_live"].astype(bool)]
        if len(c) < 20:
            continue
        to = float((c["volume"].astype(float).fillna(0)
                    * c["close"].astype(float)).rolling(20).mean().iloc[-1])
        if not np.isfinite(cur) or cur <= 0 or not np.isfinite(to):
            continue
        ma25 = p.rolling(25).mean().iloc[-1]
        ma75 = p.rolling(75).mean().iloc[-1] if len(p) >= 75 else np.nan
        rows.append({
            "ticker": t, "price": cur, "turnover": to,
            "d1": float(p.iloc[-1] / p.iloc[-2] - 1) if len(p) > 1 else np.nan,
            "dev25": cur / ma25 - 1 if np.isfinite(ma25) else np.nan,
            "dev75": cur / ma75 - 1 if np.isfinite(ma75) else np.nan,
            "low52": float(p.tail(250).min()), "high52": float(p.tail(250).max()),
            "date": g["date"].iloc[-1], "is_live": bool(g["is_live"].iloc[-1])})
    return pd.DataFrame(rows).merge(uni, on="ticker", how="left")


def build_message(hits, meta):
    live = meta.get("live_at")
    head = [
        "📉 スイング候補（割安大型株の深押し）",
        f"🕐 {meta['now']}",
        (f"🔴 場中の暫定値で計算（{live}時点・引けまでに変わります）" if live
         else f"📅 株価データ最終日: {meta['last_bar']}"),
        f"📊 母集団{meta['universe']}銘柄 → 条件通過 {len(hits)}件",
        "",
        f"条件: 時価総額{MIN_MCAP/1e12:.0f}兆円以上 / PER{MAX_PER:.0f}倍以下 / "
        f"25日線{MAX_DEV*100:.0f}%以下",
        "※ 順位は付けていません。乖離が最も深いものが最良とは限りません",
    ]
    if meta.get("failed"):
        head.append(f"※ 株価を取得できなかった銘柄が{meta['failed']}件あります")
    blocks = []
    for r in hits.itertuples():
        mc = f"{r.mcap/1e12:.1f}兆円" if pd.notna(r.mcap) else "?"
        rng = ((r.price - r.low52) / (r.high52 - r.low52) * 100
               if r.high52 > r.low52 else np.nan)
        note = []
        if pd.notna(r.dev75) and r.dev75 <= -0.15:
            note.append("75日線も深い")
        if pd.notna(r.d1) and r.d1 >= 0:
            note.append("当日は下げ止まり")
        if pd.notna(r.per_csv) and r.per_csv <= 11:
            note.append("PER低位")
        tag = "現在値" if getattr(r, "is_live", False) else "終値"
        lines = [f"■ {r.ticker} {r.meigara}",
                 f"  {tag} {r.price:,.0f}円 (前日比 {r.d1*100:+.2f}%)"]
        dev = f"  25日線 {r.dev25*100:+.1f}%"
        if pd.notna(r.dev75):
            dev += f" / 75日線 {r.dev75*100:+.1f}%"
        lines.append(dev)
        lines.append(f"  PER {r.per_csv:.1f}倍 / 時価総額 {mc}"
                     if pd.notna(r.per_csv) else f"  時価総額 {mc}")
        if pd.notna(rng):
            lines.append(f"  52週内の位置 {rng:.0f}%")
        if note:
            lines.append(f"  → {' / '.join(note)}")
        blocks.append("\n".join(lines))
    tail = ["", "─────────", "【使い方】",
            "・同時に持つのは5銘柄まで",
            "・売り時は自分で判断（機械的な決済だと優位が消えます）",
            "・翌日の寄付で買って問題なし（当日終値と成績はほぼ同じ）",
            "・+10%到達の実績は56%（指数は24%）",
            "・発注前に必ずニュース・適時開示を確認"]
    text = "\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n" + "\n".join(tail)
    msgs, cur = [], ""
    for part in text.split("\n\n"):
        if len(cur) + len(part) + 2 > 4600:
            msgs.append(cur)
            cur = part
        else:
            cur = (cur + "\n\n" + part) if cur else part
    msgs.append(cur)
    return msgs[:5]


def send_line(msgs):
    from linebot.v3.messaging import ApiClient, Configuration, MessagingApi
    from linebot.v3.messaging.models import PushMessageRequest, TextMessage
    cfg = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
    with ApiClient(cfg) as api:
        MessagingApi(api).push_message(
            push_message_request=PushMessageRequest(
                to=os.environ["LINE_USER_ID"],
                messages=[TextMessage(text=m[:4900]) for m in msgs]))


def main():
    now = datetime.now(JST)
    if not FORCE_RUN:
        import jpholiday
        if now.weekday() >= 5 or jpholiday.is_holiday(now.date()):
            logging.info("土日祝のため終了")
            return 0

    uni = load_screener(UNIVERSE_CSV)
    # 時価総額とPERはCSVの値で先に絞る（Yahooに投げるリクエスト数を減らす）
    sel = uni[(uni.mcap >= MIN_MCAP) & (uni.per_csv > 0) & (uni.per_csv <= MAX_PER)]
    logging.info("時価総額・PERの条件を満たす %d銘柄", len(sel))
    if len(sel) == 0:
        logging.info("条件を満たす銘柄がありません")
        return 0

    logging.info("価格の基準: %s（起動 %s）",
                 "場中なら現在値" if INTRADAY else "終値のみ",
                 os.environ.get("GITHUB_EVENT_NAME", "manual"))
    df, failed, live_at = load_prices(set(sel.ticker), now, intraday=INTRADAY)

    # 株価が古いまま通知しない（Yahoo側の遅延・障害対策）
    last_bar = df["date"].max()
    lag = (pd.Timestamp(now.date()) - last_bar).days
    logging.info("株価データ最終日 %s（%d日前）", last_bar.date(), lag)
    if lag > MAX_DATA_LAG_DAYS and not FORCE_RUN:
        logging.error("株価が%d日前で古いため通知を中止（上限%d日）",
                      lag, MAX_DATA_LAG_DAYS)
        return 1

    D = build(df, sel)
    hits = D[(D.dev25 <= MAX_DEV) & (D.turnover >= MIN_TURNOVER)].sort_values("ticker")
    logging.info("条件通過 %d件", len(hits))
    for r in hits.itertuples():
        logging.info("  %s %s 25日線%+.1f%% PER%.1f",
                     r.ticker, r.meigara, r.dev25 * 100,
                     r.per_csv if pd.notna(r.per_csv) else -1)

    if len(hits) == 0:
        for r in D.nsmallest(5, "dev25").itertuples():
            logging.info("  次点: %s %s 25日線%+.1f%%", r.ticker, r.meigara, r.dev25 * 100)
        return 0

    meta = {"now": now.strftime("%Y-%m-%d %H:%M JST"),
            "last_bar": str(D.date.max().date()), "universe": len(uni),
            "failed": len(failed), "live_at": live_at}
    msgs = build_message(hits, meta)
    if DRY_RUN:
        print("\n" + "=" * 60)
        for m in msgs:
            print(m)
            print("-" * 60)
    else:
        send_line(msgs)
        logging.info("LINE通知を送信: %d通 / %d銘柄", len(msgs), len(hits))

    try:
        os.makedirs("logs", exist_ok=True)
        hits.to_csv(f"logs/signal_{now.strftime('%Y%m%d')}.csv",
                    index=False, encoding="utf-8")
    except Exception as e:
        logging.warning("ログ保存失敗: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
