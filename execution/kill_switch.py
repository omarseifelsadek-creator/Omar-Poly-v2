"""
kill_switch.py — Shared loss budget across pair runners (backlog B8).

One KillSwitch instance is shared by every runner in a session so the
--max-loss cap bounds TOTAL session losses, not per-runner losses
(previously each headless runner got its own budget, so two timeframes
could lose 2x the cap).

The check is designed to run BEFORE each entry, not only after window
settlement: `tripped(unrealized_risk)` projects the worst case where the
caller's current unmatched exposure expires worthless. Matched YES+NO
pairs cannot lose (payout $1 >= pair cost), so unmatched cost is the
only at-risk capital in pairs mode.

Single-threaded asyncio: no locking needed — all runners share one
event loop and mutations are synchronous.
"""

import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


class KillSwitch:
    """Session-wide realized-loss budget with pre-entry projection."""

    def __init__(self, max_loss: Optional[float]):
        self.max_loss = max_loss
        self.realized_pnl: float = 0.0
        # Capital whose live outcome is UNKNOWN (ambiguous order that could
        # not be reconciled). The engine rolled back, so realized_pnl does
        # not see it — but real money may be at risk on the CLOB. Counted
        # as lost in every projection until verified manually.
        self.unverified_risk: float = 0.0
        self._warned = False

    def record(self, pnl: float) -> None:
        """Add a settled window's net P&L to the shared budget."""
        self.realized_pnl += pnl

    def note_unverified(self, cost: float) -> None:
        """Register the cost of an order whose fill status is unknown."""
        self.unverified_risk += max(0.0, cost)

    def projected_pnl(self, unrealized_risk: float = 0.0) -> float:
        """Worst-case session P&L if the at-risk capital goes to zero."""
        return self.realized_pnl - max(0.0, unrealized_risk) - self.unverified_risk

    def tripped(self, unrealized_risk: float = 0.0) -> bool:
        """True when the projected worst case breaches the cap."""
        if not self.max_loss:
            return False
        return self.projected_pnl(unrealized_risk) <= -self.max_loss

    def near_limit(self, unrealized_risk: float = 0.0) -> bool:
        """
        True (once) when projected loss reaches the warning fraction of
        the cap. Self-arming: subsequent calls return False so callers
        can warn without tracking their own state.
        """
        if not self.max_loss or self._warned:
            return False
        threshold = -self.max_loss * settings.KILL_SWITCH_WARN_FRACTION
        if self.projected_pnl(unrealized_risk) <= threshold:
            self._warned = True
            return True
        return False
