import time
import pytest
import ana

def test_anchor_zero_string_rejected():
    """'0' metni anchor olarak kabul EDİLMEMELİ (gereksinim #1)."""
    ana.DB.set_meta(ana._day_anchor_key40b(), "0")
    assert ana._restore_day_anchor40b() is None
    ana.DB.set_meta(ana._day_anchor_key40b(), "3950.5")
    assert ana._restore_day_anchor40b() == 3950.5
    ana.DB.set_meta(ana._day_anchor_key40b(), "0")

def test_anchor_not_overwritten_same_day():
    """Gün açıkken update_day_anchor çağrısı anchor'ı EZMEMELİ."""
    rm = ana.SWAP_RISK
    rm._day = str(ana.utc_now().date())
    rm._day_start_equity = 4000.0      # sabit anchor
    ana.DB.set_meta(ana._day_anchor_key40b(), "4000.0")
    rm.update_day_anchor(3850.0)       # düşüş görse de
    assert rm._day_start_equity == 4000.0   # EZİLMEDİ
    assert ana._restore_day_anchor40b() == 4000.0

def test_cancel_order_exists_and_callable_signature():
    assert hasattr(ana.CcxtAdapter, "cancel_order")   # 40.1'den beri
    assert callable(getattr(ana.CcxtAdapter, "cancel_order"))

def test_short_pnl_and_close_removes_position():
    pf = ana.FuturesPortfolio("paper-swap", 4000.0, 0.001, 5.0)
    pf.open_short("A/USDT", 2.0, 100.0, 0.2, 102.0, 98.0, "t", time.time(), 3)
    tr = pf.close_futures("A/USDT", 98.0, 0.1, "tp", time.time())
    assert tr.net_pnl == pytest.approx((100.0 - 98.0) * 2.0 - 0.3, abs=1e-6)
    assert not pf.has("A/USDT")
