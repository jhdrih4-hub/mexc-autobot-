import time

from bot.database import Database
from bot.models import OrderRecord, PositionRecord


def test_meta_roundtrip(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.set_meta("x", "1")
    assert db.get_meta("x") == "1"


def test_schema_version(tmp_path):
    assert Database(str(tmp_path / "t.db")).version() == "2"


def test_order_tag_dedupe(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    o = OrderRecord(order_id="a1", ts=time.time(), mode="paper", exchange="paper",
                    symbol="BTC/USDT", side="buy", type="market", amount=1.0,
                    price=1.0, client_tag="tag1")
    db.upsert_order(o)
    assert db.get_order_by_tag("tag1") == "a1"


def test_positions_roundtrip(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    p = PositionRecord(symbol="BTC/USDT", side="long", amount=0.5, entry_price=100.0,
                       stop_loss=95.0, take_profit=110.0, trail_high=101.0,
                       opened_at=1.0, strategy="x", fees_paid=0.1)
    db.upsert_position(p, "paper")
    pos = db.load_positions("paper")
    assert len(pos) == 1 and pos[0].symbol == "BTC/USDT" and pos[0].amount == 0.5
    db.delete_position("BTC/USDT", "paper")
    assert db.load_positions("paper") == []
