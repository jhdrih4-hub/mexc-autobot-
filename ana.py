# -*- coding: utf-8 -*-
# ============================================================
#  MEXC OTOMATİK ALIM-SATIM BOTU — ana.py (TAM SÜRÜM v3)
#  4 parça halinde yapıştırıldı. Bu: PARÇA 1/4 (Hücre 1-4)
# ============================================================

# ============================================================
# HÜCRE 1/18 — BAĞIMLILIKLAR (eksik paketi kendisi kurar)
# ============================================================
import importlib, subprocess, sys

def _ensure_packages(pairs):
    for pip_name, mod_name in pairs:
        try:
            importlib.import_module(mod_name)
        except ImportError:
            print(f"[KURULUM] {pip_name} kuruluyor...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pip_name])

_ensure_packages([
    ("ccxt", "ccxt"), ("pandas", "pandas"), ("numpy", "numpy"),
    ("feedparser", "feedparser"), ("plotly", "plotly"), ("requests", "requests"),
    ("scikit-learn", "sklearn"), ("matplotlib", "matplotlib"),
])

import os, json, math, time, re, random, calendar, hashlib, sqlite3
import logging, threading, functools
from logging.handlers import RotatingFileHandler
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from getpass import getpass
from typing import Dict, List, Optional
import ccxt
import pandas as pd
import numpy as np

print(f"ccxt={ccxt.__version__} pandas={pd.__version__} numpy={np.__version__}")
print("[HÜCRE 1] Bağımlılıklar hazır.")

# ============================================================
# HÜCRE 2/18 — AYARLAR (API anahtarı KODDA YAZILMAZ)
# ============================================================

class TradingMode(str, Enum):
    BACKTEST = "backtest"
    PAPER    = "paper"
    LIVE     = "live"

@dataclass
class RiskLimits:
    max_risk_per_trade_pct: float = 1.0
    max_position_value_pct: float = 20.0
    max_total_exposure_pct: float = 60.0
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 15.0
    max_open_positions: int = 6
    max_positions_per_symbol: int = 1
    min_24h_quote_volume_usd: float = 5_000_000.0
    max_spread_pct: float = 0.5
    max_data_age_seconds: float = 90.0
    default_stop_loss_pct: float = 2.0
    default_take_profit_pct: float = 4.0
    use_atr_stops: bool = True
    atr_stop_multiplier: float = 2.0
    atr_target_multiplier: float = 4.0
    min_atr_pct: float = 0.15
    max_atr_pct: float = 6.0
    circuit_break_after_consecutive_losses: int = 4
    cooldown_minutes_after_stop_hit: int = 60
    min_order_quote_usdt: float = 5.5

@dataclass
class Credentials:
    api_key: str = ""
    secret: str = ""
    def __repr__(self):
        f = lambda s: f"***({len(s)})" if s else "(boş)"
        return f"Credentials(api_key={f(self.api_key)}, secret={f(self.secret)})"

@dataclass
class AppConfig:
    exchange_id: str = "mexc"
    market_type: str = "spot"
    mode: TradingMode = TradingMode.PAPER      # GitHub güvenliği: varsayılan PAPER
    symbols: List[str] = field(default_factory=lambda: [
        "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
        "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT",
        "TRX/USDT", "LTC/USDT", "UNI/USDT", "ATOM/USDT", "XLM/USDT",
        "NEAR/USDT", "ETC/USDT", "FIL/USDT", "APT/USDT", "ARB/USDT"])
    primary_timeframe: str = "15m"
    higher_timeframe: str = "4h"
    base_currency: str = "USDT"
    paper_start_balance: float = 10_000.0
    paper_fee_rate: float = 0.001
    paper_slippage_bps: float = 5.0
    poll_seconds: int = 120
    news_refresh_minutes: int = 15
    reconcile_every_n_cycles: int = 5
    max_candles: int = 400
    risk: RiskLimits = field(default_factory=RiskLimits)
    strategy_weights: Dict[str, float] = field(default_factory=lambda: {
        "trend_follow": 1.2, "ema_cross": 1.0, "rsi_momentum": 0.8,
        "macd": 0.8, "bollinger_reversion": 0.7, "volume_breakout": 1.0,
        "multi_timeframe": 1.3})
    buy_score_threshold: float = 0.35
    sell_score_threshold: float = -0.35
    data_dir: str = "."
    close_positions_on_stop: bool = False

CONFIG = AppConfig()

def request_credentials(purpose: str = "gerçek işlem hesabı") -> Credentials:
    """Önce GitHub Secrets (env), yoksa interaktif istem. Sırlar asla log'a sızmaz."""
    c = Credentials()
    c.api_key = (os.environ.get("MEXC_API_KEY") or "").strip()
    c.secret  = (os.environ.get("MEXC_SECRET") or "").strip()
    if c.api_key and c.secret:
        print("[MEXC] API anahtarları ortam değişkenlerinden okundu (GitHub Secrets).")
        return c
    try:
        if sys.stdin.isatty():
            print(f"\n[MEXC] API anahtarları ({purpose}). Boş bırak = sadece piyasa verisi.")
            c.api_key = getpass("  API Key   : ").strip()
            if c.api_key:
                c.secret = getpass("  API Secret: ").strip()
    except Exception:
        pass
    if c.api_key and not c.secret:
        print("[UYARI] Secret yok — private işlemler kapalı.")
        c.api_key = ""
    return c

print("[HÜCRE 2] Ayarlar hazır.")

# ============================================================
# HÜCRE 3/18 — LOGGING, HATA SINIFLARI, YARDIMCILAR
# ============================================================

class TradingSystemError(Exception):
    def __init__(self, message: str, user_message: Optional[str] = None):
        super().__init__(message)
        self.user_message = user_message or message

class RetryableError(TradingSystemError):
    def __init__(self, message, backoff: float = 2.0, user_message=None):
        super().__init__(message, user_message)
        self.backoff = backoff

class FatalError(TradingSystemError): pass
class TradeBlockedError(TradingSystemError): pass
class InsufficientDataError(TradingSystemError): pass
class DataQualityError(TradingSystemError): pass
class FeatureNotSupportedError(TradingSystemError): pass

_SECRET_STORE: set = set()
def register_secret(s: str):
    if s and len(s) >= 6:
        _SECRET_STORE.add(s)

def scrub(text: str) -> str:
    t = str(text)
    for s in _SECRET_STORE:
        t = t.replace(s, "***")
    return re.sub(r"\b[A-Fa-f0-9]{64}\b", "***", t)

class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return scrub(super().format(record))

def setup_logging(data_dir: str) -> logging.Logger:
    log = logging.getLogger("uts")
    log.setLevel(logging.INFO)
    if log.handlers:
        return log
    fmt = RedactingFormatter("%(asctime)s | %(levelname)-7s | %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)
    try:
        fh = RotatingFileHandler(os.path.join(data_dir, "system.log"),
                                 maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    return log

LOG = setup_logging(CONFIG.data_dir)

def with_retry(attempts: int = 3, base_delay: float = 1.5, max_delay: float = 30.0):
    """Yalnızca RetryableError için sınırlı yeniden deneme."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            last = None
            for i in range(attempts):
                try:
                    return fn(*a, **k)
                except RetryableError as e:
                    last = e
                    d = min(max_delay, base_delay * (2 ** i)) * (getattr(e, "backoff", 1.0) or 1.0)
                    d *= 0.8 + 0.4 * random.random()
                    LOG.warning("Yeniden deneme %d/%d %s: %s (%.1fs)",
                                i + 1, attempts, fn.__name__, e, d)
                    time.sleep(d)
            raise last
        return wrapper
    return deco

class RateLimiter:
    def __init__(self, min_interval: float = 0.25):
        self.min_interval = min_interval
        self._last = defaultdict(float)
        self._lock = threading.Lock()
    def wait(self, key: str = "global"):
        with self._lock:
            dt = time.time() - self._last[key]
            if dt < self.min_interval:
                time.sleep(self.min_interval - dt)
            self._last[key] = time.time()

RATE = RateLimiter(0.25)

TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
              "12h": 43200, "1d": 86400, "1w": 604800}

def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")

def safe_float(x, default=None):
    try:
        f = float(x)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default

def clamp(x, lo, hi): return max(lo, min(hi, x))
def fmt_money(x): return f"{x:,.2f}" if x is not None else "n/a"
def _ok(v): return v is not None and not (isinstance(v, float) and math.isnan(v))

print("[HÜCRE 3] Loglama, hata sınıfları ve yardımcılar hazır.")

# ============================================================
# HÜCRE 4/18 — VERİ MODELLERİ + SQLITE KALICILIK
# ============================================================

class OrderStatus(str, Enum):
    PENDING_SUBMIT = "pending_submit"; OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"; FILLED = "filled"
    CANCELED = "canceled"; REJECTED = "rejected"
    EXPIRED = "expired"; UNKNOWN = "unknown"

@dataclass
class Ticker:
    symbol: str; last: float; bid: float; ask: float
    pct_change_24h: float = None; quote_volume: float = None
    timestamp_ms: float = None; fetched_at: float = 0.0
    @property
    def spread_pct(self):
        if self.bid and self.ask and self.ask > 0:
            return (self.ask - self.bid) / self.ask * 100.0
        return float("nan")

@dataclass
class OrderRecord:
    order_id: str; ts: float; mode: str; exchange: str; symbol: str
    side: str; type: str; amount: float; price: Optional[float]
    status: str = OrderStatus.PENDING_SUBMIT.value; filled: float = 0.0
    avg_fill_price: Optional[float] = None; fee_cost: float = 0.0
    fee_currency: str = ""; strategy: str = ""; client_tag: str = ""; raw: str = ""

@dataclass
class Position:
    symbol: str; side: str; amount: float; entry_price: float
    stop_loss: float = None; take_profit: float = None
    opened_at: float = 0.0; strategy: str = ""; fees_paid: float = 0.0
    def unrealized_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.amount
    def value(self, price: float) -> float:
        return self.amount * price

@dataclass
class TradeRecord:
    opened_at: float; closed_at: float; mode: str; exchange: str; symbol: str
    side: str; amount: float; entry_price: float; exit_price: float
    gross_pnl: float; fees: float; net_pnl: float
    strategy: str = ""; close_reason: str = ""

@dataclass
class Signal:
    ts: float; symbol: str; timeframe: str; strategy: str
    direction: str = "flat"; strength: float = 0.0
    price: float = 0.0; reason: str = ""

@dataclass
class Decision:
    ts: float; symbol: str; action: str
    score: float; confidence: float; reasons: List[str]
    regime: str = "unknown"; data_ok: bool = True

class Database:
    def __init__(self, path: str):
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self._lock = threading.RLock()
            self._schema(); self.conn.commit()
        except sqlite3.Error as e:
            raise FatalError(f"Veritabanı açılamadı ({path}): {e}")
        LOG.info("SQLite hazır: %s", path)

    def _schema(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders(
          order_id TEXT PRIMARY KEY, ts REAL, mode TEXT, exchange TEXT, symbol TEXT,
          side TEXT, type TEXT, amount REAL, price REAL, status TEXT, filled REAL,
          avg_fill_price REAL, fee_cost REAL, fee_currency TEXT,
          strategy TEXT, client_tag TEXT, raw TEXT);
        CREATE TABLE IF NOT EXISTS trades(
          id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at REAL, closed_at REAL,
          mode TEXT, exchange TEXT, symbol TEXT, side TEXT, amount REAL,
          entry_price REAL, exit_price REAL, gross_pnl REAL, fees REAL,
          net_pnl REAL, strategy TEXT, close_reason TEXT);
        CREATE TABLE IF NOT EXISTS decisions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mode TEXT, symbol TEXT,
          action TEXT, score REAL, confidence REAL, reasons TEXT, regime TEXT, data_ok INTEGER);
        CREATE TABLE IF NOT EXISTS signals(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, timeframe TEXT,
          strategy TEXT, direction TEXT, strength REAL, price REAL, reason TEXT);
        CREATE TABLE IF NOT EXISTS equity(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mode TEXT, cash REAL,
          positions_value REAL, equity REAL, unrealized REAL, realized_total REAL, fees_total REAL);
        CREATE TABLE IF NOT EXISTS news(
          id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT UNIQUE, published REAL,
          fetched REAL, source TEXT, title TEXT, url TEXT, sentiment REAL,
          impact REAL, assets TEXT);
        CREATE TABLE IF NOT EXISTS errors(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT,
          module TEXT, message TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def set_meta(self, k, v):
        self._exec("INSERT INTO meta(k,v) VALUES(?,?) "
                   "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def get_meta(self, k, default=None):
        r = self._exec("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def upsert_order(self, o: OrderRecord):
        self._exec("""INSERT INTO orders(order_id,ts,mode,exchange,symbol,side,type,amount,
                      price,status,filled,avg_fill_price,fee_cost,fee_currency,
                      strategy,client_tag,raw)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(order_id) DO UPDATE SET status=excluded.status,
                      filled=excluded.filled, avg_fill_price=excluded.avg_fill_price,
                      fee_cost=excluded.fee_cost""",
                   (o.order_id, o.ts, o.mode, o.exchange, o.symbol, o.side, o.type,
                    o.amount, o.price, o.status, o.filled, o.avg_fill_price,
                    o.fee_cost, o.fee_currency, o.strategy, o.client_tag, o.raw[:3500]))

    def save_trade(self, t: TradeRecord):
        self._exec("""INSERT INTO trades(opened_at,closed_at,mode,exchange,symbol,side,amount,
                      entry_price,exit_price,gross_pnl,fees,net_pnl,strategy,close_reason)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (t.opened_at, t.closed_at, t.mode, t.exchange, t.symbol, t.side,
                    t.amount, t.entry_price, t.exit_price, t.gross_pnl, t.fees,
                    t.net_pnl, t.strategy, t.close_reason))

    def trades_df(self, mode: str, since: float = 0) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT * FROM trades WHERE mode=? AND closed_at>=? ORDER BY closed_at",
            self.conn, params=(mode, since))

    def save_decision(self, d: Decision):
        self._exec("""INSERT INTO decisions(ts,mode,symbol,action,score,confidence,reasons,regime,data_ok)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (d.ts, CONFIG.mode.value, d.symbol, d.action, d.score, d.confidence,
                    json.dumps(d.reasons, ensure_ascii=False), d.regime, int(d.data_ok)))

    def save_signal(self, s: Signal):
        self._exec("""INSERT INTO signals(ts,symbol,timeframe,strategy,direction,strength,price,reason)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (s.ts, s.symbol, s.timeframe, s.strategy, s.direction,
                    s.strength, s.price, s.reason))

    def save_equity_point(self, mode, cash, positions_value, equity,
                          unrealized, realized_total, fees_total):
        self._exec("""INSERT INTO equity(ts,mode,cash,positions_value,equity,unrealized,realized_total,fees_total)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (time.time(), mode, cash, positions_value, equity,
                    unrealized, realized_total, fees_total))

    def equity_df(self, mode: str) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM equity WHERE mode=? ORDER BY ts",
                                 self.conn, params=(mode,))

    def save_error(self, module, message, detail="", level="ERROR"):
        try:
            self._exec("INSERT INTO errors(ts,level,module,message,detail) VALUES(?,?,?,?,?)",
                       (time.time(), level, module, scrub(message)[:800], scrub(detail)[:2000]))
        except sqlite3.Error:
            pass

    def insert_news(self, h, published, source, title, url, sentiment, impact, assets):
        cur = self._exec("""INSERT OR IGNORE INTO news(hash,published,fetched,source,title,url,
                            sentiment,impact,assets) VALUES(?,?,?,?,?,?,?,?,?)""",
                         (h, published, time.time(), source, title[:300], url,
                          sentiment, impact, json.dumps(assets)))
        return cur.rowcount > 0

    def recent_news(self, since: float) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM news WHERE published>=? ORDER BY published DESC",
                                 self.conn, params=(since,))

def log_exception(db: Database, module: str, e: Exception):
    LOG.error("[%s] %s: %s", module, type(e).__name__, e)
    db.save_error(module, f"{type(e).__name__}: {e}")

DB = Database(os.path.join(CONFIG.data_dir, "trading.db"))
print("[HÜCRE 4] Veri modelleri ve SQLite hazır.")

# ============================================================
# HÜCRE 5/18 — MEXC BORSA ADAPTÖRÜ (ccxt)
# ============================================================

@dataclass
class Capabilities:
    fetch_ohlcv: bool = False; fetch_ticker: bool = False
    fetch_order_book: bool = False; fetch_balance: bool = False
    create_order: bool = False; cancel_order: bool = False
    fetch_order: bool = False; fetch_open_orders: bool = False
    fetch_my_trades: bool = False; fetch_funding_rate: bool = False
    fetch_positions: bool = False

class CcxtAdapter:
    def __init__(self, exchange_id: str, market_type: str = "spot",
                 credentials: Optional[Credentials] = None):
        if not hasattr(ccxt, exchange_id):
            raise FatalError(f"Bilinmeyen borsa id: '{exchange_id}'.")
        self.id = exchange_id
        self.exchange_name = exchange_id.capitalize()
        self.market_type = market_type
        self.client = getattr(ccxt, exchange_id)({
            "enableRateLimit": True,
            "options": {"defaultType": "spot" if market_type == "spot" else "swap"},
        })
        self.credentials = credentials or Credentials()
        self._apply_credentials(self.credentials)
        self._markets = None
        self._capabilities = Capabilities(
            fetch_ohlcv=bool(self.client.has.get("fetchOHLCV")),
            fetch_ticker=bool(self.client.has.get("fetchTicker")),
            fetch_order_book=bool(self.client.has.get("fetchOrderBook")),
            fetch_balance=bool(self.client.has.get("fetchBalance")),
            create_order=bool(self.client.has.get("createOrder")),
            cancel_order=bool(self.client.has.get("cancelOrder")),
            fetch_order=bool(self.client.has.get("fetchOrder")),
            fetch_open_orders=bool(self.client.has.get("fetchOpenOrders")),
            fetch_my_trades=bool(self.client.has.get("fetchMyTrades")),
            fetch_funding_rate=bool(self.client.has.get("fetchFundingRate")),
            fetch_positions=bool(self.client.has.get("fetchPositions")),
        )

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    def _call(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except ccxt.RateLimitExceeded as e:
            ra = None
            try:
                h = getattr(e, "headers", None) or {}
                ra = h.get("Retry-After") or h.get("retry-after")
            except Exception:
                ra = None
            raise RetryableError(f"hız sınırı: {e}",
                                 backoff=max(4.0, safe_float(ra, 4.0) or 4.0))
        except ccxt.InvalidNonce as e:
            raise RetryableError(f"geçersiz nonce: {e}", backoff=10.0)
        except ccxt.OnMaintenance as e:
            raise RetryableError(f"bakımda: {e}", backoff=30.0)
        except ccxt.ExchangeNotAvailable as e:
            raise RetryableError(f"erişilemiyor: {e}", backoff=8.0)
        except ccxt.RequestTimeout as e:
            raise RetryableError(f"zaman aşımı: {e}", backoff=3.0)
        except ccxt.NetworkError as e:
            raise RetryableError(f"ağ hatası: {e}", backoff=3.0)
        except ccxt.AuthenticationError as e:
            raise FatalError(f"kimlik doğrulama hatası: {e}")
        except ccxt.PermissionDenied as e:
            raise FatalError(f"izin reddedildi: {e}")
        except ccxt.InsufficientFunds as e:
            raise TradeBlockedError(f"bakiye yetersiz: {e}")
        except ccxt.InvalidOrder as e:
            raise TradeBlockedError(f"geçersiz emir: {e}")
        except ccxt.BadSymbol as e:
            raise FatalError(f"geçersiz sembol: {e}")
        except ccxt.NotSupported as e:
            raise FeatureNotSupportedError(f"{self.id} desteklemiyor: {e}")
        except ccxt.ExchangeError as e:
            raise RetryableError(f"borsa hatası: {e}", backoff=2.0)

    def _apply_credentials(self, c: Credentials):
        register_secret(c.api_key)
        register_secret(c.secret)
        self.client.apiKey = c.api_key or None
        self.client.secret = c.secret or None
        self.credentials = c

    def attach_credentials(self, c: Credentials):
        self._apply_credentials(c)
        if c.api_key:
            self.load_markets(force=True)

    @with_retry()
    def load_markets(self, force: bool = False):
        RATE.wait("markets")
        if self._markets is None or force:
            self._markets = self._call(self.client.load_markets, force)
        return self._markets

    def markets(self):
        return self.load_markets()

    def validate_symbol(self, symbol: str):
        m = self.markets()
        if symbol not in m:
            raise FatalError(f"'{symbol}' bu borsada bulunamadı.")
        if m[symbol].get("active") is False:
            raise TradeBlockedError(f"'{symbol}' pasif/işlem dışı.")

    def supported_timeframes(self) -> Optional[set]:
        tfs = getattr(self.client, "timeframes", None)
        return set(tfs.keys()) if tfs else None

    def check_timeframe(self, timeframe: str) -> bool:
        tf = self.supported_timeframes()
        return True if tf is None else timeframe in tf

    def order_constraints(self, symbol: str) -> dict:
        m = self.markets()[symbol]
        lim = m.get("limits") or {}
        amt_l = lim.get("amount") or {}
        cost_l = lim.get("cost") or {}
        prec = m.get("precision") or {}
        TICK_SIZE = getattr(ccxt.Exchange, "TICK_SIZE", 2)
        pmode = getattr(self.client, "precision_mode", None)
        if pmode == TICK_SIZE:
            step_amt = safe_float(prec.get("amount"), 1e-8) or 1e-8
            step_px  = safe_float(prec.get("price"), 1e-8) or 1e-8
        else:
            da, dp = prec.get("amount"), prec.get("price")
            step_amt = 10 ** -da if da is not None else 1e-8
            step_px  = 10 ** -dp if dp is not None else 1e-8
        fee = m.get("fees") or {}
        if fee.get("taker") is None:
            tf_ = self.client.fees.get("trading") or {}
            fee = {"taker": tf_.get("taker", 0.001), "maker": tf_.get("maker", 0.001)}
        return {"amount_min": safe_float(amt_l.get("min"), 0.0) or 0.0,
                "amount_max": safe_float(amt_l.get("max"), None),
                "cost_min": safe_float(cost_l.get("min"), 0.0) or 0.0,
                "amount_step": step_amt, "price_step": step_px,
                "taker_fee": safe_float(fee.get("taker"), 0.001),
                "maker_fee": safe_float(fee.get("maker"), 0.001)}

    def round_amount(self, symbol, amount) -> float:
        try:
            return float(self.client.amount_to_precision(symbol, amount))
        except Exception:
            step = self.order_constraints(symbol)["amount_step"]
            return math.floor(amount / step) * step

    def round_price(self, symbol, price) -> float:
        try:
            return float(self.client.price_to_precision(symbol, price))
        except Exception:
            step = self.order_constraints(symbol)["price_step"]
            return math.floor(price / step) * step

    @with_retry()
    def fetch_ticker(self, symbol) -> Ticker:
        self.validate_symbol(symbol)
        RATE.wait("ticker")
        t = self._call(self.client.fetch_ticker, symbol)
        return Ticker(symbol=symbol,
                      last=safe_float(t.get("last")) or safe_float(t.get("close")),
                      bid=safe_float(t.get("bid")), ask=safe_float(t.get("ask")),
                      pct_change_24h=safe_float(t.get("percentage")),
                      quote_volume=safe_float(t.get("quoteVolume")),
                      timestamp_ms=safe_float(t.get("timestamp")), fetched_at=time.time())

    @with_retry()
    def fetch_ohlcv_df(self, symbol, timeframe="15m", limit=400) -> pd.DataFrame:
        if not self.capabilities.fetch_ohlcv:
            raise FeatureNotSupportedError("OHLCV desteği yok.")
        if not self.check_timeframe(timeframe):
            raise FatalError(f"'{timeframe}' zaman dilimi desteklenmiyor.")
        self.validate_symbol(symbol)
        RATE.wait("ohlcv")
        raw = self._call(self.client.fetch_ohlcv, symbol, timeframe, None, limit)
        if not raw:
            raise InsufficientDataError(f"{symbol} {timeframe}: boş OHLCV döndü.")
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        for c_ in ["open", "high", "low", "close", "volume"]:
            df[c_] = pd.to_numeric(df[c_], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        if df.empty:
            raise DataQualityError(f"{symbol} {timeframe}: bozuk mum verisi.")
        return df

    @with_retry()
    def fetch_order_book(self, symbol, limit=50) -> dict:
        if not self.capabilities.fetch_order_book:
            raise FeatureNotSupportedError("emir defteri desteği yok.")
        RATE.wait("book")
        return self._call(self.client.fetch_order_book, symbol, limit)

    def _require_key(self, action: str):
        if not self.credentials.api_key:
            raise FeatureNotSupportedError(f"'{action}' için API anahtarı gerekli.")

    @with_retry()
    def fetch_balance(self) -> dict:
        self._require_key("bakiye sorgusu")
        b = self._call(self.client.fetch_balance)
        keep = lambda d: {k: safe_float(v) for k, v in (d or {}).items() if safe_float(v, 0)}
        return {"free": keep(b.get("free")), "used": keep(b.get("used")),
                "total": keep(b.get("total"))}

    def create_order(self, symbol, side, order_type, amount, price=None,
                     client_tag=None, params=None) -> OrderRecord:
        # DİKKAT: bilinçli olarak @with_retry YOK (timeout'ta çifte emir riski).
        self._require_key("emir oluşturma")
        if not self.capabilities.create_order:
            raise FeatureNotSupportedError("emir oluşturma desteklenmiyor.")
        self.validate_symbol(symbol)
        cons = self.order_constraints(symbol)
        amount = self.round_amount(symbol, amount)
        if amount <= 0 or amount < cons["amount_min"]:
            raise TradeBlockedError(f"{symbol}: miktar minimumun altında ({amount}).")
        if price is not None and order_type == "limit":
            price = self.round_price(symbol, price)
        RATE.wait("order")
        o = self._call(self.client.create_order, symbol, order_type, side,
                       amount, price, dict(params or {}))
        rec = OrderRecord(
            order_id=str(o.get("id") or f"unknown-{time.time():.0f}"), ts=time.time(),
            mode=CONFIG.mode.value, exchange=self.id, symbol=symbol, side=side,
            type=order_type, amount=amount, price=price,
            status=(o.get("status") or OrderStatus.OPEN.value),
            filled=safe_float(o.get("filled"), 0.0) or 0.0,
            avg_fill_price=safe_float(o.get("average")),
            client_tag=client_tag or "", raw=json.dumps(o, default=str))
        rec.fee_cost = safe_float((o.get("fee") or {}).get("cost"), 0.0) or 0.0
        rec.fee_currency = (o.get("fee") or {}).get("currency") or ""
        return rec

    @with_retry()
    def fetch_order(self, order_id, symbol=None):
        self._require_key("emir sorgusu")
        return self._call(self.client.fetch_order, order_id, symbol)

    @with_retry()
    def fetch_open_orders(self, symbol=None) -> list:
        self._require_key("açık emir listesi")
        return self._call(self.client.fetch_open_orders, symbol)

    @with_retry()
    def fetch_my_trades(self, symbol, since_ms=None, limit=200) -> list:
        self._require_key("işlem geçmişi")
        return self._call(self.client.fetch_my_trades, symbol, since_ms, limit)

    @with_retry()
    def fetch_funding_rate(self, symbol) -> dict:
        if not self.capabilities.fetch_funding_rate:
            raise FeatureNotSupportedError("fonlama oranı desteklenmiyor (spot).")
        return self._call(self.client.fetch_funding_rate, symbol)

    def capability_report(self) -> dict:
        out = {f: getattr(self._capabilities, f) for f in self._capabilities.__dataclass_fields__}
        tfs = self.supported_timeframes()
        out["timeframes"] = sorted(tfs) if tfs else "bilinmiyor"
        out["has_key"] = bool(self.credentials.api_key)
        return out

def build_adapter(config: AppConfig) -> CcxtAdapter:
    a = CcxtAdapter(config.exchange_id, config.market_type)
    a.load_markets()
    print(f"-> {a.exchange_name} bağlandı. {len(a.markets())} piyasa yüklendi.")
    return a

ADAPTER = build_adapter(CONFIG)
print("[HÜCRE 5] Borsa adaptörü hazır.")

# ============================================================
# HÜCRE 6/18 — PİYASA VERİSİ MOTORU
# ============================================================

class MarketDataEngine:
    def __init__(self, adapter: CcxtAdapter, config: AppConfig):
        self.adapter = adapter
        self.config = config
        self.max_age = config.risk.max_data_age_seconds
        self._ohlcv_cache = {}
        self._tickers = {}
        self._books = {}
        self.stats = {"ticker_calls": 0, "ohlcv_calls": 0, "book_calls": 0, "errors": 0}

    def refresh_ticker(self, symbol) -> Ticker:
        try:
            t = self.adapter.fetch_ticker(symbol)
            self._tickers[symbol] = t
            self.stats["ticker_calls"] += 1
            return t
        except TradingSystemError as e:
            self.stats["errors"] += 1
            if isinstance(e, RetryableError):
                LOG.warning("ticker %s: %s", symbol, e)
            raise

    def get_ticker(self, symbol, force: bool = False) -> Ticker:
        t = self._tickers.get(symbol)
        if force or t is None or (time.time() - t.fetched_at) > self.max_age:
            t = self.refresh_ticker(symbol)
        return t

    def refresh_order_book(self, symbol, depth=50) -> dict:
        b = self.adapter.fetch_order_book(symbol, depth)
        b["_ts"] = time.time()
        self._books[symbol] = b
        self.stats["book_calls"] += 1
        return b

    def get_order_book_stats(self, symbol, max_age=None) -> dict:
        max_age = max_age or self.max_age
        b = self._books.get(symbol)
        if b is None or (time.time() - b.get("_ts", 0)) > max_age:
            b = self.refresh_order_book(symbol)
        bids = b.get("bids") or []
        asks = b.get("asks") or []
        bid = safe_float(bids[0][0]) if bids else None
        ask = safe_float(asks[0][0]) if asks else None
        bid_v = sum(safe_float(x[1], 0) for x in bids[:10])
        ask_v = sum(safe_float(x[1], 0) for x in asks[:10])
        spread_pct = (ask - bid) / ask * 100 if (bid and ask and ask > 0) else float("nan")
        imb = bid_v / (bid_v + ask_v) if (bid_v + ask_v) > 0 else 0.5
        return {"bid": bid, "ask": ask, "spread_pct": spread_pct,
                "bid_vol_10": bid_v, "ask_vol_10": ask_v, "bid_imbalance": imb}

    @staticmethod
    def to_closed(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """Kapanmamış son mumu atar — yanlış sinyal önlemi."""
        if df.empty:
            return df
        sec = TF_SECONDS.get(timeframe)
        if not sec:
            return df
        close_time = df["ts"] + pd.to_timedelta(sec, unit="s")
        return df[close_time <= utc_now()].reset_index(drop=True)

    def get_ohlcv(self, symbol, timeframe, limit=None, min_candles: int = 0,
                  force: bool = False) -> pd.DataFrame:
        limit = limit or self.config.max_candles
        key = (symbol, timeframe)
        c = self._ohlcv_cache.get(key)
        ttl = max(TF_SECONDS[timeframe] / 3.0, 10)
        if force or c is None or (time.time() - c["fetched"]) > ttl:
            try:
                df = self.adapter.fetch_ohlcv_df(symbol, timeframe, limit)
                self._ohlcv_cache[key] = {"df": df, "fetched": time.time()}
                self.stats["ohlcv_calls"] += 1
            except RetryableError as e:
                if c is None:
                    raise
                LOG.warning("%s %s: taze veri yok (%s), önbellek kullanılıyor.", symbol, timeframe, e)
                df = c["df"]
        else:
            df = c["df"]
        closed = self.to_closed(df.copy(), timeframe)
        if len(closed) >= 3:
            gaps = closed["ts"].diff().dt.total_seconds().dropna()
            expected = TF_SECONDS[timeframe]
            if len(gaps) and float((gaps > expected * 1.5).mean()) > 0.2:
                raise DataQualityError(f"{symbol} {timeframe}: >%20 boşluk — BEKLE.")
        if len(closed) < max(min_candles, 2):
            raise InsufficientDataError(
                f"{symbol} {timeframe}: yetersiz kapalı mum ({len(closed)}).")
        return closed

    def snapshot(self, symbol) -> dict:
        t = self.get_ticker(symbol)
        try:
            book = self.get_order_book_stats(symbol)
        except TradingSystemError:
            book = {"spread_pct": float("nan")}
        age = time.time() - t.fetched_at
        return {"symbol": symbol, "last": t.last, "bid": t.bid, "ask": t.ask,
                "spread_pct": book.get("spread_pct", float("nan")),
                "quote_volume_24h": t.quote_volume, "pct_change_24h": t.pct_change_24h,
                "data_age_s": age, "fresh": age <= self.max_age}

    def freshness_ok(self, symbol) -> bool:
        t = self._tickers.get(symbol)
        return t is not None and (time.time() - t.fetched_at) <= self.max_age

ENGINE = MarketDataEngine(ADAPTER, CONFIG)
print("[HÜCRE 6] Piyasa verisi motoru hazır.")

# ============================================================
# HÜCRE 7/18 — TEKNİK ANALİZ MOTORU
# ============================================================

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0.0)
    loss = -d.clip(upper=0.0)
    ag = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    al = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = ag / al.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(al != 0, 100.0).where(ag.notna() | al.notna())

def macd(close, fast=12, slow=26, signal=9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig

def bollinger(close, n=20, k=2.0):
    mid = sma(close, n)
    sd = close.rolling(n).std(ddof=0)
    up, lo = mid + k * sd, mid - k * sd
    bandwidth = (up - lo) / mid.replace(0, np.nan) * 100
    percent_b = (close - lo) / (up - lo).replace(0, np.nan)
    return mid, up, lo, bandwidth, percent_b

def _true_range(df):
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                      (df["low"] - pc).abs()], axis=1).max(axis=1)

def atr(df, period=14) -> pd.Series:
    return _true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

def adx(df, period=14):
    h, l = df["high"], df["low"]
    up, dn = h.diff(), -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr_ = _true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_
    mdi = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean(), pdi, mdi

def stochastic_rsi(close, rsi_n=14, stoch_n=14, k_n=3, d_n=3):
    r = rsi(close, rsi_n)
    lo = r.rolling(stoch_n).min()
    hi = r.rolling(stoch_n).max()
    k = 100 * (r - lo) / (hi - lo).replace(0, np.nan)
    return k, k.rolling(d_n).mean()

def obv(df) -> pd.Series:
    return (np.sign(df["close"].diff()).fillna(0) * df["volume"]).cumsum()

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
    """Salınım tepe/diplerinden kümelenmiş destek & direnç seviyeleri."""
    if len(df) < 2 * w + 5:
        return [], []
    d = df.tail(lookback).reset_index(drop=True)
    highs, lows = [], []
    H = d["high"].values
    L = d["low"].values
    for i in range(w, len(d) - w):
        if H[i] >= max(H[i - w:i + w + 1]):
            highs.append(float(H[i]))
        if L[i] <= min(L[i - w:i + w + 1]):
            lows.append(float(L[i]))
    return _cluster(highs), _cluster(lows)

print("[HÜCRE 7] Teknik analiz motoru hazır.")

# ============================================================
# HÜCRE 8/18 — HABER & DUYARLILIK MOTORU (RSS, anahtarsız)
# ============================================================
import feedparser
from urllib.parse import urlparse

class NewsEngine:
    FEEDS = ["https://cointelegraph.com/rss",
             "https://www.coindesk.com/arc/outboundfeeds/rss/"]
    POS = {"surge", "rally", "bullish", "gain", "gains", "soar", "jump", "adoption",
           "partnership", "approval", "upgrade", "inflow", "breakout", "record",
           "bull", "launch", "etf", "positive", "integration"}
    NEG = {"crash", "plunge", "bearish", "loss", "losses", "hack", "hacked", "exploit",
           "ban", "lawsuit", "fraud", "sell-off", "selloff", "dump", "outflow",
           "liquidation", "warning", "fears", "collapse", "downgrade", "delist", "risk"}
    ALIASES = {"BTC": ["bitcoin"], "ETH": ["ethereum"], "SOL": ["solana"],
               "BNB": ["binance", "bnb"], "XRP": ["ripple", "xrp"], "DOGE": ["dogecoin"],
               "ADA": ["cardano"], "AVAX": ["avalanche"], "DOT": ["polkadot"],
               "LINK": ["chainlink"], "TRX": ["tron"], "LTC": ["litecoin"],
               "UNI": ["uniswap"], "ATOM": ["cosmos"], "XLM": ["stellar"],
               "NEAR": ["near protocol"], "ETC": ["ethereum classic"],
               "FIL": ["filecoin"], "APT": ["aptos"], "ARB": ["arbitrum"]}

    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.cfg = config
        self._last = 0.0

    def refresh(self, force: bool = False):
        if not force and time.time() - self._last < self.cfg.news_refresh_minutes * 60:
            return
        n = 0
        for url in self.FEEDS:
            try:
                d = feedparser.parse(url)
                for e in getattr(d, "entries", [])[:20]:
                    title = (getattr(e, "title", "") or "").strip()
                    link = getattr(e, "link", "") or ""
                    if not title:
                        continue
                    h = hashlib.sha1((title or link).encode("utf-8")).hexdigest()
                    sent = self._sentiment(title)
                    if self.db.insert_news(h, self._entry_ts(e),
                                           urlparse(url).netloc,
                                           title, link, sent, abs(sent), self._assets(title)):
                        n += 1
            except Exception as e:
                LOG.warning("Haber akışı okunamadı %s: %s", url, e)
        self._last = time.time()
        LOG.info("Haber taraması bitti: %d yeni kayıt", n)

    def _entry_ts(self, e) -> float:
        try:
            pp = getattr(e, "published_parsed", None)
            if pp:
                return float(calendar.timegm(pp))
        except Exception:
            pass
        return time.time()

    def _sentiment(self, text: str) -> float:
        t = text.lower()
        s = 0
        for w in self.POS:
            if w in t:
                s += 1
        for w in self.NEG:
            if w in t:
                s -= 1
        return float(math.tanh(s / 3.0))

    def _assets(self, title: str) -> list:
        t = title.lower()
        bases = sorted({s.split("/")[0] for s in self.cfg.symbols})
        return [b for b in bases
                if re.search(rf"\b{b.lower()}\b", t) or
                any(a in t for a in self.ALIASES.get(b, []))]

    def sentiment_for(self, symbol: str, hours: int = 24) -> dict:
        base = symbol.split("/")[0]
        aliases = [base.lower()] + [a.lower() for a in self.ALIASES.get(base, [])]
        try:
            df = self.db.recent_news(time.time() - hours * 3600)
        except Exception:
            return {"sentiment": 0.0, "count": 0}
        if df is None or df.empty:
            return {"sentiment": 0.0, "count": 0}
        num = den = cnt = 0.0
        now = time.time()
        for _, row in df.iterrows():
            title_l = str(row.get("title") or "").lower()
            if not any(a in title_l for a in aliases):
                continue
            age = max(0.0, now - safe_float(row.get("published"), now))
            w = max(safe_float(row.get("impact"), 0.0), 0.05) * math.exp(-age / 21600.0)
            num += safe_float(row.get("sentiment"), 0.0) * w
            den += w
            cnt += 1
        return {"sentiment": num / den if den > 0 else 0.0, "count": int(cnt)}

NEWS = NewsEngine(DB, CONFIG)
print("[HÜCRE 8] Haber & duyarlılık motoru hazır.")

# ============================================================
# HÜCRE 9/18 — STRATEJİ MOTORU (7 strateji)
# ============================================================

def _sig(ts, symbol, tf, name, direction, strength, price, reason=""):
    return Signal(ts=ts, symbol=symbol, timeframe=tf, strategy=name,
                  direction=direction, strength=float(clamp(strength, 0.0, 1.0)),
                  price=float(price), reason=reason)

class StrategyEngine:
    def __init__(self, config: AppConfig):
        self.cfg = config

    def compute_all(self, symbol, df: pd.DataFrame, df_high=None):
        ts = time.time()
        tf = self.cfg.primary_timeframe
        price = float(df["close"].iloc[-1])
        names = list(self.cfg.strategy_weights.keys())
        if len(df) < 60:
            return [_sig(ts, symbol, tf, n, "flat", 0, price, "yetersiz veri") for n in names], {}
        close = df["close"]
        _, _, mh = macd(close)
        _, _, _, _, pb = bollinger(close)
        ax, pdi, mdi = adx(df)
        ind = {"close": price,
               "ema9": safe_float(ema(close, 9).iloc[-1]),
               "ema21": safe_float(ema(close, 21).iloc[-1]),
               "ema50": safe_float(ema(close, 50).iloc[-1]),
               "ema200": safe_float(ema(close, 200).iloc[-1]) if len(close) >= 200 else None,
               "atr": safe_float(atr(df).iloc[-1]),
               "adx": safe_float(ax.iloc[-1]),
               "plus_di": safe_float(pdi.iloc[-1]),
               "minus_di": safe_float(mdi.iloc[-1]),
               "rsi": safe_float(rsi(close).iloc[-1]),
               "macd_hist": safe_float(mh.iloc[-1]),
               "macd_hist_prev": safe_float(mh.iloc[-2]),
               "bb_pb": safe_float(pb.iloc[-1])}
        ind["atr_pct"] = ind["atr"] / price * 100 if _ok(ind["atr"]) and ind["atr"] > 0 else float("nan")
        vma = safe_float(df["volume"].rolling(20).mean().iloc[-1])
        ind["vol_ratio"] = float(df["volume"].iloc[-1]) / vma if vma else float("nan")
        ind["don_high"] = safe_float(df["high"].rolling(20).max().iloc[-2])
        sigs = [self._trend_follow(ts, symbol, tf, price, ind),
                self._ema_cross(ts, symbol, tf, price, ind),
                self._rsi_momentum(ts, symbol, tf, price, ind),
                self._macd(ts, symbol, tf, price, ind),
                self._bollinger_rev(ts, symbol, tf, price, ind),
                self._volume_breakout(ts, symbol, tf, price, ind),
                self._multi_timeframe(ts, symbol, tf, price, df_high)]
        return sigs, ind

    def _trend_follow(self, ts, symbol, tf, price, ind):
        e50, e200, ax = ind.get("ema50"), ind.get("ema200"), ind.get("adx")
        if not (_ok(e50) and _ok(ax)):
            return _sig(ts, symbol, tf, "trend_follow", "flat", 0, price, "veri hazırlanıyor")
        e2 = e200 if _ok(e200) else e50
        if e50 > e2 and price > e50 and ax >= 20:
            return _sig(ts, symbol, tf, "trend_follow", "long",
                        clamp((ax - 15) / 35, 0, 1), price, f"yükseliş trendi ADX={ax:.0f}")
        if e50 < e2 and price < e50 and ax >= 20:
            return _sig(ts, symbol, tf, "trend_follow", "short",
                        clamp((ax - 15) / 35, 0, 1), price, f"düşüş trendi ADX={ax:.0f}")
        return _sig(ts, symbol, tf, "trend_follow", "flat", 0, price, "nötr")

    def _ema_cross(self, ts, symbol, tf, price, ind):
        e9, e21, a = ind.get("ema9"), ind.get("ema21"), ind.get("atr")
        if not (_ok(e9) and _ok(e21) and _ok(a) and a > 0):
            return _sig(ts, symbol, tf, "ema_cross", "flat", 0, price, "veri hazırlanıyor")
        d = e9 - e21
        if d > 0:
            return _sig(ts, symbol, tf, "ema_cross", "long",
                        clamp(abs(d) / (2 * a), 0, 1), price, "EMA9>EMA21")
        if d < 0:
            return _sig(ts, symbol, tf, "ema_cross", "short",
                        clamp(abs(d) / (2 * a), 0, 1), price, "EMA9<EMA21")
        return _sig(ts, symbol, tf, "ema_cross", "flat", 0, price, "nötr")

    def _rsi_momentum(self, ts, symbol, tf, price, ind):
        r = ind.get("rsi")
        if not _ok(r):
            return _sig(ts, symbol, tf, "rsi_momentum", "flat", 0, price, "veri hazırlanıyor")
        if r <= 32:
            return _sig(ts, symbol, tf, "rsi_momentum", "long",
                        clamp((35 - r) / 20, 0.2, 1), price, f"RSI={r:.0f} aşırı satım")
        if r >= 68:
            return _sig(ts, symbol, tf, "rsi_momentum", "short",
                        clamp((r - 65) / 20, 0.2, 1), price, f"RSI={r:.0f} aşırı alım")
        return _sig(ts, symbol, tf, "rsi_momentum", "flat", 0, price, "nötr")

    def _macd(self, ts, symbol, tf, price, ind):
        mh, mp, a = ind.get("macd_hist"), ind.get("macd_hist_prev"), ind.get("atr")
        if not (_ok(mh) and _ok(mp) and _ok(a) and a > 0):
            return _sig(ts, symbol, tf, "macd", "flat", 0, price, "veri hazırlanıyor")
        if mh > 0 and mh >= mp:
            return _sig(ts, symbol, tf, "macd", "long",
                        clamp(mh / (3 * a), 0.2, 1), price, "MACD pozitif&güçleniyor")
        if mh < 0 and mh <= mp:
            return _sig(ts, symbol, tf, "macd", "short",
                        clamp(-mh / (3 * a), 0.2, 1), price, "MACD negatif&güçleniyor")
        return _sig(ts, symbol, tf, "macd", "flat", 0, price, "zayıf")

    def _bollinger_rev(self, ts, symbol, tf, price, ind):
        pb = ind.get("bb_pb")
        if not _ok(pb):
            return _sig(ts, symbol, tf, "bollinger_reversion", "flat", 0, price, "veri hazırlanıyor")
        if pb <= 0.05:
            return _sig(ts, symbol, tf, "bollinger_reversion", "long",
                        clamp((0.05 - pb) * 12, 0.2, 1), price, f"%B={pb:.2f} alt bant")
        if pb >= 0.95:
            return _sig(ts, symbol, tf, "bollinger_reversion", "short",
                        clamp((pb - 0.95) * 12, 0.2, 1), price, f"%B={pb:.2f} üst bant")
        return _sig(ts, symbol, tf, "bollinger_reversion", "flat", 0, price, "nötr")

    def _volume_breakout(self, ts, symbol, tf, price, ind):
        vr, dh = ind.get("vol_ratio"), ind.get("don_high")
        if not (_ok(vr) and _ok(dh)):
            return _sig(ts, symbol, tf, "volume_breakout", "flat", 0, price, "veri hazırlanıyor")
        if vr >= 1.8 and price >= dh * 0.995:
            return _sig(ts, symbol, tf, "volume_breakout", "long",
                        clamp((vr - 1.5) / 1.5, 0.2, 1), price, f"hacim {vr:.1f}x + kırılım")
        return _sig(ts, symbol, tf, "volume_breakout", "flat", 0, price, "kırılım yok")

    def _multi_timeframe(self, ts, symbol, tf, price, df_high):
        if df_high is None or len(df_high) < 60:
            return _sig(ts, symbol, tf, "multi_timeframe", "flat", 0, price, "üst TF verisi yok")
        ch = df_high["close"]
        e50h = safe_float(ema(ch, 50).iloc[-1])
        e200h = safe_float(ema(ch, 200).iloc[-1]) if len(ch) >= 200 else e50h
        if not _ok(e50h):
            return _sig(ts, symbol, tf, "multi_timeframe", "flat", 0, price, "üst TF hazırlanıyor")
        s = clamp(abs(price / e50h - 1) * 50, 0.3, 1.0)
        if price > e50h and e50h >= e200h:
            return _sig(ts, symbol, tf, "multi_timeframe", "long", s, price, "üst TF yükseliş")
        if price < e50h and e50h <= e200h:
            return _sig(ts, symbol, tf, "multi_timeframe", "short", s, price, "üst TF düşüş")
        return _sig(ts, symbol, tf, "multi_timeframe", "flat", 0, price, "üst TF nötr")

STRATEGIES = StrategyEngine(CONFIG)
print("[HÜCRE 9] Strateji motoru hazır (7 strateji).")

# ============================================================
# HÜCRE 10/18 — PİYASA REJİMİ TESPİTİ
# ============================================================

class RegimeDetector:
    def classify(self, df: pd.DataFrame, ind: dict) -> str:
        r = CONFIG.risk
        ap, ax = ind.get("atr_pct"), ind.get("adx")
        e50, e200 = ind.get("ema50"), ind.get("ema200")
        if not (_ok(ap) and _ok(ax) and _ok(e50)):
            return "unknown"
        if ap > r.max_atr_pct:
            return "volatile"
        if ap < r.min_atr_pct:
            return "quiet"
        if ax >= 25:
            e2 = e200 if _ok(e200) else e50
            return "trend_up" if e50 > e2 else "trend_down"
        return "range"

    def adjust_weights(self, weights: dict, regime: str) -> dict:
        w = dict(weights)
        if regime == "volatile":
            return {k: 0.0 for k in w}
        if regime == "quiet":
            return {k: v * 0.5 for k, v in w.items()}
        if regime == "range":
            w["trend_follow"] = w.get("trend_follow", 1.0) * 0.3
            w["multi_timeframe"] = w.get("multi_timeframe", 1.0) * 0.5
            w["bollinger_reversion"] = w.get("bollinger_reversion", 1.0) * 1.5
            w["rsi_momentum"] = w.get("rsi_momentum", 1.0) * 1.2
        elif regime in ("trend_up", "trend_down"):
            w["trend_follow"] = w.get("trend_follow", 1.0) * 1.2
            w["multi_timeframe"] = w.get("multi_timeframe", 1.0) * 1.2
            w["bollinger_reversion"] = w.get("bollinger_reversion", 1.0) * 0.5
        return w

REGIME = RegimeDetector()
print("[HÜCRE 10] Rejim dedektörü hazır.")

# ============================================================
# HÜCRE 11/18 — RİSK YÖNETİCİSİ (temel)
# ============================================================

class RiskManager:
    def __init__(self, config: AppConfig, db: Database):
        self.cfg = config
        self.db = db
        self._day = None
        self._day_start_equity = 0.0

    def update_day_anchor(self, equity: float):
        day = str(utc_now().date())
        if self._day != day:
            self._day = day
            self._day_start_equity = equity or 0.0
            LOG.info("Gün başı equity referansı: %s", fmt_money(self._day_start_equity))

    def daily_loss_pct(self, equity: float) -> float:
        if not self._day_start_equity:
            return 0.0
        return max(0.0, (self._day_start_equity - equity) / self._day_start_equity * 100.0)

    def drawdown_pct(self, equity: float, mode: str) -> float:
        try:
            df = self.db.equity_df(mode)
            peak = float(df["equity"].max()) if not df.empty else equity
        except Exception:
            peak = equity
        peak = max(peak or 0.0, equity or 0.0)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - equity) / peak * 100.0)

    def consecutive_losses(self, mode: str) -> int:
        try:
            df = self.db.trades_df(mode)
        except Exception:
            return 0
        if df.empty:
            return 0
        n = self.cfg.risk.circuit_break_after_consecutive_losses
        c = 0
        for v in reversed(df["net_pnl"].tail(n).tolist()):
            if v < 0:
                c += 1
            else:
                break
        return c

    def cooldown_active(self, symbol: str) -> bool:
        v = self.db.get_meta(f"stop_hit:{symbol}")
        if not v:
            return False
        try:
            ts = float(v)
        except (TypeError, ValueError):
            return False
        return (time.time() - ts) < self.cfg.risk.cooldown_minutes_after_stop_hit * 60

    def register_stop_hit(self, symbol: str):
        self.db.set_meta(f"stop_hit:{symbol}", str(time.time()))
        LOG.warning("[%s] STOP vuruldu — %d dk bekleme başladı.",
                    symbol, self.cfg.risk.cooldown_minutes_after_stop_hit)

    def pre_trade_checks(self, symbol, snapshot: dict, ind: dict, portfolio, prices) -> tuple:
        r = self.cfg.risk
        reasons = []
        eq = portfolio.equity(prices)
        if snapshot.get("fresh") is not True:
            reasons.append("veri bayat")
        dl = self.daily_loss_pct(eq)
        if dl >= r.max_daily_loss_pct:
            reasons.append(f"günlük zarar {dl:.1f}%≥{r.max_daily_loss_pct}%")
        dd = self.drawdown_pct(eq, portfolio.mode)
        if dd >= r.max_drawdown_pct:
            reasons.append(f"max drawdown {dd:.1f}%")
        if self.consecutive_losses(portfolio.mode) >= r.circuit_break_after_consecutive_losses:
            reasons.append("devre kesici: üst üste zarar")
        if self.cooldown_active(symbol):
            reasons.append("stop sonrası bekleme")
        if len(portfolio.positions) >= r.max_open_positions:
            reasons.append("maks açık pozisyon")
        if portfolio.has(symbol):
            reasons.append("sembolde zaten pozisyon var")
        sp = snapshot.get("spread_pct")
        if not _ok(sp) or sp > r.max_spread_pct:
            reasons.append("spread geniş")
        qv = snapshot.get("quote_volume_24h")
        if not qv or qv < r.min_24h_quote_volume_usd:
            reasons.append("24s hacim düşük")
        ap = ind.get("atr_pct")
        if not _ok(ap) or ap < r.min_atr_pct or ap > r.max_atr_pct:
            reasons.append("ATR% uygun değil")
        exposure = sum(p.value(prices.get(s, p.entry_price)) for s, p in portfolio.positions.items())
        if eq > 0 and exposure / eq * 100.0 >= r.max_total_exposure_pct:
            reasons.append("toplam maruziyet limiti")
        return (len(reasons) == 0, reasons)

    def position_size(self, equity, price, stop_price, constraints) -> tuple:
        r = self.cfg.risk
        if not equity or equity <= 0:
            return 0.0, "özkaynak yok"
        if not stop_price or price <= stop_price:
            return 0.0, "stop mesafesi geçersiz"
        risk_amount = equity * r.max_risk_per_trade_pct / 100.0
        qty = risk_amount / (price - stop_price)
        max_qty = (equity * r.max_position_value_pct / 100.0) / price
        qty = min(qty, max_qty)
        step = constraints.get("amount_step") or 1e-8
        qty = math.floor(qty / step) * step
        amin = constraints.get("amount_min") or 0.0
        if qty < amin:
            return 0.0, f"miktar min altında ({qty:.8f}<{amin})"
        cost = qty * price
        cmin = max(constraints.get("cost_min") or 0.0, r.min_order_quote_usdt)
        if cost < cmin:
            return 0.0, f"tutar min altı ({cost:.2f}<{cmin} USDT)"
        amax = constraints.get("amount_max")
        if amax and qty > amax:
            qty = float(amax)
        if qty <= 0:
            return 0.0, "miktar 0"
        return qty, "ok"

RISK = RiskManager(CONFIG, DB)
print("[HÜCRE 11] Risk yöneticisi hazır.")

# ============================================================
# HÜCRE 12/18 — KARAR MOTORU
# ============================================================

class DecisionEngine:
    def decide(self, symbol, snapshot: dict, signals: list, weights: dict,
               sentiment: dict, has_position: bool, regime: str = "unknown") -> Decision:
        if snapshot.get("fresh") is False:
            return Decision(ts=time.time(), symbol=symbol, action="WAIT_DATA",
                            score=0.0, confidence=0.0,
                            reasons=["veri bayat — bekleniyor"], regime=regime, data_ok=False)
        num = den = 0.0
        contrib = []
        for s in signals:
            w = weights.get(s.strategy, 0.0)
            if w <= 0 or s.direction == "flat":
                continue
            d = 1 if s.direction == "long" else -1
            c = w * d * s.strength
            num += c
            den += w
            contrib.append((c, s))
        score = num / den if den > 0 else 0.0
        sent = sentiment.get("sentiment") or 0.0
        cnt = sentiment.get("count") or 0
        if cnt >= 3:
            score = clamp(score + clamp(sent * 0.15, -0.1, 0.1), -1, 1)
        reasons = []
        contrib.sort(key=lambda x: -abs(x[0]))
        for c, s in contrib[:3]:
            reasons.append(f"{s.strategy}:{'AL' if s.direction == 'long' else 'SAT'} g={s.strength:.2f}")
        if cnt >= 3:
            reasons.append(f"haber={sent:+.2f}({cnt})")
        action = "HOLD"
        if regime == "volatile":
            reasons.append("REJİM: aşırı volatil — işlem yok")
        elif score >= CONFIG.buy_score_threshold and not has_position:
            action = "BUY"
            reasons.append(f"skor {score:.2f} ≥ {CONFIG.buy_score_threshold}")
        elif score <= CONFIG.sell_score_threshold and has_position:
            action = "SELL"
            reasons.append(f"skor {score:.2f} ≤ {CONFIG.sell_score_threshold}")
        elif score <= CONFIG.sell_score_threshold:
            reasons.append("satış sinyali ama pozisyon yok")
        return Decision(ts=time.time(), symbol=symbol, action=action, score=score,
                        confidence=abs(score), reasons=reasons, regime=regime, data_ok=True)

DECISION = DecisionEngine()
print("[HÜCRE 12] Karar motoru hazır.")

# ============================================================
# HÜCRE 13/18 — PORTFÖY YÖNETİCİSİ
# ============================================================

class Portfolio:
    def __init__(self, mode: str, start_balance: float = 0.0,
                 fee_rate: float = 0.001, slippage_bps: float = 5.0):
        self.mode = mode
        self.exchange = "paper" if mode != "live" else CONFIG.exchange_id
        self.cash = float(start_balance)
        self.start_balance = float(start_balance)
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.positions: Dict[str, Position] = {}
        self.realized = 0.0
        self.fees = 0.0

    def has(self, symbol) -> bool:
        return symbol in self.positions

    def open_position(self, symbol, amount, price, fee, sl, tp, strategy, ts) -> Position:
        self.cash -= amount * price + fee
        pos = Position(symbol=symbol, side="long", amount=amount, entry_price=price,
                       stop_loss=sl, take_profit=tp, opened_at=ts,
                       strategy=strategy, fees_paid=fee)
        self.positions[symbol] = pos
        return pos

    def close(self, symbol, price, exit_fee, reason, ts=None) -> TradeRecord:
        pos = self.positions.get(symbol)
        if not pos:
            return None
        gross = (price - pos.entry_price) * pos.amount
        total_fees = pos.fees_paid + exit_fee
        net = gross - total_fees
        self.cash += pos.amount * price - exit_fee
        self.realized += net
        self.fees += total_fees
        tr = TradeRecord(opened_at=pos.opened_at, closed_at=ts or time.time(),
                         mode=self.mode, exchange=self.exchange, symbol=symbol,
                         side=pos.side, amount=pos.amount, entry_price=pos.entry_price,
                         exit_price=price, gross_pnl=gross, fees=total_fees,
                         net_pnl=net, strategy=pos.strategy, close_reason=reason)
        self.positions.pop(symbol, None)
        return tr

    def equity(self, prices: dict) -> float:
        pv = sum(p.value(prices.get(s, p.entry_price)) for s, p in self.positions.items())
        return self.cash + pv

    def unrealized(self, prices: dict) -> float:
        return sum(p.unrealized_pnl(prices.get(s, p.entry_price))
                   for s, p in self.positions.items())

print("[HÜCRE 13] Portföy yöneticisi hazır.")

# ============================================================
# HÜCRE 14/18 — EMİR GERÇEKLEŞTİRME MOTORU (temel)
# ============================================================

def compute_sl_tp(risk_cfg: RiskLimits, price: float, atr_val) -> tuple:
    if risk_cfg.use_atr_stops and _ok(atr_val) and atr_val > 0:
        sl = price - risk_cfg.atr_stop_multiplier * atr_val
        tp = price + risk_cfg.atr_target_multiplier * atr_val
    else:
        sl = price * (1 - risk_cfg.default_stop_loss_pct / 100.0)
        tp = price * (1 + risk_cfg.default_take_profit_pct / 100.0)
    if not sl or sl >= price:
        sl = price * (1 - risk_cfg.default_stop_loss_pct / 100.0)
    if not tp or tp <= price:
        tp = price * (1 + risk_cfg.default_take_profit_pct / 100.0)
    return sl, tp

class ExecutionEngine:
    def __init__(self, adapter, portfolio: Portfolio, config: AppConfig, db: Database):
        self.adapter = adapter
        self.pf = portfolio
        self.cfg = config
        self.db = db

    def _await_fill(self, rec: OrderRecord, tries: int = 5, delay: float = 1.5) -> tuple:
        filled = rec.filled or 0.0
        avg = rec.avg_fill_price
        fee = rec.fee_cost or 0.0
        status = (rec.status or "").lower()
        for _ in range(tries):
            if filled > 0 and avg and status in ("closed", "filled"):
                break
            time.sleep(delay)
            try:
                o = self.adapter.fetch_order(rec.order_id, rec.symbol)
            except FeatureNotSupportedError:
                break
            except TradingSystemError as e:
                LOG.warning("emir sorgusu: %s", e)
                continue
            filled = safe_float(o.get("filled"), filled) or filled
            avg = safe_float(o.get("average"), avg) or avg
            fee = safe_float((o.get("fee") or {}).get("cost"), fee) or fee
            status = (o.get("status") or status or "").lower()
            if status in ("closed", "filled", "canceled", "rejected", "expired"):
                break
        return filled, avg, fee

    def market_buy(self, symbol, amount, ref_price, atr_val, strategy="combined") -> Position:
        pf = self.pf
        if pf.mode in ("paper", "backtest"):
            px = ref_price * (1 + pf.slippage_bps / 10000.0)
            fee = amount * px * pf.fee_rate
            if amount * px + fee > pf.cash:
                raise TradeBlockedError(f"bakiye yetersiz ({fmt_money(pf.cash)})")
            sl, tp = compute_sl_tp(self.cfg.risk, px, atr_val)
            pos = pf.open_position(symbol, amount, px, fee, sl, tp, strategy, time.time())
            self.db.upsert_order(OrderRecord(
                order_id=f"paper-{time.time():.3f}-{symbol.replace('/', '')}",
                ts=time.time(), mode=pf.mode, exchange=pf.exchange, symbol=symbol,
                side="buy", type="market", amount=amount, price=px,
                status=OrderStatus.FILLED.value, filled=amount, avg_fill_price=px,
                fee_cost=fee, strategy=strategy))
            return pos
        cons = self.adapter.order_constraints(symbol)
        min_cost = max(cons["cost_min"], self.cfg.risk.min_order_quote_usdt)
        if amount * ref_price < min_cost:
            raise TradeBlockedError(f"tutar min altı ({amount * ref_price:.2f}<{min_cost} USDT)")
        amount = self.adapter.round_amount(symbol, amount)
        rec = self.adapter.create_order(symbol, "buy", "market", amount, client_tag="bot")
        self.db.upsert_order(rec)
        filled, avg, fee = self._await_fill(rec)
        if not filled or not avg:
            raise TradeBlockedError(f"alış emri dolmadı (id={rec.order_id})")
        sl, tp = compute_sl_tp(self.cfg.risk, avg, atr_val)
        return pf.open_position(symbol, filled, avg, fee, sl, tp, strategy, time.time())

    def market_close(self, symbol, ref_price, reason) -> TradeRecord:
        pf = self.pf
        pos = pf.positions.get(symbol)
        if not pos:
            return None
        if pf.mode in ("paper", "backtest"):
            px = ref_price * (1 - pf.slippage_bps / 10000.0)
            fee = pos.amount * px * pf.fee_rate
            tr = pf.close(symbol, px, fee, reason, time.time())
            if tr:
                self.db.save_trade(tr)
            return tr
        rec = self.adapter.create_order(symbol, "sell", "market", pos.amount, client_tag="close")
        self.db.upsert_order(rec)
        filled, avg, fee = self._await_fill(rec)
        if not filled or not avg:
            raise TradeBlockedError(f"satış emri dolmadı (id={rec.order_id})")
        tr = pf.close(symbol, avg, fee or 0.0, reason, time.time())
        if tr:
            self.db.save_trade(tr)
        return tr

    def check_stops(self, symbol, price) -> TradeRecord:
        pos = self.pf.positions.get(symbol)
        if not pos:
            return None
        if pos.stop_loss and price <= pos.stop_loss:
            ref = price if self.pf.mode == "live" else pos.stop_loss
            return self.market_close(symbol, ref, "stop_loss")
        if pos.take_profit and price >= pos.take_profit:
            ref = price if self.pf.mode == "live" else pos.take_profit
            return self.market_close(symbol, ref, "take_profit")
        return None

print("[HÜCRE 14] Emir motoru hazır.")

# ============================================================
# HÜCRE 15/18 — BACKTEST MOTORU
# ============================================================

class Backtester:
    def run(self, symbol, timeframe=None, limit=800, df_override=None):
        tf = timeframe or CONFIG.primary_timeframe
        df = df_override if df_override is not None else ENGINE.get_ohlcv(symbol, tf, limit=limit, min_candles=210)
        pf = Portfolio("backtest", CONFIG.paper_start_balance,
                       CONFIG.paper_fee_rate, CONFIG.paper_slippage_bps)
        fee = CONFIG.paper_fee_rate
        slip = CONFIG.paper_slippage_bps / 10000.0
        trades = []
        eq = []
        LOG.info("Backtest başlıyor: %s %s (%d mum) — 1-2 dk sürebilir...", symbol, tf, len(df))
        try:
            cons = ADAPTER.order_constraints(symbol)
        except TradingSystemError:
            cons = {"amount_step": 1e-8, "amount_min": 0.0, "cost_min": 0.0, "amount_max": None}
        for i in range(210, len(df)):
            w = df.iloc[max(0, i - 400):i]
            signals, ind = STRATEGIES.compute_all(symbol, w)
            regime = REGIME.classify(w, ind)
            weights = REGIME.adjust_weights(CONFIG.strategy_weights, regime)
            dec = DECISION.decide(symbol, {"fresh": True}, signals, weights,
                                  {"sentiment": 0.0, "count": 0}, pf.has(symbol), regime)
            o = float(df["open"].iloc[i])
            h = float(df["high"].iloc[i])
            l = float(df["low"].iloc[i])
            c = float(df["close"].iloc[i])
            if pf.has(symbol):
                p = pf.positions[symbol]
                if l <= p.stop_loss:
                    trades.append(pf.close(symbol, p.stop_loss,
                                           p.amount * p.stop_loss * fee, "stop_loss"))
                elif h >= p.take_profit:
                    trades.append(pf.close(symbol, p.take_profit,
                                           p.amount * p.take_profit * fee, "take_profit"))
                elif dec.action == "SELL":
                    px = o * (1 - slip)
                    trades.append(pf.close(symbol, px,
                                           px * pf.positions[symbol].amount * fee, "sinyal"))
            elif dec.action == "BUY":
                sl, tp = compute_sl_tp(CONFIG.risk, o, ind.get("atr"))
                qty, _ = RISK.position_size(pf.equity({symbol: o}), o, sl, cons)
                if qty > 0:
                    px = o * (1 + slip)
                    f = qty * px * fee
                    if qty * px + f <= pf.cash:
                        pf.open_position(symbol, qty, px, f, sl, tp, "combined",
                                         float(df["ts"].iloc[i].timestamp()))
            eq.append({"ts": df["ts"].iloc[i], "equity": pf.equity({symbol: c})})
        if pf.has(symbol):
            c_last = float(df["close"].iloc[-1])
            trades.append(pf.close(symbol, c_last,
                                   c_last * pf.positions[symbol].amount * fee, "backtest_son"))
        eq_df = pd.DataFrame(eq)
        tr_df = pd.DataFrame([t.__dict__ for t in trades if t])
        return self._stats(tr_df, eq_df, pf), eq_df, tr_df

    def _stats(self, tr_df, eq_df, pf) -> dict:
        final = float(eq_df["equity"].iloc[-1]) if not eq_df.empty else pf.cash
        out = {"başlangıç": pf.start_balance, "bitiş": final,
               "getiri_%": (final / pf.start_balance - 1) * 100.0 if pf.start_balance else 0.0,
               "işlem": 0, "kazanan": 0, "kaybeden": 0, "win_rate_%": 0.0,
               "profit_factor": 0.0, "max_dd_%": 0.0}
        if tr_df is None or tr_df.empty:
            return out
        wins = tr_df[tr_df["net_pnl"] > 0]
        losses = tr_df[tr_df["net_pnl"] <= 0]
        gp = float(wins["net_pnl"].sum())
        gl = float(abs(losses["net_pnl"].sum()))
        out.update({"işlem": int(len(tr_df)), "kazanan": int(len(wins)),
                    "kaybeden": int(len(losses)),
                    "win_rate_%": len(wins) / len(tr_df) * 100.0,
                    "profit_factor": (gp / gl) if gl > 0 else float("inf")})
        e = eq_df["equity"]
        if not e.empty and float(e.cummax().max()) > 0:
            dd = (e - e.cummax()) / e.cummax() * 100.0
            out["max_dd_%"] = float(dd.min())
        return out

print("[HÜCRE 15] Backtest motoru hazır.")

# ============================================================
# HÜCRE 16/18 — RAPORLAMA & GRAFİK
# ============================================================

def print_report(title, stats: dict):
    print("\n" + "=" * 52)
    print(f"  {title}")
    print("=" * 52)
    for k, v in stats.items():
        print(f"  {k:16s}: {v:,.2f}" if isinstance(v, float) else f"  {k:16s}: {v}")
    print("=" * 52)

def plot_equity_curve(eq_df: pd.DataFrame, title="Equity Eğrisi"):
    try:
        import plotly.graph_objects as go
        if eq_df is None or eq_df.empty:
            print("[GRAFİK] Veri yok.")
            return
        fig = go.Figure(go.Scatter(x=eq_df["ts"], y=eq_df["equity"], mode="lines",
                                   line=dict(color="#00cc96")))
        fig.update_layout(title=title, xaxis_title="Zaman", yaxis_title="Equity (USDT)",
                          template="plotly_dark", height=420)
        fig.show()
    except Exception as e:
        LOG.warning("Grafik atlandı: %s", e)

print("[HÜCRE 16] Raporlama hazır.")

# ============================================================
# HÜCRE 17/18 — TRADING BOT ORKESTRATÖRÜ (temel)
# ============================================================

class TradingBot:
    def __init__(self, config: AppConfig, portfolio: Portfolio, execution: ExecutionEngine):
        self.cfg = config
        self.pf = portfolio
        self.execution = execution
        self.stop_flag = False
        self.cycle = 0

    def start(self):
        LOG.info("=" * 60)
        LOG.info("BOT BAŞLIYOR | mod=%s | semboller=%s | tf=%s/%s",
                 self.cfg.mode.value, self.cfg.symbols,
                 self.cfg.primary_timeframe, self.cfg.higher_timeframe)
        LOG.info("Eşikler: AL≥%.2f SAT≤%.2f | risk/işlem=%.1f%% | günlük max zarar=%.1f%%",
                 self.cfg.buy_score_threshold, self.cfg.sell_score_threshold,
                 self.cfg.risk.max_risk_per_trade_pct, self.cfg.risk.max_daily_loss_pct)
        self.stop_flag = False
        if self.pf.mode == "live":
            self._reconcile()
        NEWS.refresh(force=True)
        env_max = os.environ.get("MAX_RUNTIME_MINUTES")
        deadline = time.time() + float(env_max) * 60.0 if env_max else None
        try:
            while not self.stop_flag:
                self.cycle += 1
                t0 = time.time()
                self._cycle()
                if (self.pf.mode == "live" and self.cfg.reconcile_every_n_cycles
                        and self.cycle % self.cfg.reconcile_every_n_cycles == 0):
                    self._reconcile()
                if deadline and time.time() >= deadline:
                    LOG.info("MAX_RUNTIME_MINUTES doldu — durduruluyor.")
                    break
                sleep_until = t0 + max(5, self.cfg.poll_seconds)
                while time.time() < sleep_until and not self.stop_flag:
                    time.sleep(1)
        except KeyboardInterrupt:
            LOG.warning("CTRL+C algılandı — kapanılıyor...")
        finally:
            self._shutdown()

    def stop(self):
        self.stop_flag = True

    def _cycle(self):
        NEWS.refresh()
        prices = {}
        for symbol in self.cfg.symbols:
            try:
                self._process_symbol(symbol, prices)
            except TradingSystemError as e:
                LOG.warning("[%s] döngü atlandı: %s", symbol, e.user_message or e)
                DB.save_error("cycle", f"{symbol}: {e}")
            except Exception as e:
                log_exception(DB, f"cycle:{symbol}", e)
        eq = self.pf.equity(prices)
        RISK.update_day_anchor(eq)
        try:
            DB.save_equity_point(self.pf.mode, self.pf.cash, eq - self.pf.cash, eq,
                                 self.pf.unrealized(prices), self.pf.realized, self.pf.fees)
        except Exception:
            pass
        LOG.info("DÖNGÜ %d | equity=%s | açık pozisyon=%d | realized=%s",
                 self.cycle, fmt_money(eq), len(self.pf.positions), fmt_money(self.pf.realized))

    def _process_symbol(self, symbol, prices):
        snap = ENGINE.snapshot(symbol)
        prices[symbol] = snap["last"]
        df = ENGINE.get_ohlcv(symbol, self.cfg.primary_timeframe, min_candles=210)
        try:
            dfh = ENGINE.get_ohlcv(symbol, self.cfg.higher_timeframe, min_candles=210)
        except TradingSystemError:
            dfh = None
        signals, ind = STRATEGIES.compute_all(symbol, df, dfh)
        for s in signals:
            try:
                DB.save_signal(s)
            except Exception:
                pass
        regime = REGIME.classify(df, ind)
        weights = REGIME.adjust_weights(self.cfg.strategy_weights, regime)
        sent = NEWS.sentiment_for(symbol)
        if self.pf.has(symbol):
            tr = self.execution.check_stops(symbol, snap["last"])
            if tr:
                LOG.info("[%s] POZİSYON KAPANDI (%s) net=%s",
                         symbol, tr.close_reason, fmt_money(tr.net_pnl))
                if tr.close_reason == "stop_loss":
                    RISK.register_stop_hit(symbol)
        dec = DECISION.decide(symbol, snap, signals, weights, sent,
                              self.pf.has(symbol), regime)
        try:
            DB.save_decision(dec)
        except Exception:
            pass
        LOG.info("[%s] %s skor=%+.2f rejim=%s haber=%+.2f(%d) | %s",
                 symbol, dec.action, dec.score, regime,
                 sent.get("sentiment", 0.0), sent.get("count", 0),
                 "; ".join(dec.reasons[:3]))
        if dec.action == "BUY" and not self.pf.has(symbol):
            ok, why = RISK.pre_trade_checks(symbol, snap, ind, self.pf, prices)
            if not ok:
                LOG.info("[%s] ALIŞ engellendi: %s", symbol, ", ".join(why))
                return
            try:
                equity = self.pf.equity(prices)
                px = snap.get("ask") or snap["last"]
                sl, _ = compute_sl_tp(self.cfg.risk, snap["last"], ind.get("atr"))
                cons = ADAPTER.order_constraints(symbol)
                qty, why = RISK.position_size(equity, px, sl, cons)
                if qty <= 0:
                    LOG.info("[%s] boyut 0: %s", symbol, why)
                    return
                pos = self.execution.market_buy(symbol, qty, px, ind.get("atr"))
                if pos:
                    LOG.info("[%s] >>> AÇILDI qty=%.8f @%s | SL=%s TP=%s",
                             symbol, pos.amount, fmt_money(pos.entry_price),
                             fmt_money(pos.stop_loss), fmt_money(pos.take_profit))
            except TradingSystemError as e:
                LOG.warning("[%s] alış başarısız: %s", symbol, e.user_message or e)
        elif dec.action == "SELL" and self.pf.has(symbol):
            try:
                tr = self.execution.market_close(symbol, snap["last"], "sinyal")
                if tr:
                    LOG.info("[%s] <<< SİNYALLE KAPANDI net=%s", symbol, fmt_money(tr.net_pnl))
            except TradingSystemError as e:
                LOG.warning("[%s] satış başarısız: %s", symbol, e.user_message or e)

    def _reconcile(self):
        try:
            bal = ADAPTER.fetch_balance()
            usdt = safe_float((bal.get("total") or {}).get("USDT"), 0.0) or 0.0
        except TradingSystemError as e:
            LOG.warning("[RECONCILE] bakiye okunamadı: %s", e)
            return
        self.pf.cash = usdt
        LOG.info("[RECONCILE] USDT bakiye=%s", fmt_money(usdt))
        since = int((time.time() - 7 * 86400) * 1000)
        for symbol in self.cfg.symbols:
            try:
                fills = ADAPTER.fetch_my_trades(symbol, since)
            except TradingSystemError as e:
                LOG.warning("[RECONCILE] %s geçmiş okunamadı: %s", symbol, e)
                continue
            net = cost = 0.0
            for f in fills or []:
                amt = safe_float(f.get("amount"), 0.0) or 0.0
                px = safe_float(f.get("price"), 0.0) or 0.0
                if (f.get("side") or "").lower() == "buy":
                    net += amt
                    cost += amt * px
                else:
                    net -= amt
                    cost -= amt * px
            if net > 1e-12:
                avg = cost / net
                cur = self.pf.positions.get(symbol)
                if cur is None or abs(cur.amount - net) > max(1e-12, net * 0.01):
                    LOG.info("[RECONCILE] %s senkron: %.8f @ %.2f", symbol, net, avg)
                    self.pf.positions[symbol] = Position(symbol=symbol, side="long",
                                                         amount=net, entry_price=avg,
                                                         opened_at=time.time())
            elif self.pf.has(symbol):
                LOG.info("[RECONCILE] %s borsada pozisyon yok — bot kaydı temizlendi.", symbol)
                self.pf.positions.pop(symbol, None)

    def _save_paper_state(self):
        if self.pf.mode != "paper":
            return
        data = {"cash": self.pf.cash, "start": self.pf.start_balance,
                "positions": {s: {"amount": p.amount, "entry": p.entry_price,
                                  "sl": p.stop_loss, "tp": p.take_profit,
                                  "opened_at": p.opened_at, "strategy": p.strategy}
                              for s, p in self.pf.positions.items()}}
        try:
            DB.set_meta("paper_state", json.dumps(data))
        except Exception as e:
            LOG.warning("[PAPER] durum kaydedilemedi: %s", e)

    def _load_paper_state(self):
        if self.pf.mode != "paper":
            return
        v = DB.get_meta("paper_state")
        if not v:
            return
        try:
            data = json.loads(v)
            self.pf.cash = float(data.get("cash", self.pf.cash))
            self.pf.start_balance = float(data.get("start", self.pf.start_balance))
            for s, p in data.get("positions", {}).items():
                self.pf.positions[s] = Position(
                    symbol=s, side="long", amount=float(p["amount"]),
                    entry_price=float(p["entry"]),
                    stop_loss=safe_float(p.get("sl")),
                    take_profit=safe_float(p.get("tp")),
                    opened_at=float(p.get("opened_at", 0)),
                    strategy=str(p.get("strategy", "")))
            LOG.info("[PAPER] Önceki oturum yüklendi: cash=%s pozisyon=%d",
                     fmt_money(self.pf.cash), len(self.pf.positions))
        except Exception as e:
            LOG.warning("[PAPER] durum yüklenemedi: %s", e)

    def _shutdown(self):
        self._save_paper_state()
        if self.cfg.close_positions_on_stop and self.pf.positions:
            LOG.info("Kapanışta pozisyonlar kapatılıyor...")
            for symbol in list(self.pf.positions):
                try:
                    px = ENGINE.get_ticker(symbol).last
                    tr = self.execution.market_close(symbol, px, "kapanis")
                    if tr:
                        LOG.info("  %s kapatıldı net=%s", symbol, fmt_money(tr.net_pnl))
                except TradingSystemError as e:
                    LOG.warning("  %s kapatılamadı: %s", symbol, e)
        prices = {}
        for s in list(self.pf.positions):
            try:
                prices[s] = ENGINE.get_ticker(s).last
            except Exception:
                pass
        LOG.info("BOT DURDU | equity=%s | realized=%s | fees=%s",
                 fmt_money(self.pf.equity(prices)), fmt_money(self.pf.realized),
                 fmt_money(self.pf.fees))

print("[HÜCRE 17] Orkestratör hazır.")

# ============================================================
# HÜCRE 18/18 — START (tek ve nihai)
# ============================================================

def _select_mode() -> TradingMode:
    env = (os.environ.get("TRADING_MODE") or "").strip().lower()
    if env in ("backtest", "paper", "live"):
        return TradingMode(env)
    try:
        if sys.stdin.isatty():
            v = input("\nMod seç — [1] CANLI (gerçek para) [2] PAPER (sanal) [3] BACKTEST (Enter=2): ").strip()
            return {"1": TradingMode.LIVE, "3": TradingMode.BACKTEST}.get(v, TradingMode.PAPER)
    except Exception:
        pass
    print("[INFO] Etkileşimsiz ortam — PAPER mod. (Canlı için TRADING_MODE=live)")
    return TradingMode.PAPER

def _confirm_live() -> bool:
    if not sys.stdin.isatty():
        return True
    try:
        v = input("!!! GERÇEK PARA MODU !!! Onay için EVET yazın: ").strip().upper()
        return v == "EVET"
    except Exception:
        return False

def main():
    print("=" * 60)
    print("   MEXC OTOMATİK ALIM-SATIM BOTU — START")
    print("=" * 60)
    mode = _select_mode()
    CONFIG.mode = mode
    print(f"Seçilen mod: {mode.value.upper()}")

    try:
        mkts = ADAPTER.markets()
        gecerli = [s for s in CONFIG.symbols
                   if s in mkts and mkts[s].get("active", True)]
        cikan = [s for s in CONFIG.symbols if s not in gecerli]
        if cikan:
            print(f"[UYARI] MEXC'te yok/pasif, listeden çıkarıldı: {cikan}")
        if gecerli:
            CONFIG.symbols = gecerli
    except TradingSystemError:
        pass
    print(f"Aktif semboller ({len(CONFIG.symbols)}): {CONFIG.symbols}")

    if mode == TradingMode.BACKTEST:
        stats, eq_df, _ = Backtester().run("BTC/USDT")
        print_report("BACKTEST RAPORU — BTC/USDT", stats)
        plot_equity_curve(eq_df, "BTC/USDT Backtest Equity")
        return

    creds = request_credentials()
    if mode == TradingMode.LIVE and not creds.api_key:
        print("[DURDUR] CANLI mod için MEXC_API_KEY / MEXC_SECRET gerekli.")
        return
    if creds.api_key:
        ADAPTER.attach_credentials(creds)

    start_balance = CONFIG.paper_start_balance
    if creds.api_key:
        try:
            usdt = safe_float((ADAPTER.fetch_balance().get("total") or {}).get("USDT"), 0.0) or 0.0
            print(f"MEXC USDT bakiyesi: {fmt_money(usdt)}")
            if mode == TradingMode.LIVE:
                if usdt <= 0:
                    print("[DURDUR] USDT bakiye 0 — canlı işlem başlatılmadı.")
                    return
                start_balance = usdt
        except TradingSystemError as e:
            print(f"[UYARI] Bakiye doğrulanamadı: {e.user_message or e}")
            if mode == TradingMode.LIVE:
                print("[DURDUR] API doğrulanamadı — canlı işlem başlatılmadı.")
                return

    if mode == TradingMode.LIVE and not _confirm_live():
        print("İptal edildi.")
        return

    portfolio = Portfolio(mode.value, start_balance,
                          CONFIG.paper_fee_rate, CONFIG.paper_slippage_bps)
    execution = ExecutionEngine(ADAPTER, portfolio, CONFIG, DB)
    bot = TradingBot(CONFIG, portfolio, execution)
    print("\n▶ BOT ÇALIŞIYOR — durdurmak için CTRL+C (Colab'da durdur butonu)\n")
    bot.start()

# ============================================================
# BÖLÜM 19 — GÜNCELLEME KATMANI (ML + Telegram + Makro + V2)
# ============================================================
print("[BÖLÜM 19] Güncelleme katmanı yükleniyor...")

_SKLEARN_OK = True
try:
    import joblib
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.dummy import DummyClassifier
    from sklearn.metrics import brier_score_loss
except Exception as _e:
    _SKLEARN_OK = False
    print(f"[ML] scikit-learn yok ({_e}) — kural tabanlı mod aynen çalışır.")

@dataclass
class RiskLimitsV2(RiskLimits):
    use_trailing_stop: bool = True
    trailing_activate_rr: float = 1.0
    trailing_atr_mult: float = 2.0
    use_break_even: bool = True
    break_even_trigger_rr: float = 1.0
    break_even_buffer_pct: float = 0.10
    min_reward_risk: float = 1.2
    stale_order_minutes: int = 30
    abnormal_candle_pct: float = 8.0
    abnormal_candle_cooldown_min: int = 30

_risk_old = CONFIG.risk
CONFIG.risk = RiskLimitsV2()
for _f in RiskLimits.__dataclass_fields__:
    setattr(CONFIG.risk, _f, getattr(_risk_old, _f))

@dataclass
class MLConfig:
    enabled: bool = True
    min_trades: int = 60
    retrain_min_new: int = 20
    shadow_min_trades: int = 15
    filter_threshold: float = 0.55
    model_dir: str = "models"

CONFIG.ml = MLConfig()
if (os.environ.get("ML_ENABLED") or "").strip().lower() in ("0", "false", "no"):
    CONFIG.ml.enabled = False
if os.environ.get("ML_MIN_TRADES"):
    try:
        CONFIG.ml.min_trades = int(os.environ["ML_MIN_TRADES"])
    except ValueError:
        pass

CONFIG.excluded_symbols = [s.strip() for s in (os.environ.get("EXCLUDED_SYMBOLS") or "").split(",") if s.strip()]
CONFIG.require_1h_align = (os.environ.get("REQUIRE_1H_ALIGN", "1").strip() not in ("0", "false", "no"))
print("[BÖLÜM 19] Konfig uzantıları hazır.")

# ============================================================
# BÖLÜM 20 — TELEGRAM (opsiyonel; sekretler ASLA loglanmaz)
# ============================================================
try:
    import requests as _requests
except Exception:
    _requests = None

class TelegramNotifier:
    def __init__(self, cfg):
        self.token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self.chat  = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        self.enabled = bool(_requests and self.token and self.chat)
        self._min_gap = 15.0
        self._last = {}
        if not self.enabled:
            print("[TG] Kapalı (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID yok).")

    def send(self, text, category="info", force=False):
        if not self.enabled:
            return
        now = time.time()
        if not force and now - self._last.get(category, 0.0) < self._min_gap:
            return
        self._last[category] = now
        try:
            _requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                           json={"chat_id": self.chat, "text": str(text)[:3500]}, timeout=10)
        except Exception as e:
            LOG.debug("Telegram gönderilemedi: %s", e)

TG = TelegramNotifier(CONFIG)
print("[BÖLÜM 20] Telegram hazır.")

# ============================================================
# BÖLÜM 21 — DB MIGRATION (mevcut veriler korunur)
# ============================================================
def migrate_db(db: Database):
    with db._lock:
        db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_journal(
          trade_uid TEXT UNIQUE, mode TEXT, exchange TEXT, symbol TEXT, market_type TEXT,
          strategy TEXT, strategy_version TEXT,
          model_version TEXT, shadow_model_version TEXT, model_prob REAL, shadow_prob REAL,
          buy_order_id TEXT, sell_order_id TEXT,
          side TEXT, amount REAL, entry_price REAL, exit_price REAL,
          sl_initial REAL, sl_final REAL, tp REAL,
          entry_ts REAL, exit_ts REAL, duration_s REAL,
          exit_reason TEXT, gross_pnl REAL, fees REAL, net_pnl REAL,
          slippage_bps REAL, funding_cost REAL DEFAULT 0,
          features TEXT, regime TEXT, confidence REAL, sentiment REAL,
          equity_before REAL, equity_after REAL, simulated INTEGER, created_at REAL);
        CREATE INDEX IF NOT EXISTS idx_tj_symbol ON trade_journal(symbol, exit_ts);
        CREATE INDEX IF NOT EXISTS idx_tj_mode ON trade_journal(mode, exit_ts);
        CREATE TABLE IF NOT EXISTS ml_models(
          version TEXT PRIMARY KEY, trained_at REAL, n_trades INTEGER,
          metrics TEXT, features TEXT, status TEXT);
        CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(mode, closed_at);
        """)
        db.conn.commit()

migrate_db(DB)
LOG.info("[BÖLÜM 21] DB migration tamam — mevcut veriler korundu.")

def last_order_id(db, symbol):
    r = db._exec("SELECT order_id FROM orders WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                 (symbol,)).fetchone()
    return r[0] if r else None

# ============================================================
# BÖLÜM 22 — ML FİLTRE (yalnızca karar anı bilgisi; gelecek verisi YOK)
# ============================================================
REGIME_CODES = {"unknown": 0, "range": 1, "trend_up": 2, "trend_down": 3, "quiet": 4, "volatile": 5}
FEATURES = ["rsi", "macd_hist_norm", "atr_pct", "vol_ratio", "adx",
            "ema_gap_9_21", "price_vs_ema50", "price_vs_ema200", "bb_pb",
            "spread_pct", "sentiment", "news_count", "pct_change_24h",
            "regime_code", "hour_utc"]

def build_features(snapshot, ind, sentiment, regime):
    price = safe_float(snapshot.get("last"), np.nan)

    def ratio(a, b):
        a, b = safe_float(a), safe_float(b)
        return (a / b - 1.0) if (a is not None and b not in (None, 0)
                                 and math.isfinite(a) and math.isfinite(b)) else np.nan
    mh, a = safe_float(ind.get("macd_hist"), np.nan), safe_float(ind.get("atr"), np.nan)
    return {"rsi": safe_float(ind.get("rsi"), np.nan),
            "macd_hist_norm": (mh / a) if (mh is not None and a not in (None, 0)
                                           and math.isfinite(mh) and math.isfinite(a)) else np.nan,
            "atr_pct": safe_float(ind.get("atr_pct"), np.nan),
            "vol_ratio": safe_float(ind.get("vol_ratio"), np.nan),
            "adx": safe_float(ind.get("adx"), np.nan),
            "ema_gap_9_21": ratio(ind.get("ema9"), ind.get("ema21")),
            "price_vs_ema50": ratio(price, ind.get("ema50")),
            "price_vs_ema200": ratio(price, ind.get("ema200")),
            "bb_pb": safe_float(ind.get("bb_pb"), np.nan),
            "spread_pct": safe_float(snapshot.get("spread_pct"), np.nan),
            "sentiment": safe_float(sentiment.get("sentiment"), 0.0),
            "news_count": float(sentiment.get("count") or 0),
            "pct_change_24h": safe_float(snapshot.get("pct_change_24h"), np.nan),
            "regime_code": REGIME_CODES.get(regime, 0),
            "hour_utc": float(utc_now().hour)}

class MLManager:
    def __init__(self, config, db):
        self.cfg = config
        self.db = db
        self.model = None
        self.version = None
        self.candidate = None
        self.candidate_version = None
        self.features = list(FEATURES)
        try:
            os.makedirs(config.ml.model_dir, exist_ok=True)
        except OSError:
            pass
        self._load_approved()
        self._load_candidate()

    def _path(self, v):
        return os.path.join(self.cfg.ml.model_dir, f"model_{v}.joblib")

    def _load_approved(self):
        if not _SKLEARN_OK:
            return
        row = self.db._exec("SELECT version FROM ml_models WHERE status='approved' "
                            "ORDER BY trained_at DESC LIMIT 1").fetchone()
        if not row:
            LOG.info("[ML] Onaylı model yok — kural tabanlı mod aktif.")
            return
        p = self._path(row[0])
        if not os.path.exists(p):
            LOG.warning("[ML] Model dosyası eksik (%s) — kural tabanlı mod aktif.", p)
            return
        try:
            b = joblib.load(p)
            self.model = b["model"]
            self.version = row[0]
            self.features = b.get("features", self.features)
            LOG.info("[ML] Onaylı model aktif: %s", row[0])
        except Exception as e:
            LOG.warning("[ML] Model yüklenemedi: %s — kural tabanlı mod aktif.", e)

    def _load_candidate(self):
        if not _SKLEARN_OK:
            return
        row = self.db._exec("SELECT version FROM ml_models WHERE status='shadow' "
                            "ORDER BY trained_at DESC LIMIT 1").fetchone()
        if not row:
            return
        p = self._path(row[0])
        if not os.path.exists(p):
            return
        try:
            b = joblib.load(p)
            self.candidate = b["model"]
            self.candidate_version = row[0]
            LOG.info("[ML] Shadow aday yüklendi: %s", row[0])
        except Exception as e:
            LOG.warning("[ML] Aday yüklenemedi: %s", e)

    def active(self):
        return bool(_SKLEARN_OK and self.cfg.ml.enabled and self.model is not None)

    def proba_from_features(self, model, f):
        if model is None:
            return None
        try:
            return float(model.predict_proba(pd.DataFrame([f])[self.features])[0, 1])
        except Exception as e:
            LOG.debug("[ML] tahmin hatası: %s", e)
            return None

    def predict_proba(self, symbol, snapshot, ind, regime="unknown"):
        return self.proba_from_features(
            self.model, build_features(snapshot, ind, NEWS.sentiment_for(symbol), regime))

    def dataset(self):
        try:
            df = pd.read_sql_query("SELECT features, net_pnl FROM trade_journal ORDER BY exit_ts",
                                   self.db.conn)
        except Exception:
            return None, None, None
        rows, labels, pnl = [], [], []
        for _, r in df.iterrows():
            try:
                f = json.loads(r["features"] or "{}")
            except Exception:
                continue
            rows.append(f)
            labels.append(1 if safe_float(r["net_pnl"], 0.0) > 0 else 0)
            pnl.append(safe_float(r["net_pnl"], 0.0))
        if not rows:
            return None, None, None
        X = pd.DataFrame(rows)
        for c in self.features:
            if c not in X.columns:
                X[c] = np.nan
        return X[self.features].reset_index(drop=True), np.array(labels), np.array(pnl)

    def _new_since_last(self):
        try:
            total = self.db._exec("SELECT COUNT(*) FROM trade_journal").fetchone()[0]
        except Exception:
            return 0
        return max(0, int(total) - int(self.db.get_meta("ml_last_train_count", "0") or 0))

    def startup(self):
        if not _SKLEARN_OK or not self.cfg.ml.enabled:
            return
        try:
            self._evaluate_shadow()
            self._train_if_ready()
        except Exception as e:
            LOG.warning("[ML] startup hatası: %s — kural tabanlı mod aynen devam ediyor.", e)
            TG.send(f"[ALARM] ML startup hatası: {e}", "ml", force=True)

    def _precision_at(self, model, X, y, thr=None):
        if X is None or len(y) == 0:
            return None
        try:
            p = model.predict_proba(X)[:, 1]
        except Exception:
            return None
        mask = p >= (thr or self.cfg.ml.filter_threshold)
        if int(mask.sum()) < 5:
            return None
        return float((np.asarray(y)[mask] == 1).mean())

    def _filtered_expectancy(self, model, X, pnl, thr=None):
        try:
            p = model.predict_proba(X)[:, 1]
        except Exception:
            return None
        mask = p >= (thr or self.cfg.ml.filter_threshold)
        if int(mask.sum()) < 5:
            return None
        return float(np.asarray(pnl)[mask].mean())

    def _walk_forward_precision(self, X, y, folds=3):
        n = len(y)
        res = []
        fold = n // (folds + 1)
        for k in range(1, folds + 1):
            tr_end, te_end = fold * k, min(n, fold * k + fold)
            if tr_end < 30 or te_end - tr_end < 10:
                continue
            m = HistGradientBoostingClassifier(max_depth=3, max_iter=100, random_state=42)
            try:
                m.fit(X.iloc[:tr_end], y[:tr_end])
            except Exception:
                continue
            p = self._precision_at(m, X.iloc[tr_end:te_end], y[tr_end:te_end])
            if p is not None:
                res.append(p)
        return float(np.mean(res)) if res else None

    def _train_if_ready(self):
        X, y, pnl = self.dataset()
        if X is None or len(y) < self.cfg.ml.min_trades:
            LOG.info("[ML] Veri yetersiz (%s/%s işlem) — eğitim atlandı.",
                     0 if X is None else len(y), self.cfg.ml.min_trades)
            return
        if self.model is not None and self._new_since_last() < self.cfg.ml.retrain_min_new:
            LOG.info("[ML] Yeni işlem az (%d<%d) — eğitim atlandı.",
                     self._new_since_last(), self.cfg.ml.retrain_min_new)
            return
        n = len(y)
        i1, i2 = int(n * 0.6), int(n * 0.8)
        if len(np.unique(y[:i1])) < 2:
            LOG.info("[ML] Eğitim seti tek sınıflı — atlandı.")
            return
        model = HistGradientBoostingClassifier(max_depth=3, max_iter=150, learning_rate=0.06,
                                               min_samples_leaf=10, random_state=42)
        try:
            model.fit(X.iloc[:i1], y[:i1])
        except Exception as e:
            LOG.warning("[ML] fit hatası: %s", e)
            return
        base = DummyClassifier(strategy="prior")
        base.fit(X.iloc[:i1], y[:i1])
        m = {"n_trades": int(n), "split": [i1, i2 - i1, n - i2],
             "baseline_precision": self._precision_at(base, X.iloc[i1:i2], y[i1:i2]),
             "precision": self._precision_at(model, X.iloc[i1:i2], y[i1:i2])}
        if (n - i2) >= 10:
            m["precision_test"] = self._precision_at(model, X.iloc[i2:], y[i2:])
            if len(np.unique(y[i2:])) == 2:
                try:
                    m["brier_test"] = float(brier_score_loss(y[i2:], model.predict_proba(X.iloc[i2:])[:, 1]))
                except Exception:
                    pass
            m["expectancy_test"] = self._filtered_expectancy(model, X.iloc[i2:], pnl[i2:])
        m["walk_forward_precision"] = self._walk_forward_precision(X, y)
        pretty = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items() if v is not None}
        LOG.info("[ML] Aday değerlendirme: %s", json.dumps(pretty))
        self._register_candidate(model, m)

    def _register_candidate(self, model, metrics):
        version = time.strftime("%Y%m%d%H%M%S")
        try:
            joblib.dump({"model": model, "features": self.features}, self._path(version))
        except Exception as e:
            LOG.warning("[ML] model kaydedilemedi: %s", e)
            return
        cur = self.db._exec("SELECT version, metrics FROM ml_models WHERE status='approved' "
                            "ORDER BY trained_at DESC LIMIT 1").fetchone()
        if cur is None:
            status = "approved"
        else:
            try:
                cm = json.loads(cur[1] or "{}")
            except Exception:
                cm = {}
            new_exp = metrics.get("expectancy_test")
            cur_exp = cm.get("expectancy_test")
            better = ((metrics.get("precision") or 0) >= (cm.get("precision") or 0)) and \
                     ((new_exp if new_exp is not None else -1e9) >= (cur_exp if cur_exp is not None else -1e9))
            status = "shadow" if better else "rejected"
            if status == "rejected":
                LOG.info("[ML] Aday mevcut modelden zayıf — OTOMATİK PROMOSYON YOK.")
        self.db._exec("INSERT OR IGNORE INTO ml_models(version,trained_at,n_trades,metrics,features,status) "
                      "VALUES(?,?,?,?,?,?)",
                      (version, time.time(), metrics.get("n_trades"),
                       json.dumps(metrics), json.dumps(self.features), status))
        self.db.set_meta("ml_last_train_count",
                         str(self.db._exec("SELECT COUNT(*) FROM trade_journal").fetchone()[0]))
        if status == "approved":
            self._load_approved()
        elif status == "shadow":
            self._load_candidate()
        TG.send(f"[ML] Aday {version} → {status} | precision={metrics.get('precision')}",
                "ml", force=True)

    def _evaluate_shadow(self):
        row = self.db._exec("SELECT version FROM ml_models WHERE status='shadow' "
                            "ORDER BY trained_at DESC LIMIT 1").fetchone()
        if not row:
            return
        ver = row[0]
        df = pd.read_sql_query("SELECT shadow_prob, net_pnl FROM trade_journal "
                               "WHERE shadow_model_version=? ORDER BY exit_ts",
                               self.db.conn, params=(ver,))
        if len(df) < self.cfg.ml.shadow_min_trades:
            LOG.info("[ML] Shadow %s: %d/%d işlem — bekliyor.", ver, len(df), self.cfg.ml.shadow_min_trades)
            return
        mask = df["shadow_prob"].fillna(0.0) >= self.cfg.ml.filter_threshold
        if int(mask.sum()) < 5:
            self.db._exec("UPDATE ml_models SET status='rejected' WHERE version=?", (ver,))
            return
        fe = float(df[mask]["net_pnl"].mean())
        oe = float(df["net_pnl"].mean())
        fp = float((df[mask]["net_pnl"] > 0).mean())
        op = float((df["net_pnl"] > 0).mean())
        if fe > oe and fp > op:
            self.db._exec("UPDATE ml_models SET status='retired' WHERE status='approved'")
            self.db._exec("UPDATE ml_models SET status='approved' WHERE version=?", (ver,))
            self._load_approved()
            self.candidate = None
            self.candidate_version = None
            TG.send(f"[ML] Shadow {ver} ONAYLANDI (exp {fe:.2f}>{oe:.2f})", "ml", force=True)
        else:
            self.db._exec("UPDATE ml_models SET status='rejected' WHERE version=?", (ver,))
            LOG.info("[ML] Shadow %s rejected (fe=%.2f oe=%.2f)", ver, fe, oe)

# ============================================================
# BÖLÜM 23 — BALANCE MANAGER + KARAR V2 + RİSK V2
# ============================================================
class BalanceManager:
    """Gerçek MEXC SPOT bakiyesi. Doğrulanamıyorsa UNKNOWN — asla 0 göstermez.
    Futures bakiyeleri bilinçli olarak SORGULANMAZ (spot config) — raporda belirtilir."""
    def __init__(self, adapter, config):
        self.adapter = adapter
        self.cfg = config
        self.status = "UNKNOWN"
        self.total = None
        self.free = None
        self.used = None
        self.last_sync = 0.0

    def sync(self):
        try:
            b = self.adapter.fetch_balance()
            self.total = safe_float((b.get("total") or {}).get("USDT"))
            self.free  = safe_float((b.get("free") or {}).get("USDT"))
            self.used  = safe_float((b.get("used") or {}).get("USDT"))
            self.status = "OK" if self.total is not None else "UNKNOWN"
            self.last_sync = time.time()
            TG.send(f"[BALANCE] MEXC spot senkron OK — toplam {fmt_money(self.total)} USDT", "balance")
        except TradingSystemError as e:
            self.status = "UNKNOWN"
            LOG.warning("[BALANCE] doğrulanamadı → UNKNOWN: %s", e)
            TG.send(f"[ALARM] Bakiye senkronu başarısız: {e}", "api", force=True)
        return self.status

    def report(self, portfolio, prices):
        dd = RISK.daily_loss_pct(portfolio.equity(prices))
        ok = self.status == "OK"
        lines = [
            "📊 PORTFÖY RAPORU",
            f"1) Borsa bakiyesi (MEXC spot): {fmt_money(self.total) if ok else 'UNKNOWN'} USDT",
            f"2) Kullanılabilir: {fmt_money(self.free) if ok else 'UNKNOWN'} USDT",
            f"3) Kullanımda: {fmt_money(self.used) if ok else 'UNKNOWN'} USDT",
            f"4) PAPER kasası (nakit): {fmt_money(portfolio.cash)} | TOPLAM SERVET: {fmt_money(portfolio.equity(prices))} USDT",
            f"5) Açık pozisyon: {len(portfolio.positions)}",
            f"6) Unrealized PnL: {fmt_money(portfolio.unrealized(prices))} USDT",
            f"7) Realized PnL: {fmt_money(portfolio.realized)} USDT",
            f"8) Ücretler: {fmt_money(portfolio.fees)} USDT",
            f"9) Günlük düşüş: {dd:.2f}%",
            f"10) Son bakiye senkronu: "
            f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(self.last_sync)) if self.last_sync else 'HİÇ'}",
            "Not: Diğer cüzdan/borsalardaki varlıklar GÖRÜNMEZ. Futures sorgulanmadı.",
        ]
        text = "\n".join(lines)
        print(text)
        TG.send(text, "report")
        return text

