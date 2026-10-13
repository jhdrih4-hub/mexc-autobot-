import numpy as np
import pandas as pd

from bot.config import AppConfig
from bot.strategies import StrategyEngine


def _df(n=400, seed=3, drift=0.002):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    ts = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"ts": ts, "open": close, "high": close * 1.004,
                         "low": close * 0.996, "close": close,
                         "volume": np.abs(rng.normal(1000, 200, n))})


def test_decision_shape():
    d, sigs, _ = StrategyEngine(AppConfig()).compute_full("BTC/USDT", _df())
    assert d.action in {"buy", "sell", "hold"}
    assert -1.0 <= d.score <= 1.0
    assert len(sigs) == 8


def test_uptrend_not_deep_sell():
    d, _, _ = StrategyEngine(AppConfig()).compute_full("BTC/USDT", _df(drift=0.004, seed=5))
    assert d.score > -0.5


def test_insufficient_data():
    d, _, _ = StrategyEngine(AppConfig()).compute_full("BTC/USDT", _df()[:30])
    assert d.data_ok is False and d.action == "hold"
