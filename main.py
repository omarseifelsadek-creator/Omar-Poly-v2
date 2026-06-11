"""
main.py — CLI entry point for Polymarket OBI.

No flags → the main menu (modes/launcher.py): every bot in one place.
Flags → direct dispatch for scripted runs (nohup/caffeinate can't
answer menus).

HOW TO RUN:
    python main.py                  # Main menu (pair trading / analysis / recorder)
    python main.py --pairs --asset btc --timeframe 5m --mode paper
    python main.py --headless       # Both BTC timeframes, CSV logging only
    python main.py --token <ID>     # Intelligence dashboard on a single token
    python main.py --record         # L2 book recorder for backtesting
"""

import asyncio
import argparse
import logging

from rich.console import Console

from modes.launcher import (
    confirm_live_mode,
    run_headless,
    run_launcher,
    run_pairs,
    run_recorder,
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
  python main.py                              # Main menu (interactive)
  python main.py --pairs --asset btc --timeframe 5m --mode paper
  python main.py --headless --max-loss 50    # Both BTC timeframes, shared kill switch
  python main.py --token <TOKEN_ID>           # Intelligence dashboard on one token
        """,
    )
    parser.add_argument(
        "--token", type=str, default=None,
        help="Polymarket token ID to monitor (intelligence dashboard)",
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
        "--pairs", action="store_true",
        help="Pair trading with dashboard (needs --asset + --timeframe to skip the menu)",
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
        help="Crypto asset (btc, eth, sol, xrp)",
    )
    parser.add_argument(
        "--timeframe", type=str, default=None,
        choices=["5m", "15m"],
        help="Timeframe (5m, 15m)",
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


async def main():
    args = parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Real-money gate for flag-driven runs (menu flows gate themselves)
    if args.mode == "live" and not confirm_live_mode(
        args.asset, args.timeframe, args.max_loss, assume_yes=args.yes
    ):
        return

    # Direct token — intelligence dashboard on a single token
    if args.token:
        from modes.intelligence import OBIApp

        app = OBIApp(
            token_id=args.token,
            market_question="Direct Token Monitor",
            trading_mode=args.mode,
        )
        await app.run()
        return

    # L2 order book recorder
    if args.record:
        await run_recorder(args.asset or "btc", args.timeframe or "5m")
        return

    # Headless multi-runner
    if args.headless:
        timeframes = [args.timeframe] if args.timeframe else ["5m", "15m"]
        await run_headless(args.asset or "btc", args.mode, timeframes, args.max_loss)
        return

    # Pair trading with all params supplied — direct run
    if args.pairs and args.asset and args.timeframe:
        from execution.market_spec import make_market_spec

        spec = make_market_spec(args.asset, args.timeframe)
        await run_pairs(spec, args.mode, args.max_loss)
        return

    # Everything else lands on the main menu (bare --pairs included)
    await run_launcher(max_loss=args.max_loss, assume_yes=args.yes)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down OBI...[/yellow]")
