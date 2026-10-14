import time
import pandas as pd
import pytest
import ana

def _mk_swap_pf(bal=4000.0):
    return ana.FuturesPortfolio("paper-swap", bal, 0.001, 5.0)

def test_swap_multiple_positions_tracked():
    pf = _mk_swap_pf()
    pf.open_short("A/USDT", 1.0, 100.0, 0.1, 101.0, 99.0, "t", time.time(), 3)
    pf.open_short("B/USDT", 1.0, 50.0, 0.05, 51.0, 49.0, "t", time.time(), 3)
    pf.open_long("C/USDT", 1.0, 10.0, 0.01, 9.0, 12.0, "t", time.time(), 3)
    assert len(pf.positions) == 3
    assert pf.positions["A/USDT"].side == "short"
    assert pf.positions["C/USDT"].side == "long"

def test_swap_short_pnl_math():
    pf = _mk_swap_pf()
    pf.open_short("A/USDT", 2.0, 100.0, 0.2, 101.0, 99.0, "t", time.time(), 3)
    tr = pf.close_futures("A/USDT", 95.0, 0.1, "tp", time.time())
    assert tr.net_pnl == pytest.approx((100.0 - 95.0) * 2.0 - 0.2 - 0.1, abs=1e-6)
    assert not pf.has("A/USDT")

def test_swap_state_roundtrip_multi():
    pf = _mk_swap_pf(4000.0)
    pf.open_short("A/USDT", 1.0, 100.0, 0.1, 101.0, 99.0, "t", time.time(), 3)
    pf.open_short("B/USDT", 3.0, 50.0, 0.15, 51.0, 49.0, "t", time.time(), 3)
    import json as _json
    data = {"cash": pf.cash, "start": pf.start_balance,
            "positions": {s: {"amount": p.amount, "entry": p.entry_price,
                              "sl": p.stop_loss, "tp": p.take_profit,
                              "opened_at": p.opened_at, "strategy": p.strategy,
                              "side": p.side, "leverage": p.leverage}
                          for s, p in pf.positions.items()}}
    ana.DB.set_meta("swap_paper_state", _json.dumps(data))
    pf2 = _mk_swap_pf(1.0)
    v = ana.DB.get_meta("swap_paper_state")
    d = _json.loads(v)
    pf2.cash = float(d["cash"])
    for s, p in d["positions"].items():
        pf2.positions[s] = ana.FuturesPosition(symbol=s, side=p["side"],
                                               amount=p["amount"],
                                               entry_price=p["entry"],
                                               stop_loss=p["sl"],
                                               take_profit=p["tp"],
                                               opened_at=p["opened_at"],
                                               strategy=p["strategy"],
                                               leverage=p["leverage"])
    assert len(pf2.positions) == 2
    assert abs(pf2.cash - pf.cash) < 1e-6
    assert pf2.positions["B/USDT"].side == "short"
    ana.DB.set_meta("swap_paper_state", "")

def test_spot_cash_not_double_counted():
    pf = ana.Portfolio("paper", 6000.0, 0.001, 5.0)
    pf.open_position("X/USDT", 10.0, 100.0, 1.0, 95.0, 120.0, "t", time.time())
    eq = pf.equity({"X/USDT": 100.0})
    # açılışta cash: 6000 - 1000 - 1(ücret) = 4999; pozisyon değeri: 1000
    # equity = 4999 + 1000 = 5999 (ücret kadar düşer — çifte sayım yok)
    assert eq == pytest.approx(5999.0, abs=0.01)
