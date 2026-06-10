"""
research_cli.py — Command-line interface for Phase 4 research tools.

Run offline analysis on stored OBI data.

USAGE:
    python research_cli.py heatmap                    # Generate heatmap HTML
    python research_cli.py heatmap --minutes 120      # Last 2 hours
    python research_cli.py replay                     # Replay and show summary
    python research_cli.py replay --minutes 30        # Last 30 minutes
    python research_cli.py backtest                   # Full backtest with report
    python research_cli.py backtest --minutes 120     # Backtest last 2 hours
    python research_cli.py export                     # Export all data to CSV/JSON
    python research_cli.py export --minutes 60        # Last hour
    python research_cli.py summary                    # Show database summary

BEGINNER NOTE:
These tools all work OFFLINE — they read from the SQLite database
that the live OBI system populates. Run OBI live for a while first
to collect data, then use these tools to analyze it.
"""

import argparse
import sys
import os

# Add parent directory to path so we can import our modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def cmd_heatmap(args):
    """Generate an order book heatmap."""
    from research.heatmap import generate_heatmap

    print(f"Generating heatmap for the last {args.minutes} minutes...")
    try:
        path = generate_heatmap(
            since_minutes=args.minutes,
            output_path=args.output or "heatmap.html",
            price_levels=args.levels,
            time_buckets=args.buckets,
        )
        print(f"✓ Heatmap saved to: {path}")
        print(f"  Open in your browser: file://{os.path.abspath(path)}")
    except (FileNotFoundError, ValueError) as e:
        print(f"✗ Error: {e}")


def cmd_replay(args):
    """Replay stored data through analytics."""
    from research.replay import replay_session

    print(f"Replaying last {args.minutes} minutes...")
    try:
        result = replay_session(
            since_minutes=args.minutes,
            verbose=True,
        )

        print(f"\n{'=' * 60}")
        print("  REPLAY SUMMARY")
        print(f"{'=' * 60}")
        summary = result.summary()
        for key, val in summary.items():
            if isinstance(val, dict):
                print(f"  {key}:")
                for k, v in val.items():
                    print(f"    {k}: {v}")
            else:
                print(f"  {key}: {val}")
        print(f"{'=' * 60}")

        # Show top insights
        if result.insights:
            print(f"\nTop insights ({min(10, len(result.insights))} of {len(result.insights)}):")
            alerts = [i for i in result.insights if i.severity == "alert"][:5]
            warnings = [i for i in result.insights if i.severity == "warning"][:5]
            for insight in (alerts + warnings)[:10]:
                print(f"  [{insight.severity:7s}] {insight.message}")

    except (FileNotFoundError, ValueError) as e:
        print(f"✗ Error: {e}")


def cmd_backtest(args):
    """Run backtest on stored data."""
    from research.replay import replay_session
    from research.backtest import run_backtest
    from research.export import export_backtest_json

    print(f"Running backtest on last {args.minutes} minutes...")
    try:
        # Step 1: Replay
        result = replay_session(since_minutes=args.minutes, verbose=True)

        # Step 2: Backtest
        report = run_backtest(result, verbose=True)

        # Step 3: Export if requested
        if args.export:
            path = export_backtest_json(report, args.export)
            print(f"\n✓ Report exported to: {path}")

    except (FileNotFoundError, ValueError) as e:
        print(f"✗ Error: {e}")


def cmd_export(args):
    """Export all data to CSV/JSON."""
    from research.export import export_all

    print(f"Exporting last {args.minutes} minutes to {args.output_dir}/...")
    try:
        files = export_all(
            output_dir=args.output_dir,
            since_minutes=args.minutes,
        )

        print(f"\n✓ Exported {len(files)} files:")
        for name, path in files.items():
            size = os.path.getsize(path) if os.path.exists(path) else 0
            print(f"  {name:12s} → {path} ({size:,} bytes)")

    except (FileNotFoundError, ValueError) as e:
        print(f"✗ Error: {e}")


def cmd_summary(args):
    """Show database summary."""
    from research.export import get_db_summary

    print("Database Summary:")
    print(f"{'=' * 60}")

    summary = get_db_summary()
    if "error" in summary:
        print(f"✗ {summary['error']}")
        print("  Run the live OBI system first to collect data.")
        return

    for table, info in summary.items():
        count = info.get("count", 0)
        first = info.get("first", "—")
        last = info.get("last", "—")
        span = info.get("span_minutes", 0)
        print(f"  {table:15s}: {count:>8,} rows | {first} → {last} ({span:.0f} min)")

    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(
        description="OBI Research Tools — Offline analysis of order book data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  heatmap     Generate interactive HTML order book heatmap
  replay      Replay stored data through analytics pipeline
  backtest    Test signal accuracy against price outcomes
  export      Export data to CSV/JSON for external tools
  summary     Show what's in the database
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="Research command")

    # Heatmap
    p_heat = subparsers.add_parser("heatmap", help="Generate order book heatmap")
    p_heat.add_argument("--minutes", type=float, default=60, help="Time window (default: 60)")
    p_heat.add_argument("--output", type=str, default=None, help="Output HTML path")
    p_heat.add_argument("--levels", type=int, default=40, help="Price levels to show")
    p_heat.add_argument("--buckets", type=int, default=200, help="Time slices")

    # Replay
    p_replay = subparsers.add_parser("replay", help="Replay stored data")
    p_replay.add_argument("--minutes", type=float, default=60, help="Time window (default: 60)")

    # Backtest
    p_bt = subparsers.add_parser("backtest", help="Backtest signal accuracy")
    p_bt.add_argument("--minutes", type=float, default=60, help="Time window (default: 60)")
    p_bt.add_argument("--export", type=str, default=None, help="Export report to JSON")

    # Export
    p_exp = subparsers.add_parser("export", help="Export data to CSV/JSON")
    p_exp.add_argument("--minutes", type=float, default=60, help="Time window (default: 60)")
    p_exp.add_argument("--output-dir", type=str, default="exports", help="Output directory")

    # Summary
    subparsers.add_parser("summary", help="Show database summary")

    args = parser.parse_args()

    commands = {
        "heatmap": cmd_heatmap,
        "replay": cmd_replay,
        "backtest": cmd_backtest,
        "export": cmd_export,
        "summary": cmd_summary,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
