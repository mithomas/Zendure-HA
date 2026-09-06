"""Generate a Markdown event table from Zendure telemetry exports."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .run_analysis import (
        DEVICE_IDS,
        analyze_rows,
        estimate_ac_input,
        group_episodes,
        read_export,
        resolve_export_files,
        select_management_rows,
    )
except ImportError:
    from run_analysis import (
        DEVICE_IDS,
        analyze_rows,
        estimate_ac_input,
        group_episodes,
        read_export,
        resolve_export_files,
        select_management_rows,
    )


def _device_summary(row: dict, device_id: str) -> str:
    device = row["devices"][device_id]
    management = "M" if device["managed"] is True else "U" if device["managed"] is False else "?"
    ac_input = estimate_ac_input(device)
    ac_text = "?" if ac_input is None else f"{ac_input:.0f}"
    solar_text = "?" if device["solar"] is None else f"{device['solar']:.0f}"
    battery_text = "?" if device["battery_flow"] is None else f"{device['battery_flow']:.0f}"
    return f"{management}, {device['mode']}, PV {solar_text}, bat {battery_text}, AC {ac_text}"


def generate_table(
    file_path: str | Path, *, only_unmanaged: tuple[str, ...] = (), limit: int = 100
) -> str:
    """Build a table of routing-relevant import and export samples."""
    _, all_rows = read_export(file_path)
    rows = select_management_rows(all_rows, unmanaged_devices=only_unmanaged)
    result = analyze_rows(rows)

    events = []
    for episode in group_episodes(result["grid_import_while_charging_rows"], gap_allowance_sec=1.5):
        events.append(
            (
                episode[0]["time"],
                episode,
                max(episode, key=lambda row: row["sml"]),
                "managed AC charging contributes to import",
            )
        )
    for episode in group_episodes(result["battery_backed_export_rows"], gap_allowance_sec=1.5):
        events.append(
            (
                episode[0]["time"],
                episode,
                min(episode, key=lambda row: row["sml"]),
                "managed battery discharges while grid exports",
            )
        )
    events.sort(key=lambda event: event[0])

    lines = [
        f"## {Path(file_path).name}",
        "",
        "M = managed, U = unmanaged, ? = unknown. AC is measured or estimated actual AC intake.",
        "",
        "| Period | Duration | Peak grid W | WZ-Balkon (scope, mode, PV, battery, AC) | "
        "K-Balkon (scope, mode, PV, battery, AC) | Finding |",
        "|---|---:|---:|---|---|---|",
    ]
    for _, episode, row, reason in events[:limit]:
        start = episode[0]["time"]
        end = episode[-1]["time"]
        period = str(start) if start == end else f"{start} to {end.time()}"
        duration = sum(event_row["dt"] for event_row in episode)
        lines.append(
            f"| {period} | {duration:.0f}s | {row['sml']:.0f} | {_device_summary(row, 'wz_balkon')} | "
            f"{_device_summary(row, 'k_balkon')} | {reason} |"
        )
    if not events:
        lines.append("| - | - | - | - | - | No routing-relevant events |")
    elif len(events) > limit:
        lines.extend(["", f"Showing {limit} of {len(events)} episodes."])
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
    parser.add_argument("--limit", type=int, default=100, help="maximum event episodes per export")
    return parser.parse_args()


def main() -> None:
    """Print an event table for each matching export."""
    args = _parse_args()
    files = resolve_export_files(args.path)
    if not files:
        raise SystemExit(f"No export CSV files found at {args.path!r}")
    print(
        "\n\n".join(
            generate_table(path, only_unmanaged=tuple(args.only_unmanaged), limit=args.limit)
            for path in files
        )
    )


if __name__ == "__main__":
    main()
