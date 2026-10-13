import json
import os
from dataclasses import dataclass, field, fields
from enum import Enum

from .errors import FatalError
from .utils import TF_SECONDS


class TradingMode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


def _env_str(name, default=""):
    v = os.environ.get(name)
    return v.strip() if v is not None else default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_list(name, default=None):
    v = os.environ.get(name)
    if not v:
        return default
    return [s.strip() for s in v.split(",") if s.strip()]


@dataclass
class RiskLimits:
    max_risk_per_trade_pct: float = 1.0
    max_position_value_pct: float = 20.0
    max_total_exposure_pct: float = 60.0
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 15.0
    max_open_positions: int = 5
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
    trail_activation_pct: float = 2.0
    trail_distance_pct: float = 1.2
    max_hold_bars: int = 96
    circuit_break_after_losses: int = 4
    cooldown_minutes_after_stop: int = 60
    min_order_quote_usdt: float = 5.5

    @classmethod
    def from_env(cls):
        obj = cls()
        for f in fields(cls):
            env = "RISK_" + f.name.upper()
            raw = os.environ.get(env)
            if raw is None or raw == "":
                continue
            try:
                cur = getattr(obj, f.name)
                if isinstance(cur, bool):
                    setattr(obj, f.name, raw.strip().lower() in {"1", "true", "yes", "on"})
                elif isinstance(cur, int):
                    setattr(obj, f.name, int(float(raw)))
                elif isinstance(cur, float):
                    setattr(obj, f.name, float(raw))
            except (TypeError, ValueError):
                raise FatalError(f"Invalid env {env}={raw!r}")
        return obj


@dataclass
class Credentials:
    api_key: str = ""
    secret: str = ""

    @property
    def has_private(self):
        return bool(self.api_key and self.secret)

    def __repr__(self):
        m = lambda s: f"***({len(s)})" if s else "(empty)"
        return f"Credentials(api_key={m(self.api_key)}, secret={m(self.secret)})"


DEFAULT_WEIGHTS = {"trend_follow": 1.2, "ema_cross": 1.0, "rsi_momentum": 0.8,
                   "macd": 0.8, "bollinger_reversion": 0.7, "volume_breakout": 1.0,
                   "multi_timeframe": 1.3, "vwap_reversion": 0.6}


