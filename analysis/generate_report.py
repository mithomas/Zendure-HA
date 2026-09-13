"""Generate a Markdown summary from Zendure telemetry exports."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .run_analysis import (
        DEVICE_IDS,
        LOW_POWER_EXPORT_MIN_DURATION_SECONDS,
        LOW_POWER_EXPORT_THRESHOLD_W,
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
        LOW_POWER_EXPORT_MIN_DURATION_SECONDS,
        LOW_POWER_EXPORT_THRESHOLD_W,
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


def generate_report(
    file_path: str | Path,
    *,
    only_unmanaged: tuple[str, ...] = (),
    external_solar_devices: tuple[str, ...] = (),
) -> str:
    """Build a routing-aware Markdown summary for one CSV export."""
    raw_count, all_rows = read_export(file_path)
    rows = select_management_rows(all_rows, unmanaged_devices=only_unmanaged)
    if not rows:
        return f"## {Path(file_path).name}\n\nNo matching rows."

    result = analyze_rows(rows, external_solar_devices=external_solar_devices)
    low_power_export_periods = sorted(
        result["low_power_export_periods"],
        key=lambda period: period["duration"],
        reverse=True,
    )
    low_power_export_kwh = -sum(period["energy_kwh"] for period in low_power_export_periods)
    scope = ", ".join(f"{device_id}=unmanaged" for device_id in only_unmanaged) or "all rows"
    external_solar = ", ".join(external_solar_devices) or "none"
    lines = [
        f"## {Path(file_path).name}",
        "",
        f"- Scope: {scope}",
        f"- External solar context: {external_solar}",
        f"- Window: {rows[0]['time']} to {rows[-1]['time']}",
        f"- Rows: {len(rows)} of {raw_count}",
        "",
        "### Manager participation",
        "",
        "| Device | Managed | Unmanaged | Unknown | Mode switches | Input interruptions | "
        "Output interruptions | PV-withholding periods |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for device_id in DEVICE_IDS:
        samples = result["management_samples"][device_id]
        lines.append(
            f"| {device_id} | {samples['managed']} | {samples['unmanaged']} | "
            f"{samples['unknown']} | {result['mode_switches'][device_id]} | "
            f"{result['input_interruption_counts'][device_id]} | "
            f"{result['output_interruption_counts'][device_id]} | "
            f"{result['local_pv_withheld_import_counts'][device_id]} |"
        )

    withheld_periods = [
        period for device_id in DEVICE_IDS for period in result["local_pv_withheld_import_periods"][device_id]
    ]
    lines.extend(
        [
            "",
            "### Routing-aware findings",
            "",
            "| Metric | Value |",
            "|---|---:|",
            f"| Grid import attributable to managed AC charging | {result['grid_import_while_charging_kwh']:.6f} kWh |",
            f"| Battery-backed grid export | {result['battery_backed_export_kwh']:.6f} kWh |",
            f"| Grid export while a managed battery was full | {result['full_export_kwh']:.6f} kWh |",
            f"| Export -> import -> export cycles | {len(result['overcorrection_cycles'])} |",
            "| Usable local PV withheld during grid import | "
            f"{sum(period['withheld_energy_kwh'] for period in withheld_periods):.6f} kWh |",
            f"| Sustained import periods | {_period_count(rows, importing=True)} |",
            f"| Sustained export periods | {_period_count(rows, importing=False)} |",
            "| Export periods "
            f"> {LOW_POWER_EXPORT_THRESHOLD_W} W for "
            f"> {LOW_POWER_EXPORT_MIN_DURATION_SECONDS} s | "
            f"{len(low_power_export_periods)} |",
            "| Energy in export periods "
            f"> {LOW_POWER_EXPORT_THRESHOLD_W} W for "
            f"> {LOW_POWER_EXPORT_MIN_DURATION_SECONDS} s | "
            f"{low_power_export_kwh:.6f} kWh |",
            "",
            "AC intake is measured from an explicit input-power column when available. Otherwise it is "
            "estimated from charging battery flow, simultaneous home output, and local DC solar. Solar "
            "marked as external remains grid context and is not subtracted from device AC intake. "
            "Unmanaged devices remain visible as grid context but are excluded from routing metrics.",
        ]
    )

    interruptions = [
        episode
        for direction in ("input", "output")
        for device_id in DEVICE_IDS
        for episode in result[f"{direction}_interruptions"][device_id]
    ]
    interruptions.sort(key=lambda episode: episode["stop"])
    if interruptions:
        lines.extend(
            [
                "",
                "### First actual-flow interruptions",
                "",
                "| Stop | Device | Flow | Power before | Duration | Restart | Peak grid impact | "
                "Command limit cleared |",
                "|---|---|---|---:|---:|---|---:|---|",
            ]
        )
        for episode in interruptions[:20]:
            restart = episode["restart"] or "-"
            cleared = (
                "unknown"
                if episode["command_limit_cleared"] is None
                else "yes"
                if episode["command_limit_cleared"]
                else "no"
            )
            lines.append(
                f"| {episode['stop']} | {episode['device_id']} | {episode['direction']} | "
                f"{episode['power_before_w']:.0f} W | {episode['duration']:.0f} s | {restart} | "
                f"{episode['peak_grid_impact_w']:.0f} W | {cleared} |"
            )

    if withheld_periods:
        withheld_periods.sort(key=lambda period: period["peak_withheld_w"], reverse=True)
        lines.extend(
            [
                "",
                "### Local PV withheld during grid import",
                "",
                "| Period | Device | Duration | Peak import | Peak local battery charge | "
                "Peak usable withheld power | Withheld energy |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for period in withheld_periods[:20]:
            lines.append(
                f"| {period['start']} to {period['end'].time()} | {period['device_id']} | "
                f"{period['duration']:.0f} s | {period['peak_grid_import_w']:.0f} W | "
                f"{period['peak_local_battery_charge_w']:.0f} W | "
                f"{period['peak_withheld_w']:.0f} W | {period['withheld_energy_kwh']:.6f} kWh |"
            )

    if low_power_export_periods:
        lines.extend(
            [
                "",
                f"### Export periods above {LOW_POWER_EXPORT_THRESHOLD_W} W",
                "",
                "| Period | Duration | Average grid power | Exported energy |",
                "|---|---:|---:|---:|",
            ]
        )
        for period in low_power_export_periods[:10]:
            lines.append(
                f"| {period['start']} to {period['end'].time()} | "
                f"{period['duration']:.0f} s | {period['avg_sml']:.1f} W | "
                f"{-period['energy_kwh']:.6f} kWh |"
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
    parser.add_argument(
        "--external-solar",
        action="append",
        default=[],
        choices=DEVICE_IDS,
        metavar="DEVICE",
        help="treat DEVICE's solar column as external grid context; may be repeated",
    )
    return parser.parse_args()


def main() -> None:
    """Print one Markdown report per matching export."""
    args = _parse_args()
    files = resolve_export_files(args.path)
    if not files:
        raise SystemExit(f"No export CSV files found at {args.path!r}")
    print(
        "\n\n".join(
            generate_report(
                path,
                only_unmanaged=tuple(args.only_unmanaged),
                external_solar_devices=tuple(args.external_solar),
            )
            for path in files
        )
    )


if __name__ == "__main__":
    main()
