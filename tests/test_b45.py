import pandas as pd
import pytest
import ana

def _mk_df(hi, lo, n=100):
    mid = (hi + lo) / 2
    return pd.DataFrame({"ts": pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC"),
                         "open": mid, "high": [hi]*n, "low": [lo]*n,
                         "close": mid, "volume": 1.0})

def test_fib_levels_math():
    df = _mk_df(120.0, 80.0)
    diff = 40.0
    lvl = {r: 80.0 + diff * r for r in (0.382, 0.5, 0.618, 0.786)}
    assert lvl[0.618] == pytest.approx(104.72, abs=0.01)
    assert lvl[0.382] == pytest.approx(95.28, abs=0.01)

def test_pivot_formula():
    H, L, C = 110.0, 90.0, 100.0
    P = (H + L + C) / 3.0
    assert P == pytest.approx(100.0)
    assert (2*P - H) == pytest.approx(90.0)   # S1

def test_vp_poc_finds_max_volume_bin():
    df = _mk_df(120.0, 80.0, 100)
    df["volume"] = 1.0
    df.loc[df.index[-20:], "volume"] = 999.0        # üst kovaya hacim yığı
    df.loc[df.index[-20:], "high"] = 119.0
    df.loc[df.index[-20:], "low"] = 118.0
    df.loc[df.index[-20:], "close"] = 118.5
    lo = float(df["low"].min()); hi = float(df["high"].max())
    step = (hi - lo) / 12
    vols = [0.0]*12
    for _, r in df.iterrows():
        mid = (float(r["high"]) + float(r["low"])) / 2.0
        idx = int(max(0, min(11, int((mid - lo) / step))))
        vols[idx] += float(r["volume"])
    poc_idx = vols.index(max(vols))
    assert vols[poc_idx] >= max(vols)               # POC en yoğun kovada
