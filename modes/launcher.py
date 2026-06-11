"""
modes/launcher.py — the OBI main menu: one front door for every bot.

`python main.py` with no flags lands here. Each bot registers a slot in
BOTS; adding a future bot (weather, directional, ...) means writing its
module and appending one BotEntry — the menu renders itself.

Navigation contract:
- Ctrl+C inside a running bot returns to this menu.
- Ctrl+C (or q) at the menu exits to the terminal.
- Live trading is gated by the typed-`yes` confirmation no matter which
  path reaches it.

CLI flags in main.py bypass this menu entirely — scripted/overnight
runs (nohup/caffeinate) cannot answer prompts.
"""

import asyncio
import logging
import signal
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from rich.console import Console
from rich.prompt import IntPrompt, Prompt

from execution.kill_switch import KillSwitch
from execution.market_spec import make_market_spec
from execution.pair_runner import PairRunner

logger = logging.getLogger(__name__)
console = Console()

CRYPTO_MENU = [
    ("btc", "BTC", "Bitcoin"),
    ("eth", "ETH", "Ethereum"),
    ("sol", "SOL", "Solana"),
    ("xrp", "XRP", "Ripple"),
]
TIMEFRAME_MENU = [
    ("5m", "5 minutes"),
    ("15m", "15 minutes"),
]
PAIRS_MODE_MENU = [
    ("paper", "Paper", "Simulated fills, no auth needed"),
    ("dry-run", "Dry Run", "Signs real orders but never posts them"),
    ("live", "Live", "Real FOK orders to Polymarket — typed-yes gate"),
    ("headless", "Headless", "Both timeframes, no dashboard, CSV only (then paper or live)"),
]


# ──────────────────────────────────────────────────────────────
# Shared gates & pickers
# ──────────────────────────────────────────────────────────────

def confirm_live_mode(
    asset: Optional[str] = None,
    timeframe: Optional[str] = None,
    max_loss: Optional[float] = None,
    assume_yes: bool = False,
) -> bool:
    """
    Gate real-money trading behind an explicit typed confirmation.

    Scripted/non-interactive runs must pass --yes; an interactive run
    must type 'yes' exactly.
    """
    if assume_yes:
        return True

    console.print("\n[bold red]═══ LIVE MODE — REAL MONEY ═══[/bold red]")
    console.print(f"  Asset/timeframe: {(asset or 'btc').upper()}/{timeframe or '5m+15m'}")
    console.print(
        f"  Kill switch:     "
        f"{'$' + str(max_loss) if max_loss else '[bold red]NONE SET[/bold red] (consider --max-loss)'}"
    )
    console.print("  Orders are FOK against the Polymarket CLOB and cannot be recalled.\n")

    if not sys.stdin.isatty():
        console.print("[red]Non-interactive session: pass --yes to confirm live mode.[/red]")
        return False

    answer = input("Type 'yes' to start live trading: ").strip().lower()
    if answer != "yes":
        console.print("[yellow]Live mode not confirmed.[/yellow]")
        return False
    return True


def _pick_asset() -> str:
    console.print("\n[bold]Select crypto:[/bold]")
    for i, (_, sym, name) in enumerate(CRYPTO_MENU, 1):
        console.print(f"  [cyan]{i}[/cyan] — {sym} ({name})")
    c = IntPrompt.ask("Crypto", default=1)
    c = max(1, min(c, len(CRYPTO_MENU)))
    return CRYPTO_MENU[c - 1][0]


def _pick_timeframe() -> str:
    console.print("\n[bold]Select timeframe:[/bold]")
    for i, (_, label) in enumerate(TIMEFRAME_MENU, 1):
        console.print(f"  [cyan]{i}[/cyan] — {label}")
    t = IntPrompt.ask("Timeframe", default=1)
    t = max(1, min(t, len(TIMEFRAME_MENU)))
    return TIMEFRAME_MENU[t - 1][0]


