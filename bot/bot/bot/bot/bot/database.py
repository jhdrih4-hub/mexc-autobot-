import json
import os
import sqlite3
import threading
import time

import pandas as pd

from .errors import FatalError
from .models import PositionRecord
from .utils import LOG, safe_float, scrub

SCHEMA_VERSION = 2

SCHEMA_V1 = """
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
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mode TEXT, symbol TEXT, timeframe TEXT,
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
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS positions(
  mode TEXT, symbol TEXT, side TEXT, amount REAL, entry_price REAL,
  stop_loss REAL, take_profit REAL, trail_high REAL, opened_at REAL,
  strategy TEXT, fees_paid REAL, PRIMARY KEY(mode, symbol));
CREATE TABLE IF NOT EXISTS risk_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mode TEXT, kind TEXT,
  symbol TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS model_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, name TEXT, path TEXT,
  metrics TEXT, active INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_tag ON orders(client_tag) WHERE client_tag != '';
CREATE INDEX IF NOT EXISTS idx_trades_mode_closed ON trades(mode, closed_at);
CREATE INDEX IF NOT EXISTS idx_equity_mode_ts ON equity(mode, ts);
"""


class Database:
    def __init__(self, path):
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self._lock = threading.RLock()
            self._migrate()
        except sqlite3.Error as e:
            raise FatalError(f"Cannot open database {path}: {e}")
        LOG.info("SQLite ready: %s (schema v%s)", path, self.version())

    def _migrate(self):
        with self._lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            row = self.conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
            ver = int(row[0]) if row else 0
            if ver < 1:
                self.conn.executescript(SCHEMA_V1)
            if ver < 2:
                self.conn.executescript(SCHEMA_V2)
            self.conn.execute(
                "INSERT INTO meta(k,v) VALUES('schema_version',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(SCHEMA_VERSION),))
            self.conn.commit()

    def version(self):
        return self.get_meta("schema_version", "0")

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql, params=()):
        with self._lock:
            return pd.read_sql_query(sql, self.conn, params=params)

    def checkpoint(self):
        try:
            with self._lock:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.Error:
            pass

    # ---- meta
    def set_meta(self, k, v):
        self._exec("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                   (k, str(v)))

    def get_meta(self, k, default=None):
        r = self._exec("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    # ---- orders
    def upsert_order(self, o):
        try:
            self._exec("""INSERT INTO orders(order_id,ts,mode,exchange,symbol,side,type,amount,
                          price,status,filled,avg_fill_price,fee_cost,fee_currency,
                          strategy,client_tag,raw)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(order_id) DO UPDATE SET status=excluded.status,
                          filled=excluded.filled, avg_fill_price=excluded.avg_fill_price,
                          fee_cost=excluded.fee_cost""",
                       (o.order_id, o.ts, o.mode, o.exchange, o.symbol, o.side, o.type,
                        o.amount, o.price, o.status, o.filled, o.avg_fill_price,
                        o.fee_cost, o.fee_currency, o.strategy, o.client_tag, (o.raw or "")[:3500]))
        except sqlite3.IntegrityError:
            LOG.warning("duplicate client_tag suppressed: %s", o.client_tag)

    def get_order_by_tag(self, tag):
        if not tag:
            return None
        r = self._exec("SELECT order_id FROM orders WHERE client_tag=?", (tag,)).fetchone()
        return r[0] if r else None

    # ---- trades
    def save_trade(self, t):
        self._exec("""INSERT INTO trades(opened_at,closed_at,mode,exchange,symbol,side,amount,
                      entry_price,exit_price,gross_pnl,fees,net_pnl,strategy,close_reason)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (t.opened_at, t.closed_at, t.mode, t.exchange, t.symbol, t.side,
                    t.amount, t.entry_price, t.exit_price, t.gross_pnl, t.fees,
                    t.net_pnl, t.strategy, t.close_reason))

    def trades_df(self, mode, since=0.0):
        return self._query("SELECT * FROM trades WHERE mode=? AND closed_at>=? ORDER BY closed_at",
                           (mode, since))

    def sum_pnl_since(self, mode, since):
        r = self._exec("SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE mode=? AND closed_at>=?",
                       (mode, since)).fetchone()
        return float(r[0]) if r else 0.0

    # ---- positions
    def upsert_position(self, p, mode):
        self._exec("""INSERT INTO positions(mode,symbol,side,amount,entry_price,stop_loss,
                      take_profit,trail_high,opened_at,strategy,fees_paid)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(mode,symbol) DO UPDATE SET side=excluded.side,
                      amount=excluded.amount, entry_price=excluded.entry_price,
                      stop_loss=excluded.stop_loss, take_profit=excluded.take_profit,
                      trail_high=excluded.trail_high, opened_at=excluded.opened_at,
                      strategy=excluded.strategy, fees_paid=excluded.fees_paid""",
                   (mode, p.symbol, p.side, p.amount, p.entry_price, p.stop_loss,
                    p.take_profit, p.trail_high, p.opened_at, p.strategy, p.fees_paid))

    def delete_position(self, symbol, mode):
        self._exec("DELETE FROM positions WHERE mode=? AND symbol=?", (mode, symbol))

    def load_positions(self, mode):
        rows = self._query("SELECT * FROM positions WHERE mode=?", (mode,)).to_dict("records")
        return [PositionRecord(symbol=r["symbol"], side=r["side"], amount=float(r["amount"]),
                               entry_price=float(r["entry_price"]),
                               stop_loss=safe_float(r.get("stop_loss")),
                               take_profit=safe_float(r.get("take_profit")),
                               trail_high=safe_float(r.get("trail_high")),
                               opened_at=float(r.get("opened_at") or 0.0),
                               strategy=r.get("strategy") or "",
                               fees_paid=float(r.get("fees_paid") or 0.0)) for r in rows]

    # ---- decisions / signals / equity
    def save_decision(self, d, mode):
        self._exec("""INSERT INTO decisions(ts,mode,symbol,action,score,confidence,reasons,regime,data_ok)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (d.ts, mode, d.symbol, d.action, d.score, d.confidence,
                    json.dumps(d.reasons, ensure_ascii=False), d.regime, int(d.data_ok)))

    def save_signals(self, symbol, timeframe, sigs, mode):
        with self._lock:
            for s in sigs:
                self.conn.execute(
                    """INSERT INTO signals(ts,mode,symbol,timeframe,strategy,direction,strength,price,reason)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (s.ts, mode, symbol, timeframe, s.strategy, s.direction,
                     s.strength, s.price, s.reason))
            self.conn.commit()

    def save_equity(self, mode, cash, pv, eq, unreal, realized, fees):
        self._exec("""INSERT INTO equity(ts,mode,cash,positions_value,equity,unrealized,realized_total,fees_total)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (time.time(), mode, cash, pv, eq, unreal, realized, fees))

    def equity_df(self, mode):
        return self._query("SELECT * FROM equity WHERE mode=? ORDER BY ts", (mode,))

    # ---- news / risk / models / errors
    def insert_news(self, h, published, source, title, url, sentiment, impact, assets):
        try:
            cur = self._exec("""INSERT OR IGNORE INTO news(hash,published,fetched,source,title,url,
                                sentiment,impact,assets) VALUES(?,?,?,?,?,?,?,?,?)""",
                             (h, published, time.time(), source, title[:300], url,
                              sentiment, impact, json.dumps(assets)))
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def recent_news(self, since):
        return self._query("SELECT * FROM news WHERE published>=? ORDER BY published DESC", (since,))

    def save_risk_event(self, mode, kind, symbol, detail):
        self._exec("INSERT INTO risk_events(ts,mode,kind,symbol,detail) VALUES(?,?,?,?,?)",
                   (time.time(), mode, kind, symbol, scrub(detail)[:500]))

    def save_model_version(self, name, path, metrics, active):
        self._exec("INSERT INTO model_versions(ts,name,path,metrics,active) VALUES(?,?,?,?,?)",
                   (time.time(), name, path, json.dumps(metrics), int(active)))

    def save_error(self, module, message, detail="", level="ERROR"):
        try:
            self._exec("INSERT INTO errors(ts,level,module,message,detail) VALUES(?,?,?,?,?)",
                       (time.time(), level, module, scrub(str(message))[:800], scrub(str(detail))[:2000]))
        except sqlite3.Error:
            pass

    def prune(self, days=14):
        cutoff = time.time() - days * 86400
        for table in ("signals", "decisions", "equity", "errors"):
            self._exec(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        self._exec("DELETE FROM news WHERE published < ?", (cutoff,))
        LOG.info("pruned rows older than %d days", days)
