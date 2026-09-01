import yfinance as yf, pandas as pd
print("yfinance", yf.__version__, "pandas", pd.__version__)
print(pd.Timestamp.now(), "UTC")
for t in ["7203.T", "6758.T", "285A.T"]:
    df = yf.download(t, period="10d", interval="1d",
                     auto_adjust=True, progress=False)
    print("=== ", t, " index tz:", df.index.tz)
    print(df.tail(6))
