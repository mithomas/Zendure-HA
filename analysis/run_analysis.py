"""Shared parsing and telemetry analysis for Zendure CSV exports."""

from __future__ import annotations

import argparse
import csv
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


DEVICE_IDS = ("wz_balkon", "k_balkon")
UNKNOWN_VALUES = {"", "none", "null", "unknown", "unavailable"}
MAX_INTEGRATION_GAP_SECONDS = 5
POWER_THRESHOLD_W = 100

ParsedRow = dict[str, Any]
AnalysisResult = dict[str, Any]


def parse_float(value: object) -> float | None:
    """Parse a telemetry number without turning missing data into zero."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in UNKNOWN_VALUES:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_managed(value: object) -> bool | None:
    """Parse the per-row manager participation marker."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in UNKNOWN_VALUES:
        return None
    if text == "managed":
        return True
    if text == "unmanaged":
        return False
    return None


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _device_from_row(row: Mapping[str, object], device_id: str) -> dict[str, Any]:
    return {
        "managed": parse_managed(row.get(f"{device_id}_fusegroup")),
        "solar": parse_float(row.get(f"{device_id}_solar_power")),
        "output": parse_float(row.get(f"{device_id}_output_power")),
        "input_power": parse_float(row.get(f"{device_id}_input_power")),
        "input_limit": parse_float(row.get(f"{device_id}_input_limit")),
        "output_limit": parse_float(row.get(f"{device_id}_output_limit")),
        "battery_flow": parse_float(row.get(f"{device_id}_bat_flow")),
        "state": row.get(f"{device_id}_device_state") or None,
        "mode": row.get(f"{device_id}_ac_mode") or None,
    }


def _set_intervals(rows: list[ParsedRow]) -> None:
    previous_time: datetime | None = None
    for row in rows:
        timestamp = row["time"]
        raw_dt = 0.0 if previous_time is None else (timestamp - previous_time).total_seconds()
        row["raw_dt"] = raw_dt
        row["dt"] = raw_dt if 0 < raw_dt <= MAX_INTEGRATION_GAP_SECONDS else 0.0
        previous_time = timestamp


def parse_rows(raw_rows: Iterable[Mapping[str, object]]) -> list[ParsedRow]:
    """Parse and chronologically order export rows."""
    parsed_rows: list[ParsedRow] = []
    for index, raw_row in enumerate(raw_rows):
        timestamp = _parse_time(raw_row.get("time"))
        if timestamp is None:
            continue
        parsed_rows.append(
            {
                "idx": index,
                "time": timestamp,
                "sml": parse_float(raw_row.get("sml_power")),
                "primary": raw_row.get("primary_device") or None,
                "devices": {
                    device_id: _device_from_row(raw_row, device_id) for device_id in DEVICE_IDS
                },
            }
        )

    parsed_rows.sort(key=lambda row: row["time"])
    _set_intervals(parsed_rows)
    return parsed_rows


def read_export(file_path: str | Path) -> tuple[int, list[ParsedRow]]:
    """Read an export and return its raw and valid row counts."""
    with Path(file_path).open(encoding="utf-8", newline="") as file_handle:
        raw_rows = list(csv.DictReader(file_handle))
    return len(raw_rows), parse_rows(raw_rows)


def resolve_export_files(path: str | Path) -> list[Path]:
    """Resolve one CSV file or all export CSV files in a directory."""
    export_path = Path(path)
    if export_path.is_file():
        return [export_path]
    if export_path.is_dir():
        return sorted(export_path.glob("export*.csv"))
    return []


def select_management_rows(
    rows: list[ParsedRow], *, unmanaged_devices: tuple[str, ...] = ()
) -> list[ParsedRow]:
    """Select rows by explicit manager participation and recalculate intervals."""
    invalid_devices = set(unmanaged_devices) - set(DEVICE_IDS)
    if invalid_devices:
        invalid_list = ", ".join(sorted(invalid_devices))
        raise ValueError(f"unknown device IDs: {invalid_list}")

    selected = [
        {**row}
        for row in rows
        if all(row["devices"][device_id]["managed"] is False for device_id in unmanaged_devices)
    ]
    _set_intervals(selected)
    return selected


