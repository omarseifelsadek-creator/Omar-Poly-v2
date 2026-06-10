"""
main.py — CLI entry point and mode dispatcher for Polymarket OBI.

Thin by design (B13): parse args, gate live mode, dispatch. The modes
themselves live in modes/ (intelligence dashboard, btc5m rotation,
market pickers) and execution/ (pairs, recorder).

HOW TO RUN:
    python main.py                  # Synthetic Market Microstructure Engine
    python main.py --btc5m          # Auto-rotating intelligence dashboard
    python main.py --pairs          # Pair trading (YES+NO accumulation)
    python main.py --headless       # Both BTC timeframes, CSV logging only
    python main.py --record         # L2 book recorder for backtesting
"""

import asyncio
import argparse
import logging
import sys

from rich.console import Console

from data.rest_client import RestClient
from modes.btc5m import run_btc5m
from modes.select import (
    select_pair_market,
    _extract_both_tokens,
    _select_market_for_engine,
    _display_and_pick_market_raw,
)

# Configure logging (only show warnings+ to avoid cluttering the terminal)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("obi.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

console = Console()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Polymarket Order Book Intelligence (OBI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                              # Interactive market selector
  python main.py --search "bitcoin"           # Search for a market
  python main.py --slug will-btc-hit-100k     # Use Polymarket URL slug
  python main.py --token <TOKEN_ID>           # Direct token ID
        """,
    )
    parser.add_argument(
        "--token", type=str, default=None,
        help="Polymarket token ID to monitor",
    )
    parser.add_argument(
        "--slug", type=str, default=None,
        help="Polymarket market URL slug",
    )
    parser.add_argument(
        "--search", type=str, default=None,
        help="Search for markets by keyword",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--mode", type=str, default="paper", choices=["paper", "dry-run", "live"],
        help="Trading mode: paper (simulated) | dry-run (sign only, no post_order) | live (real orders)",
    )
    parser.add_argument(
        "--btc5m", action="store_true",
        help="Auto-rotating BTC 5-minute Up/Down markets",
    )
    parser.add_argument(
        "--btc5m-side", type=str, default="auto", choices=["auto", "up", "down"],
        help="Which side to watch: auto (picks active side), up, or down",
    )
    parser.add_argument(
        "--pairs", action="store_true",
        help="Pair trading mode: accumulate YES+NO pairs for guaranteed profit",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="No dashboard — run both BTC timeframes (5m + 15m) simultaneously, log to CSV only",
    )
    parser.add_argument(
        "--record", action="store_true",
        help="Record L2 order book snapshots to CSV for backtesting (no trading)",
    )
    parser.add_argument(
        "--asset", type=str, default=None,
        choices=["btc", "eth", "sol", "xrp"],
        help="Crypto asset for pair trading (btc, eth, sol, xrp). Skips interactive menu.",
    )
    parser.add_argument(
        "--timeframe", type=str, default=None,
        choices=["5m", "15m"],
        help="Timeframe for pair trading (5m, 15m). Skips interactive menu.",
    )
    parser.add_argument(
        "--max-loss", type=float, default=None,
        help="Kill switch: stop trading after losing this many dollars (e.g. --max-loss 50)",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the live-mode confirmation prompt (for scripted runs)",
    )
    return parser.parse_args()


def confirm_live_mode(args) -> bool:
    """
    Gate real-money trading behind an explicit typed confirmation.

    Returns True if it is safe to proceed. Scripted/non-interactive runs
    must pass --yes; an interactive run must type 'yes' exactly.
    """
    if args.mode != "live" or args.yes:
        return True

    console.print("\n[bold red]═══ LIVE MODE — REAL MONEY ═══[/bold red]")
    console.print(f"  Asset/timeframe: {(args.asset or 'btc').upper()}/{args.timeframe or '5m'}")
    console.print(f"  Kill switch:     {'$' + str(args.max_loss) if args.max_loss else '[bold red]NONE SET[/bold red] (consider --max-loss)'}")
    console.print("  Orders are FOK against the Polymarket CLOB and cannot be recalled.\n")

    if not sys.stdin.isatty():
        console.print("[red]Non-interactive session: pass --yes to confirm live mode.[/red]")
        return False

    answer = input("Type 'yes' to start live trading: ").strip().lower()
    if answer != "yes":
        console.print("[yellow]Live mode not confirmed — exiting.[/yellow]")
        return False
    return True


# ──────────────────────────────────────────────────────────────
# BTC 5-MINUTE AUTO-ROTATING MODE
# ──────────────────────────────────────────────────────────────



async def main():
    args = parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Real-money gate — covers every mode before any runner is constructed
    if not confirm_live_mode(args):
        return

    # BTC 5-minute auto-rotating mode
    if args.btc5m:
        await run_btc5m(args)
        return

    # L2 order book recorder — observe and log, no trading
    if args.record:
        from execution.book_recorder import BookRecorder
        from execution.market_spec import make_market_spec

        spec = make_market_spec(args.asset or "btc", args.timeframe or "5m")
        recorder = BookRecorder(spec=spec)

        import signal
        signal.signal(signal.SIGINT, lambda s, f: recorder.request_stop())

        await recorder.run()
        return

    # Headless multi-runner: both BTC timeframes simultaneously, no dashboard
    if args.headless:
        from execution.pair_runner import PairRunner
        from execution.market_spec import make_market_spec

        asset = args.asset or "btc"
        mode = args.mode
        timeframes = ["5m", "15m"]

        if args.timeframe:
            # Single headless runner for specific timeframe
            timeframes = [args.timeframe]

        console.print(f"\n[bold #00FFFF]═══ HEADLESS MODE ═══[/bold #00FFFF]")
        console.print(f"  Asset: [bold]{asset.upper()}[/bold]  Timeframes: {', '.join(timeframes)}")
        console.print(f"  Mode: [bold]{mode}[/bold]  Runners: {len(timeframes)}")
        console.print(f"  Logging to: data/logs/")
        console.print(f"  [dim]Press Ctrl+C to stop all runners[/dim]\n")

        # One shared budget: --max-loss caps the SESSION, not each runner (B8)
        from execution.kill_switch import KillSwitch
        shared_switch = KillSwitch(args.max_loss)

        runners = []
        for tf in timeframes:
            spec = make_market_spec(asset, tf)
            runner = PairRunner(
                mode=mode, spec=spec, headless=True,
                max_loss=args.max_loss, kill_switch=shared_switch,
            )
            runners.append(runner)
            console.print(f"  [#00FF41]●[/#00FF41] {asset.upper()}/{tf} runner created")

        console.print()

        # Run all runners as tasks so we can cancel them on Ctrl+C
        tasks = [asyncio.create_task(r.run()) for r in runners]

        # Install signal handler: first Ctrl+C → graceful stop, second → force kill
        import signal
        _stop_count = 0

        def _headless_stop(sig, frame):
            nonlocal _stop_count
            _stop_count += 1
            if _stop_count == 1:
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

        # return_exceptions=True so one runner's MarketDiscoveryError
        # (or any other failure) doesn't cancel its siblings. Surface
        # each error individually so the user knows which timeframe died.
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
        return

    # Pair trading mode
    if args.pairs:
        from execution.pair_runner import PairRunner
        from execution.market_spec import make_market_spec
        if args.asset and args.timeframe:
            spec = make_market_spec(args.asset, args.timeframe)
            mode = args.mode
        else:
            spec, mode = select_pair_market()
        runner = PairRunner(mode=mode, spec=spec, max_loss=args.max_loss)
        await runner.run()
        return

    # ── Default: Synthetic Market Microstructure Engine ──
    from ui.cyber_engine import SyntheticEngine

    yes_token = None
    no_token = None
    question = ""

    if args.slug:
        rest = RestClient()
        market = await rest.get_market_by_slug(args.slug)
        await rest.close()
        if market:
            result = _extract_both_tokens(market)
            if result:
                yes_token, no_token, question = result

    elif args.search:
        rest = RestClient()
        markets = await rest.search_markets(args.search)
        await rest.close()
        if markets:
            selected = _display_and_pick_market_raw(markets)
            if selected:
                result = _extract_both_tokens(selected)
                if result:
                    yes_token, no_token, question = result

    # Interactive selection if nothing resolved yet
    if not yes_token:
        result = await _select_market_for_engine()
        if result:
            yes_token, no_token, question = result

    if not yes_token or not no_token:
        console.print("[red]No market selected (need YES + NO tokens). Exiting.[/red]")
        return

    console.print(f"\n[bold]Market:[/bold] {question}")
    console.print(f"[#00FFFF]YES:[/#00FFFF] {yes_token[:40]}...")
    console.print(f"[#FF1493]NO:[/#FF1493]  {no_token[:40]}...")
    console.print()

    engine = SyntheticEngine(
        yes_token_id=yes_token,
        no_token_id=no_token,
        market_question=question,
    )
    await engine.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down OBI...[/yellow]")
