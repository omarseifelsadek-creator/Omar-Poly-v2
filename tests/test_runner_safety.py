"""
Runner-level safety gate tests (backlog B7/B8).

Reuses the synthetic-book setup from test_fill_logging: a cheap pair the
engine would normally buy, then asserts the safety gates stop it.
"""

import asyncio
import time

from data.models import BookSnapshot, OrderLevel
from execution.kill_switch import KillSwitch
from execution.market_spec import make_market_spec
from execution.pair_runner import PairRunner


def _snapshot(asset_id: str, bid: float, ask: float, size: float = 1000.0) -> BookSnapshot:
    return BookSnapshot(
        asset_id=asset_id,
        market="test-market",
        bids=[OrderLevel(price=bid, size=size)],
        asks=[OrderLevel(price=ask, size=size)],
        timestamp_ms=int(time.time() * 1000),
    )


def _buyable_runner(**kwargs) -> PairRunner:
    """A paper runner staring at a pair it would definitely buy."""
    runner = PairRunner(mode="paper", spec=make_market_spec("btc", "5m"), **kwargs)
    runner.engine.reset()
    runner.yes_book.apply_snapshot(_snapshot("yes-token", bid=0.29, ask=0.30))
    runner.no_book.apply_snapshot(_snapshot("no-token", bid=0.54, ask=0.55))
    runner.engine.update_ask_age("YES", 0.30)
    runner.engine.update_ask_age("NO", 0.55)
    runner.engine._yes_ask_since = time.time() - 2.0
    runner.engine._no_ask_since = time.time() - 2.0
    return runner


def test_sanity_buys_when_no_gates_active():
    runner = _buyable_runner()
    asyncio.run(runner._try_evaluate())
    assert runner.engine.legs


def test_tripped_kill_switch_blocks_entries():
    switch = KillSwitch(max_loss=50)
    switch.record(-55)  # session already past the cap
    runner = _buyable_runner(kill_switch=switch)

    asyncio.run(runner._try_evaluate())

    assert not runner.engine.legs, "kill switch must block new entries"
    assert runner._halt_entries == "kill switch"


def test_shared_switch_blocks_sibling_runner():
    switch = KillSwitch(max_loss=50)
    runner_a = _buyable_runner(kill_switch=switch)
    runner_b = _buyable_runner(kill_switch=switch)

    switch.record(-60)  # runner A's window settles past the cap

    asyncio.run(runner_b._try_evaluate())
    assert not runner_b.engine.legs, "sibling runner shares the budget"
    # runner A unused — just proves two runners can hold the same switch
    assert runner_a.kill_switch is runner_b.kill_switch


def test_halt_entries_flag_short_circuits_evaluation():
    runner = _buyable_runner()
    runner._halt_entries = "ambiguous:exception:TimeoutError"

    asyncio.run(runner._try_evaluate())
    assert not runner.engine.legs, "halted window must not trade"


def test_crash_streak_ends_window_only_without_progress():
    runner = PairRunner(mode="paper", spec=make_market_spec("btc", "5m"))

    # three crashes in a row with no messages processed between them
    assert runner._note_msg_crash() is False
    assert runner._note_msg_crash() is False
    assert runner._note_msg_crash() is True, "third no-progress crash ends window"

    # progress between crashes resets the streak
    runner._crash_streak = 0
    runner._last_crash_msg_count = 0
    runner._msg_count = 500
    assert runner._note_msg_crash() is False
    runner._msg_count = 1200  # plenty of messages processed since
    assert runner._note_msg_crash() is False, "progress resets the streak"
