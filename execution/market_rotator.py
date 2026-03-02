"""
market_rotator.py — Auto-rotating market finder for BTC 5-minute markets.

Computes the current 5-minute window from UTC time, generates the
event slug, fetches token IDs from Gamma API, and signals when
it's time to rotate to the next window.

SLUG FORMAT: btc-updown-5m-{unix_timestamp}
The timestamp is the START of each 5-minute window, rounded down
to the nearest 300-second boundary.

Example:
  UTC 03:10:00 → btc-updown-5m-1740798600  (window 03:10-03:15)
  UTC 03:15:00 → btc-updown-5m-1740798900  (window 03:15-03:20)
"""

import time
import json
import asyncio
import logging
from typing import Optional, Tuple
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
INTERVAL = 300  # 5 minutes in seconds


@dataclass
class MarketWindow:
    """A single 5-minute BTC Up/Down market window."""
    slug: str
    event_slug: str
    start_ts: int           # Unix timestamp of window start
    end_ts: int             # Unix timestamp of window end
    question: str
    up_token_id: str        # "Up" token (YES equivalent)
    down_token_id: str      # "Down" token (NO equivalent)
    market_id: str

    @property
    def seconds_remaining(self) -> float:
        return max(0, self.end_ts - time.time())

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.end_ts

    @property
    def time_label(self) -> str:
        start = time.strftime("%H:%M", time.gmtime(self.start_ts))
        end = time.strftime("%H:%M", time.gmtime(self.end_ts))
        return f"{start}-{end} UTC"


def _current_window_start() -> int:
    """Get the unix timestamp of the current 5-minute window start."""
    now = int(time.time())
    return now - (now % INTERVAL)


def _next_window_start() -> int:
    """Get the unix timestamp of the next 5-minute window start."""
    return _current_window_start() + INTERVAL


def _resolve_token_order(market: dict, token_ids: list) -> tuple:
    """
    Determine which token index is Up and which is Down.

    The Gamma API returns `outcomes` and `clobTokenIds` as parallel arrays.
    outcomes might be ["Up", "Down"] or ["Down", "Up"] or ["Yes", "No"].
    We check the outcomes array to find the correct mapping.

    Returns (up_index, down_index) — indices into token_ids.
    """
    outcomes_raw = market.get("outcomes", [])
    if isinstance(outcomes_raw, str):
        try:
            outcomes = json.loads(outcomes_raw)
        except (json.JSONDecodeError, TypeError):
            outcomes = []
    else:
        outcomes = outcomes_raw

    if len(outcomes) >= 2:
        # Normalize to lowercase for matching
        o0 = str(outcomes[0]).lower().strip()
        o1 = str(outcomes[1]).lower().strip()

        # "Up" / "Yes" → up token; "Down" / "No" → down token
        up_keywords = {"up", "yes"}
        down_keywords = {"down", "no"}

        if o0 in up_keywords and o1 in down_keywords:
            return (0, 1)
        elif o0 in down_keywords and o1 in up_keywords:
            logger.info(f"Token order swapped: outcomes={outcomes}")
            return (1, 0)
        else:
            logger.warning(f"Unknown outcomes format: {outcomes}, assuming [0]=Up [1]=Down")

    # Default: assume first token is Up
    return (0, 1)


def _parse_end_date(end_date_str: Optional[str], window_start: int) -> int:
    """
    Parse the endDate from Gamma API into a unix timestamp.
    Falls back to window_start + INTERVAL if parsing fails.

    IMPORTANT: The Gamma API endDate can be the EVENT-level end date
    (hours/days away), not the 5-minute WINDOW end date. We validate
    that the parsed end is within a reasonable range (2-10 minutes from
    window_start). If not, we fall back to the computed end.

    endDate format: "2026-03-01T14:00:00Z" (ISO 8601)
    """
    if end_date_str:
        try:
            from datetime import datetime, timezone
            # Handle various ISO formats
            clean = end_date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean)
            parsed_ts = int(dt.timestamp())

            # Validate: end should be 2-10 minutes after window start
            delta = parsed_ts - window_start
            if 120 <= delta <= 600:
                return parsed_ts
            else:
                logger.warning(
                    f"endDate {end_date_str} is {delta}s from window start "
                    f"(expected ~300s) — using computed end instead"
                )
        except (ValueError, TypeError) as e:
            logger.warning(f"Could not parse endDate '{end_date_str}': {e}")

    # Fallback: computed from window start (always correct for 5-min windows)
    return window_start + INTERVAL


