import os
import time

from .config import AppConfig, TradingMode
from .database import Database
from .errors import FatalError, TradingSystemError
from .exchange import CcxtAdapter
from .executor import LiveExecutor, PaperExecutor
from .marketdata import MarketDataEngine
from .ml import MLEngine
from .news import NewsEngine
from .portfolio import LivePortfolio, PaperPortfolio
from .risk import RiskManager
from .strategies import StrategyEngine
from .telegram_notify import TelegramNotifier
from .utils import LOG, TF_SECONDS, clamp, fmt_money, safe_float


class TradingEngine:
    def __init__(self, config: AppConfig):
        self.cfg = config
        os.makedirs(config.data_dir, exist_ok=True)
        self.db = Database(os.path.join(config.data_dir, "trading.db"))
        self.tg = TelegramNotifier(config.telegram_enabled,
                                   config.telegram_token, config.telegram_chat_id)
        self.adapter = CcxtAdapter(config.exchange_id, config.market_type, config.credentials)
        self.md = MarketDataEngine(self.adapter, config)
        self.strategies = StrategyEngine(config)
        self.news = NewsEngine(self.db, config)
        self.ml = MLEngine(config, self.db)
        self.risk = RiskManager(config, self.db, self.tg)
        if config.mode == TradingMode.LIVE:
            self.pf = LivePortfolio(config, self.db, self.adapter, self.tg)
            self.executor = LiveExecutor(config, self.db, self.adapter,
                                         self.pf, self.risk, self.tg)
        else:
            self.pf = PaperPortfolio(config, self.db)
            self.executor = PaperExecutor(config, self.db, self.pf, self.risk, self.tg)
        self.risk.attach_portfolio(self.pf)
        self.universe = []
        self.cycle_no = 0
        self._bucket = 0
        self.failed_cycles = 0
        self._pause_notified = False
        self.start_ts = time.time()

    # ---------- lifecycle
    def startup(self):
        LOG.info("mode=%s exchange=%s type=%s", self.cfg.mode.value,
                 self.cfg.exchange_id, self.cfg.market_type)
        LOG.info("capabilities: %s", {k: v for k, v in self.adapter.capability_report().items() if v})
        self.adapter.load_markets()
        if self.cfg.mode == TradingMode.LIVE:
            self.pf.reconcile()
            LOG.info("live reconcile: %s", self.pf.summary())
        self.universe = self._discover_universe(initial=True)
        self.md.warm_up(self.universe, min_candles=210)
        if self.cfg.ml_enabled:
            df_map = {}
            for s in self.universe[:10]:
                try:
                    df_map[s] = self.md.get_ohlcv(s, self.cfg.primary_timeframe)
                except TradingSystemError:
                    pass
            self.ml.maybe_train(df_map)
        lines = [f"mode: {self.cfg.mode.value}",
                 f"universe ({len(self.universe)}): {', '.join(self.universe)}",
                 f"equity: {fmt_money(self.pf.equity())} {self.cfg.base_currency}",
                 f"risk: pos<={self.cfg.risk.max_open_positions} "
                 f"risk/trade={self.cfg.risk.max_risk_per_trade_pct}% "
                 f"daily_loss<={self.cfg.risk.max_daily_loss_pct}%"]
        if self.tg.enabled:
            self.tg.started(lines)
        LOG.info("startup complete: %s", " | ".join(lines))

    def run(self):
        self.startup()
        deadline = (time.time() + self.cfg.max_runtime_minutes * 60
                    if self.cfg.max_runtime_minutes > 0 else None)
        while True:
            t0 = time.monotonic()
            try:
                self.cycle()
                self.failed_cycles = 0
                self._pause_notified = False
            except FatalError:
                raise
            except Exception as e:
                self.failed_cycles += 1
                LOG.exception("cycle error: %s", e)
                self.db.save_error("engine", f"cycle: {e}")
                if self.tg.enabled:
                    self.tg.send(f"cycle error: {type(e).__name__}: {e}")
                if self.failed_cycles >= 3 and not self._pause_notified:
                    self._pause_notified = True
                    self.risk.record_event("engine_pause", "",
                                           "3 failed cycles - new entries paused")
            if deadline and time.time() >= deadline - 180:
                LOG.info("runtime limit approaching - graceful stop")
                break
            if self.cfg.max_cycles and self.cycle_no >= self.cfg.max_cycles:
                break
            elapsed = time.monotonic() - t0
            time.sleep(max(5.0, self.cfg.poll_seconds - elapsed))
        self.shutdown()

    def shutdown(self):
        if self.cfg.mode == TradingMode.LIVE:
            self.executor.cancel_all()
        if self.cfg.close_on_stop:
            for sym in list(self.pf.positions.keys()):
                try:
                    t = self.md.get_ticker(sym)
                    self.executor.close(sym, t.bid or t.last, "shutdown")
                except TradingSystemError as e:
                    LOG.warning("shutdown close %s failed: %s", sym, e.user_message)
        self.pf.equity_point()
        self.db.checkpoint()
        if self.tg.enabled:
            self.tg.stopped([f"cycles={self.cycle_no}", self.pf.summary()])
        LOG.info("shutdown: %s", self.pf.summary())

    # ---------- universe
    def _discover_universe(self, initial=False):
        try:
            syms = self.adapter.top_usdt_symbols(self.cfg.top_n_symbols,
                                                 self.cfg.risk.min_24h_quote_volume_usd)
        except TradingSystemError as e:
            LOG.warning("universe discovery failed: %s", e.user_message)
            syms = []
        if self.cfg.symbols:
            syms = self.cfg.symbols
        if not syms:
            syms = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
            LOG.warning("fallback universe: %s", syms)
        prev = [s for s in (self.db.get_meta("universe") or "").split(",") if s]
        added = [s for s in syms if s not in prev]
        removed = [s for s in prev if s not in syms]
        if (added or removed) and not initial:
            LOG.info("universe updated: +%s -%s", added, removed)
            if self.tg.enabled:
                self.tg.send(f"Universe updated\n+{', '.join(added) or '-'}\n"
                             f"-{', '.join(removed) or '-'}")
        self.db.set_meta("universe", ",".join(syms))
        self.db.set_meta("universe_ts", str(time.time()))
        return syms

    # ---------- main cycle
    def cycle(self):
        self.cycle_no += 1
        if self.risk.roll_day_if_needed() and self.cycle_no > 1:
            self._send_daily_report()
        uts = safe_float(self.db.get_meta("universe_ts"), 0.0)
        if time.time() - uts > self.cfg.universe_refresh_hours * 3600:
            self.universe = self._discover_universe()
        needed = list(set(self.universe) | set(self.pf.positions.keys()))
        price_map = {}
        for s, t in self.md.ticker_map(needed).items():
            if t.last:
                price_map[s] = t.last
        self.pf.set_prices(price_map)
        self._manage_positions()
        self.news.refresh()
        self._bucket = int(time.time() // max(self.cfg.poll_seconds, 1))
        blocked = self.risk.entries_blocked_reasons()
        for symbol in self.universe:
            try:
                self._evaluate_symbol(symbol, entries_blocked=bool(blocked))
            except TradingSystemError as e:
                LOG.info("skip %s: %s", symbol, e.user_message)
            except Exception as e:
                LOG.exception("eval %s failed: %s", symbol, e)
        self.pf.equity_point()
        if self.cfg.mode == TradingMode.LIVE and \
                self.cycle_no % self.cfg.reconcile_every_cycles == 0:
            try:
                self.pf.reconcile()
            except TradingSystemError as e:
                LOG.error("reconcile failed: %s", e.user_message)
        if self.cycle_no % 1440 == 0:
            self.db.prune(days=14)
        if self.cycle_no % 10 == 0:
            LOG.info("cycle %d done: %s", self.cycle_no, self.pf.summary())

    def _evaluate_symbol(self, symbol, entries_blocked):
        df = self.md.get_ohlcv(symbol, self.cfg.primary_timeframe, min_candles=210)
        df_high = None
        try:
            df_high = self.md.get_ohlcv(symbol, self.cfg.higher_timeframe,
                                        limit=210, min_candles=60)
        except TradingSystemError:
            pass
        news_sent = self.news.sentiment_for(symbol)
        tilt = clamp(self.ml.tilt(df) + news_sent * self.cfg.news_tilt_max +
                     self.news.last_macro * 0.05, -0.2, 0.2)
        decision, sigs, ind = self.strategies.compute_full(symbol, df, df_high, ml_tilt=tilt)
        self.db.save_signals(symbol, self.cfg.primary_timeframe, sigs, self.pf.mode)
        self.db.save_decision(decision, self.pf.mode)
        pos = self.pf.positions.get(symbol)
        if pos:
            self._maybe_exit_on_signal(symbol, decision)
            return
        if entries_blocked or decision.action != "buy":
            return
        self._try_enter(symbol, decision, ind)

    def _try_enter(self, symbol, decision, ind):
        if self.risk.cooldown_active(symbol):
            return
        t = self.md.get_ticker(symbol)
        if not (t.last and t.ask):
            return
        try:
            book = self.md.get_book_stats(symbol)
        except TradingSystemError:
            book = {}
        qty, _, msg = self.risk.position_size(t.last, ind.get("atr"),
                                              self.pf.equity(), self.pf.available_cash())
        if qty <= 0:
            LOG.info("size=0 %s: %s", symbol, msg)
            return
        ok, why = self.risk.pre_trade_check(symbol, "buy", qty, t.last,
                                            book.get("spread_pct"), t.quote_volume)
        if not ok:
            if why.startswith("blocked"):
                self.risk.record_event("entry_rejected", symbol, why, notify=False)
            else:
                LOG.info("entry skip %s: %s", symbol, why)
            return
        cons = self.adapter.order_constraints(symbol)
        qty = self.adapter.round_amount(symbol, qty)
        min_cost = max(cons["cost_min"], self.cfg.risk.min_order_quote_usdt)
        if qty <= 0 or qty * t.last < min_cost:
            LOG.info("dust skip %s qty=%.8f", symbol, qty)
            return
        pos, msg = self.executor.open_long(symbol, qty, t.last, ind.get("atr"),
                                           "composite", self._bucket)
        if pos:
            LOG.info("OPEN %s qty=%.8f @ %.6f SL=%.6f TP=%.6f",
                     symbol, pos.amount, pos.entry_price,
                     pos.stop_loss or 0, pos.take_profit or 0)
        else:
            LOG.info("open failed %s: %s", symbol, msg)

    def _maybe_exit_on_signal(self, symbol, decision):
        if decision.action == "sell":
            t = self.md.get_ticker(symbol)
            px = t.bid or t.last
            tr, msg = self.executor.close(symbol, px, "signal_exit")
            if tr:
                LOG.info("exit by signal %s net=%.2f", symbol, tr.net_pnl)
            else:
                LOG.info("signal exit failed %s: %s", symbol, msg)

    def _manage_positions(self):
        rl = self.cfg.risk
        kill = self.risk.kill_active()
        for symbol, pos in list(self.pf.positions.items()):
            try:
                df = self.md.get_ohlcv(symbol, self.cfg.primary_timeframe,
                                       limit=60, min_candles=1)
                t = self.md.get_ticker(symbol)
            except TradingSystemError as e:
                LOG.warning("manage %s: data unavailable (%s) - holding", symbol, e.user_message)
                continue
            hi = float(df["high"].iloc[-1])
            lo = float(df["low"].iloc[-1])
            px = t.last or float(df["close"].iloc[-1])
            if pos.trail_high is None or px > pos.trail_high:
                pos.trail_high = px
                self.db.upsert_position(pos, self.pf.mode)
            if pos.stop_loss and lo <= pos.stop_loss:
                self.executor.close(symbol, pos.stop_loss, "stop_loss")
                continue
            if pos.take_profit and hi >= pos.take_profit:
                self.executor.close(symbol, pos.take_profit, "take_profit")
                continue
            if pos.trail_high and rl.trail_activation_pct > 0:
                profit_pct = (pos.trail_high - pos.entry_price) / pos.entry_price * 100.0
                if profit_pct >= rl.trail_activation_pct:
                    trail_stop = pos.trail_high * (1 - rl.trail_distance_pct / 100.0)
                    if px <= trail_stop:
                        self.executor.close(symbol, trail_stop, "trailing_stop")
                        continue
            if rl.max_hold_bars > 0 and \
                    time.time() - pos.opened_at > rl.max_hold_bars * TF_SECONDS[self.cfg.primary_timeframe]:
                self.executor.close(symbol, px, "time_stop")
                continue
            if kill and self.cfg.close_on_kill:
                self.executor.close(symbol, px, "kill_switch")
                continue

    def _send_daily_report(self):
        mode = self.pf.mode
        now = time.time()
        midnight = now - (now % 86400)
        df = self.db.trades_df(mode, since=midnight - 86400)
        n = len(df)
        net = float(df["net_pnl"].sum()) if n else 0.0
        wins = int((df["net_pnl"] > 0).sum()) if n else 0
        lines = [f"day: {time.strftime('%Y-%m-%d', time.gmtime(midnight - 86400))}",
                 f"trades: {n} (wins {wins})",
                 f"net PnL: {net:+.2f} USDT",
                 f"equity: {fmt_money(self.pf.equity())}",
                 f"drawdown: {self.risk.drawdown_pct():.1f}%"]
        if self.tg.enabled:
            self.tg.daily(lines)
