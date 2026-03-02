"""
heatmap.py — Order book heatmap generator.

WHAT IS AN ORDER BOOK HEATMAP:
A 2D visualization where:
  - X-axis = time
  - Y-axis = price levels
  - Color intensity = liquidity (size) at that price/time

It reveals patterns invisible in raw numbers:
  - Walls appearing and disappearing over time
  - Liquidity "holes" that precede breakouts
  - Where the market concentrated its depth before a move
  - Spoofing patterns (bright spots that flash and vanish)

OUTPUT:
Generates a self-contained HTML file with an interactive heatmap
using only inline CSS/JS (no external dependencies). You can open
it in any browser, zoom, and hover for details.

DATA SOURCE:
Reads from the SQLite database (ob_snapshots table) populated
by the live OBI system. Can also accept data directly for testing.

BEGINNER NOTE:
This is an OFFLINE tool — run it after a trading session to
visualize what happened. It doesn't run in real-time (that's
what the terminal UI is for).
"""

import json
import sqlite3
import time
import os
import logging
from typing import Optional
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)


def generate_heatmap(
    db_path: str = None,
    token_id: str = None,
    since_minutes: float = 60,
    output_path: str = "heatmap.html",
    price_levels: int = 40,
    time_buckets: int = 200,
) -> str:
    """
    Generate an interactive HTML heatmap from stored order book data.

    Args:
        db_path: Path to SQLite database (default: settings.DB_PATH)
        token_id: Filter by token ID (None = use latest)
        since_minutes: How far back to look (default: last 60 minutes)
        output_path: Where to save the HTML file
        price_levels: Number of price levels on Y-axis
        time_buckets: Number of time slices on X-axis

    Returns:
        Path to the generated HTML file
    """
    db_path = db_path or settings.DB_PATH

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}. Run OBI first to collect data.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Determine token_id if not specified
    if not token_id:
        row = conn.execute("SELECT DISTINCT token_id FROM ob_snapshots ORDER BY timestamp_ms DESC LIMIT 1").fetchone()
        if not row:
            conn.close()
            raise ValueError("No snapshot data found in database.")
        token_id = row["token_id"]

    # Fetch snapshots
    cutoff_ms = int((time.time() - since_minutes * 60) * 1000)
    rows = conn.execute(
        "SELECT timestamp_ms, bids_json, asks_json, best_bid, best_ask, spread, obi "
        "FROM ob_snapshots WHERE token_id = ? AND timestamp_ms >= ? ORDER BY timestamp_ms",
        (token_id, cutoff_ms),
    ).fetchall()
    conn.close()

    if not rows:
        raise ValueError(f"No snapshots found in the last {since_minutes} minutes.")

    logger.info(f"Loaded {len(rows)} snapshots for heatmap generation")

    # Parse all snapshots
    snapshots = []
    all_prices = set()
    for row in rows:
        bids = json.loads(row["bids_json"])
        asks = json.loads(row["asks_json"])
        ts = row["timestamp_ms"]

        levels = {}
        for b in bids:
            p, s = round(b["p"], 3), b["s"]
            levels[p] = {"size": s, "side": "bid"}
            all_prices.add(p)
        for a in asks:
            p, s = round(a["p"], 3), a["s"]
            levels[p] = {"size": s, "side": "ask"}
            all_prices.add(p)

        snapshots.append({
            "ts": ts,
            "levels": levels,
            "best_bid": row["best_bid"],
            "best_ask": row["best_ask"],
            "spread": row["spread"],
            "obi": row["obi"],
        })

    # Determine price range
    sorted_prices = sorted(all_prices)
    if len(sorted_prices) > price_levels:
        # Focus on the middle range around the most active prices
        mid_idx = len(sorted_prices) // 2
        half = price_levels // 2
        sorted_prices = sorted_prices[max(0, mid_idx - half): mid_idx + half]

    # Downsample time if needed
    if len(snapshots) > time_buckets:
        step = len(snapshots) / time_buckets
        sampled = [snapshots[int(i * step)] for i in range(time_buckets)]
    else:
        sampled = snapshots

    # Build heatmap data grid: [time_idx][price_idx] = {size, side}
    grid_data = []
    time_labels = []
    for snap in sampled:
        t = datetime.fromtimestamp(snap["ts"] / 1000)
        time_labels.append(t.strftime("%H:%M:%S"))

        row_data = []
        for price in sorted_prices:
            info = snap["levels"].get(price, {"size": 0, "side": "none"})
            row_data.append({
                "size": info["size"],
                "side": info["side"],
            })
        grid_data.append(row_data)

    # Find max size for color scaling
    max_size = max(
        cell["size"]
        for row_data in grid_data
        for cell in row_data
    ) if grid_data else 1

    # Build midpoint trace
    midpoints = []
    for snap in sampled:
        bb = snap["best_bid"]
        ba = snap["best_ask"]
        if bb and ba:
            midpoints.append((bb + ba) / 2)
        else:
            midpoints.append(None)

    # Generate HTML
    html = _render_html(
        grid_data=grid_data,
        price_labels=[f"{p:.3f}" for p in sorted_prices],
        time_labels=time_labels,
        max_size=max_size,
        midpoints=midpoints,
        sorted_prices=sorted_prices,
        token_id=token_id[:20] + "...",
        since_minutes=since_minutes,
        snapshot_count=len(rows),
    )

    with open(output_path, "w") as f:
        f.write(html)

    logger.info(f"Heatmap saved to {output_path}")
    return output_path


