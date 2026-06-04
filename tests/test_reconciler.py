"""Reconciler maps the exchange's positions/balance into local state (no live creds)."""
from config import Config
from src.database import Database
from src.position_manager import PositionManager
from src.reconciler import Reconciler


class FakeClient:
    def get_positions(self):
        return {"market_positions": [
            {"ticker": "KXHIGHNY-26JUN03-T80", "position": 5, "market_exposure": 210},   # long YES
            {"ticker": "KXHIGHCHI-26JUN03-T85", "position": -3, "market_exposure": 180}, # long NO
            {"ticker": "KXHIGHMIA-26JUN03-T90", "position": 0, "market_exposure": 0},     # flat -> ignored
        ]}

    def get_balance(self):
        return {"balance": 9500}  # cents


def test_sync_positions_from_exchange():
    pm = PositionManager(100.0)
    r = Reconciler(Config(), FakeClient(), pm, Database(":memory:"))
    assert r.sync_positions() is True
    assert pm.positions["KXHIGHNY-26JUN03-T80:yes"].contracts == 5
    assert pm.positions["KXHIGHNY-26JUN03-T80:yes"].avg_price == 0.42  # (210c/100)/5
    assert pm.positions["KXHIGHCHI-26JUN03-T85:no"].contracts == 3
    assert "KXHIGHMIA-26JUN03-T90:yes" not in pm.positions             # flat excluded
    assert pm.cash == 95.0                                             # 9500 cents synced
