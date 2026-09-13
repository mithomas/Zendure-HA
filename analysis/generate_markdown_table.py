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


def _device_summary(row: dict, device_id: str, external_solar_devices: set[str]) -> str:
    device = row["devices"][device_id]
    management = "M" if device["managed"] is True else "U" if device["managed"] is False else "?"
    ac_input = estimate_ac_input(device, solar_is_external=device_id in external_solar_devices)
    ac_text = "?" if ac_input is None else f"{ac_input:.0f}"
    solar_text = "?" if device["solar"] is None else f"{device['solar']:.0f}"
    battery_text = "?" if device["battery_flow"] is None else f"{device['battery_flow']:.0f}"
    return f"{management}, {device['mode']}, PV {solar_text}, bat {battery_text}, AC {ac_text}"


def generate_table(
    file_path: str | Path,
    *,
    only_unmanaged: tuple[str, ...] = (),
    external_solar_devices: tuple[str, ...] = (),
    limit: int = 100,
) -> str:
    """Build a table of routing-relevant import and export samples."""
    _, all_rows = read_export(file_path)
    rows = select_management_rows(all_rows, unmanaged_devices=only_unmanaged)
    result = analyze_rows(rows, external_solar_devices=external_solar_devices)
    external_solar = set(external_solar_devices)

    events = []
    for episode in group_episodes(result["grid_import_while_charging_rows"], gap_allowance_sec=1.5):
        events.append(
            (
                episode[0]["time"],
                episode,
                max(episode, key=lambda row: row["sml"]),
                "managed AC charging contributes to import",
                None,
            )
        )
    for episode in group_episodes(result["battery_backed_export_rows"], gap_allowance_sec=1.5):
        events.append(
            (
                episode[0]["time"],
                episode,
                min(episode, key=lambda row: row["sml"]),
                "managed battery discharges while grid exports",
                None,
            )
        )
    for direction in ("input", "output"):
        for device_id in DEVICE_IDS:
            for interruption in result[f"{direction}_interruptions"][device_id]:
                episode = interruption["rows"]
                row = (
                    min(episode, key=lambda sample: sample["sml"] or 0)
                    if direction == "input"
                    else max(episode, key=lambda sample: sample["sml"] or 0)
                )
                restart = (
                    f"restarted after {interruption['duration']:.0f}s"
                    if interruption["same_mode_restart"]
                    else "no same-mode restart within 30s"
                )
                cleared = (
                    "command limit unknown"
                    if interruption["command_limit_cleared"] is None
                    else "command limit cleared"
                    if interruption["command_limit_cleared"]
                    else "limit retained"
                )
                events.append(
                    (
                        interruption["stop"],
                        episode,
                        row,
                        f"{device_id} actual {direction} stopped from "
                        f"{interruption['power_before_w']:.0f} W; {restart}; {cleared}",
                        interruption["duration"],
                    )
                )
    for device_id in DEVICE_IDS:
        for period in result["local_pv_withheld_import_periods"][device_id]:
            episode = period["rows"]
            events.append(
                (
                    period["start"],
                    episode,
                    max(episode, key=lambda row: row["sml"]),
                    f"{device_id} stores local PV while grid imports; "
                    f"peak usable withheld power {period['peak_withheld_w']:.0f} W",
                    period["duration"],
                )
            )
    events.sort(key=lambda event: event[0])

    lines = [
        f"## {Path(file_path).name}",
        "",
        "M = managed, U = unmanaged, ? = unknown. AC is measured or estimated actual AC intake.",
        f"External solar context: {', '.join(external_solar_devices) or 'none'}.",
        "",
        "| Period | Duration | Peak grid W | WZ-Balkon (scope, mode, PV, battery, AC) | "
        "K-Balkon (scope, mode, PV, battery, AC) | Finding |",
        "|---|---:|---:|---|---|---|",
    ]
    for _, episode, row, reason, duration_override in events[:limit]:
        start = episode[0]["time"]
        end = episode[-1]["time"]
        period = str(start) if start == end else f"{start} to {end.time()}"
        duration = sum(event_row["dt"] for event_row in episode) if duration_override is None else duration_override
        grid_power = "?" if row["sml"] is None else f"{row['sml']:.0f}"
        lines.append(
            f"| {period} | {duration:.0f}s | {grid_power} | "
            f"{_device_summary(row, 'wz_balkon', external_solar)} | "
            f"{_device_summary(row, 'k_balkon', external_solar)} | {reason} |"
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
    parser.add_argument(
        "--external-solar",
        action="append",
        default=[],
        choices=DEVICE_IDS,
        metavar="DEVICE",
        help="treat DEVICE's solar column as external grid context; may be repeated",
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
            generate_table(
                path,
                only_unmanaged=tuple(args.only_unmanaged),
                external_solar_devices=tuple(args.external_solar),
                limit=args.limit,
            )
            for path in files
        )
    )


if __name__ == "__main__":
    main()
