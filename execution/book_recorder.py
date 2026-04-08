"""
book_recorder.py — L2 order book data recorder for backtesting.

Records order book snapshots and trades to CSV without trading.
Piggybacks on the same WebSocket feed the bot uses.

Upgrades:
  - Snapshot on every trade event (+ 30s periodic fallback)
  - Latency tracking: local_ms vs server timestamp_ms
  - BTC oracle price from Chainlink WS on every snapshot/trade
  - Fee rate metadata per window

Usage:
    python3 main.py --record --asset btc --timeframe 15m
"""

import asyncio
import csv
import json
import os
import time
import logging
import signal

import websockets
from websockets.exceptions import ConnectionClosed

from rich.console import Console

from config import settings
from data.message_parser import parse_messages
from data.models import BookSnapshot, PriceChangeEvent, TradeEvent
from state.orderbook import OrderBook
from execution.market_spec import MarketSpec, make_market_spec
from execution.market_rotator import MarketRotator, MarketWindow
from execution.pair_strategy import polymarket_taker_fee

logger = logging.getLogger(__name__)
console = Console()

LOG_DIR = "data/logs"
SNAPSHOT_INTERVAL = 30  # seconds between periodic L2 snapshots
BOOK_DEPTH = 10         # top N levels per side

CHAINLINK_WS = "wss://ws-live-data.polymarket.com"


def _ensure_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _date_str():
    return time.strftime("%Y%m%d")


class _CSVWriter:
    """Lazy CSV writer — creates file with header on first row."""

    def __init__(self, prefix: str, header: list[str]):
        self._prefix = prefix
        self._header = header
        self._current_date = None
        self._file = None
        self._writer = None

    def write(self, row: list):
        today = _date_str()
        if today != self._current_date:
            self.close()
            self._current_date = today
            _ensure_dir()
            path = os.path.join(LOG_DIR, f"{self._prefix}_{today}.csv")
            exists = os.path.exists(path)
            self._file = open(path, "a", newline="")
            self._writer = csv.writer(self._file)
            if not exists:
                self._writer.writerow(self._header)

        self._writer.writerow(row)
        self._file.flush()

    def close(self):
        if self._file:
            self._file.close()
            self._file = None
            self._writer = None


SNAPSHOT_HEADER = [
    "timestamp_ms", "market_slug", "token", "side", "level", "price", "size",
    "btc_price", "trigger",
]
TRADE_HEADER = [
    "timestamp_ms", "local_ms", "latency_ms",
    "market_slug", "token", "side", "price", "size", "btc_price",
]
WINDOW_HEADER = [
    "timestamp", "market_slug", "timeframe", "start_ts", "end_ts",
    "up_token_id", "down_token_id", "fee_rate_bps_at_50c",
]


class _ChainlinkTracker:
    """Lightweight BTC price tracker for the recorder."""

    def __init__(self, symbol: str = "btc/usd"):
        self.symbol = symbol
        self.latest_price: float | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self):
        self._running = True
        self._task = asyncio.create_task(self._ws_loop())

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _ws_loop(self):
        while self._running:
            try:
                async with websockets.connect(
                    CHAINLINK_WS, ping_interval=30, ping_timeout=10,
                ) as ws:
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": json.dumps({"symbol": self.symbol}),
                        }],
                    }))
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            price = msg.get("payload", {}).get("value")
                            if price is not None:
                                self.latest_price = float(price)
                        except (json.JSONDecodeError, ValueError):
                            continue
            except asyncio.CancelledError:
                return
            except Exception:
                if self._running:
                    await asyncio.sleep(3)


