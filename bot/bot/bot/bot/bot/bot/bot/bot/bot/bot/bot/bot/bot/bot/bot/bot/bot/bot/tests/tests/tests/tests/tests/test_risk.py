import pytest

from bot.config import AppConfig, TradingMode
from bot.database import Database
from bot.portfolio import PaperPortfolio
from bot.risk import RiskManager
from bot.telegram_notify import TelegramNotifier


@pytest.fixture
def env(tmp_path):
    cfg = AppConfig()
    cfg.mode = TradingMode.PAPER
    cfg.data_dir = str(tmp_path)
    db = Database(str(tmp_path / "t.db"))
    tg = TelegramNotifier(False)
    pf = PaperPortfolio(cfg, db)
    rm = RiskManager(cfg, db, tg)
    rm.attach_portfolio(pf)
    return cfg, db, pf, rm


def test_kill_switch_blocks(env):
    _, _, _, rm = env
    rm.set_kill(True, "test")
    assert any("kill" in r for r in rm.entries_blocked_reasons())


def test_position_size_caps(env):
    _, _, _, rm = env
    qty, _, _ = rm.position_size(100.0, 1.0, 10_000.0, 10_000.0)
    assert qty == pytest.approx(20.0, rel=0.01)  # capped by 20% position value


def test_pre_trade_rejects_existing_position(env):
    _, _, pf, rm = env
    pf.open_position("BTC/USDT", "long", 0.1, 100.0, 0.01, 95.0, 110.0, "t")
    ok, _ = rm.pre_trade_check("BTC/USDT", "buy", 0.1, 100.0)
    assert not ok
