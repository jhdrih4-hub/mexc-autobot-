import os
import time

import numpy as np
import pandas as pd

from .config import AppConfig
from .errors import TradingSystemError
from .exchange import CcxtAdapter
from .strategies import StrategyEngine
from .utils import LOG, TF_SECONDS


def fetch_history(adapter, symbol, timeframe, days, max_bars=4000):
    now_ms = int(time.time() * 1000)
    since = now_ms - int(days * 86400 * 1000)
    frames = []
    while since < now_ms and sum(len(f) for f in frames) < max_bars:
        try:
            df = adapter.fetch_ohlcv_df(symbol, timeframe, limit=500, since_ms=since)
        except TradingSystemError:
            break
        if df.empty:
            break
        frames.append(df)
        last_ts = int(df["ts"].iloc[-1].value / 1e6)
        if last_ts <= since:
            break
        since = last_ts + 1
