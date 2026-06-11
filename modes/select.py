"""
modes/select.py — interactive market pickers shared by the CLI modes.

Moved verbatim from main.py (B13).

--token launches OBIApp directly (B19). The pairs menu here is the
no-flag default since the synthetic engine's removal (2026-06-10).
"""

import logging

from rich.console import Console
from rich.prompt import IntPrompt


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

