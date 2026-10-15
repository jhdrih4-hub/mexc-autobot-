import time
import pytest
import ana

def test_lockout_isolated_per_engine():
    # swap kilidi spot'u etkilememeli
    ana.DB.set_meta(f"lockout:swap:{ana.utc_now().date()}", "1")
    pf = ana.Portfolio("paper", 10_000.0, 0.001, 5.0)
    ok_spot, _ = ana.RISK.pre_trade_checks(
        "BTC/USDT", {"fresh": True, "last": 100.0, "spread_pct": 0.1,
                     "quote_volume_24h": 1e9},
        {"atr_pct": 1.0}, pf, {"BTC/USDT": 100.0})
    ok_swap, r_swap = ana.SWAP_RISK.pre_trade_checks(
        "BTC/USDT", {"fresh": True, "last": 100.0, "spread_pct": 0.1,
                     "quote_volume_24h": 1e9},
        {"atr_pct": 1.0}, pf, {"BTC/USDT": 100.0})
    assert ok_spot is True          # spot serbest
    assert ok_swap is False         # swap kilitli
    ana.DB.set_meta(f"lockout:swap:{ana.utc_now().date()}", "0")

def test_daily_loss_pct_math():
    rm = ana.RiskManager(ana.CONFIG, ana.DB)
    rm._day_start_equity = 4000.0
    assert rm.daily_loss_pct(4000.0) == 0.0
    assert rm.daily_loss_pct(3880.0) == pytest.approx(3.0, abs=0.01)
    assert rm.daily_loss_pct(4100.0) == 0.0   # kâr zarar sayılmaz

def test_anchor_new_day_resets():
    rm = ana.RiskManager(ana.CONFIG, ana.DB)
    rm._day = "2000-01-01"; rm._day_start_equity = 5000.0
    rm.update_day_anchor(3900.0)              # yeni gün → anchor yenilenir
    assert rm._day != "2000-01-01"
    assert rm._day_start_equity == 3900.0     # eski değer TAŞINMAZ (gereksinim #8)
    assert rm.daily_loss_pct(3900.0) == 0.0