BALANCE = BalanceManager(ADAPTER, CONFIG)

class DecisionEngineV2(DecisionEngine):
    def __init__(self):
        super().__init__()
        self.last_scores = {}

    def decide(self, symbol, snapshot, signals, weights, sentiment, has_position, regime):
        d = super().decide(symbol, snapshot, signals, weights, sentiment, has_position, regime)
        self.last_scores[symbol] = d.score
        if d.action in ("BUY", "SELL"):
            TG.send(f"[SİNYAL] {symbol} {d.action} skor={d.score:+.2f} "
                    f"({'; '.join(d.reasons[:2])})", "signal")
        return d

DECISION = DecisionEngineV2()

class RiskManagerV2(RiskManager):
    """Mevcut RiskManager'ı GENİŞLETİR; mevcut kontroller korunur."""
    def __init__(self, config, db):
        super().__init__(config, db)
        self.ml = MLManager(config, db) if _SKLEARN_OK else None
        self._candle_lock = {}
        self._last_entry_candle = {}
        self._last_entry_wall = {}
        self.last_ml_prob = {}
        self.last_shadow_prob = {}
        self.last_regime = {}
        self.last_features = {}

    def kill_switch_active(self):
        env = (os.environ.get("KILL_SWITCH") or "").strip().lower()
        if env in ("1", "true", "yes"):
            return True
        return str(self.db.get_meta("kill_switch", "") or "").strip().lower() in ("1", "true")

    def _lockout_flag(self):
        return str(self.db.get_meta(f"lockout:{utc_now().date()}", "") or "") == "1"

    def _enter_daily_lockout(self, portfolio):
        self.db.set_meta(f"lockout:{utc_now().date()}", "1")
        LOG.warning("[RİSK] GÜNLÜK ZARAR LİMİTİ — yeni girişler UTC günü sonuna kadar kilitli.")
        if portfolio.mode == "live":
            try:
                for o in (ADAPTER.fetch_open_orders() or []):
                    try:
                        ADAPTER.cancel_order(o.get("id"), o.get("symbol"))
                    except TradingSystemError as e2:
                        LOG.warning("[RİSK] emir iptali başarısız (kilit yine de AKTİF): %s", e2)
            except TradingSystemError as e:
                LOG.warning("[RİSK] açık emir listesi alınamadı (kilit yine de AKTİF): %s", e)
        self.db.save_error("risk", f"Günlük zarar limiti lockout: {self.cfg.risk.max_daily_loss_pct}%")
        TG.send("🔒 [RİSK] GÜNLÜK ZARAR LİMİTİ DOLDU — yeni işlemler kilitli.", "risk", force=True)

    def _trend_align_1h(self, symbol, price):
        try:
            df = ENGINE.get_ohlcv(symbol, "1h", limit=210, min_candles=60)
        except TradingSystemError:
            return True, ""
        c = df["close"]
        e50 = safe_float(ema(c, 50).iloc[-1])
        if not _ok(e50) or not _ok(price):
            return True, ""
        if price >= e50:
            return True, ""
        e200 = safe_float(ema(c, 200).iloc[-1]) if len(c) >= 200 else None
        if _ok(e200) and e200 >= e50:
            return True, ""
        return False, "1h trend aşağı — ALIŞ engellendi"

    def pre_trade_checks(self, symbol, snapshot, ind, portfolio, prices):
        r = self.cfg.risk
        if self.kill_switch_active():
            return False, ["KILL SWITCH aktif"]
        if symbol in (getattr(self.cfg, "excluded_symbols", []) or []):
            return False, ["sembol hariç-listede"]
        eq = portfolio.equity(prices)
        if self._lockout_flag() or self.daily_loss_pct(eq) >= r.max_daily_loss_pct:
            if not self._lockout_flag():
                self._enter_daily_lockout(portfolio)
            return False, ["günlük zarar limiti — KİLİTLİ"]
        ok, reasons = super().pre_trade_checks(symbol, snapshot, ind, portfolio, prices)

        df_last, last_ts = None, None
        try:
            df_last = ENGINE.get_ohlcv(symbol, self.cfg.primary_timeframe, limit=30)
            last_ts = df_last["ts"].iloc[-1]
        except TradingSystemError:
            df_last = None
        if getattr(self.cfg, "require_1h_align", True):
            ok1, why1 = self._trend_align_1h(symbol, snapshot.get("last"))
            if not ok1:
                reasons.append(why1)
        regime = REGIME.classify(df_last, ind) if df_last is not None else "unknown"
        self.last_regime[symbol] = regime
        if last_ts is not None and self._last_entry_candle.get(symbol) == last_ts:
            reasons.append("aynı mumda tekrar giriş engellendi")
        if time.time() - self._last_entry_wall.get(symbol, 0.0) < 120:
            reasons.append("yinelenen giriş koruması")
        if df_last is not None and len(df_last):
            o_ = float(df_last["open"].iloc[-1])
            c_ = float(df_last["close"].iloc[-1])
            if abs(c_ - o_) / max(o_, 1e-12) * 100.0 >= r.abnormal_candle_pct:
                self._candle_lock[symbol] = time.time() + r.abnormal_candle_cooldown_min * 60
        if time.time() < self._candle_lock.get(symbol, 0.0):
            reasons.append("anormal mum sonrası bekleme")
        a = ind.get("atr")
        rr = (r.atr_target_multiplier / r.atr_stop_multiplier) \
            if (r.use_atr_stops and _ok(a) and a > 0) \
            else (r.default_take_profit_pct / max(r.default_stop_loss_pct, 1e-9))
        if rr < r.min_reward_risk:
            reasons.append(f"R:R {rr:.2f} < {r.min_reward_risk}")
        sent = NEWS.sentiment_for(symbol)
        f = build_features(snapshot, ind, sent, regime)
        self.last_features[symbol] = f
        if self.ml is not None and self.ml.active():
            p = self.ml.proba_from_features(self.ml.model, f)
            self.last_ml_prob[symbol] = p
            self.last_shadow_prob[symbol] = self.ml.proba_from_features(self.ml.candidate, f)
            if p is not None and p < self.cfg.ml.filter_threshold:
                reasons.append(f"ML filtre P(win)={p:.2f}<{self.cfg.ml.filter_threshold}")
        return (len(reasons) == 0), reasons

