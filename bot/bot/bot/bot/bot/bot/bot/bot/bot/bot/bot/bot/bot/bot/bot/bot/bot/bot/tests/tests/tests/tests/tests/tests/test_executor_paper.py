import pytest

from bot.config import AppConfig, TradingMode
from bot.database import Database
from bot.executor import PaperExecutor
from bot.portfolio import PaperPortfolio
from bot.risk import RiskManager
from bot.telegram_notify import TelegramNotifier


@pytest.fixture
def setup(tmp_path):
    cfg = AppConfig()
    cfg.mode = TradingMode.PAPER
    cfg.data_dir = str(tmp_path)
    db = Database(str(tmp_path / "t.db"))
    tg = TelegramNotifier(False)
    pf = PaperPortfolio(cfg, db)
    rm = RiskManager(cfg, db, tg)
    rm.attach_portfolio(pf)
    return cfg, pf, rm, PaperExecutor(cfg, db, pf, rm, tg)


def test_open_close_paper(setup):
    cfg, pf, rm, ex = setup
    pos, _ = ex.open_long("BTC/USDT", 0.1, 100.0, 1.0, "test", 1)
    assert pos is not None and "BTC/USDT" in pf.positions
    assert pf.cash < cfg.paper_start_balance
    tr, _ = ex.close("BTC/USDT", 110.0, "test_exit")
    assert tr is not None and tr.net_pnl > 0
    assert "BTC/USDT" not in pf.positions


def test_duplicate_suppressed(setup):
    _, _, _, ex = setup
    ex.open_long("BTC/USDT", 0.1, 100.0, 1.0, "test", 1)
    pos2, msg2 = ex.open_long("BTC/USDT", 0.1, 100.0, 1.0, "test", 1)
    assert pos2 is None and "duplicate" in msg2


def test_close_without_position(setup):
    _, _, _, ex = setup
    tr, _ = ex.close("BTC/USDT", 100.0, "x")
    assert tr is None