async def fetch_market_window(
    client: httpx.AsyncClient,
    window_start: Optional[int] = None,
) -> Optional[MarketWindow]:
    """
    Fetch market data for a specific 5-minute BTC window.

    If window_start is None, uses the current window.
    """
    if window_start is None:
        window_start = _current_window_start()

    event_slug = f"btc-updown-5m-{window_start}"

    try:
        # Try events endpoint first (contains nested markets)
        resp = await client.get(
            f"{GAMMA_API}/events",
            params={"slug": event_slug},
            timeout=10.0,
        )
        resp.raise_for_status()
        events = resp.json()

        if events and len(events) > 0:
            event = events[0]
            markets = event.get("markets", [])

            if markets:
                market = markets[0]
                token_ids_raw = market.get("clobTokenIds", "[]")
                if isinstance(token_ids_raw, str):
                    token_ids = json.loads(token_ids_raw)
                else:
                    token_ids = token_ids_raw

                # Use actual endDate from API if available
                end_ts = _parse_end_date(market.get("endDate"), window_start)

                if len(token_ids) >= 2:
                    # Determine which token is Up vs Down from outcomes
                    up_idx, down_idx = _resolve_token_order(market, token_ids)

                    return MarketWindow(
                        slug=market.get("slug", event_slug),
                        event_slug=event_slug,
                        start_ts=window_start,
                        end_ts=end_ts,
                        question=market.get("question", f"BTC Up or Down {time.strftime('%H:%M', time.gmtime(window_start))}"),
                        up_token_id=token_ids[up_idx],
                        down_token_id=token_ids[down_idx],
                        market_id=str(market.get("id", "")),
                    )

        # Fallback: try markets endpoint directly
        resp2 = await client.get(
            f"{GAMMA_API}/markets",
            params={
                "slug": event_slug,
                "active": "true",
            },
            timeout=10.0,
        )
        resp2.raise_for_status()
        markets = resp2.json()

        if markets:
            market = markets[0]
            token_ids_raw = market.get("clobTokenIds", "[]")
            if isinstance(token_ids_raw, str):
                token_ids = json.loads(token_ids_raw)
            else:
                token_ids = token_ids_raw

            end_ts = _parse_end_date(market.get("endDate"), window_start)

            if len(token_ids) >= 2:
                up_idx, down_idx = _resolve_token_order(market, token_ids)

                return MarketWindow(
                    slug=market.get("slug", event_slug),
                    event_slug=event_slug,
                    start_ts=window_start,
                    end_ts=end_ts,
                    question=market.get("question", f"BTC Up or Down 5m"),
                    up_token_id=token_ids[up_idx],
                    down_token_id=token_ids[down_idx],
                    market_id=str(market.get("id", "")),
                )

        logger.warning(f"No market found for {event_slug}")
        return None

    except Exception as e:
        logger.error(f"Failed to fetch market window {event_slug}: {e}")
        return None


