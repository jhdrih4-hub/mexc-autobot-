import time

from .config import AppConfig
from .database import Database
from .errors import TradingSystemError
from .exchange import CcxtAdapter
from .models import PositionRecord, TradeRecord
from .telegram_notify import TelegramNotifier
from .utils import LOG, safe_float


class PaperPortfolio:
    mode = "paper"

    def __init__(self, config: AppConfig, db: Database):
        self.cfg = config
        self.db = db
        bal = safe_float(db.get_meta("paper:cash"), None)
        if bal is None:
            bal = config.paper_start_balance
            db.set_meta("paper:cash", f"{bal:.2f}")
            db.set_meta("paper:started", f"{bal:.2f}")
        self.cash = bal
        self.positions = {p.symbol: p for p in db.load_positions("paper")}
        self.realized_total = safe_float(db.get_meta("paper:realized"), 0.0)
        self.fees_total = safe_float(db.get_meta("paper:fees"), 0.0)
        self._prices = {}

    def set_prices(self, pm):
        self._prices.update(pm)

    def available_cash(self):
        return self.cash

    def _pv(self):
        return sum(p.amount * (self._prices.get(p.symbol) or p.entry_price)
                   for p in self.positions.values())

    def equity(self):
        return self.cash + self._pv()

    def unrealized(self):
        tot = 0.0
        for p in self.positions.values():
            m = self._prices.get(p.symbol)
            if m:
                tot += (m - p.entry_price) * p.amount
        return tot

    def open_position(self, symbol, side, qty, price, fee, stop_loss, take_profit, strategy):
        cost = qty * price + fee
        if cost > self.cash + 1e-9:
            return None, "insufficient cash"
        self.cash -= cost
        self.fees_total += fee
        pos = PositionRecord(symbol=symbol, side=side, amount=qty, entry_price=price,
                             stop_loss=stop_loss, take_profit=take_profit, trail_high=price,
                             opened_at=time.time(), strategy=strategy, fees_paid=fee)
        self.positions[symbol] = pos
        self.db.upsert_position(pos, self.mode)
        self.db.set_meta("paper:cash", f"{self.cash:.2f}")
        self.db.set_meta("paper:fees", f"{self.fees_total:.2f}")
        return pos, "ok"

    def close_position(self, symbol, price, reason, fee):
        pos = self.positions.get(symbol)
        if not pos:
            return None, "no position"
        gross = (price - pos.entry_price) * pos.amount
        fees = pos.fees_paid + fee
        net = gross - fees
        self.cash += pos.amount * price - fee
        self.fees_total += fee
        self.realized_total += net
        tr = TradeRecord(opened_at=pos.opened_at, closed_at=time.time(), mode=self.mode,
                         exchange="paper", symbol=symbol, side=pos.side, amount=pos.amount,
                         entry_price=pos.entry_price, exit_price=price,
                         gross_pnl=gross, fees=fees, net_pnl=net,
                         strategy=pos.strategy, close_reason=reason)
        self.db.save_trade(tr)
        self.db.delete_position(symbol, self.mode)
        del self.positions[symbol]
        self.db.set_meta("paper:cash", f"{self.cash:.2f}")
        self.db.set_meta("paper:realized", f"{self.realized_total:.2f}")
        self.db.set_meta("paper:fees", f"{self.fees_total:.2f}")
        return tr, "ok"

    def equity_point(self):
        self.db.save_equity(self.mode, self.cash, self._pv(), self.equity(),
                            self.unrealized(), self.realized_total, self.fees_total)

    def summary(self):
        return (f"cash={self.cash:.2f} equity={self.equity():.2f} "
                f"positions={len(self.positions)} realized={self.realized_total:.2f} "
                f"fees={self.fees_total:.2f}")


class LivePortfolio:
    mode = "live"

    def __init__(self, config: AppConfig, db: Database, adapter: CcxtAdapter,
                 telegram: TelegramNotifier):
        self.cfg = config
        self.db = db
        self.adapter = adapter
        self.tg = telegram
        self.cash = 0.0
        self.state_uncertain = True
        self.positions = {p.symbol: p for p in db.load_positions("live")}
        self._prices = {}
        self.realized_total = safe_float(db.get_meta("live:realized"), 0.0)
        self.fees_total = 0.0

    def reconcile(self):
        try:
            bal = self.adapter.fetch_balance()
        except TradingSystemError as e:
            self.state_uncertain = True
            raise TradingSystemError(f"balance fetch failed: {e.user_message}")
        quote = self.cfg.base_currency
        self.cash = bal["free"].get(quote, 0.0)
        self.state_uncertain = False
        self.db.set_meta("live:last_balance_ts", str(time.time()))
        desync = []
        for sym, pos in list(self.positions.items()):
            base = sym.split("/")[0]
            held = bal["free"].get(base, 0.0) + bal["used"].get(base, 0.0)
            if held < pos.amount * 0.999:
                desync.append(f"{sym}: db={pos.amount:.8f} exchange={held:.8f}")
        if desync:
            self.state_uncertain = True
            msg = "position/balance desync: " + "; ".join(desync)
            LOG.error(msg)
            if self.tg.enabled:
                self.tg.send("DESYNC ALERT\n" + msg)
        return bal

    def set_prices(self, pm):
        self._prices.update(pm)

    def available_cash(self):
        return 0.0 if self.state_uncertain else self.cash

    def _pv(self):
        return sum(p.amount * (self._prices.get(p.symbol) or p.entry_price)
                   for p in self.positions.values())

    def equity(self):
        return self.cash + self._pv()

    def unrealized(self):
        tot = 0.0
        for p in self.positions.values():
            m = self._prices.get(p.symbol)
            if m:
                tot += (m - p.entry_price) * p.amount
        return tot

    def register_open(self, pos, fee):
        self.positions[pos.symbol] = pos
        self.db.upsert_position(pos, self.mode)
        self.fees_total += fee

    def register_close(self, symbol, trade: TradeRecord, fee):
        self.positions.pop(symbol, None)
        self.db.delete_position(symbol, self.mode)
        self.realized_total += trade.net_pnl
        self.fees_total += fee
        self.db.set_meta("live:realized", f"{self.realized_total:.2f}")

    def equity_point(self):
        self.db.save_equity(self.mode, self.cash, self._pv(), self.equity(),
                            self.unrealized(), self.realized_total, self.fees_total)

    def summary(self):
        return (f"cash={self.cash:.2f} equity={self.equity():.2f} "
                f"positions={len(self.positions)} uncertain={self.state_uncertain}")
