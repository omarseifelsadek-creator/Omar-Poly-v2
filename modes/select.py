"""
modes/select.py — interactive market pickers shared by the CLI modes.

Moved verbatim from main.py (B13).

NOTE (B19): select_market_interactive/_display_and_pick_market are
currently dead — no mode calls them since the synthetic engine became
the no-flag default. Kept until the --token path decision is made.
"""

import json
import logging
from typing import Optional

from rich.console import Console
from rich.prompt import Prompt, IntPrompt

from data.rest_client import RestClient

logger = logging.getLogger(__name__)
console = Console()


async def select_market_interactive() -> tuple[str, str]:
    """
    Interactive market selector — helps the user find a market to monitor.

    Returns:
        (token_id, market_question) tuple
    """
    rest = RestClient()

    console.print("\n[bold cyan]═══ POLYMARKET OBI — Market Selector ═══[/bold cyan]\n")

    while True:
        console.print("[bold]Choose an option:[/bold]")
        console.print("  [cyan]1[/cyan] — Search markets by keyword")
        console.print("  [cyan]2[/cyan] — Browse top active markets")
        console.print("  [cyan]3[/cyan] — Enter a token ID directly")
        console.print("  [cyan]4[/cyan] — Enter a market URL slug")
        console.print()

        choice = Prompt.ask("Select", choices=["1", "2", "3", "4"], default="1")

        if choice == "1":
            query = Prompt.ask("Search for")
            console.print(f"\n[dim]Searching for '{query}'...[/dim]")
            markets = await rest.search_markets(query, limit=10)

            if not markets:
                console.print("[yellow]No markets found. Try a different search.[/yellow]\n")
                continue

            result = _display_and_pick_market(markets)
            if result:
                await rest.close()
                return result

        elif choice == "2":
            console.print("\n[dim]Fetching top active markets...[/dim]")
            markets = await rest.get_active_markets(limit=15)

            if not markets:
                console.print("[yellow]Could not fetch markets.[/yellow]\n")
                continue

            result = _display_and_pick_market(markets)
            if result:
                await rest.close()
                return result

        elif choice == "3":
            token_id = Prompt.ask("Enter token ID")
            question = Prompt.ask("Market question (optional)", default="Custom Market")
            await rest.close()
            return token_id, question, "Yes"

        elif choice == "4":
            slug = Prompt.ask("Enter market URL slug")
            console.print(f"\n[dim]Looking up '{slug}'...[/dim]")
            market = await rest.get_market_by_slug(slug)

            if market:
                token_ids = market.get("clobTokenIds", [])
                if isinstance(token_ids, str):
                    try:
                        token_ids = json.loads(token_ids)
                    except (json.JSONDecodeError, TypeError):
                        token_ids = [token_ids]

                if token_ids:
                    # If multiple tokens, let user pick
                    if len(token_ids) > 1:
                        outcomes = market.get("outcomes", [])
                        if isinstance(outcomes, str):
                            try:
                                outcomes = json.loads(outcomes)
                            except (json.JSONDecodeError, TypeError):
                                outcomes = []
                        console.print(f"\n  Tokens available:")
                        for i, tid in enumerate(token_ids):
                            label = outcomes[i] if i < len(outcomes) else f"Token {i+1}"
                            console.print(f"  [cyan]{i+1}[/cyan]  {label}  [dim]{tid[:40]}...[/dim]")
                        tc = IntPrompt.ask("Pick token", default=1)
                        ti = max(0, min(tc - 1, len(token_ids) - 1))
                        chosen_label = outcomes[ti] if ti < len(outcomes) else "Yes"
                        await rest.close()
                        return token_ids[ti], market.get("question", slug), chosen_label
                    else:
                        await rest.close()
                        return token_ids[0], market.get("question", slug), "Yes"
                else:
                    console.print("[yellow]Market found but no token IDs available.[/yellow]\n")
            else:
                console.print("[yellow]Market not found. Check the slug.[/yellow]\n")

    await rest.close()


def _display_and_pick_market(markets: list[dict]) -> Optional[tuple[str, str, str]]:
    """Display a list of markets and let the user pick one. Returns (token_id, question, token_label)."""
    console.print()

    for i, m in enumerate(markets, 1):
        question = m.get("question", "Unknown")
        vol_24h = float(m.get("volume24hr", 0) or 0)
        vol_total = float(m.get("volume", 0) or 0)
        # Show 24hr volume if available, otherwise total
        vol_display = vol_24h if vol_24h > 0 else vol_total
        vol_label = "24h" if vol_24h > 0 else "tot"
        active = "🟢" if m.get("active") and not m.get("closed") else "🔴"
        token_ids = m.get("clobTokenIds", [])
        has_tokens = "✓" if token_ids else "✗"

        console.print(
            f"  [cyan]{i:2d}[/cyan]  {active} {question[:65]}"
            f"  [dim]Vol({vol_label}): ${vol_display:,.0f}  Tokens: {has_tokens}[/dim]"
        )

    console.print(f"\n  [dim] 0  — Go back[/dim]")
    console.print()

    idx = IntPrompt.ask("Pick a market", default=1)

    if idx == 0 or idx > len(markets):
        return None

    selected = markets[idx - 1]
    token_ids = selected.get("clobTokenIds", [])

    if not token_ids:
        console.print("[yellow]This market has no trading tokens available.[/yellow]\n")
        return None

    question = selected.get("question", "Unknown Market")

    # Parse token IDs — they may be a JSON string or already a list
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except (json.JSONDecodeError, TypeError):
            token_ids = [token_ids]

    # Get outcome labels (YES/NO) if available
    outcomes = selected.get("outcomes", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (json.JSONDecodeError, TypeError):
            outcomes = []

    console.print(f"\n[green]Selected:[/green] {question}")

    if len(token_ids) == 1:
        label = outcomes[0] if outcomes else "Yes"
        console.print(f"[dim]Token ({label}): {token_ids[0][:40]}...[/dim]")
        return token_ids[0], question, label

    # Multiple tokens — let user pick YES or NO
    console.print(f"\n  This market has {len(token_ids)} tokens:")
    for i, tid in enumerate(token_ids):
        label = outcomes[i] if i < len(outcomes) else f"Token {i + 1}"
        console.print(f"  [cyan]{i + 1}[/cyan]  {label}  [dim]{tid[:40]}...[/dim]")

    token_choice = IntPrompt.ask("Pick token", default=1)
    token_idx = max(0, min(token_choice - 1, len(token_ids) - 1))
    chosen_token = token_ids[token_idx]
    chosen_label = outcomes[token_idx] if token_idx < len(outcomes) else f"Token {token_idx + 1}"

    console.print(f"[dim]Using {chosen_label} token: {chosen_token[:40]}...[/dim]")

    return chosen_token, question, chosen_label


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
    console.print(f"\n[bold]Select execution mode:[/bold]")
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

    console.print(f"\n[bold #00FFFF]>>> SYNTHETIC MARKET MICROSTRUCTURE ENGINE <<<[/bold #00FFFF]")
    console.print(f"[dim]Pure visualization — no trading logic[/dim]\n")

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

    console.print(f"\n  [dim] 0  — Go back[/dim]\n")
    idx = IntPrompt.ask("Pick a market", default=1)

    if idx == 0 or idx > len(markets):
        return None
    return markets[idx - 1]