# ──────────────────────────────────────────────────────────────
# Bot runners (shared by the menu and the CLI-flag bypasses)
# ──────────────────────────────────────────────────────────────

async def run_pairs(spec, mode: str, max_loss: Optional[float]) -> None:
    """Single pair-trading runner with the live dashboard."""
    runner = PairRunner(mode=mode, spec=spec, max_loss=max_loss)
    await runner.run()


async def run_headless(
    asset: str,
    mode: str,
    timeframes: list[str],
    max_loss: Optional[float],
) -> None:
    """Multi-timeframe headless pair trading with one shared kill switch (B8)."""
    console.print("\n[bold #00FFFF]═══ HEADLESS MODE ═══[/bold #00FFFF]")
    console.print(f"  Asset: [bold]{asset.upper()}[/bold]  Timeframes: {', '.join(timeframes)}")
    console.print(f"  Mode: [bold]{mode}[/bold]  Runners: {len(timeframes)}")
    console.print("  Logging to: data/logs/")
    console.print("  [dim]Press Ctrl+C to stop all runners[/dim]\n")

    shared_switch = KillSwitch(max_loss)

    runners = []
    for tf in timeframes:
        spec = make_market_spec(asset, tf)
        runner = PairRunner(
            mode=mode, spec=spec, headless=True,
            max_loss=max_loss, kill_switch=shared_switch,
        )
        runners.append(runner)
        console.print(f"  [#00FF41]●[/#00FF41] {asset.upper()}/{tf} runner created")
    console.print()

    tasks = [asyncio.create_task(r.run()) for r in runners]

    # First Ctrl+C → graceful stop, second → force kill
    stop_count = 0

    def _headless_stop(sig, frame):
        nonlocal stop_count
        stop_count += 1
        if stop_count == 1:
            console.print(
                "\n[bold yellow]⚠  Ctrl+C — stopping all runners after current windows settle...[/bold yellow]"
            )
            for r in runners:
                r.request_stop()
        else:
            console.print("\n[bold red]Force stopping — flushing reports...[/bold red]")
            for r in runners:
                try:
                    r._print_session_summary()
                except Exception:
                    pass
            for t in tasks:
                t.cancel()

    signal.signal(signal.SIGINT, _headless_stop)
    try:
        # return_exceptions=True so one runner's failure doesn't cancel
        # its siblings; surface each error individually.
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            return

        for runner, result in zip(runners, results):
            if isinstance(result, asyncio.CancelledError) or result is None:
                continue
            if isinstance(result, BaseException):
                console.print(
                    f"[bold red]✗ {runner.spec.display_name} runner failed:[/bold red] "
                    f"{type(result).__name__}: {result}"
                )
    finally:
        # The menu loop needs default Ctrl+C behavior back
        signal.signal(signal.SIGINT, signal.default_int_handler)


async def run_recorder(asset: str, timeframe: str) -> None:
    """L2 order book recorder — observe and log, no trading."""
    from execution.book_recorder import BookRecorder

    spec = make_market_spec(asset, timeframe)
    recorder = BookRecorder(spec=spec)
    signal.signal(signal.SIGINT, lambda s, f: recorder.request_stop())
    try:
        await recorder.run()
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler)


# ──────────────────────────────────────────────────────────────
# Menu flows (one per bot slot)
# ──────────────────────────────────────────────────────────────

