import os
import tempfile

from src.database import Database


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Database(path), path


def test_trades_since_returns_only_newer_opens():
    db, path = _db()
    try:
        id1 = db.record_trade({"ticker": "T1", "side": "yes", "action": "open",
                               "price": 0.4, "contracts": 3, "fee": 0.01,
                               "cash_flow": -1.2, "mode": "paper", "position_id": "T1:yes"})
        db.record_trade({"ticker": "T1", "side": "yes", "action": "close",
                         "price": 0.5, "contracts": 3, "fee": 0.01,
                         "cash_flow": 1.5, "mode": "paper", "position_id": "T1:yes"})
        id3 = db.record_trade({"ticker": "T2", "side": "no", "action": "open",
                               "price": 0.3, "contracts": 2, "fee": 0.01,
                               "cash_flow": -0.6, "mode": "paper", "position_id": "T2:no"})
        new = db.trades_since(id1)  # only opens AFTER id1
        assert [t["id"] for t in new] == [id3]
        assert new[0]["ticker"] == "T2"
    finally:
        db.close()
        os.remove(path)


def test_latest_signal_returns_most_recent():
    db, path = _db()
    try:
        class Sig:
            ticker = "T1"; signal_type = "BUY_YES"; side = "yes"; edge = 0.12
            model_probability = 0.6; reason_codes = ["EDGE_OK"]; detail = {}
        db.record_signal(Sig(), mode="paper")
        Sig.edge = 0.21
        db.record_signal(Sig(), mode="paper")
        row = db.latest_signal("T1")
        assert row is not None
        assert abs(row["edge"] - 0.21) < 1e-9
        assert db.latest_signal("NOPE") is None
    finally:
        db.close()
        os.remove(path)
