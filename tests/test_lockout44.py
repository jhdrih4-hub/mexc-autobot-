import time
import pytest
import ana

def test_lockout_expires_after_duration():
    ana.DB.set_meta(f"lockout:swap:{ana.utc_now().date()}", "1")
    ana.DB.set_meta(f"lockout:swap:expire:{ana.utc_now().date()}",
                    str(time.time() - 10))    # 10 sn önce bitti
    assert ana.SWAP_RISK._lockout_flag() is False   # süre doldu → açıl
    # bayrak temizlenmiş mi?
    assert ana.DB.get_meta(f"lockout:swap:{ana.utc_now().date()}") != "1"

def test_lockout_active_within_duration():
    ana.DB.set_meta(f"lockout:swap:{ana.utc_now().date()}", "1")
    ana.DB.set_meta(f"lockout:swap:expire:{ana.utc_now().date()}",
                    str(time.time() + 3600))    # 1 saat sonra biter
    assert ana.SWAP_RISK._lockout_flag() is True
    ana.DB.set_meta(f"lockout:swap:{ana.utc_now().date()}", "0")

def test_spot_isolated_from_swap_lockout():
    ana.DB.set_meta(f"lockout:swap:{ana.utc_now().date()}", "1")
    ana.DB.set_meta(f"lockout:swap:expire:{ana.utc_now().date()}",
                    str(time.time() + 3600))
    pf = ana.Portfolio("paper", 10_000.0, 0.001, 5.0)
    ok, reasons = ana.RISK.pre_trade_checks(
        "BTC/USDT", {"fresh": True, "last": 100.0, "spread_pct": 0.1,
                     "quote_volume_24h": 1e9},
        {"atr_pct": 1.0}, pf, {"BTC/USDT": 100.0})
    assert ok is True   # spot bağımsız ✓
    ana.DB.set_meta(f"lockout:swap:{ana.utc_now().date()}", "0")

def test_day_change_resets():
    d_key = f"lockout:swap:{ana.utc_now().date()}"
    e_key = f"lockout:swap:expire:{ana.utc_now().date()}"
    ana.DB.set_meta(d_key, "1")
    ana.DB.set_meta(e_key, str(time.time() + 3600))
    # UTC günü değişince anahtar değişecek → yeni anahtar boş → kilit yok
    assert ana.SWAP_RISK._lockout_flag() is True
    # simülasyon: yeni gün
    ana.SWAP_RISK._lockout_key44 = ana._types44.MethodType(
        lambda self, tag: f"lockout:{tag}:2099-01-01", ana.SWAP_RISK)
    assert ana.SWAP_RISK._lockout_flag() is False
