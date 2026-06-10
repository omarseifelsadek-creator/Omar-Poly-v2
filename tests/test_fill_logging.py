"""
Regression test for the fill-logging dead-code bug (audit C1 / backlog B1).

An indentation regression in PairRunner._try_evaluate placed all success-path
fill bookkeeping (CSV buy logging, live-PnL tracking, dashboard recent-buys,
hourly report fills, zone counts) after an early `return` inside the rejection
branch — so no fills were ever logged. This test drives a synthetic cheap pair
through _try_evaluate in paper mode and asserts the bookkeeping actually runs.

Run from the repo root:  env/bin/python -m pytest tests/test_fill_logging.py -v
"""

import asyncio
import time
from datetime import datetime
from pathlib import Path

from data.models import BookSnapshot, OrderLevel
from execution.market_spec import make_market_spec
from execution.pair_runner import PairRunner


def _snapshot(asset_id: str, bid: float, ask: float, size: float = 1000.0) -> BookSnapshot:
    """Build a minimal one-level book snapshot."""
    return BookSnapshot(
        asset_id=asset_id,
        market="test-market",
        bids=[OrderLevel(price=bid, size=size)],
        asks=[OrderLevel(price=ask, size=size)],
        timestamp_ms=int(time.time() * 1000),
    )


def test_fill_bookkeeping_runs_on_successful_paper_buy(tmp_path, monkeypatch):
    import execution.pair_logger as pair_logger

    runner = PairRunner(mode="paper", spec=make_market_spec("btc", "5m"))
    runner.engine.reset()  # fresh 300s window, full theta sizing

    # YES at $0.30 is in the Sniper zone (<= 0.35): buys aggressively,
    # no signal filters. Pair cost 0.30 + 0.55 = 0.85 passes the atomic
    # entry gate (<= 0.99) and the first leg is above the falling-knife
    # floor (>= 0.15).
    runner.yes_book.apply_snapshot(_snapshot("yes-token", bid=0.29, ask=0.30))
    runner.no_book.apply_snapshot(_snapshot("no-token", bid=0.54, ask=0.55))

    # Backdate ask ages past the latency model's 500ms threshold so the
    # fill probability is 100% (deterministic).
    runner.engine.update_ask_age("YES", 0.30)
    runner.engine.update_ask_age("NO", 0.55)
    runner.engine._yes_ask_since = time.time() - 2.0
    runner.engine._no_ask_since = time.time() - 2.0

    # conftest redirects LOG_DIR to a temp dir — never data/logs/
    buys_csv = Path(pair_logger.LOG_DIR) / f"pair_buys_{datetime.now():%Y%m%d}.csv"
    rows_before = len(buys_csv.read_text().splitlines()) if buys_csv.exists() else 0

    asyncio.run(runner._try_evaluate())

    # The engine itself filled a leg (sanity: the setup is valid)
    assert runner.engine.legs, (
        f"engine did not buy — filters: {runner.engine.filter_reasons}, "
        f"last: {runner.engine.last_filter_reason}"
    )

    # Every assertion below exercises code that was unreachable before the
    # B1 fix — these are the regression checks.
    assert sum(runner._zone_counts.values()) >= 1, "zone counts not updated"
    assert runner._report_fills, "hourly report fills not appended"
    assert runner._recent_buys_display, "dashboard recent-buys not appended"

    rows_after = len(buys_csv.read_text().splitlines()) if buys_csv.exists() else 0
    assert rows_after > rows_before, "pair_buys CSV did not gain a row"