class BookRecorder:
    """Records L2 order book snapshots and trades to CSV."""

    def __init__(self, spec: MarketSpec):
        self.spec = spec
        self.yes_book = OrderBook()
        self.no_book = OrderBook()
        self._snap_writer = _CSVWriter("l2_snapshots", SNAPSHOT_HEADER)
        self._trade_writer = _CSVWriter("l2_trades", TRADE_HEADER)
        self._window_writer = _CSVWriter("l2_windows", WINDOW_HEADER)
        self._last_periodic_snap = 0.0
        self._stop = False
        self._msg_count = 0
        self._snap_count = 0
        self._trade_count = 0
        self._windows_recorded = 0
        self._oracle = _ChainlinkTracker()

    def request_stop(self):
        self._stop = True

    async def run(self):
        console.print(f"\n[bold cyan]  L2 RECORDER — {self.spec.display_name}[/bold cyan]")
        console.print(f"  Snapshot interval: {SNAPSHOT_INTERVAL}s + on-trade | Depth: {BOOK_DEPTH} levels")
        console.print(f"  Oracle: Chainlink BTC/USD | Latency: server vs local")
        console.print(f"  Output: {LOG_DIR}/l2_*.csv")
        console.print(f"  [dim]Press Ctrl+C to stop[/dim]\n")

        self._oracle.start()

        rotator = MarketRotator(spec=self.spec, token_side="auto")
        window = await rotator.start()

        if not window:
            console.print("[yellow]  Waiting for market window...[/yellow]")
            for _ in range(12):
                await asyncio.sleep(5)
                window = await rotator.start()
                if window:
                    break

        if not window:
            console.print("[red]  No market found. Check connection.[/red]")
            self._oracle.stop()
            return

        try:
            while not self._stop:
                await self._record_window(window)
                self._windows_recorded += 1

                if self._stop:
                    break

                console.print(f"  [yellow]Rotating to next window...[/yellow]")
                await asyncio.sleep(3)

                window = None
                for attempt in range(60):
                    window = await rotator.rotate()
                    if window:
                        break
                    await asyncio.sleep(min(5 + attempt, 15))

                if not window:
                    console.print("[red]  Rotation failed — restarting rotator[/red]")
                    try:
                        await rotator.stop()
                    except Exception:
                        pass
                    rotator = MarketRotator(spec=self.spec, token_side="auto")
                    window = await rotator.start()
                    if not window:
                        console.print("[red]  Still no market. Stopping recorder.[/red]")
                        break
        finally:
            self._close()
            self._oracle.stop()
            await rotator.stop()
            console.print(f"\n[bold cyan]  Recording stopped.[/bold cyan]")
            console.print(f"  Windows: {self._windows_recorded} | Snapshots: {self._snap_count} | Trades: {self._trade_count}")

    async def _record_window(self, window: MarketWindow):
        """Record one market window."""
        yes_id = window.up_token_id
        no_id = window.down_token_id
        slug = window.slug

        console.print(f"  [green]Recording:[/green] {window.question}")
        rem = window.seconds_remaining
        t_str = f"{rem/60:.1f}m" if rem >= 120 else f"{rem:.0f}s"
        console.print(f"  [dim]{t_str} remaining[/dim]")

        # Fee rate at 50c (peak fee) in basis points
        fee_at_50c = polymarket_taker_fee(0.50)
        fee_bps = round(fee_at_50c * 10000, 1)

        self._window_writer.write([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            slug, self.spec.timeframe,
            window.start_ts, window.end_ts,
            yes_id, no_id, fee_bps,
        ])

        # Reset books
        self.yes_book = OrderBook()
        self.no_book = OrderBook()
        self._last_periodic_snap = 0.0

        uri = settings.CLOB_WS_URL
        reconnect_count = 0

        while not self._stop and window.seconds_remaining > 5:
            try:
                async with websockets.connect(
                    uri, ping_interval=20, ping_timeout=10, close_timeout=5,
                ) as ws:
                    await ws.send(json.dumps({
                        "type": "market",
                        "assets_ids": [yes_id, no_id],
                    }))

                    async for raw in ws:
                        if self._stop or window.seconds_remaining <= 2:
                            break

                        self._msg_count += 1
                        local_ms = int(time.time() * 1000)

                        for parsed in parse_messages(raw):
                            asset_id = getattr(parsed, "asset_id", "")

                            if isinstance(parsed, BookSnapshot):
                                if asset_id == yes_id:
                                    self.yes_book.apply_snapshot(parsed)
                                elif asset_id == no_id:
                                    self.no_book.apply_snapshot(parsed)
                                self._maybe_periodic_snapshot(slug, "periodic")

                            elif isinstance(parsed, PriceChangeEvent):
                                has_yes = any(c.asset_id == yes_id for c in parsed.price_changes)
                                has_no = any(c.asset_id == no_id for c in parsed.price_changes)
                                if has_yes:
                                    self.yes_book.apply_price_change(parsed)
                                if has_no:
                                    self.no_book.apply_price_change(parsed)
                                self._maybe_periodic_snapshot(slug, "periodic")

                            elif isinstance(parsed, TradeEvent):
                                token_label = "YES" if asset_id == yes_id else "NO"
                                server_ms = parsed.timestamp_ms
                                latency_ms = local_ms - server_ms if server_ms else 0
                                btc = self._oracle.latest_price or 0

                                self._trade_writer.write([
                                    server_ms, local_ms, latency_ms,
                                    slug, token_label,
                                    parsed.side.value, parsed.price, parsed.size,
                                    btc,
                                ])
                                self._trade_count += 1

                                # Snapshot on trade
                                self._log_snapshot(slug, "trade")

            except ConnectionClosed:
                reconnect_count += 1
                if window.seconds_remaining > 10:
                    await asyncio.sleep(1)
                    continue
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Recorder WS error: {e}")
                reconnect_count += 1
                if window.seconds_remaining > 10:
                    await asyncio.sleep(2)
                    continue
                break

        console.print(f"  [dim]Window done — {self._snap_count} snapshots, {self._trade_count} trades total[/dim]")

    def _maybe_periodic_snapshot(self, slug: str, trigger: str):
        """Write L2 snapshot if 30s have passed since last periodic."""
        now = time.time()
        if now - self._last_periodic_snap < SNAPSHOT_INTERVAL:
            return
        self._last_periodic_snap = now
        self._log_snapshot(slug, trigger)

    def _log_snapshot(self, slug: str, trigger: str):
        """Write full L2 book state to CSV."""
        ts_ms = int(time.time() * 1000)
        btc = self._oracle.latest_price or 0

        for token_label, book in [("YES", self.yes_book), ("NO", self.no_book)]:
            for i, level in enumerate(book.get_sorted_bids(BOOK_DEPTH)):
                self._snap_writer.write([
                    ts_ms, slug, token_label, "BID", i + 1, level.price, level.size,
                    btc, trigger,
                ])
            for i, level in enumerate(book.get_sorted_asks(BOOK_DEPTH)):
                self._snap_writer.write([
                    ts_ms, slug, token_label, "ASK", i + 1, level.price, level.size,
                    btc, trigger,
                ])

        self._snap_count += 1

    def _close(self):
        self._snap_writer.close()
        self._trade_writer.close()
        self._window_writer.close()
