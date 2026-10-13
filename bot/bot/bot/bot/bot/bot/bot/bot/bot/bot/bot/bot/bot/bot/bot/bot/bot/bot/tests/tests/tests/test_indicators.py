import numpy as np
import pandas as pd

from bot.indicators import atr, ema, rsi


def _df(n=300, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    ts = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"ts": ts, "open": close, "high": close * 1.005,
                         "low": close * 0.995, "close": close,
                         "volume": np.abs(rng.normal(1000, 100, n))})


def test_rsi_bounds():
    r = rsi(_df()["close"]).dropna()
    assert r.between(0, 100).all()


def test_no_lookahead_rsi():
    df = _df()
    assert abs(rsi(df["close"]).iloc[199] - rsi(df["close"].iloc[:200]).iloc[199]) < 1e-9


def test_no_lookahead_ema():
    df = _df()
    assert abs(ema(df["close"], 21).iloc[199] - ema(df["close"].iloc[:200], 21).iloc[199]) < 1e-9


def test_atr_positive():
    assert (atr(_df()).dropna() > 0).all()