def _period_stats(period_rows: list[ParsedRow]) -> dict[str, Any]:
    durations = [0.0]
    durations.extend(
        min(
            max((row["time"] - previous["time"]).total_seconds(), 0.0),
            MAX_INTEGRATION_GAP_SECONDS,
        )
        for previous, row in zip(period_rows, period_rows[1:], strict=False)
    )
    sml_values = [row["sml"] for row in period_rows if row["sml"] is not None]
    return {
        "start": period_rows[0]["time"],
        "end": period_rows[-1]["time"],
        "duration": sum(durations),
        "avg_sml": sum(sml_values) / len(sml_values),
        "energy_kwh": sum(
            row["sml"] * duration for row, duration in zip(period_rows, durations, strict=True)
        )
        / 3_600_000,
        "rows": period_rows,
    }


def find_sustained_periods(
    rows: list[ParsedRow],
    condition_fn: Callable[[ParsedRow], bool],
    gap_allowance_sec: float = 30,
) -> list[dict[str, Any]]:
    """Find continuous matching periods, allowing only gaps in sampling."""
    periods: list[list[ParsedRow]] = []
    current_period: list[ParsedRow] = []

    for row in rows:
        if not condition_fn(row):
            if current_period:
                periods.append(current_period)
                current_period = []
            continue

        if current_period:
            gap = (row["time"] - current_period[-1]["time"]).total_seconds()
            if gap > gap_allowance_sec:
                periods.append(current_period)
                current_period = []
        current_period.append(row)

    if current_period:
        periods.append(current_period)
    return [_period_stats(period) for period in periods]


def group_episodes(rows: list[ParsedRow], gap_allowance_sec: float = 30) -> list[list[ParsedRow]]:
    """Group a prefiltered row list by timestamp proximity."""
    if not rows:
        return []
    episodes = [[rows[0]]]
    for row in rows[1:]:
        gap = (row["time"] - episodes[-1][-1]["time"]).total_seconds()
        if gap <= gap_allowance_sec:
            episodes[-1].append(row)
        else:
            episodes.append([row])
    return episodes


def find_large_swings(
    parsed_rows: list[ParsedRow], window_sec: float = 120, limit: int = 5
) -> list[dict[str, Any]]:
    """Return the largest distinct grid-power ranges in rolling windows."""
    swings: list[dict[str, Any]] = []
    for index, start_row in enumerate(parsed_rows):
        window = []
        for row in parsed_rows[index:]:
            if (row["time"] - start_row["time"]).total_seconds() > window_sec:
                break
            if row["sml"] is not None:
                window.append(row)
        if len(window) < 2:
            continue
        sml_values = [row["sml"] for row in window]
        min_sml = min(sml_values)
        max_sml = max(sml_values)
        swings.append(
            {
                "start_time": start_row["time"],
                "end_time": window[-1]["time"],
                "swing": max_sml - min_sml,
                "min_sml": min_sml,
                "max_sml": max_sml,
                "rows": window,
            }
        )

    swings.sort(key=lambda swing: swing["swing"], reverse=True)
    distinct_swings: list[dict[str, Any]] = []
    for swing in swings:
        if all(
            abs((swing["start_time"] - existing["start_time"]).total_seconds()) >= window_sec
            for existing in distinct_swings
        ):
            distinct_swings.append(swing)
            if len(distinct_swings) == limit:
                break
    return distinct_swings


def estimate_ac_input(device: Mapping[str, Any]) -> float | None:
    """Estimate actual AC intake, preferring an explicit measurement."""
    if device["mode"] != "input":
        return 0.0
    input_power = device["input_power"]
    if input_power is not None:
        return max(float(input_power), 0.0)
    battery_flow = device["battery_flow"]
    solar = device["solar"]
    if battery_flow is None or solar is None:
        return None
    return max(0.0, -float(battery_flow) - float(solar))


def _has_managed_normal_input(row: ParsedRow) -> bool:
    return any(
        device["managed"] is True
        and device["state"] == "normal"
        and device["mode"] == "input"
        for device in row["devices"].values()
    )


