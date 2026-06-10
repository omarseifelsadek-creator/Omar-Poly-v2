"""
modes/select.py — interactive market pickers shared by the CLI modes.

Moved verbatim from main.py (B13).

--token launches OBIApp directly (B19); the pickers here serve the
synthetic engine (no-flag default) and the pairs menu.
"""

import json
import logging
from typing import Optional

from rich.console import Console
from rich.prompt import Prompt, IntPrompt

from data.rest_client import RestClient

logger = logging.getLogger(__name__)
console = Console()


def select_pair_market():
    """
    Interactive market selector for pair trading — like the OBI monitor menu.
    Picks crypto, timeframe, and execution mode from one place.
    Returns (MarketSpec, mode_string).
    """
    from execution.market_spec import make_market_spec

    _CRYPTO_MENU = [
        ("btc", "BTC", "Bitcoin"),
        ("eth", "ETH", "Ethereum"),
        ("sol", "SOL", "Solana"),
        ("xrp", "XRP", "Ripple"),
    ]
    _TF_MENU = [
        ("5m",  "5 minutes"),
        ("15m", "15 minutes"),
    ]
    _MODE_MENU = [
        ("paper",   "Paper",   "Simulated fills, no auth needed"),
        ("dry-run", "Dry Run", "Signs orders but never posts them"),
        ("live",    "Live",    "Real FOK orders to Polymarket"),
    ]

    console.print("\n[bold cyan]═══ POLYMARKET OBI — Pair Trading ═══[/bold cyan]\n")

    # ── Step 1: Pick crypto ──
    console.print("[bold]Select crypto:[/bold]")
    for i, (_, sym, name) in enumerate(_CRYPTO_MENU, 1):
        console.print(f"  [cyan]{i}[/cyan] — {sym} ({name})")
    console.print()
    c = IntPrompt.ask("Crypto", default=1)
    c = max(1, min(c, len(_CRYPTO_MENU)))
    asset_key = _CRYPTO_MENU[c - 1][0]
    asset_sym = _CRYPTO_MENU[c - 1][1]

    # ── Step 2: Pick timeframe ──
    console.print(f"\n[bold]Select timeframe for {asset_sym}:[/bold]")
    for i, (_, label) in enumerate(_TF_MENU, 1):
        console.print(f"  [cyan]{i}[/cyan] — {label}")
    console.print()
    t = IntPrompt.ask("Timeframe", default=1)
    t = max(1, min(t, len(_TF_MENU)))
    tf_key = _TF_MENU[t - 1][0]

    # ── Step 3: Pick execution mode ──
    console.print("\n[bold]Select execution mode:[/bold]")
    for i, (_, label, desc) in enumerate(_MODE_MENU, 1):
        console.print(f"  [cyan]{i}[/cyan] — {label}  [dim]({desc})[/dim]")
    console.print()
    m = IntPrompt.ask("Mode", default=1)
    m = max(1, min(m, len(_MODE_MENU)))
    mode_key = _MODE_MENU[m - 1][0]
    mode_label = _MODE_MENU[m - 1][1]

    # ── Summary ──
    spec = make_market_spec(asset_key, tf_key)
    console.print(f"\n[green]Selected:[/green] {spec.display_name_long}  •  [bold]{mode_label}[/bold]")
    console.print(f"[dim]Window: {spec.interval_seconds}s | Panic: {spec.panic_time_seconds}s | Slug: {spec.slug_prefix}-*[/dim]\n")
    return spec, mode_key


