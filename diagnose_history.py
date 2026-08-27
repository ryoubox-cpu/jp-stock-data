#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yfinanceから財務データを何年分取れるのか、全ての入口を試す。

前回の誤り:
  quarterly_income_stmt だけを試して「4〜5年取れる」と推測した。
  実際は1〜5四半期しか返らなかった。

今回は yfinance が持つ財務系の入口を網羅的に試し、
  ・何期分returnされるか
  ・どこまで過去に遡れるか
  ・EPS/BPSを復元できるか
を実測する。推測しない。
"""
import sys
import time


import pandas as pd
import yfinance as yf

TEST = ["7203.T", "6758.T", "8306.T", "9433.T", "4063.T"]
PAUSE = float(sys.argv[1]) if len(sys.argv) > 1 else 1.5
pd.set_option("display.width", 200)
print(f"yfinance {yf.__version__} / 間隔 {PAUSE}秒\n")


def show(name, obj, t):
    """返ってきたものの形と期間を表示する。"""
    try:
        if obj is None:
            print(f"    {name:<28} None")
            return None
        if isinstance(obj, pd.DataFrame):
            if len(obj) == 0:
                print(f"    {name:<28} 空のDataFrame")
                return None
            # 列が日付なら期間を出す
            try:
                cols = pd.to_datetime(obj.columns)
                span = f"{cols.min().date()} 〜 {cols.max().date()}"
                print(f"    {name:<28} {obj.shape}  {len(obj.columns)}期  {span}")
            except Exception:
                try:
                    idx = pd.to_datetime(obj.index)
                    span = f"{idx.min().date()} 〜 {idx.max().date()}"
                    print(f"    {name:<28} {obj.shape}  {len(obj)}行  {span}")
                except Exception:
                    print(f"    {name:<28} {obj.shape}  列={list(obj.columns)[:4]}")
            return obj
        if isinstance(obj, pd.Series):
            print(f"    {name:<28} Series {len(obj)}要素")
            return obj
        print(f"    {name:<28} {type(obj).__name__}")
        return obj
    except Exception as e:
        print(f"    {name:<28} 表示失敗 {type(e).__name__}: {str(e)[:50]}")
        return None


ENTRIES = [
    ("income_stmt(年次)", lambda tk: tk.income_stmt),
    ("quarterly_income_stmt", lambda tk: tk.quarterly_income_stmt),
    ("balance_sheet(年次)", lambda tk: tk.balance_sheet),
    ("quarterly_balance_sheet", lambda tk: tk.quarterly_balance_sheet),
    ("cashflow(年次)", lambda tk: tk.cashflow),
    ("earnings_history", lambda tk: tk.earnings_history),
    ("earnings_dates", lambda tk: tk.get_earnings_dates(limit=40)),
    ("income_stmt(freq=quarterly)", lambda tk: tk.get_income_stmt(freq="quarterly")),
    ("income_stmt(freq=yearly)", lambda tk: tk.get_income_stmt(freq="yearly")),
    ("balance_sheet(freq=quarterly)", lambda tk: tk.get_balance_sheet(freq="quarterly")),
    ("ttm_income_stmt", lambda tk: getattr(tk, "ttm_income_stmt", None)),
]

results = {}
for t in TEST:
    print("=" * 90)
    print(f"■ {t}")
    print("=" * 90)
    tk = yf.Ticker(t)
    for name, fn in ENTRIES:
        try:
            obj = fn(tk)
            r = show(name, obj, t)
            if isinstance(r, pd.DataFrame) and len(r):
                try:
                    n = len(r.columns)
                    cols = pd.to_datetime(r.columns)
                    yrs = (cols.max() - cols.min()).days / 365.25
                except Exception:
                    n, yrs = len(r), 0
                results.setdefault(name, []).append((n, yrs))
        except Exception as e:
            print(f"    {name:<28} 例外 {type(e).__name__}: {str(e)[:60]}")
        time.sleep(PAUSE)
    print()

print("=" * 90)
print("まとめ: どの入口が何期分・何年分返すか（5銘柄の中央値）")
print("=" * 90)
rows = []
for name, vals in results.items():
    ns = [v[0] for v in vals]
    ys = [v[1] for v in vals if v[1] and v[1] > 0]
    rows.append({"入口": name, "銘柄数": len(vals),
                 "期数(中央値)": int(pd.Series(ns).median()),
                 "期数(最大)": int(max(ns)),
                 "年数(中央値)": round(pd.Series(ys).median(), 1) if ys else None})
print(pd.DataFrame(rows).sort_values("年数(中央値)", ascending=False,
                                     na_position="last").to_string(index=False))

print("\n" + "=" * 90)
print("EPS/BPSの復元に使えるか（7203.T で具体的に確認）")
print("=" * 90)
tk = yf.Ticker("7203.T")
for label, fn in [("年次 income_stmt", lambda: tk.income_stmt),
                  ("年次 balance_sheet", lambda: tk.balance_sheet)]:
    try:
        df = fn()
        if df is None or len(df) == 0:
            print(f"\n{label}: 空")
            continue
        print(f"\n{label}: {len(df.columns)}期 {[str(c)[:10] for c in df.columns]}")
        for key in ["Net Income", "Basic EPS", "Diluted EPS", "Stockholders Equity",
                    "Ordinary Shares Number", "Basic Average Shares"]:
            if key in df.index:
                v = df.loc[key]
                print(f"   {key:<26} " + "  ".join(
                    f"{x:,.0f}" if pd.notna(x) else "NaN" for x in v.values[:6]))
    except Exception as e:
        print(f"{label}: 例外 {type(e).__name__}: {str(e)[:60]}")
    time.sleep(PAUSE)

print("\n" + "=" * 90)
print("earnings_history の中身（EPSの履歴として使えるか）")
print("=" * 90)
try:
    eh = tk.earnings_history
    if eh is not None and len(eh):
        print(f"形 {eh.shape} / 列 {list(eh.columns)}")
        print(eh.head(12).to_string())
    else:
        print("空")
except Exception as e:
    print(f"例外 {type(e).__name__}: {str(e)[:80]}")
