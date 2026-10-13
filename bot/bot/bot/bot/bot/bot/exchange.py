import json
import math
import re
import time
from typing import List

import ccxt
import pandas as pd

from .config import Credentials
from .errors import (DataQualityError, FatalError, FeatureNotSupportedError,
                     InsufficientDataError, RetryableError, TradeBlockedError, TradingSystemError)
from .models import OrderRecord, Ticker
from .utils import LOG, RATE, TF_SECONDS, register_secret, safe_float, with_retry

STABLE_BASES = {"USDC", "TUSD", "USDP", "DAI", "FDUSD", "BUSD", "USD1", "AEUR",
                "EUR", "GBP", "TRY", "BRL", "USDE", "USDD", "EURI", "XUSD", "USDT"}
LEV_PAT = re.compile(r"(3|5)(L|S|LONG|SHORT)$|(UP|DOWN|BULL|BEAR)$", re.IGNORECASE)


class Capabilities:
    def __init__(self, client):
        h = client.has or {}
        self.fetch_ohlcv = bool(h.get("fetchOHLCV"))
        self.fetch_ticker = bool(h.get("fetchTicker"))
        self.fetch_tickers = bool(h.get("fetchTickers"))
        self.fetch_order_book = bool(h.get("fetchOrderBook"))
        self.fetch_balance = bool(h.get("fetchBalance"))
        self.create_order = bool(h.get("createOrder"))
        self.cancel_order = bool(h.get("cancelOrder"))
        self.fetch_order = bool(h.get("fetchOrder"))
        self.fetch_open_orders = bool(h.get("fetchOpenOrders"))
        self.fetch_my_trades = bool(h.get("fetchMyTrades"))
        self.fetch_funding_rate = bool(h.get("fetchFundingRate"))
        self.fetch_positions = bool(h.get("fetchPositions"))


