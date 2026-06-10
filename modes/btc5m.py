"""
modes/btc5m.py — auto-rotating BTC 5-minute windows around OBIApp.

Moved verbatim from main.py (B13).
"""

import asyncio
import logging

from rich.console import Console

from config.live_config import LiveConfig
from data.websocket_client import WebSocketClient
from modes.intelligence import OBIApp

logger = logging.getLogger(__name__)
console = Console()


async def run_btc5m(args):
    """
    Auto-rotating BTC 5-minute mode.

    Automatically detects the current 5-minute window,
    connects to it, and rotates to the next one when it expires.
    Keeps paper trading stats across rotations.
    """
    from execution.market_rotator import MarketRotator

    console.print("\n[bold cyan]═══ BTC 5-Minute Auto Mode ═══[/bold cyan]")
    console.print(f"[dim]Mode: {args.mode.upper()} | Config: config/strategy.conf (edit live)[/dim]")
    console.print("[dim]Auto-rotates every 5 minutes[/dim]\n")

    live_conf = LiveConfig()
    side = live_conf.rotation_side if args.btc5m_side == "auto" else args.btc5m_side
    rotator = MarketRotator(token_side=side)
    window = await rotator.start()

    if not window:
        console.print("[red]Could not find current BTC 5-min market. Retrying in 10s...[/red]")
        await asyncio.sleep(10)
        window = await rotator.start()

    if not window:
        console.print("[red]Still no market found. Check your internet connection.[/red]")
        return

    console.print(f"[green]Found:[/green] {window.question}")
    console.print(f"[dim]Window: {window.time_label} ({window.seconds_remaining:.0f}s remaining)[/dim]")
    console.print(f"[dim]Token: {rotator.get_active_token_id()[:40]}...[/dim]\n")

    # Run in a loop, rotating every 5 minutes
    while True:
        token_id = rotator.get_active_token_id()
        token_label = rotator.get_token_label()
        question = f"BTC Up/Down 5m — {window.time_label}"

        app = OBIApp(
            token_id=token_id,
            market_question=question,
            token_label=token_label,
            trading_mode=args.mode,
        )

        # Run until window is about to expire
        try:
            # Start components
            if app.db:
                await app.db.initialize()
            await app.telegram.start()

            app._ws_client = WebSocketClient(
                token_id=token_id,
                on_message=app._handle_message,
                on_connected=app._on_connected,
                on_disconnected=app._on_disconnected,
            )

            # Run WebSocket, UI, and key listener concurrently
            # But also run a rotation checker
            async def rotation_watchdog():
                """Watch for window expiry and cancel tasks."""
                while not rotator.should_rotate():
                    remaining = rotator.current_window.seconds_remaining if rotator.current_window else 0
                    app.ui.set_rotation_info(remaining, rotator.rotation_count)
                    await asyncio.sleep(1)
                # Time to rotate
                logger.info("Window expiring, rotating...")

            tasks = [
                asyncio.create_task(app._ws_client.start()),
                asyncio.create_task(app._run_ui()),
                asyncio.create_task(app._key_listener()),
                asyncio.create_task(rotation_watchdog()),
            ]
            if app.db:
                tasks.append(asyncio.create_task(app._update_db_stats_loop()))

            # Wait for rotation watchdog to finish (or Ctrl+C)
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except asyncio.CancelledError:
            break
        except KeyboardInterrupt:
            break
        finally:
            await app._shutdown()

        # Rotate to next window — keep trying until we find one
        console.print("\n[yellow]Rotating to next 5-minute window...[/yellow]")
        await asyncio.sleep(3)  # Small delay for next window to appear

        window = None
        retry_count = 0
        max_retries = 60  # Try for up to 5 minutes (60 * 5s)

        while window is None and retry_count < max_retries:
            window = await rotator.rotate()
            if window:
                break
            retry_count += 1
            wait_time = min(5 + retry_count, 15)  # 5s, then up to 15s
            console.print(
                f"[yellow]Waiting for next window... "
                f"(attempt {retry_count}/{max_retries}, "
                f"retry in {wait_time}s)[/yellow]"
            )
            await asyncio.sleep(wait_time)

        if not window:
            console.print("[red]Could not find window after 5 minutes. Retrying from scratch...[/red]")
            await asyncio.sleep(10)
            continue  # Go back to top of while loop instead of breaking

        console.print(f"[green]New window:[/green] {window.question} ({window.time_label})")

    await rotator.stop()
    console.print("\n[cyan]BTC 5m session complete.[/cyan]")
