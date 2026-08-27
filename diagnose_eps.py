#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yfinanceで取れるものを出し切る確認。

前回の診断で判明:
  年次 income_stmt / balance_sheet  → 4年分（EPS・純資産・株数あり）
  earnings_dates                    → 50行・2014年〜（12年分）※中身は未確認

今回確認すること:
  ① earnings_dates の実績EPSが、どこまで過去に埋まっているか（最重要）
  ② 銘柄によってばらつくか
  ③ 年次データからEPS/BPSを復元できる銘柄の割合
  ④ まだ試していない入口（get_shares_full, dividends, splits, capital_gains,
     sustainability, recommendations, calendar 等）に使えるものがあるか
"""
import sys
import time

import numpy as np
import pandas as pd
import yfinance as yf

TEST = ["7203.T", "6758.T", "8306.T", "9433.T", "4063.T", "6501.T", "9984.T", "4568.T"]
PAUSE = float(sys.argv[1]) if len(sys.argv) > 1 else 1.2
pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 60)
print(f"yfinance {yf.__version__} / 間隔 {PAUSE}秒\n")

print("=" * 92)
print("① earnings_dates の中身（実績EPSがどこまで埋まっているか）")
print("=" * 92)
summary = []
for t in TEST[:4]:
    try:
        ed = yf.Ticker(t).get_earnings_dates(limit=60)
        if ed is None or len(ed) == 0:
            print(f"\n■ {t}: 空")
            continue
        ed = ed.sort_index()
        col = None
        for c in ed.columns:
            if "actual" in str(c).lower():
                col = c
                break
        n_all = len(ed)
        n_act = int(ed[col].notna().sum()) if col else 0
        oldest_act = ed[ed[col].notna()].index.min() if col and n_act else None
        print(f"\n■ {t}: {n_all}行 / 列={list(ed.columns)}")
        print(f"   実績EPSが入っている行: {n_act}/{n_all}")
        print(f"   最も古い実績: {oldest_act}")
        print(ed.head(6).to_string())
        print("   ...")
        print(ed.tail(4).to_string())
        summary.append({"ticker": t, "行数": n_all, "実績あり": n_act,
                        "最古の実績": str(oldest_act)[:10] if oldest_act is not None else None})
    except Exception as e:
        print(f"\n■ {t}: 例外 {type(e).__name__}: {str(e)[:70]}")
    time.sleep(PAUSE)
if summary:
    print("\n--- まとめ ---")
    print(pd.DataFrame(summary).to_string(index=False))
    yrs = []
    for s in summary:
        if s["最古の実績"]:
            yrs.append((pd.Timestamp("2026-08-27") - pd.Timestamp(s["最古の実績"])).days / 365.25)
    if yrs:
        print(f"\n→ 実績EPSの遡及: 中央値 {np.median(yrs):.1f}年 / 最長 {max(yrs):.1f}年")

print("\n" + "=" * 92)
print("② 年次データからEPS/BPSを復元できるか（8銘柄で成功率）")
print("=" * 92)
rows = []
for t in TEST:
    try:
        tk = yf.Ticker(t)
        inc = tk.income_stmt
        bal = tk.balance_sheet
        r = {"ticker": t}
        if inc is not None and len(inc):
            r["年次期数"] = len(inc.columns)
            r["EPS"] = "○" if "Basic EPS" in inc.index else "✕"
            r["純利益"] = "○" if "Net Income" in inc.index else "✕"
            r["最古"] = str(pd.to_datetime(inc.columns).min().date())
        if bal is not None and len(bal):
            r["純資産"] = "○" if "Stockholders Equity" in bal.index else "✕"
            r["株数"] = "○" if "Ordinary Shares Number" in bal.index else "✕"
        rows.append(r)
    except Exception as e:
        rows.append({"ticker": t, "年次期数": f"例外 {type(e).__name__}"})
    time.sleep(PAUSE)
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 92)
print("③ まだ試していない入口")
print("=" * 92)
tk = yf.Ticker("7203.T")
OTHERS = [
    ("get_shares_full(発行済株数の推移)", lambda: tk.get_shares_full(start="2010-01-01")),
    ("dividends(配当履歴)", lambda: tk.dividends),
    ("splits(分割履歴)", lambda: tk.splits),
    ("actions", lambda: tk.actions),
    ("calendar(次回決算)", lambda: tk.calendar),
    ("growth_estimates", lambda: getattr(tk, "growth_estimates", None)),
    ("eps_trend", lambda: getattr(tk, "eps_trend", None)),
    ("eps_revisions", lambda: getattr(tk, "eps_revisions", None)),
    ("analyst_price_targets", lambda: getattr(tk, "analyst_price_targets", None)),
    ("major_holders", lambda: tk.major_holders),
    ("institutional_holders", lambda: tk.institutional_holders),
]
for name, fn in OTHERS:
    try:
        o = fn()
        if o is None:
            print(f"  {name:<34} None")
        elif isinstance(o, pd.DataFrame):
            if len(o) == 0:
                print(f"  {name:<34} 空")
            else:
                try:
                    idx = pd.to_datetime(o.index)
                    print(f"  {name:<34} {o.shape}  {idx.min().date()}〜{idx.max().date()}")
                except Exception:
                    print(f"  {name:<34} {o.shape}  列={list(o.columns)[:5]}")
        elif isinstance(o, pd.Series):
            if len(o) == 0:
                print(f"  {name:<34} 空")
            else:
                try:
                    idx = pd.to_datetime(o.index)
                    print(f"  {name:<34} {len(o)}件  {idx.min().date()}〜{idx.max().date()}")
                except Exception:
                    print(f"  {name:<34} {len(o)}件")
        elif isinstance(o, dict):
            print(f"  {name:<34} dict キー={list(o.keys())[:6]}")
        else:
            print(f"  {name:<34} {type(o).__name__}")
    except Exception as e:
        print(f"  {name:<34} 例外 {type(e).__name__}: {str(e)[:50]}")
    time.sleep(PAUSE)

print("\n" + "=" * 92)
print("④ 発行済株数の推移（BPS計算に使えるか）")
print("=" * 92)
try:
    sf = tk.get_shares_full(start="2010-01-01")
    if sf is not None and len(sf):
        print(f"  {len(sf)}件 / {pd.to_datetime(sf.index).min().date()}〜"
              f"{pd.to_datetime(sf.index).max().date()}")
        print(sf.head(5).to_string())
        print("  ...")
        print(sf.tail(5).to_string())
    else:
        print("  空")
except Exception as e:
    print(f"  例外 {type(e).__name__}: {str(e)[:70]}")

print("\n" + "=" * 92)
print("⑤ 結論")
print("=" * 92)
print("  earnings_dates の実績EPSが10年以上埋まっていれば → 四半期EPSで長期PER復元が可能")
print("  年次のみなら4年分 → 短期の検証に限定")
print("  どちらも不足なら → EDINETへ")