class MarketRotator:
    """
    Manages auto-rotation between 5-minute BTC Up/Down markets.

    Usage:
        rotator = MarketRotator()
        await rotator.start()

        # Check periodically
        if rotator.should_rotate():
            new_window = await rotator.rotate()
            # Reconnect WebSocket to new_window.up_token_id
    """

    def __init__(self, token_side: str = "auto"):
        """
        Args:
            token_side: "auto" picks whichever side is closer to 50%,
                        "up" always watches Up, "down" always watches Down
        """
        self.token_side_pref = token_side.lower()
        self.token_side = "up"  # Active side (updated each rotation)
        self.current_window: Optional[MarketWindow] = None
        self.rotation_count = 0
        self._client: Optional[httpx.AsyncClient] = None

    async def _pick_best_side(self, window: MarketWindow):
        """Check prices and pick whichever side is closer to 50¢."""
        if self.token_side_pref != "auto":
            self.token_side = self.token_side_pref
            return

        try:
            # Get midpoint for UP token
            resp = await self._client.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": window.up_token_id},
                timeout=5.0,
            )
            resp.raise_for_status()
            up_mid = float(resp.json().get("mid", 0.5))

            # Pick whichever is closer to 0.50
            if abs(up_mid - 0.5) <= 0.5:
                # UP is closer to 50% or equal — watch UP
                self.token_side = "up" if up_mid >= 0.3 else "down"
            else:
                self.token_side = "down"

            # If either side is extreme (<15% or >85%), watch the other
            if up_mid < 0.15:
                self.token_side = "down"
            elif up_mid > 0.85:
                self.token_side = "up"

            logger.info(f"Auto-picked side: {self.token_side.upper()} (UP midpoint: {up_mid:.2f})")
        except Exception as e:
            logger.warning(f"Could not fetch price for auto-side: {e}, defaulting to UP")
            self.token_side = "up"

    async def start(self) -> Optional[MarketWindow]:
        """Initialize and fetch the current market window."""
        self._client = httpx.AsyncClient(timeout=10.0)
        window = await fetch_market_window(self._client)
        if window:
            self.current_window = window
            await self._pick_best_side(window)
            self.rotation_count += 1
            logger.info(
                f"Market rotator started: {window.question} "
                f"(side: {self.token_side.upper()}, "
                f"{window.time_label}, {window.seconds_remaining:.0f}s remaining)"
            )
        return window

    def should_rotate(self) -> bool:
        """Check if we need to switch to the next window."""
        if not self.current_window:
            return True
        # Rotate 10 seconds before expiry to get set up for next window
        return self.current_window.seconds_remaining <= 10

    async def rotate(self) -> Optional[MarketWindow]:
        """Fetch the next window and switch to it."""
        # Use a fresh client to avoid stale connections / rate limit state
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = httpx.AsyncClient(timeout=10.0)

        # Try CURRENT window first — after settlement we're usually
        # already inside the next 5-min block with time left to trade
        current_start = _current_window_start()
        prev_start = self.current_window.start_ts if self.current_window else 0
        window = None

        if current_start != prev_start:
            window = await fetch_market_window(self._client, current_start)
            # Skip if too little time left (<60s)
            if window and window.seconds_remaining < 60:
                logger.info(f"Current window has {window.seconds_remaining:.0f}s left, skipping to next")
                window = None

        # Current window unavailable or too short — try next
        if not window:
            next_start = _next_window_start()
            window = await fetch_market_window(self._client, next_start)

        if not window:
            # Sometimes the slug is off by one interval — try adjacent
            next_start = _next_window_start()
            window = await fetch_market_window(self._client, next_start + INTERVAL)

        if window:
            self.current_window = window
            await self._pick_best_side(window)
            self.rotation_count += 1
            logger.info(
                f"Rotated to: {window.question} "
                f"(side: {self.token_side.upper()}, "
                f"{window.time_label}, {window.seconds_remaining:.0f}s remaining)"
            )
        return window

    def get_active_token_id(self) -> Optional[str]:
        """Get the token ID we're currently trading."""
        if not self.current_window:
            return None
        if self.token_side == "up":
            return self.current_window.up_token_id
        return self.current_window.down_token_id

    def get_token_label(self) -> str:
        """Get the label for the active token."""
        return "Up" if self.token_side == "up" else "Down"

    async def stop(self):
        if self._client:
            await self._client.aclose()


# ──────────────────────────────────────────────────────────
# RESOLUTION FETCHER — Get actual market outcome from API
# ──────────────────────────────────────────────────────────

