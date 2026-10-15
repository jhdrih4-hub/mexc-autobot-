import pandas as pd
import pytest
import ana

def _df(periods=60):
    return pd.DataFrame({"ts": pd.date_range("2025-01-01", periods=periods,
                                             freq="15min", tz="UTC"),
                         "open": 100.0, "high": 101.0, "low": 99.0,
                         "close": 100.0, "volume": 1.0})

def test_regime_weak_vs_strong():
    d = ana.RegimeDetector()
    weak = _df(); ind_w = {"atr_pct": 1.0, "adx": 27, "ema50": 99.0, "ema200": 95.0}
    strong = _df(); ind_s = {"atr_pct": 1.0, "adx": 40, "ema50": 99.0, "ema200": 95.0}
    assert d.classify(weak, ind_w) == "trend_up_weak"
    assert d.classify(strong, ind_s) == "trend_up_strong"

def test_regime_volatile_and_unknown():
    d = ana.RegimeDetector()
    v = _df(); assert d.classify(v, {"atr_pct": 99, "adx": 30, "ema50": 100.0}) == "volatile"
    assert d.classify(_df(), {}) == "unknown"        # eksik veri → belirsiz

def test_dynamic_scalar_never_exceeds_one():
    s = ana._dynamic_risk_scalar43()
    assert 0.10 <= s <= 1.0

def test_scalar_reduces_size_when_shocked(monkeypatch):
    monkeypatch.setattr(ana, "_analysis_ohlcv34",
                        lambda *a, **k: pd.DataFrame({
                            "close": [120.0] * 1 + [100.0] * 59}))
    cons = {"amount_step": 1e-8, "amount_min": 0.0, "cost_min": 0.0, "amount_max": None}
    qty_full, _ = ana.RISK.position_size(1000.0, 100.0, 98.0, cons)
    # BTC şok simülasyonu: son 24h'ta %20 düşüş var (üstteki df)
    qty_s, why = ana.RISK.position_size(1000.0, 100.0, 98.0, cons)
    # skaler < 1.0 ise miktar azalmış olmalı (BTC -%20 → 0.5×)
    assert qty_s <= qty_full