def find_overcorrection_cycles(
    rows: list[ParsedRow], threshold_w: float = POWER_THRESHOLD_W, max_seconds: float = 60
) -> list[dict[str, Any]]:
    """Find export -> import -> export reversals while managed charging is active."""
    cycles: list[dict[str, Any]] = []
    start_row: ParsedRow | None = None
    import_row: ParsedRow | None = None

    for row in rows:
        sml = row["sml"]
        if sml is None or not _has_managed_normal_input(row):
            start_row = None
            import_row = None
            continue

        if start_row is not None and (row["time"] - start_row["time"]).total_seconds() > max_seconds:
            start_row = None
            import_row = None

        if sml <= -threshold_w:
            if start_row is not None and import_row is not None:
                cycles.append(
                    {
                        "start": start_row["time"],
                        "turn": import_row["time"],
                        "end": row["time"],
                        "export_before_w": -start_row["sml"],
                        "import_w": import_row["sml"],
                        "export_after_w": -sml,
                    }
                )
            start_row = row
            import_row = None
        elif sml >= threshold_w and start_row is not None:
            if import_row is None or sml > import_row["sml"]:
                import_row = row

    return cycles


def analyze_rows(rows: list[ParsedRow]) -> AnalysisResult:
    """Calculate routing-aware metrics from parsed rows."""
    management_samples = {
        device_id: {"managed": 0, "unmanaged": 0, "unknown": 0} for device_id in DEVICE_IDS
    }
    mode_switches = {device_id: 0 for device_id in DEVICE_IDS}
    previous_managed_mode: dict[str, str | None] = {device_id: None for device_id in DEVICE_IDS}
    import_ws = 0.0
    battery_export_ws = 0.0
    full_export_ws = 0.0
    import_rows: list[ParsedRow] = []
    battery_export_rows: list[ParsedRow] = []

    for row in rows:
        sml = row["sml"]
        dt = row["dt"]
        managed_devices: list[Mapping[str, Any]] = []

        for device_id, device in row["devices"].items():
            managed = device["managed"]
            status = "managed" if managed is True else "unmanaged" if managed is False else "unknown"
            management_samples[device_id][status] += 1

            if managed is not True:
                previous_managed_mode[device_id] = None
                continue
            managed_devices.append(device)
            mode = device["mode"]
            previous_mode = previous_managed_mode[device_id]
            if previous_mode is not None and mode is not None and mode != previous_mode:
                mode_switches[device_id] += 1
            previous_managed_mode[device_id] = mode if isinstance(mode, str) else None

        if sml is None or dt <= 0:
            continue

        ac_inputs = [
            ac_input
            for device in managed_devices
            if device["state"] == "normal" and (ac_input := estimate_ac_input(device)) is not None
        ]
        total_ac_input = sum(ac_inputs)
        if sml >= POWER_THRESHOLD_W and total_ac_input > 0:
            import_ws += min(sml, total_ac_input) * dt
            import_rows.append(row)

        if sml <= -POWER_THRESHOLD_W and any(
            device["state"] == "normal"
            and device["battery_flow"] is not None
            and device["battery_flow"] >= POWER_THRESHOLD_W
            for device in managed_devices
        ):
            battery_export_ws += -sml * dt
            battery_export_rows.append(row)

        if sml <= -POWER_THRESHOLD_W and any(device["state"] == "full" for device in managed_devices):
            full_export_ws += -sml * dt

    return {
        "management_samples": management_samples,
        "mode_switches": mode_switches,
        "grid_import_while_charging_kwh": import_ws / 3_600_000,
        "battery_backed_export_kwh": battery_export_ws / 3_600_000,
        "full_export_kwh": full_export_ws / 3_600_000,
        "grid_import_while_charging_rows": import_rows,
        "battery_backed_export_rows": battery_export_rows,
        "overcorrection_cycles": find_overcorrection_cycles(rows),
    }


def _main() -> None:
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
    args = parser.parse_args()
    files = resolve_export_files(args.path)
    if not files:
        parser.error(f"no export CSV files found at {args.path!r}")

    for file_path in files:
        raw_count, all_rows = read_export(file_path)
        rows = select_management_rows(all_rows, unmanaged_devices=tuple(args.only_unmanaged))
        result = analyze_rows(rows)
        print(file_path)
        print(f"  rows: {len(rows)}/{raw_count}")
        if args.only_unmanaged:
            print(f"  scope: {', '.join(args.only_unmanaged)} unmanaged")
        print(f"  management samples: {result['management_samples']}")
        print(f"  managed mode switches: {result['mode_switches']}")
        print(f"  grid import while managed AC charging: {result['grid_import_while_charging_kwh']:.6f} kWh")
        print(f"  battery-backed export: {result['battery_backed_export_kwh']:.6f} kWh")
        print(f"  export while full: {result['full_export_kwh']:.6f} kWh")
        print(f"  overcorrection cycles: {len(result['overcorrection_cycles'])}")


if __name__ == "__main__":
    _main()