async def fetch_market_resolution(
    market_slug: str,
    up_token_id: str,
    down_token_id: str,
    max_wait: float = 45.0,
    poll_interval: float = 3.0,
) -> Optional[str]:
    """
    Poll the Gamma API for the actual resolution of a BTC 5-min market.

    Polymarket resolves markets shortly after the window closes (typically
    5-30 seconds). This function polls until it gets a definitive answer.

    Args:
        market_slug: The event slug (e.g., "btc-updown-5m-1740798600")
        up_token_id: Token ID for the Up/YES side
        down_token_id: Token ID for the Down/NO side
        max_wait: Maximum seconds to wait for resolution
        poll_interval: Seconds between polls

    Returns:
        "YES" or "NO" if resolved, None if timed out
    """
    start = time.time()

    async with httpx.AsyncClient() as client:
        while (time.time() - start) < max_wait:
            try:
                # Try the events endpoint
                resp = await client.get(
                    f"{GAMMA_API}/events",
                    params={"slug": market_slug},
                    timeout=10.0,
                )
                resp.raise_for_status()
                events = resp.json()

                if events and len(events) > 0:
                    event = events[0]
                    markets = event.get("markets", [])

                    for market in markets:
                        # Check for explicit resolution fields
                        outcome = market.get("outcome")
                        resolved = market.get("resolved", False)
                        winner = market.get("winner")

                        # Method 1: explicit outcome field
                        if outcome and outcome.lower() in ("yes", "up"):
                            logger.info(f"Resolution: YES (outcome={outcome})")
                            return "YES"
                        elif outcome and outcome.lower() in ("no", "down"):
                            logger.info(f"Resolution: NO (outcome={outcome})")
                            return "NO"

                        # Method 2: check token prices after resolution
                        # Resolved markets show winning token at ~$1.00
                        if resolved:
                            tokens = market.get("tokens", [])
                            if isinstance(tokens, list):
                                for token in tokens:
                                    token_id = token.get("token_id", "")
                                    price = float(token.get("price", 0))
                                    if price >= 0.90:
                                        if token_id == up_token_id:
                                            logger.info(f"Resolution: YES (Up token price={price})")
                                            return "YES"
                                        elif token_id == down_token_id:
                                            logger.info(f"Resolution: NO (Down token price={price})")
                                            return "NO"

                        # Method 3: check outcomePrices
                        outcome_prices = market.get("outcomePrices")
                        if outcome_prices:
                            if isinstance(outcome_prices, str):
                                try:
                                    outcome_prices = json.loads(outcome_prices)
                                except Exception:
                                    pass
                            if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
                                p_up = float(outcome_prices[0])
                                p_down = float(outcome_prices[1])
                                if p_up >= 0.90:
                                    logger.info(f"Resolution: YES (outcomePrices up={p_up})")
                                    return "YES"
                                elif p_down >= 0.90:
                                    logger.info(f"Resolution: NO (outcomePrices down={p_down})")
                                    return "NO"

                        # Method 4: check clobTokenPrices (CLOB final prices)
                        clob_prices = market.get("clobTokenPrices")
                        if clob_prices:
                            if isinstance(clob_prices, str):
                                try:
                                    clob_prices = json.loads(clob_prices)
                                except Exception:
                                    pass
                            if isinstance(clob_prices, list) and len(clob_prices) >= 2:
                                p_up = float(clob_prices[0])
                                p_down = float(clob_prices[1])
                                if p_up >= 0.90:
                                    logger.info(f"Resolution: YES (clobPrices up={p_up})")
                                    return "YES"
                                elif p_down >= 0.90:
                                    logger.info(f"Resolution: NO (clobPrices down={p_down})")
                                    return "NO"

                        # Method 5: winner field
                        if winner:
                            if winner.lower() in ("yes", "up", up_token_id):
                                logger.info(f"Resolution: YES (winner={winner})")
                                return "YES"
                            elif winner.lower() in ("no", "down", down_token_id):
                                logger.info(f"Resolution: NO (winner={winner})")
                                return "NO"

            except Exception as e:
                logger.debug(f"Resolution poll error: {e}")

            await asyncio.sleep(poll_interval)

    logger.warning(f"Resolution timed out after {max_wait}s for {market_slug}")
    return None