RISK = RiskManagerV2(CONFIG, DB)
print("[BÖLÜM 23] BalanceManager + Karar V2 + Risk V2 hazır.")

# ============================================================
# BÖLÜM 24 — EMİR V2 (journal + trailing) + BOT V2 + BACKTEST V2
# ============================================================
class ExecutionEngineV2(ExecutionEngine):
    def __init__(self, adapter, portfolio, config, db):
        super().__init__(adapter, portfolio, config, db)
        self.entry_ctx = {}

    def _risk_dist(self, symbol, pos):
        sl0 = (self.entry_ctx.get(symbol) or {}).get("sl_initial") or pos.stop_loss
        if sl0 and sl0 < pos.entry_price:
            return pos.entry_price - sl0
        return pos.entry_price * self.cfg.risk.default_stop_loss_pct / 100.0

    def check_stops(self, symbol, price):
        pos = self.pf.positions.get(symbol)
        if not pos:
            return None
        r = self.cfg.risk
        risk_dist = self._risk_dist(symbol, pos)
        if (r.use_trailing_stop or r.use_break_even) and risk_dist > 0:
            profit = price - pos.entry_price
            if r.use_break_even and profit >= r.break_even_trigger_rr * risk_dist:
                be = pos.entry_price * (1 + r.break_even_buffer_pct / 100.0)
                if be > (pos.stop_loss or 0.0):
                    pos.stop_loss = be
                    LOG.info("[%s] Break-even SL → %s", symbol, fmt_money(be))
            if r.use_trailing_stop and profit >= r.trailing_activate_rr * risk_dist:
                atr_val = None
                try:
                    df = ENGINE.get_ohlcv(symbol, self.cfg.primary_timeframe, limit=60)
                    atr_val = safe_float(atr(df).iloc[-1])
                except TradingSystemError:
                    atr_val = None
                if atr_val and atr_val > 0:
                    new_sl = price - r.trailing_atr_mult * atr_val
                    if new_sl > (pos.stop_loss or 0.0):
                        pos.stop_loss = new_sl
                        LOG.info("[%s] Trailing SL → %s", symbol, fmt_money(new_sl))
        return super().check_stops(symbol, price)

    def market_buy(self, symbol, amount, ref_price, atr_val, strategy="combined"):
        equity_before = self.pf.cash + sum(p.value(p.entry_price) for p in self.pf.positions.values())
        pos = super().market_buy(symbol, amount, ref_price, atr_val, strategy)
        if pos is None:
            return None
        ml = getattr(RISK, "ml", None)
        self.entry_ctx[symbol] = {
            "sl_initial": pos.stop_loss, "entry_ts": pos.opened_at,
            "buy_order_id": last_order_id(self.db, symbol) if self.pf.mode == "live"
                            else f"paper-{pos.opened_at:.0f}",
            "features": json.dumps(getattr(RISK, "last_features", {}).get(symbol) or {}),
            "regime": getattr(RISK, "last_regime", {}).get(symbol, "unknown"),
            "model_version": ml.version if (ml and ml.active()) else None,
            "shadow_version": ml.candidate_version if ml else None,
            "model_prob": getattr(RISK, "last_ml_prob", {}).get(symbol),
            "shadow_prob": getattr(RISK, "last_shadow_prob", {}).get(symbol),
            "score": getattr(DECISION, "last_scores", {}).get(symbol),
            "sentiment": (getattr(RISK, "last_features", {}).get(symbol) or {}).get("sentiment"),
            "equity_before": equity_before,
        }
        try:
            df = ENGINE.get_ohlcv(symbol, self.cfg.primary_timeframe, limit=3)
            RISK._last_entry_candle[symbol] = df["ts"].iloc[-1]
        except TradingSystemError:
            pass
        RISK._last_entry_wall[symbol] = time.time()
        TG.send(f"[EMİR] ALIŞ {symbol} qty={pos.amount:.8f} @ {fmt_money(pos.entry_price)} "
                f"SL={fmt_money(pos.stop_loss)} TP={fmt_money(pos.take_profit)}", "order")
        return pos

    def market_close(self, symbol, ref_price, reason):
        pos = self.pf.positions.get(symbol)
        sl_final = pos.stop_loss if pos else None
        ctx = self.entry_ctx.pop(symbol, None)
        tr = super().market_close(symbol, ref_price, reason)
        if tr is None:
            return None
        TG.send(f"[KAPANIŞ] {symbol} {reason} | net={fmt_money(tr.net_pnl)} USDT", "trade")
        try:
            uid = f"{tr.symbol}-{int(tr.opened_at)}-{int(tr.closed_at)}-{reason}"
            equity_after = self.pf.cash + sum(p.value(p.entry_price) for p in self.pf.positions.values())
            sell_id = last_order_id(self.db, tr.symbol) if self.pf.mode == "live" \
                else f"paper-{tr.closed_at:.0f}"
            slip_bps = abs(tr.exit_price - ref_price) / max(ref_price, 1e-12) * 10000.0 if ref_price else None
            cols = ("trade_uid,mode,exchange,symbol,market_type,strategy,strategy_version,"
                    "model_version,shadow_model_version,model_prob,shadow_prob,"
                    "buy_order_id,sell_order_id,side,amount,entry_price,exit_price,"
                    "sl_initial,sl_final,tp,entry_ts,exit_ts,duration_s,exit_reason,"
                    "gross_pnl,fees,net_pnl,slippage_bps,funding_cost,features,regime,"
                    "confidence,sentiment,equity_before,equity_after,simulated,created_at")
            vals = ",".join(["?"] * 37)
            self.db._exec(f"INSERT OR IGNORE INTO trade_journal({cols}) VALUES({vals})",
                (uid, tr.mode, tr.exchange, tr.symbol, self.cfg.market_type, tr.strategy, "v1",
                 (ctx or {}).get("model_version"), (ctx or {}).get("shadow_version"),
                 (ctx or {}).get("model_prob"), (ctx or {}).get("shadow_prob"),
                 (ctx or {}).get("buy_order_id"), sell_id, tr.side, tr.amount,
                 tr.entry_price, tr.exit_price, (ctx or {}).get("sl_initial"), sl_final, None,
                 tr.opened_at, tr.closed_at, tr.closed_at - tr.opened_at, reason,
                 tr.gross_pnl, tr.fees, tr.net_pnl, slip_bps, 0.0,
                 (ctx or {}).get("features"), (ctx or {}).get("regime"),
                 (ctx or {}).get("score"), (ctx or {}).get("sentiment"),
                 (ctx or {}).get("equity_before"), equity_after,
                 1 if self.pf.mode != "live" else 0, time.time()))
        except Exception as e:
            LOG.warning("[JOURNAL] yazılamadı: %s", e)
            TG.send(f"[ALARM] Trade journal hatası: {e}", "db", force=True)
        return tr

