import hashlib
import time

from .config import AppConfig
from .database import Database
from .errors import TradeBlockedError, TradingSystemError
from .exchange import CcxtAdapter
from .models import OrderRecord, PositionRecord, TradeRecord
from .risk import RiskManager
from .telegram_notify import TelegramNotifier
from .utils import LOG, safe_float


def make_tag(mode, symbol, side, strategy, bucket):
    raw = f"{mode}|{symbol}|{side}|{strategy}|{bucket}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


class PaperExecutor:
    """Simulated fills only. NEVER touches the real exchange."""

    def __init__(self, config: AppConfig, db: Database, portfolio, risk: RiskManager,
                 telegram: TelegramNotifier):
        self.cfg = config
        self.db = db
        self.pf = portfolio
        self.risk = risk
        self.tg = telegram
        self._inflight = {}

    def _dedupe(self, tag):
        now = time.time()
        if tag in self._inflight and now - self._inflight[tag] < 300:
            return False
        if self.db.get_order_by_tag(tag):
            return False
        self._inflight[tag] = now
        return True

    def open_long(self, symbol, qty, price, atr_value, strategy, cycle_bucket):
        tag = make_tag("paper", symbol, "buy", strategy, cycle_bucket)
        if not self._dedupe(tag):
            return None, "duplicate order suppressed"
        slip = price * (1 + self.cfg.paper_slippage_bps / 10000.0)
        fee = qty * slip * self.cfg.paper_fee_rate
        rl = self.cfg.risk
        if rl.use_atr_stops and atr_value:
            stop = slip - atr_value * rl.atr_stop_multiplier
            take = slip + atr_value * rl.atr_target_multiplier
        else:
            stop = slip * (1 - rl.default_stop_loss_pct / 100.0)
            take = slip * (1 + rl.default_take_profit_pct / 100.0)
        pos, msg = self.pf.open_position(symbol, "long", qty, slip, fee, stop, take, strategy)
        if pos is None:
            self._inflight.pop(tag, None)
            return None, msg
        rec = OrderRecord(order_id=f"paper-{tag}", ts=time.time(), mode="paper",
                          exchange="paper", symbol=symbol, side="buy", type="market",
                          amount=qty, price=slip, status="filled", filled=qty,
                          avg_fill_price=slip, fee_cost=fee, fee_currency="USDT",
                          strategy=strategy, client_tag=tag, raw="")
        self.db.upsert_order(rec)
        if self.tg.enabled:
            self.tg.order_filled("BUY", symbol, qty, slip, fee, strategy)
        return pos, "ok"

    def close(self, symbol, price, reason, strategy="risk"):
        pos = self.pf.positions.get(symbol)
        if not pos:
            return None, "no position"
        slip = price * (1 - self.cfg.paper_slippage_bps / 10000.0)
        fee = pos.amount * slip * self.cfg.paper_fee_rate
        tr, msg = self.pf.close_position(symbol, slip, reason, fee)
        if tr is None:
            return None, msg
        tag = make_tag("paper", symbol, "sell", strategy, int(time.time()))
        rec = OrderRecord(order_id=f"paper-{tag}", ts=time.time(), mode="paper",
                          exchange="paper", symbol=symbol, side="sell", type="market",
                          amount=pos.amount, price=slip, status="filled", filled=pos.amount,
                          avg_fill_price=slip, fee_cost=fee, fee_currency="USDT",
                          strategy=strategy, client_tag=tag, raw=reason)
        self.db.upsert_order(rec)
        self.risk.register_trade(tr)
        if self.tg.enabled:
            self.tg.position_closed(symbol, tr)
        return tr, "ok"

    def cancel_all(self, symbol=None):
        pass


