import time

import pandas as pd

from .config import AppConfig
from .errors import DataQualityError, InsufficientDataError, TradingSystemError
from .exchange import CcxtAdapter
from .models import Ticker
from .utils import LOG, TF_SECONDS, safe_float


class MarketDataEngine:
    def __init__(self, adapter: CcxtAdapter, config: AppConfig):
        self.adapter = adapter
        self.config = config
        self.max_age = config.risk.max_data_age_seconds
        self._ohlcv = {}
        self._tickers = {}
        self._books = {}
        self.stats = {"tickers": 0, "ohlcv": 0, "books": 0, "errors": 0}

    def refresh_ticker(self, symbol) -> Ticker:
        t = self.adapter.fetch_ticker(symbol)
        self._tickers[symbol] = t
        self.stats["tickers"] += 1
        return t

    def get_ticker(self, symbol, force=False) -> Ticker:
        t = self._tickers.get(symbol)
        if force or t is None or time.time() - t.fetched_at > self.max_age:
            t = self.refresh_ticker(symbol)
        return t

    def ticker_map(self, symbols) -> dict:
        out = {}
        for s in symbols:
            try:
                out[s] = self.get_ticker(s)
            except TradingSystemError as e:
                self.stats["errors"] += 1
                LOG.warning("ticker %s failed: %s", s, e.user_message)
        return out

    @staticmethod
    def to_closed(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """Drops the unclosed last candle (look-ahead protection)."""
        if df.empty:
            return df
        sec = TF_SECONDS.get(timeframe)
        if not sec:
            return df
        close_time = df["ts"] + pd.to_timedelta(sec, unit="s")
        return df[close_time <= pd.Timestamp.now(tz="UTC")].reset_index(drop=True)

    def get_ohlcv(self, symbol, timeframe, limit=300, min_candles=0, force=False) -> pd.DataFrame:
        key = (symbol, timeframe)
        c = self._ohlcv.get(key)
        ttl = max(TF_SECONDS[timeframe] / 3.0, 10)
        if force or c is None or time.time() - c["fetched"] > ttl:
            try:
                df = self.adapter.fetch_ohlcv_df(symbol, timeframe, limit)
                self._ohlcv[key] = {"df": df, "fetched": time.time()}
                self.stats["ohlcv"] += 1
            except TradingSystemError as e:
                if c is None:
                    raise
                LOG.warning("%s %s: no fresh data (%s); using cache", symbol, timeframe, e)
                df = c["df"]
        else:
            df = c["df"]
        closed = self.to_closed(df.copy(), timeframe)
        if len(closed) >= 3:
            gaps = closed["ts"].diff().dt.total_seconds().dropna()
            expected = TF_SECONDS[timeframe]
            if len(gaps) and float((gaps > expected * 1.5).mean()) > 0.2:
                raise DataQualityError(f"{symbol} {timeframe}: >20% candle gaps")
        if len(closed) < max(min_candles, 2):
            raise InsufficientDataError(
                f"{symbol} {timeframe}: insufficient closed candles ({len(closed)})")
        return closed

    def get_book_stats(self, symbol, depth=20) -> dict:
        b = self._books.get(symbol)
        if b is None or time.time() - b.get("_ts", 0) > 15:
            b = self.adapter.fetch_order_book(symbol, depth)
            b["_ts"] = time.time()
            self._books[symbol] = b
            self.stats["books"] += 1
        bids, asks = b.get("bids") or [], b.get("asks") or []
        bid = safe_float(bids[0][0]) if bids else None
        ask = safe_float(asks[0][0]) if asks else None
        bid_v = sum(safe_float(x[1], 0) for x in bids[:10])
        ask_v = sum(safe_float(x[1], 0) for x in asks[:10])
        spread = (ask - bid) / ask * 100 if (bid and ask and ask > 0) else float("nan")
        imb = bid_v / (bid_v + ask_v) if (bid_v + ask_v) > 0 else 0.5
        return {"bid": bid, "ask": ask, "spread_pct": spread, "bid_imbalance": imb}

    def freshness_ok(self, symbol) -> bool:
        t = self._tickers.get(symbol)
        return t is not None and (time.time() - t.fetched_at) <= self.max_age

    def warm_up(self, symbols, min_candles=210):
        for s in symbols:
            try:
                self.get_ohlcv(s, self.config.primary_timeframe, min_candles=min_candles)
                self.get_ticker(s)
            except TradingSystemError as e:
                LOG.warning("warm-up %s: %s", s, e.user_message)
