"""
rest_client.py — HTTP client for Polymarket REST APIs.

TWO PURPOSES:
1. Market discovery: Find token IDs, market questions, metadata
2. Fallback: If the WebSocket disconnects, we can poll the REST API

BEGINNER NOTE:
"REST API" means we make HTTP requests (like your browser does) to get data.
It's simpler than WebSocket but slower — we have to ask for data each time
instead of receiving a continuous stream.

We use `httpx` instead of `requests` because httpx supports async,
which means our program can do other things while waiting for the API response.
"""

import asyncio
import logging
import re
from typing import Optional

import httpx

from config import settings
from data.models import BookSnapshot, OrderLevel

logger = logging.getLogger(__name__)


class RestClient:
    """
    HTTP client for Polymarket CLOB and Gamma APIs.

    Usage:
        client = RestClient()
        markets = await client.search_markets("bitcoin")
        book = await client.get_orderbook(token_id)
    """

    def __init__(self):
        # httpx.AsyncClient is like a browser session — it keeps connections
        # open for efficiency instead of reconnecting every request
        self._client = httpx.AsyncClient(
            timeout=10.0,  # Wait max 10 seconds for a response
            headers={"Accept": "application/json"},
        )

    async def close(self):
        """Clean up the HTTP client when we're done."""
        await self._client.aclose()

    # ──────────────────────────────────────────────────────────
    # MARKET DISCOVERY (Gamma API)
    # ──────────────────────────────────────────────────────────

    async def search_markets(self, query: str, limit: int = 10) -> list[dict]:
        """
        Search for markets by keyword.

        Three strategies run to maximise coverage:
        1. Paginated market fetch (sequential — needs prior page size to decide next)
        2. Event tag search (concurrent with strategy 3)
        3. Event title search — catches terms that differ from question text
           (e.g., "oscars" vs "Academy Awards"). The Gamma API has no server-side
           title filter, so we fetch top events by volume and filter client-side.

        Args:
            query: Search term (e.g., "bitcoin", "election", "AI")
            limit: Maximum number of results

        Returns:
            List of market dicts sorted by 24hr volume descending.
        """
        query_lower = query.lower()
        seen_ids: set[str] = set()
        results: list[dict] = []

        # Word boundary matching for short queries to avoid
        # substring false positives (e.g., "AI" matching "Trail")
        query_pattern = (
            re.compile(rf"\b{re.escape(query_lower)}\b", re.IGNORECASE)
            if len(query_lower) <= 3
            else None
        )

        def _text_matches(text: str) -> bool:
            if query_pattern:
                return query_pattern.search(text) is not None
            return query_lower in text.lower()

        def _is_tradeable(m: dict) -> bool:
            return bool(m.get("clobTokenIds")) and not m.get("closed", False)

        def _collect(markets: list[dict], require_question_match: bool = True) -> None:
            """Add markets to results, deduplicating by id."""
            for m in markets:
                mid = m.get("id")
                if mid is None:
                    mid = m.get("question")
                if not mid:
                    continue
                if mid in seen_ids:
                    continue
                if not _is_tradeable(m):
                    continue
                if require_question_match and not _text_matches(m.get("question", "")):
                    continue
                seen_ids.add(mid)
                results.append(m)

        # ── Strategy 1: Paginated markets sorted by volume ──
        try:
            page_size = 100
            for offset in range(0, 500, page_size):
                resp = await self._client.get(
                    f"{settings.GAMMA_API_URL}/markets",
                    params={
                        "limit": page_size,
                        "offset": offset,
                        "active": "true",
                        "closed": "false",
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                )
                resp.raise_for_status()
                page = resp.json()
                if not isinstance(page, list):
                    logger.error("Unexpected response format from /markets: %r", type(page))
                    break
                _collect(page)

                if len(page) < page_size:
                    break

        except httpx.HTTPError as e:
            logger.error("Failed to search markets (pages): %s", e)

        # ── Strategies 2 & 3: Event searches (run concurrently) ──
        async def _search_events_by_tag() -> list[dict]:
            try:
                resp = await self._client.get(
                    f"{settings.GAMMA_API_URL}/events",
                    params={
                        "tag": query_lower,
                        "limit": 20,
                        "active": "true",
                        "closed": "false",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else []
            except httpx.HTTPError as e:
                logger.error("Failed to search events by tag: %s", e)
                return []

        async def _search_events_by_volume() -> list[dict]:
            try:
                resp = await self._client.get(
                    f"{settings.GAMMA_API_URL}/events",
                    params={
                        "limit": 100,
                        "active": "true",
                        "closed": "false",
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else []
            except httpx.HTTPError as e:
                logger.error("Failed to search events by title: %s", e)
                return []

        tag_events, vol_events = await asyncio.gather(
            _search_events_by_tag(),
            _search_events_by_volume(),
        )

        # Strategy 2: Match child markets by question text
        for event in tag_events:
            _collect(event.get("markets", []))

        # Strategy 3: Match by event title, include all child markets
        for event in vol_events:
            if _text_matches(event.get("title", "")):
                _collect(event.get("markets", []), require_question_match=False)

        # Sort by 24hr volume descending
        results.sort(
            key=lambda m: float(m.get("volume24hr", 0) or 0), reverse=True
        )
        return results[:limit]

    async def get_market_by_slug(self, slug: str) -> Optional[dict]:
        """
        Get a specific market by its URL slug.

        The slug is the last part of the Polymarket URL:
        https://polymarket.com/event/will-bitcoin-hit-100k
        → slug = "will-bitcoin-hit-100k"
        """
        try:
            resp = await self._client.get(
                f"{settings.GAMMA_API_URL}/markets",
                params={"slug": slug},
            )
            resp.raise_for_status()
            markets = resp.json()
            return markets[0] if markets else None

        except (httpx.HTTPError, IndexError) as e:
            logger.error(f"Failed to get market by slug: {e}")
            return None

    async def get_market_by_id(self, condition_id: str) -> Optional[dict]:
        """Get a specific market by its condition ID."""
        try:
            resp = await self._client.get(
                f"{settings.GAMMA_API_URL}/markets",
                params={"id": condition_id},
            )
            resp.raise_for_status()
            markets = resp.json()
            return markets[0] if markets else None

        except (httpx.HTTPError, IndexError) as e:
            logger.error(f"Failed to get market by ID: {e}")
            return None

    async def get_active_markets(self, limit: int = 20) -> list[dict]:
        """
        Get the most active markets on Polymarket right now.
        Sorted by 24-hour volume so you get markets with real activity.
        """
        try:
            resp = await self._client.get(
                f"{settings.GAMMA_API_URL}/markets",
                params={
                    "limit": 100,
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            resp.raise_for_status()
            markets = resp.json()

            # Filter to only markets with trading tokens and real volume
            active = [
                m for m in markets
                if m.get("clobTokenIds")
                and not m.get("closed", False)
                and float(m.get("volume24hr", 0) or 0) > 0
            ]

            # Sort by 24hr volume descending (most active first)
            active.sort(key=lambda m: float(m.get("volume24hr", 0) or 0), reverse=True)

            return active[:limit]

        except httpx.HTTPError as e:
            logger.error(f"Failed to get active markets: {e}")
            return []

    # ──────────────────────────────────────────────────────────
    # ORDER BOOK (CLOB API — fallback for WebSocket)
    # ──────────────────────────────────────────────────────────

    async def get_orderbook(self, token_id: str) -> Optional[BookSnapshot]:
        """
        Fetch the current order book via REST (polling fallback).

        This is slower than WebSocket but useful for:
        - Initial state on startup before WS connects
        - Recovery after a disconnect
        """
        try:
            resp = await self._client.get(
                f"{settings.CLOB_REST_URL}/book",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            data = resp.json()

            bids = [
                OrderLevel(price=float(b["price"]), size=float(b["size"]))
                for b in data.get("bids", [])
                if float(b.get("size", 0)) > 0
            ]
            asks = [
                OrderLevel(price=float(a["price"]), size=float(a["size"]))
                for a in data.get("asks", [])
                if float(a.get("size", 0)) > 0
            ]

            bids.sort(key=lambda x: x.price, reverse=True)
            asks.sort(key=lambda x: x.price)

            return BookSnapshot(
                asset_id=data.get("asset_id", token_id),
                market=data.get("market", ""),
                bids=bids,
                asks=asks,
                timestamp_ms=int(data.get("timestamp", "0")),
                hash=data.get("hash", ""),
            )

        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch orderbook: {e}")
            return None

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get the current midpoint price for a token."""
        try:
            resp = await self._client.get(
                f"{settings.CLOB_REST_URL}/midpoint",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("mid", 0))

        except (httpx.HTTPError, ValueError) as e:
            logger.error(f"Failed to fetch midpoint: {e}")
            return None

    async def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Get the current best price for a side."""
        try:
            resp = await self._client.get(
                f"{settings.CLOB_REST_URL}/price",
                params={"token_id": token_id, "side": side},
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("price", 0))

        except (httpx.HTTPError, ValueError) as e:
            logger.error(f"Failed to fetch price: {e}")
            return None
