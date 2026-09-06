"""Generate a Markdown summary from Zendure telemetry exports."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .run_analysis import (
        DEVICE_IDS,
        POWER_THRESHOLD_W,
        analyze_rows,
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
        find_sustained_periods,
        read_export,
        resolve_export_files,
        select_management_rows,
    )


def _period_count(rows: list[dict], *, importing: bool) -> int:
    if importing:
        condition = lambda row: row["sml"] is not None and row["sml"] >= POWER_THRESHOLD_W
    else:
        condition = lambda row: row["sml"] is not None and row["sml"] <= -POWER_THRESHOLD_W
    periods = find_sustained_periods(rows, condition, gap_allowance_sec=5)
    return sum(period["duration"] >= 60 for period in periods)


def generate_report(file_path: str | Path, *, only_unmanaged: tuple[str, ...] = ()) -> str:
    """Build a routing-aware Markdown summary for one CSV export."""
    raw_count, all_rows = read_export(file_path)
    rows = select_management_rows(all_rows, unmanaged_devices=only_unmanaged)
    if not rows:
        return f"## {Path(file_path).name}\n\nNo matching rows."

    result = analyze_rows(rows)
    scope = ", ".join(f"{device_id}=unmanaged" for device_id in only_unmanaged) or "all rows"
    lines = [
        f"## {Path(file_path).name}",
        "",
        f"- Scope: {scope}",
        f"- Window: {rows[0]['time']} to {rows[-1]['time']}",
        f"- Rows: {len(rows)} of {raw_count}",
        "",
        "### Manager participation",
        "",
        "| Device | Managed | Unmanaged | Unknown | Managed mode switches |",
        "|---|---:|---:|---:|---:|",
    ]
    for device_id in DEVICE_IDS:
        samples = result["management_samples"][device_id]
        lines.append(
            f"| {device_id} | {samples['managed']} | {samples['unmanaged']} | "
            f"{samples['unknown']} | {result['mode_switches'][device_id]} |"
        )

    lines.extend(
        [
            "",
            "### Routing-aware findings",
            "",
            "| Metric | Value |",
            "|---|---:|",
            "| Grid import attributable to managed AC charging | "
            f"{result['grid_import_while_charging_kwh']:.6f} kWh |",
            f"| Battery-backed grid export | {result['battery_backed_export_kwh']:.6f} kWh |",
            f"| Grid export while a managed battery was full | {result['full_export_kwh']:.6f} kWh |",
            f"| Export -> import -> export cycles | {len(result['overcorrection_cycles'])} |",
            f"| Sustained import periods | {_period_count(rows, importing=True)} |",
            f"| Sustained export periods | {_period_count(rows, importing=False)} |",
            "",
            "AC intake is measured from an explicit input-power column when available. Otherwise it is "
            "estimated as charging battery flow minus local DC solar. Unmanaged devices remain visible "
            "as grid context but are excluded from routing metrics.",
        ]
    )

    cycles = result["overcorrection_cycles"]
    if cycles:
        lines.extend(
            [
                "",
                "### First overcorrection cycles",
                "",
                "| Export start | Import turn | Export return | Power sequence |",
                "|---|---|---|---|",
            ]
        )
        for cycle in cycles[:10]:
            lines.append(
                f"| {cycle['start']} | {cycle['turn']} | {cycle['end']} | "
                f"-{cycle['export_before_w']:.0f} W -> +{cycle['import_w']:.0f} W "
                f"-> -{cycle['export_after_w']:.0f} W |"
            )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".", help="CSV export or directory")
    parser.add_argument(
        "--only-unmanaged",
        action="append",
        default=[],
        choices=DEVICE_IDS,
        metavar="DEVICE",
        help="only include rows where DEVICE is explicitly unmanaged; may be repeated",
    )
    return parser.parse_args()


def main() -> None:
    """Print one Markdown report per matching export."""
    args = _parse_args()
    files = resolve_export_files(args.path)
    if not files:
        raise SystemExit(f"No export CSV files found at {args.path!r}")
    print("\n\n".join(generate_report(path, only_unmanaged=tuple(args.only_unmanaged)) for path in files))


if __name__ == "__main__":
    main()