class LiveExecutor:
    """Real orders. Requires explicit LIVE authorization (enforced in config)."""

    def __init__(self, config: AppConfig, db: Database, adapter: CcxtAdapter,
                 portfolio, risk: RiskManager, telegram: TelegramNotifier):
        self.cfg = config
        self.db = db
        self.ad = adapter
        self.pf = portfolio
        self.risk = risk
        self.tg = telegram
        self._inflight = {}

    def _dedupe(self, tag):
        if tag in self._inflight:
            return False
        if self.db.get_order_by_tag(tag):
            return False
        self._inflight[tag] = time.time()
        return True

    def _submit(self, symbol, side, qty, ref_price, tag):
        pad = self.cfg.live_slippage_pad_pct / 100.0
        px = ref_price * (1 + pad) if side == "buy" else ref_price * (1 - pad)
        price = self.ad.round_price(symbol, px, side)
        try:
            return self.ad.create_order(symbol, side, "limit", qty, price,
                                        client_tag=tag, params={"timeInForce": "IOC"})
        except TradeBlockedError as e:
            if "IOC" in str(e) or "timeInForce" in str(e):
                LOG.warning("IOC rejected; fallback limit+cancel: %s", e)
            else:
                raise
        rec = self.ad.create_order(symbol, side, "limit", qty, price, client_tag=tag)
        deadline = time.time() + self.cfg.order_timeout_seconds
        while time.time() < deadline:
            time.sleep(2)
            try:
                st = self.ad.fetch_order(rec.order_id, symbol)
                if str(st.get("status")) in {"closed", "canceled", "expired"}:
                    break
            except TradingSystemError:
                continue
        try:
            self.ad.cancel_order(rec.order_id, symbol)
        except Exception:
            pass
        return rec

    def _confirm(self, order_id, symbol, timeout):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                o = self.ad.fetch_order(order_id, symbol)
            except TradingSystemError as e:
                LOG.warning("fetch_order failed: %s", e.user_message)
                time.sleep(2)
                continue
            status = str(o.get("status") or "unknown")
            filled = safe_float(o.get("filled"), 0.0) or 0.0
            avg = safe_float(o.get("average")) or safe_float(o.get("price"))
            if status == "closed" and filled > 0:
                return {"status": "filled", "filled": filled, "avg": avg,
                        "fee": (o.get("fee") or {})}
            if status in {"canceled", "expired", "rejected"}:
                return {"status": status, "filled": filled, "avg": avg, "fee": {}}
            last = {"status": status, "filled": filled, "avg": avg}
            time.sleep(2)
        return last or {"status": "unknown", "filled": 0.0, "avg": None}

    def open_long(self, symbol, qty, price, atr_value, strategy, cycle_bucket):
        if self.pf.state_uncertain:
            return None, "account state uncertain - entries blocked"
        tag = make_tag("live", symbol, "buy", strategy, cycle_bucket)
        if not self._dedupe(tag):
            return None, "duplicate order suppressed"
        try:
            rec = self._submit(symbol, "buy", qty, price, tag)
        except TradingSystemError as e:
            self._inflight.pop(tag, None)
            self.risk.record_event("order_error", symbol, str(e.user_message), notify=False)
            return None, f"submit failed: {e.user_message}"
        conf = self._confirm(rec.order_id, symbol, self.cfg.order_timeout_seconds)
        filled = conf.get("filled", 0.0)
        avg = conf.get("avg") or price
        fee = safe_float((conf.get("fee") or {}).get("cost"), 0.0) or 0.0
        status = conf.get("status", "unknown")
        rec.status, rec.filled, rec.avg_fill_price, rec.fee_cost = status, filled, avg, fee
        self.db.upsert_order(rec)
        if filled <= 0:
            self._inflight.pop(tag, None)
            msg = f"order not filled (status={status})"
            if status == "unknown":
                self.risk.record_event("order_unknown", symbol, msg + " - manual check required")
                self.pf.state_uncertain = True
                return None, msg + " (account marked uncertain)"
            return None, msg
        rl = self.cfg.risk
        if rl.use_atr_stops and atr_value:
            stop = avg - atr_value * rl.atr_stop_multiplier
            take = avg + atr_value * rl.atr_target_multiplier
        else:
            stop = avg * (1 - rl.default_stop_loss_pct / 100.0)
            take = avg * (1 + rl.default_take_profit_pct / 100.0)
        pos = PositionRecord(symbol=symbol, side="long", amount=filled, entry_price=avg,
                             stop_loss=stop, take_profit=take, trail_high=avg,
                             opened_at=time.time(), strategy=strategy, fees_paid=fee)
        self.pf.register_open(pos, fee)
        if self.tg.enabled:
            self.tg.order_filled("BUY", symbol, filled, avg, fee, strategy)
        return pos, "ok"

    def close(self, symbol, price, reason, strategy="risk"):
        pos = self.pf.positions.get(symbol)
        if not pos:
            return None, "no position"
        tag = make_tag("live", symbol, "sell", strategy, int(time.time()))
        if not self._dedupe(tag):
            return None, "duplicate order suppressed"
        try:
            rec = self._submit(symbol, "sell", pos.amount, price, tag)
        except TradingSystemError as e:
            self._inflight.pop(tag, None)
            self.risk.record_event("order_error", symbol, str(e.user_message), notify=False)
            return None, f"submit failed: {e.user_message}"
        conf = self._confirm(rec.order_id, symbol, self.cfg.order_timeout_seconds)
        filled = conf.get("filled", 0.0)
        avg = conf.get("avg") or price
        fee = safe_float((conf.get("fee") or {}).get("cost"), 0.0) or 0.0
        status = conf.get("status", "unknown")
        rec.status, rec.filled, rec.avg_fill_price, rec.fee_cost = status, filled, avg, fee
        self.db.upsert_order(rec)
        if filled <= 0:
            if status == "unknown":
                self.risk.record_event("order_unknown", symbol,
                                       "sell unknown - entries blocked, position kept")
                self.pf.state_uncertain = True
                return None, "sell unknown"
            if status in {"canceled", "expired", "rejected"}:
                self._inflight.pop(tag, None)
            return None, f"sell not filled ({status})"
        gross = (avg - pos.entry_price) * filled
        fees = pos.fees_paid * (filled / pos.amount) + fee
        net = gross - fees
        tr = TradeRecord(opened_at=pos.opened_at, closed_at=time.time(), mode="live",
                         exchange=self.ad.id, symbol=symbol, side=pos.side, amount=filled,
                         entry_price=pos.entry_price, exit_price=avg, gross_pnl=gross,
                         fees=fees, net_pnl=net, strategy=pos.strategy, close_reason=reason)
        self.db.save_trade(tr)
        remaining = pos.amount - filled
        if remaining > 0:
            pos.amount = remaining
            pos.fees_paid = pos.fees_paid * (remaining / pos.amount)
            self.db.upsert_position(pos, "live")
            self.pf.positions[symbol] = pos
        else:
            self.pf.register_close(symbol, tr, fee)
        self.risk.register_trade(tr)
        if self.tg.enabled:
            self.tg.position_closed(symbol, tr)
        return tr, "ok"

    def cancel_all(self, symbol=None):
        try:
            for o in self.ad.fetch_open_orders(symbol):
                try:
                    self.ad.cancel_order(o.get("id"), o.get("symbol"))
                except Exception:
                    pass
        except TradingSystemError as e:
            LOG.warning("cancel_all failed: %s", e.user_message)