def _render_html(
    grid_data, price_labels, time_labels, max_size,
    midpoints, sorted_prices, token_id, since_minutes, snapshot_count,
) -> str:
    """Render the heatmap as a self-contained HTML file."""

    num_times = len(time_labels)
    num_prices = len(price_labels)

    # Pre-compute cell colors as a flat list for JS
    cells_json = []
    for t_idx, row_data in enumerate(grid_data):
        for p_idx, cell in enumerate(row_data):
            if cell["size"] > 0:
                intensity = min(cell["size"] / max_size, 1.0)
                cells_json.append({
                    "t": t_idx,
                    "p": p_idx,
                    "s": round(cell["size"], 1),
                    "i": round(intensity, 3),
                    "side": cell["side"],
                })

    # Midpoint trace as price indices (for overlay line)
    mid_indices = []
    for mp in midpoints:
        if mp and sorted_prices:
            # Find closest price index
            closest = min(range(len(sorted_prices)), key=lambda i: abs(sorted_prices[i] - mp))
            mid_indices.append(closest)
        else:
            mid_indices.append(None)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>OBI Heatmap — {token_id}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: #0a0a0f; color: #e0e0e0; font-family: 'SF Mono', 'Fira Code', monospace; }}
.header {{ padding: 16px 24px; background: #12121a; border-bottom: 1px solid #2a2a3a; display: flex; justify-content: space-between; align-items: center; }}
.header h1 {{ font-size: 18px; color: #6cb4ee; }}
.header .meta {{ font-size: 12px; color: #888; }}
.container {{ padding: 20px; display: flex; gap: 8px; }}
.y-labels {{ display: flex; flex-direction: column-reverse; justify-content: space-between; padding: 2px 8px 2px 0; font-size: 10px; color: #888; min-width: 60px; text-align: right; }}
.heatmap-wrap {{ flex: 1; overflow-x: auto; }}
.heatmap {{ position: relative; }}
canvas {{ display: block; cursor: crosshair; }}
.x-labels {{ display: flex; justify-content: space-between; padding: 4px 0; font-size: 10px; color: #888; }}
.tooltip {{ position: fixed; background: #1a1a2e; border: 1px solid #3a3a5a; border-radius: 6px; padding: 8px 12px; font-size: 12px; pointer-events: none; display: none; z-index: 100; }}
.tooltip .price {{ color: #6cb4ee; font-weight: bold; }}
.tooltip .size {{ color: #4ade80; }}
.tooltip .side-bid {{ color: #4ade80; }}
.tooltip .side-ask {{ color: #f87171; }}
.legend {{ display: flex; gap: 24px; padding: 12px 24px; background: #12121a; border-top: 1px solid #2a2a3a; font-size: 12px; align-items: center; }}
.legend-bar {{ width: 200px; height: 14px; border-radius: 4px; }}
.legend span {{ color: #888; }}
</style>
</head>
<body>
<div class="header">
    <h1>📊 Order Book Heatmap</h1>
    <div class="meta">Token: {token_id} &nbsp;|&nbsp; Last {since_minutes:.0f} min &nbsp;|&nbsp; {snapshot_count} snapshots &nbsp;|&nbsp; {num_prices} price levels × {num_times} time slices</div>
</div>

<div class="container">
    <div class="y-labels" id="yLabels"></div>
    <div class="heatmap-wrap">
        <div class="heatmap">
            <canvas id="heatmap"></canvas>
        </div>
        <div class="x-labels" id="xLabels"></div>
    </div>
</div>

<div class="tooltip" id="tooltip"></div>

<div class="legend">
    <span>Bid liquidity:</span>
    <canvas id="bidLegend" class="legend-bar" width="200" height="14"></canvas>
    <span>Ask liquidity:</span>
    <canvas id="askLegend" class="legend-bar" width="200" height="14"></canvas>
    <span style="color: #fbbf24;">━ Midpoint</span>
    <span>&nbsp;|&nbsp; Max size: {max_size:,.0f}</span>
</div>

<script>
const cells = {json.dumps(cells_json)};
const priceLabels = {json.dumps(price_labels)};
const timeLabels = {json.dumps(time_labels)};
const midIndices = {json.dumps(mid_indices)};
const numTimes = {num_times};
const numPrices = {num_prices};
const maxSize = {max_size};

const CELL_W = Math.max(3, Math.min(8, Math.floor(1400 / numTimes)));
const CELL_H = Math.max(4, Math.min(14, Math.floor(600 / numPrices)));

const canvas = document.getElementById('heatmap');
const ctx = canvas.getContext('2d');
canvas.width = numTimes * CELL_W;
canvas.height = numPrices * CELL_H;

// Draw background
ctx.fillStyle = '#0a0a0f';
ctx.fillRect(0, 0, canvas.width, canvas.height);

// Draw cells
for (const c of cells) {{
    const x = c.t * CELL_W;
    const y = (numPrices - 1 - c.p) * CELL_H;
    if (c.side === 'bid') {{
        const g = Math.floor(100 + c.i * 155);
        ctx.fillStyle = `rgba(74, ${{g}}, 80, ${{0.3 + c.i * 0.7}})`;
    }} else {{
        const r = Math.floor(100 + c.i * 155);
        ctx.fillStyle = `rgba(${{r}}, 60, 60, ${{0.3 + c.i * 0.7}})`;
    }}
    ctx.fillRect(x, y, CELL_W, CELL_H);
}}

// Draw midpoint line
ctx.strokeStyle = '#fbbf24';
ctx.lineWidth = 1.5;
ctx.setLineDash([3, 2]);
ctx.beginPath();
let started = false;
for (let t = 0; t < midIndices.length; t++) {{
    if (midIndices[t] !== null) {{
        const x = t * CELL_W + CELL_W / 2;
        const y = (numPrices - 1 - midIndices[t]) * CELL_H + CELL_H / 2;
        if (!started) {{ ctx.moveTo(x, y); started = true; }}
        else ctx.lineTo(x, y);
    }}
}}
ctx.stroke();

// Y-axis labels (every Nth price)
const yDiv = document.getElementById('yLabels');
yDiv.style.height = canvas.height + 'px';
const yStep = Math.max(1, Math.floor(numPrices / 15));
for (let i = 0; i < numPrices; i += yStep) {{
    const label = document.createElement('div');
    label.textContent = priceLabels[i];
    label.style.lineHeight = (CELL_H * yStep) + 'px';
    yDiv.appendChild(label);
}}

// X-axis labels
const xDiv = document.getElementById('xLabels');
xDiv.style.width = canvas.width + 'px';
const xStep = Math.max(1, Math.floor(numTimes / 10));
for (let i = 0; i < numTimes; i += xStep) {{
    const label = document.createElement('span');
    label.textContent = timeLabels[i];
    xDiv.appendChild(label);
}}

// Legend bars
function drawLegend(id, r, g, b) {{
    const c = document.getElementById(id).getContext('2d');
    for (let x = 0; x < 200; x++) {{
        const i = x / 200;
        c.fillStyle = `rgba(${{r}}, ${{Math.floor(g[0] + i * (g[1] - g[0]))}}, ${{b}}, ${{0.3 + i * 0.7}})`;
        c.fillRect(x, 0, 1, 14);
    }}
}}
drawLegend('bidLegend', 74, [100, 255], 80);
drawLegend('askLegend', [100, 255], 60, 60);

// Tooltip
const tooltip = document.getElementById('tooltip');
canvas.addEventListener('mousemove', (e) => {{
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const tIdx = Math.floor(x / CELL_W);
    const pIdx = numPrices - 1 - Math.floor(y / CELL_H);

    if (tIdx >= 0 && tIdx < numTimes && pIdx >= 0 && pIdx < numPrices) {{
        const cell = cells.find(c => c.t === tIdx && c.p === pIdx);
        const size = cell ? cell.s : 0;
        const side = cell ? cell.side : 'empty';
        const sideClass = side === 'bid' ? 'side-bid' : side === 'ask' ? 'side-ask' : '';

        tooltip.innerHTML = `
            <div>Time: ${{timeLabels[tIdx]}}</div>
            <div class="price">Price: ${{priceLabels[pIdx]}}</div>
            <div class="size ${{sideClass}}">Size: ${{size.toLocaleString()}} (${{side}})</div>
        `;
        tooltip.style.display = 'block';
        tooltip.style.left = (e.clientX + 12) + 'px';
        tooltip.style.top = (e.clientY - 40) + 'px';
    }}
}});
canvas.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});
</script>
</body>
</html>"""
