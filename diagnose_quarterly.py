#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四半期データが取れない原因を切り分ける診断。

確かめること:
  ① 例外が出ているのか、データが空なのか、行名が見つからないのか
  ② get_info()（成功97%）と quarterly_income_stmt で成功率が違うのか
  ③ 実際の日本株で、財務諸表の行名がどうなっているのか
  ④ 1回の問い合わせに絞れば改善するのか

少数の銘柄で試し、原因を特定してから本番を回す。
"""
import sys
import time

import yfinance as yf

TEST = ["7203.T", "6758.T", "8306.T", "9984.T", "6501.T",
        "9433.T", "4063.T", "8058.T", "6902.T", "4568.T"]
PAUSE = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0

print(f"yfinance {yf.__version__} / 間隔 {PAUSE}秒 / {len(TEST)}銘柄\n")

print("=" * 78)
print("① get_info() の成功率（前回97%だった呼び方）")
print("=" * 78)
ok_info = 0
for t in TEST:
    try:
        info = yf.Ticker(t).get_info()
        has = bool(info) and info.get("trailingPE") is not None
        ok_info += has
        print(f"  {t}: {'OK' if has else '空'} "
              f"(PER={info.get('trailingPE') if info else None})")
    except Exception as e:
        print(f"  {t}: 例外 {type(e).__name__}: {str(e)[:70]}")
    time.sleep(PAUSE)
print(f"→ {ok_info}/{len(TEST)}")

print("\n" + "=" * 78)
print("② quarterly_income_stmt の成功率と、失敗の中身")
print("=" * 78)
ok_q, empty_q, exc_q = 0, 0, 0
sample_index = None
for t in TEST:
    try:
        inc = yf.Ticker(t).quarterly_income_stmt
        if inc is None or len(inc) == 0:
            empty_q += 1
            print(f"  {t}: 空のDataFrame（例外ではない）")
        else:
            ok_q += 1
            if sample_index is None:
                sample_index = list(inc.index)
            print(f"  {t}: OK {inc.shape} 列={[str(c)[:10] for c in inc.columns[:3]]}")
    except Exception as e:
        exc_q += 1
        print(f"  {t}: 例外 {type(e).__name__}: {str(e)[:70]}")
    time.sleep(PAUSE)
print(f"→ 成功{ok_q} / 空{empty_q} / 例外{exc_q}")

if sample_index:
    print("\n" + "=" * 78)
    print("③ 実際の行名（pick()が探せているか確認）")
    print("=" * 78)
    for i in sample_index[:25]:
        print(f"  {i}")
    WANT = ["Net Income", "Net Income Common Stockholders",
            "Net Income From Continuing Operation Net Minority Interest"]
    low = [str(i).strip().lower() for i in sample_index]
    print("\n  探している行名が存在するか:")
    for w in WANT:
        hit = w.lower() in low
        part = [i for i in sample_index if w.lower() in str(i).strip().lower()]
        print(f"    {w}: 完全一致={hit} 部分一致={part[:2]}")

print("\n" + "=" * 78)
print("④ balance_sheet も同様に確認")
print("=" * 78)
ok_b, empty_b, exc_b = 0, 0, 0
sample_b = None
for t in TEST[:5]:
    try:
        bal = yf.Ticker(t).quarterly_balance_sheet
        if bal is None or len(bal) == 0:
            empty_b += 1
            print(f"  {t}: 空")
        else:
            ok_b += 1
            if sample_b is None:
                sample_b = list(bal.index)
            print(f"  {t}: OK {bal.shape}")
    except Exception as e:
        exc_b += 1
        print(f"  {t}: 例外 {type(e).__name__}: {str(e)[:70]}")
    time.sleep(PAUSE)
print(f"→ 成功{ok_b} / 空{empty_b} / 例外{exc_b}")
if sample_b:
    WANT = ["Stockholders Equity", "Common Stock Equity",
            "Ordinary Shares Number", "Share Issued"]
    low = [str(i).strip().lower() for i in sample_b]
    print("\n  探している行名:")
    for w in WANT:
        part = [i for i in sample_b if w.lower() in str(i).strip().lower()]
        print(f"    {w}: {'あり' if w.lower() in low else '完全一致なし'} 部分={part[:2]}")

print("\n" + "=" * 78)
print("⑤ 結論")
print("=" * 78)
if exc_q > ok_q:
    print("  → 例外が多い。レート制限またはネットワークの問題。間隔調整が有効")
elif empty_q > ok_q:
    print("  → 空のDataFrameが返る。日本株では四半期財務が提供されていない可能性")
    print("     間隔を空けても解決しない。別の手段を検討すべき")
elif ok_q > 0 and sample_index:
    print("  → 取得自体は成功している。pick()の行名指定が原因の可能性")
    print("     上の行名一覧を見て、指定を修正すれば解決する")