class TradingBotV2(TradingBot):
    def __init__(self, config, portfolio, execution):
        super().__init__(config, portfolio, execution)
        self._ml = getattr(RISK, "ml", None)

    def _startup_validation(self):
        problems = []
        r = self.cfg.risk
        if not (0 < r.max_risk_per_trade_pct <= 5):
            problems.append("max_risk_per_trade_pct aralık dışı")
        if not (0 < r.max_daily_loss_pct <= 20):
            problems.append("max_daily_loss_pct aralık dışı")
        if r.min_reward_risk < 1.0:
            problems.append("min_reward_risk < 1.0")
        try:
            DB._exec("SELECT 1").fetchone()
        except Exception as e:
            problems.append(f"DB erişim: {e}")
        try:
            ENGINE.get_ticker(self.cfg.symbols[0], force=True)
        except TradingSystemError as e:
            problems.append(f"piyasa verisi: {e}")
        return problems

    def _live_gate(self):
        if not ADAPTER.credentials.api_key:
            return False, "API anahtarı yok"
        if RISK.kill_switch_active():
            return False, "kill switch aktif"
        st = BALANCE.sync()
        if st != "OK" or not BALANCE.total or BALANCE.total <= 0:
            return False, "bakiye doğrulanamadı (UNKNOWN/0) — fail-closed"
        return True, ""

    def start(self):
        problems = self._startup_validation()
        TG.send(f"[START] Bot {self.cfg.mode.value.upper()} modunda başlıyor.", "lifecycle", force=True)
        if problems:
            LOG.warning("[START] Doğrulama sorunları: %s", problems)
            TG.send(f"[ALARM] Başlangıç doğrulama sorunları: {problems}", "lifecycle", force=True)
            if any(("DB erişim" in p) or ("piyasa verisi" in p) for p in problems):
                LOG.error("[START] Kritik doğrulama hatası — bot başlatılmıyor (fail-closed).")
                return
        if self.cfg.mode == TradingMode.LIVE:
            ok, why = self._live_gate()
            if not ok:
                LOG.error("[LIVE-GATE] Canlı işlem REDDİLDİ: %s", why)
                TG.send(f"[ALARM] LIVE gate reddi: {why}", "lifecycle", force=True)
                return
        self._load_paper_state()
        if self._ml is not None:
            self._ml.startup()
        if self.pf.mode == "live":
            BALANCE.sync()
        super().start()

    def _cycle(self):
        super()._cycle()
        if self.cycle % 10 == 0:
            try:
                prices = {s: ENGINE.get_ticker(s).last for s in self.cfg.symbols}
                if self.pf.mode == "live":
                    BALANCE.sync()
                BALANCE.report(self.pf, prices)
            except TradingSystemError as e:
                LOG.warning("[REPORT] %s", e)
        try:
            CHARTS.maybe_send_equity(self)
        except Exception:
            pass

    def _reconcile(self):
        super()._reconcile()
        if self.pf.mode != "live":
            return
        try:
            for o in (ADAPTER.fetch_open_orders() or []):
                ts_ms = safe_float(o.get("timestamp"), 0.0) or 0.0
                age_min = (time.time() - ts_ms / 1000.0) / 60.0 if ts_ms else 0.0
                if age_min > self.cfg.risk.stale_order_minutes:
                    LOG.warning("[RİSK] Bayat emir iptal: %s (%.0f dk)", o.get("id"), age_min)
                    try:
                        ADAPTER.cancel_order(o.get("id"), o.get("symbol"))
                    except TradingSystemError as e:
                        LOG.warning("[RİSK] iptal başarısız: %s", e)
        except TradingSystemError as e:
            LOG.warning("[RİSK] açık emir denetimi: %s", e)

    def _shutdown(self):
        TG.send("[STOP] Bot kapanıyor (güvenli kapanış).", "lifecycle", force=True)
        super()._shutdown()

