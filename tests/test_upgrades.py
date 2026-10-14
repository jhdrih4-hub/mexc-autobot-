# Opsiyonel test dosyasi. Yerelde calistirma: python -m pytest tests/ -q
# Internet gerektirir (ana importu MEXC'e baglanir).
import time
import pytest
import ana

def _snap():
    return {"fresh": True, "last": 100.0, "spread_pct": 0.1, "quote_volume_24h": 1e9}

def test_kill_switch_blocks():
    ana.DB.set_meta("kill_switch", "1")
    ok, reasons = ana.RISK.pre_trade_checks("BTC/USDT", _snap(), {},
                                            ana.Portfolio("paper", 1000.0, 0.001, 5.0), {})
    assert not ok and "KILL SWITCH aktif" in reasons[0]
    ana.DB.set_meta("kill_switch", "0")

def test_paper_mode_never_calls_live_order(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("PAPER modda canli emir cagirildi!")
    monkeypatch.setattr(ana.ADAPTER, "create_order", _boom)
    pf = ana.Portfolio("paper", 10_000.0, 0.001, 5.0)
    ex = ana.ExecutionEngineV2(ana.ADAPTER, pf, ana.CONFIG, ana.DB)
    pos = ex.market_buy("BTC/USDT", 0.001, 50000.0, 100.0)
    assert pos is not None and pf.has("BTC/USDT")
    tr = ex.market_close("BTC/USDT", 50000.0, "test")
    assert tr is not None
    ana.DB._exec("DELETE FROM trade_journal WHERE mode='paper' AND exit_reason='test'")

def test_scalar_never_exceeds_one():
    ana.MACRO.cfg.enabled = True
    ana.MACRO.data = {"fng": 15, "dxy_chg20": 3.0, "spx_below_sma50": True,
                      "us10y_chg20": 0.5, "btc_dom": 55.0, "dom_chg_1d": 2.0}
    ana.MACRO.last_refresh = time.time()
    s = ana.MACRO.get_scalar()
    assert 0.30 <= s <= 1.0 and s < 1.0