def _extract_both_tokens(market: dict) -> Optional[tuple[str, str, str]]:
    """
    Extract YES and NO token IDs from a market dict.
    Returns (yes_token_id, no_token_id, question) or None.
    """
    token_ids = market.get("clobTokenIds", [])
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except (json.JSONDecodeError, TypeError):
            token_ids = [token_ids]

    if len(token_ids) < 2:
        return None

    outcomes = market.get("outcomes", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (json.JSONDecodeError, TypeError):
            outcomes = []

    question = market.get("question", "Unknown Market")

    # Determine YES vs NO ordering
    yes_idx, no_idx = 0, 1
    if len(outcomes) >= 2:
        if outcomes[0].lower() in ("no", "down"):
            yes_idx, no_idx = 1, 0

    return token_ids[yes_idx], token_ids[no_idx], question


async def _select_market_for_engine() -> Optional[tuple[str, str, str]]:
    """
    Interactive market selector that returns BOTH token IDs.
    Returns (yes_token_id, no_token_id, question) or None.
    """
    rest = RestClient()

    console.print("\n[bold #00FFFF]>>> SYNTHETIC MARKET MICROSTRUCTURE ENGINE <<<[/bold #00FFFF]")
    console.print("[dim]Pure visualization — no trading logic[/dim]\n")

    while True:
        console.print("[bold]Choose an option:[/bold]")
        console.print("  [cyan]1[/cyan] — Search markets by keyword")
        console.print("  [cyan]2[/cyan] — Browse top active markets")
        console.print("  [cyan]3[/cyan] — Enter a market URL slug")
        console.print()

        choice = Prompt.ask("Select", choices=["1", "2", "3"], default="1")

        if choice == "1":
            query = Prompt.ask("Search for")
            console.print(f"\n[dim]Searching for '{query}'...[/dim]")
            markets = await rest.search_markets(query, limit=10)
            if not markets:
                console.print("[yellow]No markets found. Try again.[/yellow]\n")
                continue

            selected = _display_and_pick_market_raw(markets)
            if selected:
                result = _extract_both_tokens(selected)
                if result:
                    await rest.close()
                    return result
                console.print("[yellow]Market needs 2 tokens (YES + NO).[/yellow]\n")

        elif choice == "2":
            console.print("\n[dim]Fetching top active markets...[/dim]")
            markets = await rest.get_active_markets(limit=15)
            if not markets:
                console.print("[yellow]Could not fetch markets.[/yellow]\n")
                continue

            selected = _display_and_pick_market_raw(markets)
            if selected:
                result = _extract_both_tokens(selected)
                if result:
                    await rest.close()
                    return result
                console.print("[yellow]Market needs 2 tokens (YES + NO).[/yellow]\n")

        elif choice == "3":
            slug = Prompt.ask("Enter market URL slug")
            console.print(f"\n[dim]Looking up '{slug}'...[/dim]")
            market = await rest.get_market_by_slug(slug)
            if market:
                result = _extract_both_tokens(market)
                if result:
                    await rest.close()
                    return result
                console.print("[yellow]Market needs 2 tokens (YES + NO).[/yellow]\n")
            else:
                console.print("[yellow]Market not found.[/yellow]\n")

    await rest.close()


def _display_and_pick_market_raw(markets: list[dict]) -> Optional[dict]:
    """Display markets and return the selected market dict (not token)."""
    console.print()
    for i, m in enumerate(markets, 1):
        question = m.get("question", "Unknown")
        vol_24h = float(m.get("volume24hr", 0) or 0)
        vol_total = float(m.get("volume", 0) or 0)
        vol_display = vol_24h if vol_24h > 0 else vol_total
        vol_label = "24h" if vol_24h > 0 else "tot"
        active = "+" if m.get("active") and not m.get("closed") else "-"
        token_ids = m.get("clobTokenIds", [])
        n_tokens = len(token_ids) if isinstance(token_ids, list) else 1

        console.print(
            f"  [cyan]{i:2d}[/cyan]  {active} {question[:65]}"
            f"  [dim]Vol({vol_label}): ${vol_display:,.0f}  Tokens: {n_tokens}[/dim]"
        )

    console.print("\n  [dim] 0  — Go back[/dim]\n")
    idx = IntPrompt.ask("Pick a market", default=1)

    if idx == 0 or idx > len(markets):
        return None
    return markets[idx - 1]
