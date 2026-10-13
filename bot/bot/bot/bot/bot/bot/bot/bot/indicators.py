import numpy as np
import pandas as pd


def sma(s, n):
    return s.rolling(n).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close, period=14):
    d = close.diff()
    gain = d.clip(lower=0.0)
    loss = -d.clip(upper=0.0)
    ag = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    al = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = ag / al.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def macd(close, fast=12, slow=26, signal=9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def bollinger(close, n=20, k=2.0):
    mid = sma(close, n)
    sd = close.rolling(n).std(ddof=0)
    up, lo = mid + k * sd, mid - k * sd
    bw = (up - lo) / mid.replace(0, np.nan) * 100
    pb = (close - lo) / (up - lo).replace(0, np.nan)
    return mid, up, lo, bw, pb


def true_range(df):
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                      (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df, period=14):
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(df, period=14):
    h, l = df["high"], df["low"]
    up, dn = h.diff(), -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr_ = true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_
    mdi = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean(), pdi, mdi


def stoch_rsi(close, rsi_n=14, stoch_n=14, k_n=3, d_n=3):
    r = rsi(close, rsi_n)
    lo = r.rolling(stoch_n).min()
    hi = r.rolling(stoch_n).max()
    k = 100 * (r - lo) / (hi - lo).replace(0, np.nan)
    return k, k.rolling(d_n).mean()


def obv(df):
    return (np.sign(df["close"].diff()).fillna(0) * df["volume"]).cumsum()


def rolling_vwap(df, n=50):
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (tp * df["volume"]).rolling(n).sum()
    vv = df["volume"].rolling(n).sum()
    return pv / vv.replace(0, np.nan)


def donchian(df, n=20):
    return df["high"].rolling(n).max(), df["low"].rolling(n).min()


def _cluster(vals, pct_tol=0.003):
    if not vals:
        return []
    vals = sorted(vals)
    out = [vals[0]]
    for v in vals[1:]:
        if abs(v - out[-1]) / max(out[-1], 1e-12) < pct_tol:
            out[-1] = (out[-1] + v) / 2
        else:
            out.append(v)
    return out


def pivot_levels(df, lookback=240, w=3):
    if len(df) < 2 * w + 5:
        return [], []
    d = df.tail(lookback).reset_index(drop=True)
    H = d["high"].values
    L = d["low"].values
    highs, lows = [], []
    for i in range(w, len(d) - w):
        if H[i] >= max(H[i - w:i + w + 1]):
            highs.append(float(H[i]))
        if L[i] <= min(L[i - w:i + w + 1]):
            lows.append(float(L[i]))
    return _cluster(highs), _cluster(lows)


def last(s, default=None):
    try:
        v = s.iloc[-1]
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            return default
        return float(v)
    except Exception:
        return default