def log_exception_v2(db, module, e):
    LOG.error("[%s] %s: %s", module, type(e).__name__, e)
    db.save_error(module, f"{type(e).__name__}: {e}")
    TG.send(f"[HATA] {module}: {type(e).__name__}", "error")

log_exception = log_exception_v2

class BacktesterV2(Backtester):
    def _stats(self, tr_df, eq_df, pf):
        st = super()._stats(tr_df, eq_df, pf)
        if tr_df is not None and not tr_df.empty:
            wins = tr_df[tr_df["net_pnl"] > 0]
            losses = tr_df[tr_df["net_pnl"] <= 0]
            st["ort_kar"] = float(wins["net_pnl"].mean()) if len(wins) else 0.0
            st["ort_zarar"] = float(losses["net_pnl"].mean()) if len(losses) else 0.0
            st["beklenti_islem"] = float(tr_df["net_pnl"].mean())
            rets = tr_df["net_pnl"] / max(pf.start_balance, 1e-9)
            if rets.std() and rets.std() > 0:
                st["sharpe_islem"] = float(rets.mean() / rets.std())
        return st

print("[BÖLÜM 24] Emir V2 + Bot V2 + Backtest V2 hazır.")

# ============================================================
# BÖLÜM 25 — MAKRO REJİM MOTORU (anahtarsız kaynaklar; skala ASLA 1.0'ı aşmaz)
# ============================================================
import io
from urllib.parse import quote as _urlquote

