"""
Tests for LiveExecutor outcome handling (backlog B7 + B15).

The dangerous case: a FOK submission errors or returns garbage AFTER the
order may have matched on the CLOB. The executor must reconcile against
its own trade history — adopting a confirmed fill, or rolling back and
flagging the outcome as ambiguous so the runner halts the window.

No network: the ClobClient is replaced with a fake.
"""

import asyncio

import pytest

from config import settings
from execution.executor import LiveExecutor


# ── Fakes ──────────────────────────────────────────────────────────────

class FakeClob:
    """Scriptable stand-in for ClobClient."""

    def __init__(self, post_result=None, post_exc=None, trades=None):
        self._post_result = post_result
        self._post_exc = post_exc
        self._trades = trades if trades is not None else []
        self.get_trades_calls = 0

    # called via _place_order_sync's internals — we stub at that level instead

    def get_trades(self, params=None, next_cursor="MA=="):
        self.get_trades_calls += 1
        return self._trades


class FakeEngine:
    """Carries exactly the fields pre_snapshot/_rollback touch."""

    def __init__(self):
        self.yes_qty = 0.0
        self.no_qty = 0.0
        self.yes_cost = 0.0
        self.no_cost = 0.0
        self.yes_locked = False
        self.no_locked = False
        self.last_buy_time = 0.0
        self.buys_executed = 0
        self.fills_attempted = 0
        self.partial_fills = 0
        self.legs = []

    def _update_locks(self):
        pass


ACTION = {"side": "YES", "qty": 20.0, "vwap_price": 0.30, "cost": 6.0}


@pytest.fixture
def executor(monkeypatch):
    """LiveExecutor with fake creds, no client, and instant reconcile polling."""
    for var in ("POLY_PRIVATE_KEY", "POLY_API_KEY", "POLY_API_SECRET",
                "POLY_API_PASSPHRASE", "POLY_FUNDER"):
        monkeypatch.setenv(var, "test-value")
    monkeypatch.setattr(settings, "LIVE_RECONCILE_DELAY_SECONDS", 0.0)
    ex = LiveExecutor()
    ex._ensure_client = lambda: None
    return ex


def _committed_engine():
    """Engine state as it looks right after evaluate() committed a buy."""
    engine = FakeEngine()
    snapshot = LiveExecutor.pre_snapshot(None, engine)  # state BEFORE the buy
    engine.yes_qty = 20.0
    engine.yes_cost = 6.0
    engine.buys_executed = 1
    engine.legs = ["leg-1"]
    return engine, snapshot


def _run(executor, clob, action=ACTION):
    executor._client = clob
    engine, snapshot = _committed_engine()
    result = asyncio.run(executor.execute(dict(action), engine, snapshot, "tok-123"))
    return result, engine


def _stub_post(executor, response=None, exc=None):
    def fake_place(token_id, qty, price):
        if exc is not None:
            raise exc
        return response
    executor._place_order_sync = fake_place


MATCHING_TRADE = {
    "id": "trade-9", "taker_order_id": "ord-9", "side": "BUY",
    "size": "20", "price": "0.31", "status": "MATCHED",
}


# ── Clean outcomes (no reconciliation) ────────────────────────────────

def test_clean_fill_commits_state(executor):
    clob = FakeClob()
    _stub_post(executor, response={"orderID": "ord-1", "status": "matched"})
    result, engine = _run(executor, clob)

    assert result["mode"] == "LIVE" and result["order_id"] == "ord-1"
    assert engine.yes_qty == 20.0 and engine.legs == ["leg-1"]
    assert clob.get_trades_calls == 0


def test_clean_rejection_rolls_back_without_reconciling(executor):
    clob = FakeClob()
    _stub_post(executor, response={"orderID": "", "status": "unmatched",
                                   "errorMsg": "price moved"})
    result, engine = _run(executor, clob)

    assert result["rejected"] and not result.get("ambiguous")
    assert engine.yes_qty == 0.0 and engine.legs == []
    assert clob.get_trades_calls == 0


# ── Ambiguous outcomes → reconciliation ───────────────────────────────

def test_exception_with_confirmed_fill_is_adopted(executor):
    clob = FakeClob(trades=[MATCHING_TRADE])
    _stub_post(executor, exc=TimeoutError("relayer timeout"))
    result, engine = _run(executor, clob)

    assert result.get("reconciled") and result["mode"] == "LIVE"
    assert result["order_id"] == "ord-9"
    assert result["vwap_price"] == pytest.approx(0.31)  # actual fill price
    assert engine.yes_qty == 20.0, "confirmed fill must NOT be rolled back"
    assert clob.get_trades_calls >= 1


def test_exception_with_no_fill_rolls_back_and_flags_ambiguous(executor):
    clob = FakeClob(trades=[])
    _stub_post(executor, exc=TimeoutError("relayer timeout"))
    result, engine = _run(executor, clob)

    assert result["rejected"] and result["ambiguous"]
    assert engine.yes_qty == 0.0 and engine.legs == []
    assert clob.get_trades_calls == settings.LIVE_RECONCILE_ATTEMPTS


def test_garbage_response_reconciles(executor):
    clob = FakeClob(trades=[MATCHING_TRADE])
    _stub_post(executor, response="<html>502 Bad Gateway</html>")
    result, engine = _run(executor, clob)

    assert result.get("reconciled")
    assert engine.yes_qty == 20.0


def test_empty_dict_response_without_fill_is_ambiguous(executor):
    clob = FakeClob(trades=[])
    _stub_post(executor, response={})
    result, engine = _run(executor, clob)

    assert result["rejected"] and result["ambiguous"]
    assert engine.yes_qty == 0.0


# ── Trade matching heuristics ─────────────────────────────────────────

def test_match_trade_filters_side_status_and_size():
    trades = [
        {"side": "SELL", "size": "20", "status": "MATCHED"},      # wrong side
        {"side": "BUY", "size": "20", "status": "FAILED"},        # failed
        {"side": "BUY", "size": "5", "status": "MATCHED"},        # wrong size
        {"side": "BUY", "size": "not-a-number", "status": "MATCHED"},
        {"side": "BUY", "size": "20.5", "status": "MATCHED", "id": "ok"},
    ]
    match = LiveExecutor._match_trade(trades, qty=20.0)  # 2.5% diff < 5% tol
    assert match is not None and match["id"] == "ok"
    assert LiveExecutor._match_trade(trades[:4], qty=20.0) is None
