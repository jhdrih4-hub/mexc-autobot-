import time
import pytest
import ana

S = {"fresh": True, "last": 100.0, "spread_pct": 0.1, "quote_volume_24h": 1e9}

def test_spot_paper_buy_close_accounting():
    pf = ana.Portfolio("paper", 10_000.0, 0.001, 5.0)
    ex = ana.ExecutionEngineV2(ana.ADAPTER, pf, ana.CONFIG, ana.DB)
    pos = ex.market_buy("BTC/USDT", 0.001, 50000.0, 100.0)
    assert pos is not None and pf.has("BTC/USDT")
    before = pf.equity({"BTC/USDT": 50000.0})
    tr = ex.market_close("BTC/USDT", 50000.0, "test")
    assert tr is not None and not pf.has("BTC/USDT")
    assert abs(pf.equity({}) - (before - 0.0)) < 1.0
    ana.DB._exec("DELETE FROM trade_journal WHERE mode='paper' AND exit_reason='test'")

def test_duplicate_journal_uid():
    uid = f"t-{time.time()}"
    def ins():
        ana.DB._exec("INSERT OR IGNORE INTO trade_journal(trade_uid,mode,exchange,symbol,"
                     "market_type,strategy,strategy_version,net_pnl,simulated,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (uid, "paper", "paper", "T/USDT", "spot", "t", "v1", 1.0, 1, time.time()))
    ins(); ins()
    n = ana.DB._exec("SELECT COUNT(*) FROM trade_journal WHERE trade_uid=?", (uid,)).fetchone()[0]
    assert n == 1
    ana.DB._exec("DELETE FROM trade_journal WHERE trade_uid=?", (uid,))

def test_swap_lockout_does_not_block_spot():
    ana.DB.set_meta(f"lockout:swap:{ana.utc_now().date()}", "1")
    ok, reasons = ana.RISK.pre_trade_checks(
        "BTC/USDT", S, {"atr_pct": 1.0},
        ana.Portfolio("paper", 10_000.0, 0.001, 5.0), {"BTC/USDT": 100.0})
    assert not any("KİLİTLİ" in r or "günlük zarar" in r for r in reasons)
    ana.DB.set_meta(f"lockout:swap:{ana.utc_now().date()}", "0")

def test_kill_switch_blocks_both_engines():
    ana.DB.set_meta("kill_switch", "1")
    pf = ana.Portfolio("paper", 10_000.0, 0.001, 5.0)
    ok1, r1 = ana.RISK.pre_trade_checks("BTC/USDT", S, {"atr_pct": 1.0}, pf, {"BTC/USDT": 100.0})
    ok2, r2 = ana.SWAP_RISK.pre_trade_checks("BTC/USDT", S, {"atr_pct": 1.0}, pf, {"BTC/USDT": 100.0})
    assert not ok1 and not ok2
    ana.DB.set_meta("kill_switch", "0")

def test_state_roundtrip():
    pf = ana.Portfolio("paper", 9_500.0, 0.001, 5.0)
    bot = ana.TradingBotV2(ana.CONFIG, pf,
                           ana.ExecutionEngineV2(ana.ADAPTER, pf, ana.CONFIG, ana.DB))
    bot.pf.positions["T/USDT"] = ana.Position(symbol="T/USDT", side="long", amount=2.0,
                                              entry_price=10.0, stop_loss=9.0,
                                              take_profit=12.0, opened_at=time.time())
    bot._save_paper_state()
    pf2 = ana.Portfolio("paper", 1.0, 0.001, 5.0)
    bot2 = ana.TradingBotV2(ana.CONFIG, pf2,
                            ana.ExecutionEngineV2(ana.ADAPTER, pf2, ana.CONFIG, ana.DB))
    bot2._load_paper_state()
    assert abs(pf2.cash - 9_500.0) < 1e-6 and pf2.has("T/USDT")
    ana.DB.set_meta("paper_state", "")

def test_single_swap_gate_active():
    assert ana.SwapEngineRunner.step.__name__ == "_swap_step_29c"
