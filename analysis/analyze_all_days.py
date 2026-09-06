"""Analyze one or more Zendure telemetry exports."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

try:
    from .run_analysis import (
        DEVICE_IDS,
        POWER_THRESHOLD_W,
        analyze_rows,
        find_large_swings,
        find_sustained_periods,
        read_export,
        resolve_export_files,
        select_management_rows,
    )
except ImportError:
    from run_analysis import (
        DEVICE_IDS,
        POWER_THRESHOLD_W,
        analyze_rows,
        find_large_swings,
        find_sustained_periods,
        read_export,
        resolve_export_files,
        select_management_rows,
    )


def format_duration(seconds: float) -> str:
    """Format a duration for the console report."""
    minutes, remaining_seconds = divmod(int(seconds), 60)
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def _format_time(value: Any) -> str:
    return value.strftime("%H:%M:%S")


def _print_periods(title: str, periods: list[dict[str, Any]]) -> None:
    print(f"\n{title}")
    if not periods:
        print("  none")
        return
    for period in periods[:3]:
        print(
            f"  {_format_time(period['start'])} to {_format_time(period['end'])} "
            f"({format_duration(float(period['duration']))}) - Avg: {float(period['avg_sml']):.1f} W"
        )


def analyze_file(
    file_path: str | Path, *, only_unmanaged: tuple[str, ...] = ()
) -> dict[str, Any] | None:
    """Analyze a CSV export and print a routing-aware report."""
    path = Path(file_path)
    print("\n==================================================")
    print(f"Analyzing: {path}")
    print("==================================================")

    if not path.exists():
        print(f"File not found: {path}")
        return None

    raw_count, all_rows = read_export(path)
    rows = select_management_rows(all_rows, unmanaged_devices=only_unmanaged)
    print(f"Rows in scope: {len(rows)}/{raw_count}")
    if only_unmanaged:
        print(f"Scope filter: {', '.join(only_unmanaged)} unmanaged")
    if not rows:
        print("No matching rows.")
        return None
    print(f"Window: {_format_time(rows[0]['time'])} to {_format_time(rows[-1]['time'])}")

    result = analyze_rows(rows)
    print("\nManager participation samples:")
    for device_id in DEVICE_IDS:
        samples = result["management_samples"][device_id]
        print(
            f"  {device_id}: {samples['managed']} managed, "
            f"{samples['unmanaged']} unmanaged, {samples['unknown']} unknown"
        )
    if any(result["management_samples"][device_id]["unknown"] for device_id in DEVICE_IDS):
        print("  Note: unknown participation is excluded from routing-specific metrics.")

    print("\nManaged routing behavior:")
    for device_id in DEVICE_IDS:
        print(f"  {device_id} mode switches: {result['mode_switches'][device_id]}")
    print(
        "  Grid import attributable to managed AC charging: "
        f"{result['grid_import_while_charging_kwh']:.6f} kWh"
    )
    print(f"  Battery-backed grid export: {result['battery_backed_export_kwh']:.6f} kWh")
    print(f"  Grid export while a managed battery was full: {result['full_export_kwh']:.6f} kWh")
    print(f"  Export -> import -> export overcorrection cycles: {len(result['overcorrection_cycles'])}")

    import_periods = find_sustained_periods(
        rows,
        lambda row: row["sml"] is not None and row["sml"] >= POWER_THRESHOLD_W,
        gap_allowance_sec=5,
    )
    export_periods = find_sustained_periods(
        rows,
        lambda row: row["sml"] is not None and row["sml"] <= -POWER_THRESHOLD_W,
        gap_allowance_sec=5,
    )
    import_periods = sorted(
        (period for period in import_periods if period["duration"] >= 60),
        key=lambda period: period["duration"],
        reverse=True,
    )
    export_periods = sorted(
        (period for period in export_periods if period["duration"] >= 60),
        key=lambda period: period["duration"],
        reverse=True,
    )
    _print_periods("Top sustained high import periods (>= 100 W for >= 1m):", import_periods)
    _print_periods("Top sustained high export periods (<= -100 W for >= 1m):", export_periods)

    print("\nTop grid-power swings within a 2-minute window:")
    swings = find_large_swings(rows, limit=3)
    if not swings:
        print("  none")
    for swing in swings:
        print(
            f"  {_format_time(swing['start_time'])} to {_format_time(swing['end_time'])} - "
            f"{float(swing['swing']):.1f} W "
            f"({float(swing['min_sml']):.1f} to {float(swing['max_sml']):.1f})"
        )

    cycles = result["overcorrection_cycles"]
    if cycles:
        print("\nFirst overcorrection cycles:")
        for cycle in cycles[:5]:
            print(
                f"  {_format_time(cycle['start'])} -> {_format_time(cycle['turn'])} "
                f"-> {_format_time(cycle['end'])}: "
                f"-{cycle['export_before_w']:.0f} W, +{cycle['import_w']:.0f} W, "
                f"-{cycle['export_after_w']:.0f} W"
            )

    return {
        "file": str(path),
        "total_rows": raw_count,
        "parsed_rows": len(rows),
        **result,
        "sustained_import_periods": import_periods,
        "sustained_export_periods": export_periods,
        "swings": swings,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".", help="CSV export or directory")
    parser.add_argument(
        "--only-unmanaged",
        action="append",
        default=[],
        choices=DEVICE_IDS,
        metavar="DEVICE",
        help="only analyze rows where DEVICE is explicitly unmanaged; may be repeated",
    )
    return parser.parse_args()


def main() -> None:
    """Run the multi-export console report."""
    args = _parse_args()
    csv_files = resolve_export_files(args.path)
    if not csv_files:
        print(f"No export files found at '{args.path}'.")
        return
    for file_path in csv_files:
        analyze_file(file_path, only_unmanaged=tuple(args.only_unmanaged))


if __name__ == "__main__":
    main()
