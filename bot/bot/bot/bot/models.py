from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Ticker:
    symbol: str
    last: float
    bid: float
    ask: float
    pct_change_24h: Optional[float] = None
    quote_volume: Optional[float] = None
    timestamp_ms: Optional[float] = None
    fetched_at: float = 0.0

    @property
    def spread_pct(self):
        if self.bid and self.ask and self.ask > 0:
            return (self.ask - self.bid) / self.ask * 100.0
        return float("nan")


@dataclass
class OrderRecord:
    order_id: str
    ts: float
    mode: str
    exchange: str
    symbol: str
    side: str
    type: str
    amount: float
    price: Optional[float]
    status: str = "pending_submit"
    filled: float = 0.0
    avg_fill_price: Optional[float] = None
    fee_cost: float = 0.0
    fee_currency: str = ""
    strategy: str = ""
    client_tag: str = ""
    raw: str = ""


@dataclass
class PositionRecord:
    symbol: str
    side: str
    amount: float
    entry_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trail_high: Optional[float] = None
    opened_at: float = 0.0
    strategy: str = ""
    fees_paid: float = 0.0


@dataclass
class TradeRecord:
    opened_at: float
    closed_at: float
    mode: str
    exchange: str
    symbol: str
    side: str
    amount: float
    entry_price: float
    exit_price: float
    gross_pnl: float
    fees: float
    net_pnl: float
    strategy: str = ""
    close_reason: str = ""


@dataclass
class Signal:
    ts: float
    symbol: str
    timeframe: str
    strategy: str
    direction: str = "flat"
    strength: float = 0.0
    price: float = 0.0
    reason: str = ""


@dataclass
class Decision:
    ts: float
    symbol: str
    action: str
    score: float
    confidence: float
    reasons: List[str] = field(default_factory=list)
    regime: str = "unknown"
    data_ok: bool = True