class CcxtAdapter:
    def __init__(self, exchange_id, market_type="spot", credentials=None):
        if not hasattr(ccxt, exchange_id):
            raise FatalError(f"Unknown exchange id: {exchange_id!r}")
        self.id = exchange_id
        self.market_type = market_type
        self.credentials = credentials or Credentials()
        register_secret(self.credentials.api_key)
        register_secret(self.credentials.secret)
        self.client = getattr(ccxt, exchange_id)({
            "enableRateLimit": True,
            "apiKey": self.credentials.api_key or None,
            "secret": self.credentials.secret or None,
            "options": {"defaultType": market_type},
        })
        self._markets = None
        self.capabilities = Capabilities(self.client)

    # ---- error mapping
    def _call(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except ccxt.RateLimitExceeded as e:
            raise RetryableError(f"rate limit: {e}", 4.0)
        except ccxt.OnMaintenance as e:
            raise RetryableError(f"maintenance: {e}", 30.0)
        except ccxt.ExchangeNotAvailable as e:
            raise RetryableError(f"unavailable: {e}", 8.0)
        except ccxt.RequestTimeout as e:
            raise RetryableError(f"timeout: {e}", 3.0)
        except ccxt.NetworkError as e:
            raise RetryableError(f"network: {e}", 3.0)
        except ccxt.AuthenticationError as e:
            raise FatalError(f"auth failed: {e}")
        except ccxt.PermissionDenied as e:
            raise FatalError(f"permission denied: {e}")
        except ccxt.InsufficientFunds as e:
            raise TradeBlockedError(f"insufficient funds: {e}")
        except ccxt.InvalidOrder as e:
            raise TradeBlockedError(f"invalid order: {e}")
        except ccxt.BadSymbol as e:
            raise TradeBlockedError(f"bad symbol: {e}")
        except ccxt.NotSupported as e:
            raise FeatureNotSupportedError(str(e))
        except ccxt.ExchangeError as e:
            raise RetryableError(f"exchange error: {e}", 2.0)

    # ---- markets
    @with_retry()
    def load_markets(self, force=False):
        RATE.wait("markets")
        if self._markets is None or force:
            self._markets = self._call(self.client.load_markets, force)
        return self._markets

    def markets(self):
        return self.load_markets()

    def market(self, symbol):
        return self.markets()[symbol]

    def validate_symbol(self, symbol):
        m = self.markets()
        if symbol not in m:
            raise TradeBlockedError(f"{symbol} not found on {self.id}")
        if m[symbol].get("active") is False:
            raise TradeBlockedError(f"{symbol} inactive")

    @staticmethod
    def _step(v, default):
        f = safe_float(v, None)
        if f is None or f <= 0:
            return default
        if f < 1:
            return f
        return 10 ** (-int(f))

    def order_constraints(self, symbol):
        m = self.market(symbol)
        lim = m.get("limits") or {}
        amt, cost = lim.get("amount") or {}, lim.get("cost") or {}
        prec = m.get("precision") or {}
        taker = safe_float(m.get("taker"), None)
        maker = safe_float(m.get("maker"), None)
        if taker is None:
            tf = (self.client.fees or {}).get("trading") or {}
            taker = safe_float(tf.get("taker"), 0.001)
            maker = safe_float(tf.get("maker"), 0.001)
        return {"amount_min": safe_float(amt.get("min"), 0.0) or 0.0,
                "amount_max": safe_float(amt.get("max"), None),
                "cost_min": safe_float(cost.get("min"), 0.0) or 0.0,
                "amount_step": self._step(prec.get("amount"), 1e-8),
                "price_step": self._step(prec.get("price"), 1e-8),
                "taker_fee": taker, "maker_fee": maker}

    def round_amount(self, symbol, amount):
        try:
            return float(self.client.amount_to_precision(symbol, amount))
        except Exception:
            step = self.order_constraints(symbol)["amount_step"]
            return math.floor(amount / step + 1e-9) * step

    def round_price(self, symbol, price, side="buy"):
        step = self.order_constraints(symbol)["price_step"]
        try:
            p = float(self.client.price_to_precision(symbol, price))
        except Exception:
            p = price
        if side == "buy":
            return math.floor(min(p, price) / step + 1e-9) * step
        return math.ceil(max(p, price) / step - 1e-9) * step

    # ---- public data
    def _to_ticker(self, symbol, t) -> Ticker:
        return Ticker(symbol=symbol,
                      last=safe_float(t.get("last")) or safe_float(t.get("close")),
                      bid=safe_float(t.get("bid")), ask=safe_float(t.get("ask")),
                      pct_change_24h=safe_float(t.get("percentage")),
                      quote_volume=safe_float(t.get("quoteVolume")),
                      timestamp_ms=safe_float(t.get("timestamp")), fetched_at=time.time())

    @with_retry()
    def fetch_ticker(self, symbol) -> Ticker:
        self.validate_symbol(symbol)
        RATE.wait("ticker")
        return self._to_ticker(symbol, self._call(self.client.fetch_ticker, symbol))

    @with_retry()
    def fetch_tickers(self, symbols=None) -> dict:
        RATE.wait("tickers")
        raw = self._call(self.client.fetch_tickers, symbols) if self.capabilities.fetch_tickers else {}
        out = {}
        for sym, t in (raw or {}).items():
            try:
                out[sym] = self._to_ticker(sym, t)
            except Exception:
                continue
        return out

    @with_retry()
    def fetch_ohlcv_df(self, symbol, timeframe="15m", limit=300, since_ms=None) -> pd.DataFrame:
        if not self.capabilities.fetch_ohlcv:
            raise FeatureNotSupportedError("OHLCV unsupported")
        if TF_SECONDS.get(timeframe) is None:
            raise FatalError(f"unsupported timeframe {timeframe}")
        self.validate_symbol(symbol)
        RATE.wait("ohlcv")
        raw = self._call(self.client.fetch_ohlcv, symbol, timeframe, since_ms, limit)
        if not raw:
            raise InsufficientDataError(f"{symbol} {timeframe}: empty OHLCV")
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        if df.empty:
            raise DataQualityError(f"{symbol} {timeframe}: corrupt candles")
        return df

    @with_retry()
    def fetch_order_book(self, symbol, limit=50) -> dict:
        if not self.capabilities.fetch_order_book:
            raise FeatureNotSupportedError("order book unsupported")
        RATE.wait("book")
        return self._call(self.client.fetch_order_book, symbol, limit)

    # ---- private
    def _require_key(self, action):
        if not self.credentials.has_private:
            raise FeatureNotSupportedError(f"{action} requires API keys")

    @with_retry()
    def fetch_balance(self) -> dict:
        self._require_key("fetch_balance")
        b = self._call(self.client.fetch_balance)
        keep = lambda d: {k: safe_float(v, 0.0) for k, v in (d or {}).items() if safe_float(v, 0.0)}
        return {"free": keep(b.get("free")), "used": keep(b.get("used")),
                "total": keep(b.get("total"))}

    def create_order(self, symbol, side, order_type, amount, price=None,
                     client_tag=None, params=None) -> OrderRecord:
        # deliberately NO retry: timeout may mean the order actually exists
        self._require_key("create_order")
        if not self.capabilities.create_order:
            raise FeatureNotSupportedError("create_order unsupported")
        self.validate_symbol(symbol)
        cons = self.order_constraints(symbol)
        amount = self.round_amount(symbol, amount)
        if amount <= 0 or amount < cons["amount_min"]:
            raise TradeBlockedError(f"{symbol}: amount below minimum ({amount})")
        cost = None
        if price is not None:
            price = self.round_price(symbol, price, side)
            cost = amount * price
            if cons["cost_min"] and cost < cons["cost_min"]:
                raise TradeBlockedError(
                    f"{symbol}: cost {cost:.2f} below exchange min {cons['cost_min']}")
        p = dict(params or {})
        if client_tag:
            p.setdefault("clientOrderId", client_tag)
        RATE.wait("order")
        o = self._call(self.client.create_order, symbol, order_type, side, amount, price, p)
        rec = OrderRecord(order_id=str(o.get("id") or f"unknown-{time.time():.0f}"),
                          ts=time.time(), mode="live", exchange=self.id, symbol=symbol,
                          side=side, type=order_type, amount=amount, price=price,
                          status=o.get("status") or "open",
                          filled=safe_float(o.get("filled"), 0.0) or 0.0,
                          avg_fill_price=safe_float(o.get("average")),
                          client_tag=client_tag or "",
                          raw=json.dumps(o, default=str)[:3500])
        fee = o.get("fee") or {}
        rec.fee_cost = safe_float(fee.get("cost"), 0.0) or 0.0
        rec.fee_currency = fee.get("currency") or ""
        return rec

    @with_retry()
    def fetch_order(self, order_id, symbol):
        self._require_key("fetch_order")
        return self._call(self.client.fetch_order, order_id, symbol)

    @with_retry()
    def fetch_open_orders(self, symbol=None) -> list:
        self._require_key("fetch_open_orders")
        return self._call(self.client.fetch_open_orders, symbol) or []

    @with_retry()
    def cancel_order(self, order_id, symbol):
        self._require_key("cancel_order")
        return self._call(self.client.cancel_order, order_id, symbol)

    @with_retry()
    def fetch_my_trades(self, symbol, since_ms=None, limit=100) -> list:
        self._require_key("fetch_my_trades")
        return self._call(self.client.fetch_my_trades, symbol, since_ms, limit) or []

    # ---- universe (top-N by volume, filtered)
    def top_usdt_symbols(self, n=20, min_quote_volume=5_000_000.0) -> List[str]:
        tickers = self.fetch_tickers()
        rows = []
        for sym, t in tickers.items():
            if ":" in sym or not sym.endswith("/USDT"):
                continue
            base = sym.split("/")[0]
            if base in STABLE_BASES:
                continue
            if LEV_PAT.search(base):
                continue
            try:
                m = self.market(sym)
            except Exception:
                continue
            if m.get("active") is False:
                continue
            qv = t.quote_volume or 0.0
            if qv < min_quote_volume:
                continue
            if not (t.last and t.bid and t.ask):
                continue
            rows.append((qv, sym))
        rows.sort(reverse=True)
        return [s for _, s in rows[:n]]

    def capability_report(self) -> dict:
        out = dict(vars(self.capabilities))
        out["has_key"] = self.credentials.has_private
        return out
