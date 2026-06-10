"""Tests for the shared kill switch (backlog B8)."""

from execution.kill_switch import KillSwitch


def test_no_limit_never_trips():
    ks = KillSwitch(max_loss=None)
    ks.record(-10_000)
    assert not ks.tripped()
    assert not ks.near_limit()


def test_trips_exactly_at_cap():
    ks = KillSwitch(max_loss=50)
    ks.record(-49.99)
    assert not ks.tripped()
    ks.record(-0.01)
    assert ks.tripped()


def test_unrealized_risk_projects_worst_case():
    ks = KillSwitch(max_loss=50)
    ks.record(-45)
    assert not ks.tripped()
    # $6 of unmatched exposure could expire worthless -> projected -51
    assert ks.tripped(unrealized_risk=6.0)
    # negative risk input is clamped, never helps
    assert not ks.tripped(unrealized_risk=-100.0)


def test_shared_budget_across_runners():
    ks = KillSwitch(max_loss=50)
    # two runners share the same object — losses accumulate jointly
    ks.record(-30)  # runner A
    ks.record(-25)  # runner B
    assert ks.tripped()


def test_profit_offsets_losses():
    ks = KillSwitch(max_loss=50)
    ks.record(-40)
    ks.record(+20)
    assert not ks.tripped()
    assert not ks.tripped(unrealized_risk=25.0)
    assert ks.tripped(unrealized_risk=31.0)


def test_near_limit_warns_exactly_once():
    ks = KillSwitch(max_loss=100)
    ks.record(-50)
    assert not ks.near_limit()      # 50% — below the 80% warn fraction
    ks.record(-30)
    assert ks.near_limit()          # 80% — warns
    ks.record(-5)
    assert not ks.near_limit()      # already warned — stays quiet


def test_unverified_risk_counts_as_lost():
    # An ambiguous order's cost is budgeted as lost until verified —
    # the engine rolled back, so realized P&L never sees it.
    ks = KillSwitch(max_loss=50)
    ks.record(-44)
    assert not ks.tripped()
    ks.note_unverified(6.0)
    assert ks.tripped()
    ks.note_unverified(-5.0)        # negative input clamped, never reduces risk
    assert ks.tripped()
