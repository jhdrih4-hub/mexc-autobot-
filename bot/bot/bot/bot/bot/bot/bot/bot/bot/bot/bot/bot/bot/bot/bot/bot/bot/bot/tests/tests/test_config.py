import pytest

from bot.config import AppConfig, RiskLimits, TradingMode
from bot.errors import FatalError


def test_default_paper():
    assert AppConfig().mode == TradingMode.PAPER


def test_live_blocked_without_arm(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.delenv("LIVE_ARM", raising=False)
    with pytest.raises(FatalError):
        AppConfig.from_env()


def test_live_blocked_without_keys(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("LIVE_ARM", "yes")
    monkeypatch.delenv("MEXC_API_KEY", raising=False)
    monkeypatch.delenv("MEXC_SECRET", raising=False)
    with pytest.raises(FatalError):
        AppConfig.from_env()


def test_live_ok_when_armed_and_keys(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("LIVE_ARM", "yes")
    monkeypatch.setenv("MEXC_API_KEY", "k" * 16)
    monkeypatch.setenv("MEXC_SECRET", "s" * 16)
    assert AppConfig.from_env().mode == TradingMode.LIVE


def test_risk_env_override(monkeypatch):
    monkeypatch.setenv("RISK_MAX_DAILY_LOSS_PCT", "2.5")
    assert RiskLimits.from_env().max_daily_loss_pct == 2.5
