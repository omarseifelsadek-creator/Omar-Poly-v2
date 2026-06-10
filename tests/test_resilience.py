"""
Tests for the resilience batch (backlog B9 + B16).

B9: WebSocket failures are classified — handshake rejections that will
not fix themselves (auth/geo-block/rate-limit) jump to max backoff.
B16: database write-queue overflow is counted and warned with throttling,
never silently dropped.
"""

import asyncio
import logging
from types import SimpleNamespace

from websockets.exceptions import InvalidStatus

from config import settings
from data.websocket_client import WebSocketClient
from storage.database import Database


# ── B9: handshake-rejection classification ────────────────────────────

def _rejection(status_code):
    return InvalidStatus(SimpleNamespace(status_code=status_code))


def test_auth_and_geoblock_rejections_jump_to_max_backoff():
    assert WebSocketClient._log_handshake_rejection(_rejection(401)) is True
    assert WebSocketClient._log_handshake_rejection(_rejection(403)) is True


def test_rate_limit_jumps_to_max_backoff():
    assert WebSocketClient._log_handshake_rejection(_rejection(429)) is True


def test_server_errors_keep_normal_backoff():
    assert WebSocketClient._log_handshake_rejection(_rejection(500)) is False
    assert WebSocketClient._log_handshake_rejection(_rejection(502)) is False


def test_missing_status_code_keeps_normal_backoff():
    assert WebSocketClient._log_handshake_rejection(
        InvalidStatus(SimpleNamespace())
    ) is False


# ── B16: write-queue overflow accounting ──────────────────────────────

def test_queue_overflow_counts_drops_and_throttles_warnings(monkeypatch, caplog):
    monkeypatch.setattr(settings, "DB_WRITE_QUEUE_SIZE", 2)

    async def scenario():
        db = Database(db_path="/tmp/obi-test-unused.db")  # never initialized
        for _ in range(7):
            await db._enqueue("INSERT INTO t VALUES (?)", (1,))
        return db

    with caplog.at_level(logging.WARNING):
        db = asyncio.run(scenario())

    assert db.dropped_writes == 5            # 2 queued, 5 dropped
    warnings = [r for r in caplog.records if "queue full" in r.message]
    assert len(warnings) == 1, "drop warnings must be throttled, not per-write"
    assert "5" in warnings[0].message or "1" in warnings[0].message


def test_no_drops_no_counter():
    async def scenario():
        db = Database(db_path="/tmp/obi-test-unused.db")
        await db._enqueue("INSERT INTO t VALUES (?)", (1,))
        return db

    db = asyncio.run(scenario())
    assert db.dropped_writes == 0