@dataclass
class MacroConfig:
    enabled: bool = True
    ttl_minutes: int = 60
    block_below: float = 0.70
    events_file: str = "macro_events.txt"

CONFIG.macro = MacroConfig()
if (os.environ.get("MACRO_ENABLED") or "").strip().lower() in ("0", "false", "no"):
    CONFIG.macro.enabled = False

class MacroEngine:
    def __init__(self, config):
        self.cfg = config
        self.scalar = 1.0
        self.data = {}
        self.last_refresh = 0.0
        self.category = "risk_on"
        self._dom_hist = []
        self._events = set()
        self._events_loaded = False

    def _load_events(self):
        if self._events_loaded:
            return
        self._events_loaded = True
        try:
            p = self.cfg.events_file
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        d = line.strip()[:10]
                        if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                            self._events.add(d)
                LOG.info("[MAKRO] %d olay günü yüklendi (%s)", len(self._events), p)
        except Exception as e:
            LOG.warning("[MAKRO] olay dosyası okunamadı: %s", e)

    def is_event_day(self):
        if not self.cfg.enabled:
            return False
        if (os.environ.get("MACRO_EVENT_TODAY") or "").strip().lower() in ("1", "true"):
            return True
        self._load_events()
        return str(utc_now().date()) in self._events

    def _get(self, url, as_json=False):
        if not _requests:
            return None
        try:
            r = _requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return None
            return r.json() if as_json else r.text
        except Exception:
            return None

    def _stooq_close(self, sym):
        txt = self._get(f"https://stooq.com/q/d/l/?s={_urlquote(sym)}&i=d")
        if not txt:
            return None
        try:
            df = pd.read_csv(io.StringIO(txt))
            if "Close" not in df.columns or len(df) < 55:
                return None
            return pd.to_numeric(df["Close"], errors="coerce").dropna()
        except Exception:
            return None

    def _fetch_fng(self):
        j = self._get("https://api.alternative.me/fng/?limit=1", as_json=True)
        try:
            return int(j["data"][0]["value"])
        except Exception:
            return None

    def _fetch_dxy_chg20(self):
        c = self._stooq_close("dx.f")
        if c is None or len(c) < 21:
            return None
        return float((c.iloc[-1] / c.iloc[-21] - 1.0) * 100.0)

    def _fetch_spx_below_sma50(self):
        c = self._stooq_close("^spx")
        if c is None or len(c) < 50:
            return None
        return bool(c.iloc[-1] < c.iloc[-50:].mean())

    def _fetch_us10y_chg20(self):
        c = self._stooq_close("10usy.b")
        if c is None or len(c) < 21:
            return None
        return float(c.iloc[-1] - c.iloc[-21])

    def _fetch_btc_dom(self):
        j = self._get("https://api.coingecko.com/api/v3/global", as_json=True)
        try:
            return float(j["data"]["market_cap_percentage"]["btc"])
        except Exception:
            return None

    def _dom_chg_1d(self, val):
        now = time.time()
        self._dom_hist.append((now, val))
        self._dom_hist = [x for x in self._dom_hist if now - x[0] < 3 * 86400][-40:]
        for ts, v in reversed(self._dom_hist[:-1]):
            if now - ts >= 20 * 3600:
                return float(val - v)
        return None

    def refresh(self, force=False):
        if not self.cfg.enabled:
            return
        if not force and time.time() - self.last_refresh < self.cfg.ttl_minutes * 60:
            return
        fng = self._fetch_fng()
        dxy = self._fetch_dxy_chg20()
        spx = self._fetch_spx_below_sma50()
        y10 = self._fetch_us10y_chg20()
        dom = self._fetch_btc_dom()
        dchg = self._dom_chg_1d(dom) if dom is not None else None
        self.data = {"fng": fng, "dxy_chg20": dxy, "spx_below_sma50": spx,
                     "us10y_chg20": y10, "btc_dom": dom, "dom_chg_1d": dchg}
        self.last_refresh = time.time()
        s = 1.0
        if fng is not None and (fng <= 20 or fng >= 80):
            s *= 0.85
        if dxy is not None and dxy > 2.0:
            s *= 0.80
        if spx:
            s *= 0.90
        if y10 is not None and y10 >= 0.30:
            s *= 0.90
        if dchg is not None and dchg >= 1.5:
            s *= 0.90
        self.scalar = float(clamp(s, 0.30, 1.0))
        new_cat = ("risk_on" if self.scalar >= 0.99
                   else "temkinli" if self.scalar >= self.cfg.block_below else "risk_off")
        if new_cat != self.category:
            LOG.warning("[MAKRO] Kategori: %s → %s (skala %.2f)", self.category, new_cat, self.scalar)
            TG.send(f"[MAKRO] {self.category} → {new_cat} | skala={self.scalar:.2f}", "macro", force=True)
            self.category = new_cat
        LOG.info("[MAKRO] F&G=%s DXY20d=%s SPX<SMA50=%s 10Y20d=%s dom=%s → skala=%.2f (%s)",
                 fng,
                 f"{dxy:.2f}%" if dxy is not None else "yok",
                 spx,
                 f"{y10:.2f}pp" if y10 is not None else "yok",
                 f"{dom:.1f}" if dom is not None else "yok",
                 self.scalar, self.category)

    def get_scalar(self, symbol=None):
        if not self.cfg.enabled:
            return 1.0
        if CONFIG.mode == TradingMode.BACKTEST:
            return 1.0
        self.refresh()
        return self.scalar

    def summary_line(self):
        if not self.cfg.enabled:
            return "makro: kapalı"
        return f"makro skala={self.scalar:.2f} ({self.category}) F&G={self.data.get('fng')}"