async def _pairs_flow(max_loss: Optional[float], assume_yes: bool) -> None:
    console.print("\n[bold cyan]═══ Pair Trading ═══[/bold cyan]")
    console.print("[dim]Accumulate matched YES+NO pairs below $1.00 — profit locked at settlement[/dim]\n")
    for i, (_, label, desc) in enumerate(PAIRS_MODE_MENU, 1):
        console.print(f"  [cyan]{i}[/cyan] — {label}  [dim]({desc})[/dim]")
    m = IntPrompt.ask("Mode", default=1)
    m = max(1, min(m, len(PAIRS_MODE_MENU)))
    mode_key = PAIRS_MODE_MENU[m - 1][0]

    if mode_key == "headless":
        console.print("\n[bold]Headless execution mode:[/bold]")
        console.print("  [cyan]1[/cyan] — Paper")
        console.print("  [cyan]2[/cyan] — Live  [dim](real money)[/dim]")
        sub = IntPrompt.ask("Execution", default=1)
        exec_mode = "live" if sub == 2 else "paper"
        asset = _pick_asset()
        if exec_mode == "live" and not confirm_live_mode(asset, "5m+15m", max_loss, assume_yes):
            return
        await run_headless(asset, exec_mode, ["5m", "15m"], max_loss)
        return

    asset = _pick_asset()
    timeframe = _pick_timeframe()
    if mode_key == "live" and not confirm_live_mode(asset, timeframe, max_loss, assume_yes):
        return
    spec = make_market_spec(asset, timeframe)
    console.print(f"\n[green]Selected:[/green] {spec.display_name_long}  •  [bold]{mode_key}[/bold]\n")
    await run_pairs(spec, mode_key, max_loss)


async def _order_book_analysis_flow(max_loss: Optional[float], assume_yes: bool) -> None:
    # Placeholder — Omar is designing this flow tailor-made (top markets /
    # keyword / slug → pick market → intelligence dashboard). Until then,
    # the dashboard is reachable via:  python main.py --token <TOKEN_ID>
    console.print("\n[bold cyan]═══ Order Book Analysis ═══[/bold cyan]")
    console.print("[yellow]  Under construction — being designed tailor-made.[/yellow]")
    console.print("[dim]  Meanwhile: python main.py --token <TOKEN_ID> opens the dashboard directly.[/dim]\n")


async def _recorder_flow(max_loss: Optional[float], assume_yes: bool) -> None:
    console.print("\n[bold cyan]═══ Data Recorder ═══[/bold cyan]")
    console.print("[dim]Records L2 order book snapshots + trades to CSV for backtesting — no trading[/dim]")
    asset = _pick_asset()
    timeframe = _pick_timeframe()
    await run_recorder(asset, timeframe)


# ──────────────────────────────────────────────────────────────
# The registry + main loop
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BotEntry:
    key: str
    title: str
    subtitle: str
    flow: Callable[[Optional[float], bool], Awaitable[None]]


BOTS: list[BotEntry] = [
    BotEntry("pairs", "Pair Trading",
             "YES+NO accumulation on crypto Up/Down windows", _pairs_flow),
    BotEntry("analysis", "Order Book Analysis",
             "Market microstructure dashboard (under construction)", _order_book_analysis_flow),
    BotEntry("recorder", "Data Recorder",
             "Capture L2 book data for backtesting", _recorder_flow),
    # Future bots register here — e.g. ("weather", "Weather Markets", ..., weather_flow)
]


async def run_launcher(max_loss: Optional[float] = None, assume_yes: bool = False) -> None:
    """The main menu loop. Ctrl+C in a bot returns here; q (or Ctrl+C here) exits."""
    while True:
        console.print("\n[bold cyan]═══ OBI — Polymarket Bot Suite ═══[/bold cyan]")
        for i, bot in enumerate(BOTS, 1):
            console.print(f"  [cyan]{i}[/cyan] — {bot.title}  [dim]{bot.subtitle}[/dim]")
        console.print("  [cyan]q[/cyan] — Quit\n")

        choice = Prompt.ask(
            "Select",
            choices=[str(i) for i in range(1, len(BOTS) + 1)] + ["q"],
            default="1",
        )
        if choice == "q":
            console.print("[yellow]Goodbye.[/yellow]")
            return

        bot = BOTS[int(choice) - 1]
        try:
            await bot.flow(max_loss, assume_yes)
        except KeyboardInterrupt:
            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(f"{bot.title} crashed — returning to menu")
            console.print(f"[bold red]  {bot.title} crashed (see obi.log) — back to menu.[/bold red]")
        finally:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        console.print("\n[dim]── back to main menu ──[/dim]")