@dataclass
class AppConfig:
    exchange_id: str = "mexc"
    market_type: str = "spot"
    mode: TradingMode = TradingMode.PAPER
    live_arm: str = "no"
    data_dir: str = "data"
    top_n_symbols: int = 20
    universe_refresh_hours: float = 12.0
    symbols: list = field(default_factory=list)
    primary_timeframe: str = "15m"
    higher_timeframe: str = "4h"
    base_currency: str = "USDT"
    paper_start_balance: float = 10_000.0
    paper_fee_rate: float = 0.001
    paper_slippage_bps: float = 5.0
    live_slippage_pad_pct: float = 0.15
    order_timeout_seconds: int = 30
    poll_seconds: int = 60
    max_runtime_minutes: float = 0.0
    news_refresh_minutes: float = 15
    news_max_age_hours: float = 48
    news_tilt_max: float = 0.10
    buy_score_threshold: float = 0.35
    sell_score_threshold: float = -0.35
    min_agreement: float = 0.6
    min_nonflat_signals: int = 3
    strategy_weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    ml_enabled: bool = False
    ml_retrain_days: int = 7
    backtest_days: int = 30
    max_bt_bars: int = 4000
    telegram_enabled: bool = True
    telegram_token: str = ""
    telegram_chat_id: str = ""
    close_on_stop: bool = False
    close_on_kill: bool = False
    reconcile_every_cycles: int = 10
    max_cycles: int = 0
    credentials: Credentials = field(default_factory=Credentials)
    risk: RiskLimits = field(default_factory=RiskLimits)

    @classmethod
    def from_env(cls):
        cfg = cls()
        cfg.exchange_id = _env_str("EXCHANGE_ID", "mexc").lower()
        cfg.market_type = _env_str("MARKET_TYPE", "spot").lower()
        mode_raw = _env_str("TRADING_MODE", "paper").lower()
        try:
            cfg.mode = TradingMode(mode_raw)
        except ValueError:
            raise FatalError(f"TRADING_MODE must be paper|backtest|live, got {mode_raw!r}")
        cfg.live_arm = _env_str("LIVE_ARM", "no").lower()
        cfg.data_dir = _env_str("DATA_DIR", "data")
        cfg.top_n_symbols = _env_int("TOP_N_SYMBOLS", 20)
        cfg.universe_refresh_hours = _env_float("UNIVERSE_REFRESH_HOURS", 12.0)
        cfg.symbols = _env_list("SYMBOLS", [])
        cfg.primary_timeframe = _env_str("PRIMARY_TIMEFRAME", "15m")
        cfg.higher_timeframe = _env_str("HIGHER_TIMEFRAME", "4h")
        cfg.base_currency = _env_str("BASE_CURRENCY", "USDT").upper()
        cfg.paper_start_balance = _env_float("PAPER_START_BALANCE", 10000.0)
        cfg.paper_fee_rate = _env_float("PAPER_FEE_RATE", 0.001)
        cfg.paper_slippage_bps = _env_float("PAPER_SLIPPAGE_BPS", 5.0)
        cfg.live_slippage_pad_pct = _env_float("LIVE_SLIPPAGE_PAD_PCT", 0.15)
        cfg.order_timeout_seconds = _env_int("ORDER_TIMEOUT_SECONDS", 30)
        cfg.poll_seconds = max(15, _env_int("POLL_SECONDS", 60))
        cfg.max_runtime_minutes = _env_float("MAX_RUNTIME_MINUTES", 0.0)
        cfg.news_refresh_minutes = _env_float("NEWS_REFRESH_MINUTES", 15)
        cfg.news_max_age_hours = _env_float("NEWS_MAX_AGE_HOURS", 48)
        cfg.news_tilt_max = _env_float("NEWS_TILT_MAX", 0.10)
        cfg.buy_score_threshold = _env_float("BUY_SCORE_THRESHOLD", 0.35)
        cfg.sell_score_threshold = _env_float("SELL_SCORE_THRESHOLD", -0.35)
        cfg.min_agreement = _env_float("MIN_AGREEMENT", 0.6)
        cfg.min_nonflat_signals = _env_int("MIN_NONFLAT_SIGNALS", 3)
        w_raw = os.environ.get("STRATEGY_WEIGHTS")
        if w_raw:
            try:
                parsed = json.loads(w_raw)
                if isinstance(parsed, dict):
                    cfg.strategy_weights.update({k: float(v) for k, v in parsed.items()})
            except (json.JSONDecodeError, TypeError, ValueError):
                raise FatalError("STRATEGY_WEIGHTS must be valid JSON object")
        cfg.ml_enabled = _env_bool("ML_ENABLED", False)
        cfg.ml_retrain_days = _env_int("ML_RETRAIN_DAYS", 7)
        cfg.backtest_days = _env_int("BACKTEST_DAYS", 30)
        cfg.max_bt_bars = _env_int("MAX_BT_BARS", 4000)
        cfg.telegram_enabled = _env_bool("TELEGRAM_ENABLED", True)
        cfg.telegram_token = _env_str("TELEGRAM_BOT_TOKEN")
        cfg.telegram_chat_id = _env_str("TELEGRAM_CHAT_ID")
        if not (cfg.telegram_enabled and cfg.telegram_token and cfg.telegram_chat_id):
            cfg.telegram_enabled = False
        cfg.close_on_stop = _env_bool("CLOSE_ON_STOP", False)
        cfg.close_on_kill = _env_bool("CLOSE_ON_KILL", False)
        cfg.reconcile_every_cycles = _env_int("RECONCILE_EVERY_CYCLES", 10)
        cfg.max_cycles = _env_int("MAX_CYCLES", 0)
        cfg.credentials = Credentials(api_key=_env_str("MEXC_API_KEY"),
                                      secret=_env_str("MEXC_SECRET"))
        if not cfg.credentials.has_private:
            cfg.credentials = Credentials(
                api_key=_env_str(f"{cfg.exchange_id.upper()}_API_KEY") or cfg.credentials.api_key,
                secret=_env_str(f"{cfg.exchange_id.upper()}_SECRET") or cfg.credentials.secret)
        cfg.risk = RiskLimits.from_env()
        cfg.validate()
        return cfg

    def validate(self):
        if self.mode == TradingMode.LIVE:
            if self.live_arm != "yes":
                raise FatalError("LIVE blocked: set LIVE_ARM=yes to explicitly authorize live trading.")
            if not self.credentials.has_private:
                raise FatalError("LIVE blocked: API keys missing (safe failure).")
        if self.primary_timeframe not in TF_SECONDS or self.higher_timeframe not in TF_SECONDS:
            raise FatalError("Unsupported timeframe in PRIMARY_TIMEFRAME/HIGHER_TIMEFRAME")
        if self.risk.max_risk_per_trade_pct <= 0 or self.risk.max_position_value_pct <= 0:
            raise FatalError("Risk limits must be positive")
        if not (0 < self.buy_score_threshold < 1):
            raise FatalError("BUY_SCORE_THRESHOLD must be in (0,1)")