MACRO = MacroEngine(CONFIG.macro)

if CONFIG.macro.enabled:
    _orig_pre_trade_macro = RISK.pre_trade_checks

    def _macro_pre_trade(symbol, snapshot, ind, portfolio, prices):
        ok, reasons = _orig_pre_trade_macro(symbol, snapshot, ind, portfolio, prices)
        if CONFIG.mode == TradingMode.BACKTEST:
            return ok, reasons
        if MACRO.is_event_day():
            reasons.append("makro olay günü (FOMC/CPI/NFP) — yeni giriş kapalı")
            return False, reasons
        s = MACRO.get_scalar(symbol)
        if s < CONFIG.macro.block_below:
            reasons.append(f"makro risk-off (skala={s:.2f}<{CONFIG.macro.block_below})")
            return False, reasons
        return ok, reasons

    RISK.pre_trade_checks = _macro_pre_trade

    _orig_pos_size_macro = RISK.position_size

    def _macro_position_size(equity, price, stop_price, constraints):
        qty, why = _orig_pos_size_macro(equity, price, stop_price, constraints)
        if qty > 0 and CONFIG.mode != TradingMode.BACKTEST:
            s = MACRO.get_scalar()
            if s < 1.0:
                step = constraints.get("amount_step") or 1e-8
                qty = math.floor((qty * s) / step) * step
                if qty <= 0:
                    return 0.0, "makro skalayla miktar 0'a düştü"
        return qty, why

    RISK.position_size = _macro_position_size
    LOG.info("[MAKRO] Risk kapısı bağlandı (giriş engeli skala<%.2f, boyut×skala).",
             CONFIG.macro.block_below)
    MACRO.refresh(force=True)
    TG.send(f"[MAKRO] Motor aktif | {MACRO.summary_line()}", "macro", force=True)
else:
    LOG.info("[MAKRO] Kapalı — davranış değişmedi.")

# ============================================================
# BÖLÜM 26 — TELEGRAM EQUITY GRAFİĞİ (opsiyonel; hata sessizce yutulur)
# ============================================================
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _MPL_OK = True
except Exception:
    _MPL_OK = False

CHART_EVERY_N = int(os.environ.get("CHART_EVERY_N", "30") or 30)

class TelegramCharts:
    def __init__(self):
        self.ok = bool(_MPL_OK and _requests and TG.enabled)
        if not self.ok:
            LOG.info("[GRAFİK] Kapalı (matplotlib/Telegram eksik).")

    def maybe_send_equity(self, bot):
        if not self.ok:
            return
        if CHART_EVERY_N <= 0 or bot.cycle % CHART_EVERY_N != 0:
            return
        try:
            df = DB.equity_df(bot.pf.mode)
            if df is None or len(df) < 20:
                return
            ts = pd.to_datetime(df["ts"], unit="s")
            fig, ax = plt.subplots(figsize=(8, 3.2), dpi=110)
            ax.plot(ts, df["equity"], color="#00cc96", linewidth=1.4)
            ax.set_title(f"Equity — {bot.pf.mode.upper()} (döngü {bot.cycle})", fontsize=10)
            ax.grid(alpha=0.25)
            fig.tight_layout()
            buf = io.BytesIO()
            fig.savefig(buf, format="png")
            plt.close(fig)
            buf.seek(0)
            _requests.post(f"https://api.telegram.org/bot{TG.token}/sendPhoto",
                           data={"chat_id": TG.chat,
                                 "caption": f"🪙 {MACRO.summary_line()} | "
                                            f"equity={fmt_money(df['equity'].iloc[-1])} USDT"},
                           files={"photo": ("equity.png", buf, "image/png")}, timeout=15)
        except Exception as e:
            LOG.debug("[GRAFİK] gönderilemedi: %s", e)

CHARTS = TelegramCharts()
print("[BÖLÜM 26] Makro motor + Telegram grafik hazır.")

# ============================================================
# NİHAİ START — V2 sınıfları kullanır (Parça 3'teki main'in üzerine yazar)
# ============================================================
def main():
    print("=" * 60)
    print("   MEXC OTOMATİK ALIM-SATIM BOTU — START (V2)")
    print("=" * 60)
    mode = _select_mode()
    CONFIG.mode = mode
    print(f"Seçilen mod: {mode.value.upper()}")

    try:
        mkts = ADAPTER.markets()
        gecerli = [s for s in CONFIG.symbols
                   if s in mkts and mkts[s].get("active", True)]
        cikan = [s for s in CONFIG.symbols if s not in gecerli]
        if cikan:
            print(f"[UYARI] MEXC'te yok/pasif, listeden çıkarıldı: {cikan}")
        if gecerli:
            CONFIG.symbols = gecerli
    except TradingSystemError:
        pass
    print(f"Aktif semboller ({len(CONFIG.symbols)}): {CONFIG.symbols}")

    if mode == TradingMode.BACKTEST:
        stats, eq_df, _ = BacktesterV2().run("BTC/USDT")
        print_report("BACKTEST RAPORU — BTC/USDT", stats)
        plot_equity_curve(eq_df, "BTC/USDT Backtest Equity")
        return

    creds = request_credentials()
    if mode == TradingMode.LIVE and not creds.api_key:
        print("[DURDUR] CANLI mod için MEXC_API_KEY / MEXC_SECRET gerekli.")
        return
    if creds.api_key:
        ADAPTER.attach_credentials(creds)

    start_balance = CONFIG.paper_start_balance
    if creds.api_key:
        try:
            usdt = safe_float((ADAPTER.fetch_balance().get("total") or {}).get("USDT"), 0.0) or 0.0
            print(f"MEXC USDT bakiyesi: {fmt_money(usdt)}")
            if mode == TradingMode.LIVE:
                if usdt <= 0:
                    print("[DURDUR] USDT bakiye 0 — canlı işlem başlatılmadı.")
                    return
                start_balance = usdt
        except TradingSystemError as e:
            print(f"[UYARI] Bakiye doğrulanamadı: {e.user_message or e}")
            if mode == TradingMode.LIVE:
                print("[DURDUR] API doğrulanamadı — canlı işlem başlatılmadı.")
                return

    if mode == TradingMode.LIVE and not _confirm_live():
        print("İptal edildi.")
        return

    portfolio = Portfolio(mode.value, start_balance,
                          CONFIG.paper_fee_rate, CONFIG.paper_slippage_bps)
    execution = ExecutionEngineV2(ADAPTER, portfolio, CONFIG, DB)
    bot = TradingBotV2(CONFIG, portfolio, execution)
    print("\n▶ BOT ÇALIŞIYOR — durdurmak için CTRL+C (Colab'da durdur butonu)\n")
    bot.start()

    main()
# ============================================================
# BÖLÜM 27 — FUTURES (SWAP) MOTORU: KALDIRAÇLI LONG + SHORT
# Mod seçimi: TRADING_MARKET=spot (varsayılan) | TRADING_MARKET=swap
# Sert kurallar: max kaldıraç 3x, izole marjin, pozisyon riski x0.5,
# likidasyon koruması, reduceOnly kapanış. Paper'da tam simülasyon.
# ============================================================
print("[BÖLÜM 27] Futures/Short motoru yükleniyor...")

@dataclass
class FuturesConfig:
    enabled: bool = False
    leverage: int = 3
    max_leverage: int = 3
    risk_scale: float = 0.5          # pozisyon riski %1 -> %0.5
    funding_note: str = "live'da funding cost journal'a yazilir"

CONFIG.futures = FuturesConfig()
if (os.environ.get("TRADING_MARKET") or "spot").strip().lower() == "swap":
    CONFIG.futures.enabled = True
if os.environ.get("FUTURES_LEVERAGE"):
    try:
        CONFIG.futures.leverage = int(os.environ["FUTURES_LEVERAGE"])
    except ValueError:
        pass
CONFIG.futures.leverage = max(1, min(CONFIG.futures.leverage, CONFIG.futures.max_leverage))

def spot_to_swap(symbol: str) -> str:
    """BTC/USDT -> BTC/USDT:USDT (MEXC swap sembol formatı)."""
    return symbol if ":" in symbol else symbol + ":USDT"

@dataclass
class FuturesPosition(Position):
    leverage: float = 1.0

class FuturesPortfolio(Portfolio):
    """Swap modeli (paper sim): cash açılışta düşülmez, yalnız fee kesilir.
    equity = cash + unrealized(long+short). Short PnL = (entry - price) * amount."""
    def unrealized(self, prices: dict) -> float:
        tot = 0.0
        for s, p in self.positions.items():
            cur = prices.get(s, p.entry_price)
            if p.side == "short":
                tot += (p.entry_price - cur) * p.amount
            else:
                tot += (cur - p.entry_price) * p.amount
        return tot

    def equity(self, prices: dict) -> float:
        return self.cash + self.unrealized(prices)

    def open_short(self, symbol, amount, price, fee, sl, tp, strategy, ts, lev):
        self.cash -= fee
        pos = FuturesPosition(symbol=symbol, side="short", amount=amount,
                              entry_price=price, stop_loss=sl, take_profit=tp,
                              opened_at=ts, strategy=strategy, fees_paid=fee,
                              leverage=lev)
        self.positions[symbol] = pos
        return pos

    def close_futures(self, symbol, price, exit_fee, reason, ts=None):
        pos = self.positions.get(symbol)
        if not pos:
            return None
        if pos.side == "short":
            gross = (pos.entry_price - price) * pos.amount
        else:
            gross = (price - pos.entry_price) * pos.amount
        total_fees = pos.fees_paid + exit_fee
        net = gross - total_fees
        self.cash += gross - exit_fee
        self.realized += net
        self.fees += total_fees
        tr = TradeRecord(opened_at=pos.opened_at, closed_at=ts or time.time(),
                         mode=self.mode, exchange=self.exchange, symbol=symbol,
                         side=pos.side, amount=pos.amount, entry_price=pos.entry_price,
                         exit_price=price, gross_pnl=gross, fees=total_fees,
                         net_pnl=net, strategy=pos.strategy, close_reason=reason)
        self.positions.pop(symbol, None)
        return tr

class FuturesExecutionEngine(ExecutionEngineV2):
    """Swap emir motoru. Live'da reduceOnly + set_leverage kullanır.
    Paper'da tam simülasyon (slippage + fee + likidasyon koruması)."""

    def _open_common(self, symbol, amount, ref_price, atr_val, side, strategy):
        pf = self.pf
        lev = CONFIG.futures.leverage
        if pf.mode in ("paper", "backtest"):
            slip = pf.slippage_bps / 10000.0
            px = ref_price * (1 + slip) if side == "long" else ref_price * (1 - slip)
            notional = amount * px
            fee = notional * pf.fee_rate
            margin = notional / lev
            if margin + fee > pf.cash:
                raise TradeBlockedError(f"marjin yetersiz ({fmt_money(pf.cash)})")
            sl, tp = compute_sl_tp(self.cfg.risk, px, atr_val)
            if side == "long":
                pos = pf.open_position(symbol, amount, px, fee, sl, tp, strategy, time.time())
                pos.leverage = lev
            else:
                pos = pf.open_short(symbol, amount, px, fee, sl, tp, strategy, time.time(), lev)
            self.db.upsert_order(OrderRecord(
                order_id=f"swap-{time.time():.3f}-{symbol.replace('/', '')}",
                ts=time.time(), mode=pf.mode, exchange="mexc-swap", symbol=symbol,
                side=("buy" if side == "long" else "sell"), type="market",
                amount=amount, price=px, status=OrderStatus.FILLED.value,
                filled=amount, avg_fill_price=px, fee_cost=fee, strategy=strategy))
            return pos
        # ---- LIVE swap yolu ----
        cons = self.adapter.order_constraints(spot_to_swap(symbol))
        min_cost = max(cons["cost_min"], self.cfg.risk.min_order_quote_usdt)
        if amount * ref_price < min_cost:
            raise TradeBlockedError(f"tutar min altı ({amount * ref_price:.2f})")
        amount = self.adapter.round_amount(spot_to_swap(symbol), amount)
        try:
            self.adapter.set_leverage(spot_to_swap(symbol), lev)
        except TradingSystemError as e:
            LOG.warning("[%s] kaldıraç set edilemedi (devam): %s", symbol, e)
        reduce_only = False
        order_side = "buy" if side == "long" else "sell"
        rec = self.adapter.create_order(spot_to_swap(symbol), order_side, "market",
                                        amount, client_tag="bot",
                                        params={"reduceOnly": reduce_only})
        self.db.upsert_order(rec)
        filled, avg, fee = self._await_fill(rec)
        if not filled or not avg:
            raise TradeBlockedError(f"swap emri dolmadı (id={rec.order_id})")
        sl, tp = compute_sl_tp(self.cfg.risk, avg, atr_val)
        if side == "long":
            pos = pf.open_position(symbol, filled, avg, fee, sl, tp, strategy, time.time())
            pos.leverage = lev
        else:
            pos = pf.open_short(symbol, filled, avg, fee, sl, tp, strategy, time.time(), lev)
        return pos

    def market_buy(self, symbol, amount, ref_price, atr_val, strategy="combined"):
        return self._open_common(symbol, amount, ref_price, atr_val, "long", strategy)

    def market_sell_short(self, symbol, amount, ref_price, atr_val, strategy="combined"):
        return self._open_common(symbol, amount, ref_price, atr_val, "short", strategy)

    def _risk_dist(self, symbol, pos):
        sl0 = (self.entry_ctx.get(symbol) or {}).get("sl_initial") or pos.stop_loss
        if pos.side == "short" and sl0 and sl0 > pos.entry_price:
            return sl0 - pos.entry_price
        if sl0 and sl0 < pos.entry_price:
            return pos.entry_price - sl0
        return pos.entry_price * self.cfg.risk.default_stop_loss_pct / 100.0

    def market_close(self, symbol, ref_price, reason):
        pf = self.pf
        pos = pf.positions.get(symbol)
        if not pos:
            return None
        if pf.mode in ("paper", "backtest"):
            slip = pf.slippage_bps / 10000.0
            px = ref_price * (1 - slip) if pos.side == "long" else ref_price * (1 + slip)
            fee = pos.amount * px * pf.fee_rate
            tr = pf.close_futures(symbol, px, fee, reason, time.time())
            if tr:
                self.db.save_trade(tr)
            return tr
        close_side = "sell" if pos.side == "long" else "buy"   # reduceOnly kapanış
        rec = self.adapter.create_order(spot_to_swap(symbol), close_side, "market",
                                        pos.amount, client_tag="close",
                                        params={"reduceOnly": True})
        self.db.upsert_order(rec)
        filled, avg, fee = self._await_fill(rec)
        if not filled or not avg:
            raise TradeBlockedError(f"swap kapanış emri dolmadı (id={rec.order_id})")
        tr = pf.close_futures(symbol, avg, fee or 0.0, reason, time.time())
        if tr:
            self.db.save_trade(tr)
        return tr

    def check_stops(self, symbol, price):
        pos = self.pf.positions.get(symbol)
        if not pos:
            return None
        # ---- likidasyon koruması (izole, ~%100/lev aleyhe) ----
        lev = getattr(pos, "leverage", 1.0) or 1.0
        liq_move = (1.0 / lev) * 0.7     # %70 likidasyon mesafesi = acil kapanış
        if pos.side == "long" and price <= pos.entry_price * (1 - liq_move):
            LOG.warning("[%s] LİKİDASYON KORUMASI tetiklendi!", symbol)
            return self.market_close(symbol, price, "likidasyon_koruma")
        if pos.side == "short" and price >= pos.entry_price * (1 + liq_move):
            LOG.warning("[%s] LİKİDASYON KORUMASI tetiklendi!", symbol)
            return self.market_close(symbol, price, "likidasyon_koruma")
        # ---- trailing / break-even (mevcut V2 mantığı, yön-farkındalıklı) ----
        r = self.cfg.risk
        risk_dist = self._risk_dist(symbol, pos)
        if (r.use_trailing_stop or r.use_break_even) and risk_dist > 0:
            profit = (price - pos.entry_price) if pos.side == "long" else (pos.entry_price - price)
            if r.use_break_even and profit >= r.break_even_trigger_rr * risk_dist:
                buf = pos.entry_price * (r.break_even_buffer_pct / 100.0)
                if pos.side == "long":
                    be = pos.entry_price + buf
                    if be > (pos.stop_loss or 0.0):
                        pos.stop_loss = be
                        LOG.info("[%s] Break-even SL → %s", symbol, fmt_money(be))
                else:
                    be = pos.entry_price - buf
                    if pos.stop_loss is None or be < pos.stop_loss:
                        pos.stop_loss = be
                        LOG.info("[%s] Break-even SL (short) → %s", symbol, fmt_money(be))
            if r.use_trailing_stop and profit >= r.trailing_activate_rr * risk_dist:
                atr_val = None
                try:
                    df = ENGINE.get_ohlcv(symbol, self.cfg.primary_timeframe, limit=60)
                    atr_val = safe_float(atr(df).iloc[-1])
                except TradingSystemError:
                    atr_val = None
                if atr_val and atr_val > 0:
                    if pos.side == "long":
                        new_sl = price - r.trailing_atr_mult * atr_val
                        if new_sl > (pos.stop_loss or 0.0):
                            pos.stop_loss = new_sl
                            LOG.info("[%s] Trailing SL → %s", symbol, fmt_money(new_sl))
                    else:
                        new_sl = price + r.trailing_atr_mult * atr_val
                        if pos.stop_loss is None or new_sl < pos.stop_loss:
                            pos.stop_loss = new_sl
                            LOG.info("[%s] Trailing SL (short) → %s", symbol, fmt_money(new_sl))
        # ---- SL/TP kontrolü (yön-farkındalıklı) ----
        if pos.side == "short":
            if pos.stop_loss and price >= pos.stop_loss:
                return self.market_close(symbol, pos.stop_loss if self.pf.mode != "live" else price, "stop_loss")
            if pos.take_profit and price <= pos.take_profit:
                return self.market_close(symbol, pos.take_profit if self.pf.mode != "live" else price, "take_profit")
            return None
        return super().check_stops(symbol, price)

