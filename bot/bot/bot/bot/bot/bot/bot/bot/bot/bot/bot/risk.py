import os
import time

from .config import AppConfig
from .database import Database
from .models import TradeRecord
from .telegram_notify import TelegramNotifier
from .utils import LOG, safe_float


class RiskManager:
    def __init__(self, config: AppConfig, db: Database, telegram: TelegramNotifier):
        self.cfg = config
        self.db = db
        self.tg = telegram
        self.mode = config.mode.value
        self.portfolio = None

    def attach_portfolio(self, portfolio):
        self.portfolio = portfolio

    # ---- kill switch
    def kill_active(self) -> bool:
        env = os.environ.get("KILL_SWITCH", "").strip().lower() in {"1", "true", "yes", "on"}
        if env:
            return True
        try:
            if os.path.exists(os.path.join(self.cfg.data_dir, "KILL_SWITCH")):
                return True
        except Exception:
            pass
        return self.db.get_meta(f"kill_switch:{self.mode}") == "1"

    def set_kill(self, on: bool, reason=""):
        prev = self.kill_active()
        self.db.set_meta(f"kill_switch:{self.mode}", "1" if on else "0")
        if on != prev:
            LOG.warning("KILL SWITCH %s (%s)", "ENGAGED" if on else "RELEASED", reason)
            if self.tg.enabled:
                self.tg.send(f"KILL SWITCH {'ENGAGED' if on else 'RELEASED'}\n{reason}")

    # ---- equity bookkeeping
    def equity_now(self) -> float:
        return self.portfolio.equity()

    def _day_key(self):
        return time.strftime("%Y-%m-%d", time.gmtime())

    def roll_day_if_needed(self) -> bool:
        today = self._day_key()
        if self.db.get_meta(f"day:{self.mode}") != today:
            self.db.set_meta(f"day:{self.mode}", today)
            self.db.set_meta(f"day_start_equity:{self.mode}", f"{self.equity_now():.2f}")
            return True
        return False

    def day_start_equity(self) -> float:
        v = safe_float(self.db.get_meta(f"day_start_equity:{self.mode}"), None)
        return v if v else self.equity_now()

    def realized_today(self) -> float:
        return self.db.sum_pnl_since(self.mode, time.time() - (time.time() % 86400))

    def peak_equity(self, current) -> float:
        peak = safe_float(self.db.get_meta(f"peak_equity:{self.mode}"), 0.0) or current
        if current > peak:
            peak = current
            self.db.set_meta(f"peak_equity:{self.mode}", f"{peak:.2f}")
        return peak

    def drawdown_pct(self) -> float:
        eq = self.equity_now()
        peak = self.peak_equity(eq)
        if peak <= 0:
            return 0.0
        return (peak - eq) / peak * 100.0

    # ---- gates
    def entries_blocked_reasons(self) -> list:
        r = []
        if self.kill_active():
            r.append("kill_switch")
        dd = self.drawdown_pct()
        if dd >= self.cfg.risk.max_drawdown_pct:
            r.append(f"max drawdown {dd:.1f}%")
            if dd >= self.cfg.risk.max_drawdown_pct * 1.5:
                self.set_kill(True, f"drawdown {dd:.1f}% >= 1.5x limit")
        dseq = self.day_start_equity()
        if dseq > 0:
            loss_pct = -self.realized_today() / dseq * 100.0
            if loss_pct >= self.cfg.risk.max_daily_loss_pct:
                r.append(f"daily loss {loss_pct:.1f}%")
        cons = int(safe_float(self.db.get_meta(f"cons_losses:{self.mode}"), 0) or 0)
        if cons >= self.cfg.risk.circuit_break_after_losses:
            r.append(f"circuit breaker ({cons} consecutive losses)")
        return r

    def pre_trade_check(self, symbol, side, qty, price, spread_pct=None,
                        quote_volume=None) -> tuple:
        reasons = self.entries_blocked_reasons()
        if reasons:
            return False, "blocked: " + "; ".join(reasons)
        rl = self.cfg.risk
        if spread_pct is not None and spread_pct == spread_pct and spread_pct > rl.max_spread_pct:
            return False, f"spread {spread_pct:.2f}% > {rl.max_spread_pct}%"
        if quote_volume is not None and quote_volume < rl.min_24h_quote_volume_usd:
            return False, "24h volume too low"
        positions = self.portfolio.positions
        if len(positions) >= rl.max_open_positions and symbol not in positions:
            return False, f"max open positions ({rl.max_open_positions})"
        if symbol in positions:
            return False, "position already open on symbol"
        notional = qty * price
        if notional < rl.min_order_quote_usdt:
            return False, f"notional {notional:.2f} < {rl.min_order_quote_usdt}"
        equity = self.equity_now()
        exposure = sum(p.amount * p.entry_price for p in positions.values()) + notional
        if equity > 0 and exposure / equity * 100.0 > rl.max_total_exposure_pct:
            return False, f"exposure cap {rl.max_total_exposure_pct}%"
        return True, "ok"

    def position_size(self, price, atr_value, equity, available_cash) -> tuple:
        rl = self.cfg.risk
        if not price or price <= 0:
            return 0.0, 0.0, "no price"
        if rl.use_atr_stops and atr_value and atr_value > 0:
            stop_dist = atr_value * rl.atr_stop_multiplier
        else:
            stop_dist = price * rl.default_stop_loss_pct / 100.0
        if stop_dist <= 0:
            return 0.0, 0.0, "bad stop distance"
        risk_amount = equity * rl.max_risk_per_trade_pct / 100.0
        qty = risk_amount / stop_dist
        cap_notional = equity * rl.max_position_value_pct / 100.0
        if qty * price > cap_notional:
            qty = cap_notional / price
        if available_cash is not None:
            qty = min(qty, available_cash * 0.98 / price)
        return qty, stop_dist, "ok"

    def register_trade(self, trade: TradeRecord):
        if trade.net_pnl < 0:
            cons = int(safe_float(self.db.get_meta(f"cons_losses:{self.mode}"), 0) or 0) + 1
            self.db.set_meta(f"cons_losses:{self.mode}", str(cons))
            if trade.close_reason in {"stop_loss", "trailing_stop"}:
                cd = time.time() + self.cfg.risk.cooldown_minutes_after_stop * 60
                self.db.set_meta(f"cooldown:{self.mode}:{trade.symbol}", f"{cd:.0f}")
        else:
            self.db.set_meta(f"cons_losses:{self.mode}", "0")

    def cooldown_active(self, symbol) -> bool:
        until = safe_float(self.db.get_meta(f"cooldown:{self.mode}:{symbol}"), 0.0)
        return until > time.time()

    def record_event(self, kind, symbol, detail, notify=True):
        self.db.save_risk_event(self.mode, kind, symbol, detail)
        if notify and self.tg.enabled:
            self.tg.send(f"RISK [{kind}] {symbol}\n{detail}")
