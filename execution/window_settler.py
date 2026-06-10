"""
execution/window_settler.py — window winner resolution chain (B13).

Extracted verbatim from PairRunner._settle_window. Four methods in
priority order, each falling through to the next:

  1. Chainlink  — the exact source Polymarket resolves against
  2. Binance    — candle open/close fallback
  3. Gamma API  — poll the market resolution (slow; capped under stop)
  4. Order book — last-resort guess from the final book state

Always returns a winner ("YES"/"NO") — method 4 cannot fail.
"""

import logging

from rich.console import Console

from config import settings
from execution.market_rotator import fetch_market_resolution, fetch_price_resolution

logger = logging.getLogger(__name__)
console = Console()


async def resolve_winner(
    *,
    chainlink,
    window,
    spec,
    yes_token_id: str,
    no_token_id: str,
    yes_book,
    no_book,
    engine,
    stop_requested: bool,
) -> str:
    """Resolve the window winner via the 4-method fallback chain."""
    winner = None

    # Method 1: Chainlink — exact same source as Polymarket resolution
    if chainlink.latest_price and chainlink.window_open_price:
        winner = chainlink.resolve()
        if winner:
            console.print(f"[green]  Resolution from Chainlink: {winner} won[/green]")

    # Method 2: Binance candle (fallback if Chainlink stream disconnected)
    if winner is None and window:
        console.print("[yellow]  Chainlink unavailable, checking Binance...[/yellow]")
        try:
            winner = await fetch_price_resolution(
                window.start_ts, window.end_ts,
                spec=spec,
            )
            if winner:
                console.print(f"[green]  Resolution from Binance: {winner} won[/green]")
        except Exception as e:
            logger.warning(f"Binance resolution error: {e}")

    # Method 3: Poll the Gamma API (last resort). Under a stop request
    # the poll is capped (B18) so graceful shutdown is bounded — the
    # order-book fallback below still produces a settlement.
    if winner is None and window:
        max_wait = (
            settings.SETTLE_STOP_DEADLINE_SECONDS
            if stop_requested else 90.0
        )
        console.print(
            f"[yellow]  Price feeds unavailable, polling Gamma API "
            f"(up to {max_wait:.0f}s)...[/yellow]"
        )
        try:
            winner = await fetch_market_resolution(
                market_slug=window.event_slug,
                up_token_id=yes_token_id,
                down_token_id=no_token_id,
                max_wait=max_wait,
                poll_interval=5.0,
            )
            if winner:
                console.print(f"[green]  Resolution from Gamma API: {winner} won[/green]")
        except Exception as e:
            logger.error(f"Resolution fetch error: {e}")

    # Method 4: Fallback to order book snapshot (emergency only)
    if winner is None:
        yes_mid = yes_book.midpoint
        yes_ask = yes_book.best_ask
        no_ask = no_book.best_ask
        winner = engine.determine_winner(yes_mid, yes_ask)
        console.print(
            f"[red]  WARNING: All resolution methods failed — guessing from book: "
            f"{winner} (YES mid={yes_mid}, ask={yes_ask}, NO ask={no_ask})[/red]"
        )

    return winner