if CONFIG.futures.enabled:
    try:
        FUT_ADAPTER = CcxtAdapter(CONFIG.exchange_id, "swap")
        FUT_ADAPTER.load_markets()
        print(f"-> MEXC SWAP bağlandı. {len(FUT_ADAPTER.markets())} futures piyasası yüklü.")
    except TradingSystemError as e:
        CONFIG.futures.enabled = False
        LOG.error("[BÖLÜM 27] Swap adaptörü kurulamadı — SPOT modda devam: %s", e)
else:
    FUT_ADAPTER = None
    print("[BÖLÜM 27] Futures KAPALI (TRADING_MARKET=spot) — kaldıraçsız long mod.")

# ============================================================
# BÖLÜM 27B — V3 BAĞLANTI: SHORT + KALDIRAÇ (5A düzeltmeleri dahil)
# ============================================================
print("[BÖLÜM 27B] V3 bağlantısı yükleniyor...")

# ---- 27B.1 FuturesPortfolio: long marjin modeli (5A düzeltmesi) ----
def _fut_open_long(self, symbol, amount, price, fee, sl, tp, strategy, ts, lev):
    self.cash -= fee   # yalnız fee düşülür; teminat kapanışta iade modeli
    pos = FuturesPosition(symbol=symbol, side="long", amount=amount,
                          entry_price=price, stop_loss=sl, take_profit=tp,
                          opened_at=ts, strategy=strategy, fees_paid=fee,
                          leverage=lev)
    self.positions[symbol] = pos
    return pos
FuturesPortfolio.open_long = _fut_open_long

# ---- 27B.2 _open_common (düzeltilmiş: short SL/TP yönü + entry_ctx + TG) ----
def _fut_open_common(self, symbol, amount, ref_price, atr_val, side, strategy):
    pf = self.pf
    lev = CONFIG.futures.leverage
    if pf.mode in ("paper", "backtest"):
        slip = pf.slippage_bps / 10000.0
        px = ref_price * (1 + slip) if side == "long" else ref_price * (1 - slip)
        notional = amount * px
        fee = notional * pf.fee_rate
        margin = notional / lev
        if margin + fee > pf.cash:
            raise TradeBlockedError(f"marjin yetersiz ({fmt_money(pf.cash)})")
        if side == "long":
            sl, tp = compute_sl_tp(self.cfg.risk, px, atr_val)
        else:
            a = atr_val
            if _ok(a) and a > 0:
                sl = px + self.cfg.risk.atr_stop_multiplier * a
                tp = px - self.cfg.risk.atr_target_multiplier * a
            else:
                sl = px * (1 + self.cfg.risk.default_stop_loss_pct / 100.0)
                tp = px * (1 - self.cfg.risk.default_take_profit_pct / 100.0)
            if sl <= px: sl = px * (1 + self.cfg.risk.default_stop_loss_pct / 100.0)
            if tp >= px: tp = px * (1 - self.cfg.risk.default_take_profit_pct / 100.0)
        if side == "long":
            pos = pf.open_long(symbol, amount, px, fee, sl, tp, strategy, time.time(), lev)
        else:
            pos = pf.open_short(symbol, amount, px, fee, sl, tp, strategy, time.time(), lev)
        self.db.upsert_order(OrderRecord(
            order_id=f"swap-{time.time():.3f}-{symbol.replace('/', '')}",
            ts=time.time(), mode=pf.mode, exchange="mexc-swap", symbol=symbol,
            side=("buy" if side == "long" else "sell"), type="market",
            amount=amount, price=px, status=OrderStatus.FILLED.value,
            filled=amount, avg_fill_price=px, fee_cost=fee, strategy=strategy))
        return pos
    cons = self.adapter.order_constraints(spot_to_swap(symbol))
    min_cost = max(cons["cost_min"], self.cfg.risk.min_order_quote_usdt)
    if amount * ref_price < min_cost:
        raise TradeBlockedError(f"tutar min altı ({amount * ref_price:.2f})")
    amount = self.adapter.round_amount(spot_to_swap(symbol), amount)
    try:
        self.adapter.set_leverage(spot_to_swap(symbol), lev)
    except TradingSystemError as e:
        LOG.warning("[%s] kaldıraç set edilemedi (devam): %s", symbol, e)
    order_side = "buy" if side == "long" else "sell"
    rec = self.adapter.create_order(spot_to_swap(symbol), order_side, "market",
                                    amount, client_tag="bot")
    self.db.upsert_order(rec)
    filled, avg, fee = self._await_fill(rec)
    if not filled or not avg:
        raise TradeBlockedError(f"swap emri dolmadı (id={rec.order_id})")
    if side == "long":
        sl, tp = compute_sl_tp(self.cfg.risk, avg, atr_val)
        pos = pf.open_long(symbol, filled, avg, fee, sl, tp, strategy, time.time(), lev)
    else:
        a = atr_val
        if _ok(a) and a > 0:
            sl = avg + self.cfg.risk.atr_stop_multiplier * a
            tp = avg - self.cfg.risk.atr_target_multiplier * a
        else:
            sl = avg * (1 + self.cfg.risk.default_stop_loss_pct / 100.0)
            tp = avg * (1 - self.cfg.risk.default_take_profit_pct / 100.0)
        pos = pf.open_short(symbol, filled, avg, fee, sl, tp, strategy, time.time(), lev)
    return pos
FuturesExecutionEngine._open_common = _fut_open_common

def _fut_market_buy(self, symbol, amount, ref_price, atr_val, strategy="combined"):
    equity_before = self.pf.cash
    pos = self._open_common(symbol, amount, ref_price, atr_val, "long", strategy)
    if pos is None:
        return None
    self.entry_ctx[symbol] = {"sl_initial": pos.stop_loss, "entry_ts": pos.opened_at,
                              "features": json.dumps(getattr(RISK, "last_features", {}).get(symbol) or {}),
                              "regime": getattr(RISK, "last_regime", {}).get(symbol, "unknown"),
                              "score": getattr(DECISION, "last_scores", {}).get(symbol),
                              "equity_before": equity_before}
    try:
        df = ENGINE.get_ohlcv(symbol, self.cfg.primary_timeframe, limit=3)
        RISK._last_entry_candle[symbol] = df["ts"].iloc[-1]
    except TradingSystemError:
        pass
    RISK._last_entry_wall[symbol] = time.time()
    TG.send(f"[SWAP-LONG] {symbol} {CONFIG.futures.leverage}x qty={pos.amount:.8f} "
            f"@ {fmt_money(pos.entry_price)} SL={fmt_money(pos.stop_loss)} "
            f"TP={fmt_money(pos.take_profit)}", "order")
    return pos
FuturesExecutionEngine.market_buy = _fut_market_buy

def _fut_market_close(self, symbol, ref_price, reason):
    pf = self.pf
    pos = pf.positions.get(symbol)
    if not pos:
        return None
    sl_final = pos.stop_loss
    ctx = self.entry_ctx.pop(symbol, None)
    if pf.mode in ("paper", "backtest"):
        slip = pf.slippage_bps / 10000.0
        px = ref_price * (1 - slip) if pos.side == "long" else ref_price * (1 + slip)
        fee = pos.amount * px * pf.fee_rate
        tr = pf.close_futures(symbol, px, fee, reason, time.time())
        if tr:
            self.db.save_trade(tr)
    else:
        close_side = "sell" if pos.side == "long" else "buy"
        rec = self.adapter.create_order(spot_to_swap(symbol), close_side, "market",
                                        pos.amount, client_tag="close",
                                        params={"reduceOnly": True})
        self.db.upsert_order(rec)
        filled, avg, fee = self._await_fill(rec)
        if not filled or not avg:
            raise TradeBlockedError(f"swap kapanış emri dolmadı (id={rec.order_id})")
        tr = pf.close_futures(symbol, avg, fee or 0.0, reason, time.time())
        if tr:
            self.db.save_trade(tr)
    if tr is None:
        return None
    TG.send(f"[SWAP-KAPANIŞ] {symbol} {reason} | net={fmt_money(tr.net_pnl)} USDT", "trade")
    try:
        uid = f"{tr.symbol}-{int(tr.opened_at)}-{int(tr.closed_at)}-{reason}"
        equity_after = self.pf.cash + self.pf.unrealized({})
        slip_bps = abs(tr.exit_price - ref_price) / max(ref_price, 1e-12) * 10000.0 if ref_price else None
        cols = ("trade_uid,mode,exchange,symbol,market_type,strategy,strategy_version,"
                "buy_order_id,sell_order_id,side,amount,entry_price,exit_price,"
                "sl_initial,sl_final,tp,entry_ts,exit_ts,duration_s,exit_reason,"
                "gross_pnl,fees,net_pnl,slippage_bps,funding_cost,features,regime,"
                "confidence,equity_before,equity_after,simulated,created_at")
        vals = ",".join(["?"] * 32)
        self.db._exec(f"INSERT OR IGNORE INTO trade_journal({cols}) VALUES({vals})",
            (uid, tr.mode, "mexc-swap", tr.symbol, "swap", tr.strategy, "v3",
             (ctx or {}).get("buy_order_id"),
             last_order_id(self.db, symbol) if pf.mode == "live" else f"paper-{tr.closed_at:.0f}",
             tr.side, tr.amount, tr.entry_price, tr.exit_price,
             (ctx or {}).get("sl_initial"), sl_final, None,
             tr.opened_at, tr.closed_at, tr.closed_at - tr.opened_at, reason,
             tr.gross_pnl, tr.fees, tr.net_pnl, slip_bps, 0.0,
             (ctx or {}).get("features"), (ctx or {}).get("regime"),
             (ctx or {}).get("score"), (ctx or {}).get("equity_before"), equity_after,
             1 if pf.mode != "live" else 0, time.time()))
    except Exception as e:
        LOG.warning("[JOURNAL-swap] yazılamadı: %s", e)
    return tr
FuturesExecutionEngine.market_close = _fut_market_close

# ---- 27B.3 TradingBotV3: SELL sinyali → SHORT açılış (futures modda) ----
class TradingBotV3(TradingBotV2):
    def _save_paper_state(self):
        if self.pf.mode != "paper":
            return
        data = {"cash": self.pf.cash, "start": self.pf.start_balance,
                "positions": {s: {"amount": p.amount, "entry": p.entry_price,
                                  "sl": p.stop_loss, "tp": p.take_profit,
                                  "opened_at": p.opened_at, "strategy": p.strategy,
                                  "side": p.side, "leverage": getattr(p, "leverage", 1.0)}
                              for s, p in self.pf.positions.items()}}
        try:
            DB.set_meta("paper_state", json.dumps(data))
        except Exception as e:
            LOG.warning("[PAPER] durum kaydedilemedi: %s", e)

    def _load_paper_state(self):
        if self.pf.mode != "paper":
            return
        v = DB.get_meta("paper_state")
        if not v:
            return
        try:
            data = json.loads(v)
            self.pf.cash = float(data.get("cash", self.pf.cash))
            self.pf.start_balance = float(data.get("start", self.pf.start_balance))
            for s, p in data.get("positions", {}).items():
                side = p.get("side", "long")
                if side == "short" or CONFIG.futures.enabled:
                    pos = FuturesPosition(symbol=s, side=side,
                                          amount=float(p["amount"]),
                                          entry_price=float(p["entry"]),
                                          stop_loss=safe_float(p.get("sl")),
                                          take_profit=safe_float(p.get("tp")),
                                          opened_at=float(p.get("opened_at", 0)),
                                          strategy=str(p.get("strategy", "")),
                                          leverage=float(p.get("leverage", 1.0)))
                else:
                    pos = Position(symbol=s, side="long", amount=float(p["amount"]),
                                   entry_price=float(p["entry"]),
                                   stop_loss=safe_float(p.get("sl")),
                                   take_profit=safe_float(p.get("tp")),
                                   opened_at=float(p.get("opened_at", 0)),
                                   strategy=str(p.get("strategy", "")))
                self.pf.positions[s] = pos
            LOG.info("[PAPER] Önceki oturum yüklendi: cash=%s pozisyon=%d",
                     fmt_money(self.pf.cash), len(self.pf.positions))
        except Exception as e:
            LOG.warning("[PAPER] durum yüklenemedi: %s", e)

    def _process_symbol(self, symbol, prices):
        snap = ENGINE.snapshot(symbol)
        prices[symbol] = snap["last"]
        df = ENGINE.get_ohlcv(symbol, self.cfg.primary_timeframe, min_candles=210)
        try:
            dfh = ENGINE.get_ohlcv(symbol, self.cfg.higher_timeframe, min_candles=210)
        except TradingSystemError:
            dfh = None
        signals, ind = STRATEGIES.compute_all(symbol, df, dfh)
        for s in signals:
            try:
                DB.save_signal(s)
            except Exception:
                pass
        regime = REGIME.classify(df, ind)
        weights = REGIME.adjust_weights(self.cfg.strategy_weights, regime)
        sent = NEWS.sentiment_for(symbol)
        if self.pf.has(symbol):
            tr = self.execution.check_stops(symbol, snap["last"])
            if tr:
                LOG.info("[%s] POZİSYON KAPANDI (%s) net=%s",
                         symbol, tr.close_reason, fmt_money(tr.net_pnl))
                if tr.close_reason == "stop_loss":
                    RISK.register_stop_hit(symbol)
        dec = DECISION.decide(symbol, snap, signals, weights, sent,
                              self.pf.has(symbol), regime)
        try:
            DB.save_decision(dec)
        except Exception:
            pass
        LOG.info("[%s] %s skor=%+.2f rejim=%s haber=%+.2f(%d) | %s",
                 symbol, dec.action, dec.score, regime,
                 sent.get("sentiment", 0.0), sent.get("count", 0),
                 "; ".join(dec.reasons[:3]))
        if dec.action == "BUY" and not self.pf.has(symbol):
            ok, why = RISK.pre_trade_checks(symbol, snap, ind, self.pf, prices)
            if not ok:
                LOG.info("[%s] ALIŞ engellendi: %s", symbol, ", ".join(why))
                return
            try:
                equity = self.pf.equity(prices)
                px = snap.get("ask") or snap["last"]
                sl, _ = compute_sl_tp(self.cfg.risk, snap["last"], ind.get("atr"))
                cons = ADAPTER.order_constraints(symbol)
                qty, why = RISK.position_size(equity, px, sl, cons)
                if qty <= 0:
                    LOG.info("[%s] boyut 0: %s", symbol, why)
                    return
                pos = self.execution.market_buy(symbol, qty, px, ind.get("atr"))
                if pos:
                    LOG.info("[%s] >>> AÇILDI qty=%.8f @%s | SL=%s TP=%s",
                             symbol, pos.amount, fmt_money(pos.entry_price),
                             fmt_money(pos.stop_loss), fmt_money(pos.take_profit))
            except TradingSystemError as e:
                LOG.warning("[%s] alış başarısız: %s", symbol, e.user_message or e)
        elif dec.action == "SELL":
            if self.pf.has(symbol):
                try:
                    tr = self.execution.market_close(symbol, snap["last"], "sinyal")
                    if tr:
                        LOG.info("[%s] <<< SİNYALLE KAPANDI net=%s",
                                 symbol, fmt_money(tr.net_pnl))
                except TradingSystemError as e:
                    LOG.warning("[%s] satış başarısız: %s", symbol, e.user_message or e)
            elif CONFIG.futures.enabled:
                ok, why = RISK.pre_trade_checks(symbol, snap, ind, self.pf, prices)
                if not ok:
                    LOG.info("[%s] SHORT engellendi: %s", symbol, ", ".join(why))
                    return
                try:
                    equity = self.pf.equity(prices)
                    px = snap.get("bid") or snap["last"]
                    a = ind.get("atr")
                    if _ok(a) and a > 0:
                        sl_s = px + self.cfg.risk.atr_stop_multiplier * a
                    else:
                        sl_s = px * (1 + self.cfg.risk.default_stop_loss_pct / 100.0)
                    cons = ADAPTER.order_constraints(symbol)
                    # kısa pozisyon boyutu: ters parametre hilesi (stop fiyat üstünde)
                    qty, why = RISK.position_size(equity, sl_s, px, cons)
                    if qty <= 0:
                        LOG.info("[%s] short boyut 0: %s", symbol, why)
                        return
                    pos = self.execution.market_sell_short(symbol, qty, px, a)
                    if pos:
                        LOG.info("[%s] >>> SHORT AÇILDI qty=%.8f @%s | SL=%s TP=%s",
                                 symbol, pos.amount, fmt_money(pos.entry_price),
                                 fmt_money(pos.stop_loss), fmt_money(pos.take_profit))
                except TradingSystemError as e:
                    LOG.warning("[%s] short başarısız: %s", symbol, e.user_message or e)
            else:
                LOG.info("[%s] satış sinyali ama pozisyon yok (spot mod — short kapalı)",
                         symbol)

# ---- 27B.4 main() V3 (futures modda doğru motorları kurar) ----
def main():
    print("=" * 60)
    print("   MEXC BOT — START (V3: spot/swap çift mod)")
    print("=" * 60)
    mode = _select_mode()
    CONFIG.mode = mode
    print(f"Seçilen mod: {mode.value.upper()} | "
          f"piyasa: {'SWAP ' + str(CONFIG.futures.leverage) + 'x' if CONFIG.futures.enabled else 'SPOT (kaldıraçsız)'}")

    try:
        mkts = ADAPTER.markets()
        gecerli = [s for s in CONFIG.symbols
                   if s in mkts and mkts[s].get("active", True)]
        if gecerli:
            CONFIG.symbols = gecerli
    except TradingSystemError:
        pass
    print(f"Aktif semboller ({len(CONFIG.symbols)}): {CONFIG.symbols}")

    if mode == TradingMode.BACKTEST:
        stats, eq_df, _ = BacktesterV2().run("BTC/USDT")
        print_report("BACKTEST RAPORU — BTC/USDT", stats)
        plot_equity_curve(eq_df, "BTC/USDT Backtest Equity")
        return

    creds = request_credentials()
    if mode == TradingMode.LIVE and not creds.api_key:
        print("[DURDUR] CANLI mod için MEXC_API_KEY / MEXC_SECRET gerekli.")
        return
    if creds.api_key:
        ADAPTER.attach_credentials(creds)
        if CONFIG.futures.enabled and FUT_ADAPTER is not None:
            FUT_ADAPTER.attach_credentials(creds)

    start_balance = CONFIG.paper_start_balance
    if creds.api_key:
        try:
            usdt = safe_float((ADAPTER.fetch_balance().get("total") or {}).get("USDT"), 0.0) or 0.0
            print(f"MEXC USDT bakiyesi: {fmt_money(usdt)}")
            if mode == TradingMode.LIVE:
                if usdt <= 0:
                    print("[DURDUR] USDT bakiye 0 — canlı işlem başlatılmadı.")
                    return
                start_balance = usdt
        except TradingSystemError as e:
            print(f"[UYARI] Bakiye doğrulanamadı: {e.user_message or e}")
            if mode == TradingMode.LIVE:
                print("[DURDUR] API doğrulanamadı — canlı işlem başlatılmadı.")
                return

    if mode == TradingMode.LIVE and not _confirm_live():
        print("İptal edildi.")
        return

    if CONFIG.futures.enabled:
        portfolio = FuturesPortfolio(mode.value, start_balance,
                                     CONFIG.paper_fee_rate, CONFIG.paper_slippage_bps)
        exec_adapter = FUT_ADAPTER if (mode == TradingMode.LIVE and FUT_ADAPTER) else ADAPTER
        execution = FuturesExecutionEngine(exec_adapter, portfolio, CONFIG, DB)
        bot = TradingBotV3(CONFIG, portfolio, execution)
    else:
        portfolio = Portfolio(mode.value, start_balance,
                              CONFIG.paper_fee_rate, CONFIG.paper_slippage_bps)
        execution = ExecutionEngineV2(ADAPTER, portfolio, CONFIG, DB)
        bot = TradingBotV2(CONFIG, portfolio, execution)
    print("\n▶ BOT ÇALIŞIYOR — durdurmak için CTRL+C\n")
    bot.start()

if __name__ == "__main__":
    main()
