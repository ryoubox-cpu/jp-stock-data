import yfinance as yf, pandas as pd
uni = pd.read_csv("data/universe.csv")
chunk = uni["ticker"].astype(str).tolist()[:25]
kw = dict(period="3mo", interval="1d", auto_adjust=True, progress=False)

a = yf.download(chunk, group_by="ticker", threads=True, **kw)
print("A 一括25/threads有 :", a.index.max(), a.shape)

b = yf.download(chunk, group_by="ticker", threads=False, **kw)
print("B 一括25/threads無 :", b.index.max(), b.shape)

c = yf.download(chunk[0], **kw)
print("C 単一銘柄        :", c.index.max(), c.shape)
