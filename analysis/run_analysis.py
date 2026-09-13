"""Shared parsing and telemetry analysis for Zendure CSV exports."""

from __future__ import annotations

import argparse
import csv
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


DEVICE_IDS = ("wz_balkon", "k_balkon")
UNKNOWN_VALUES = {"", "none", "null", "unknown", "unavailable"}
MAX_INTEGRATION_GAP_SECONDS = 5
POWER_THRESHOLD_W = 30
LOW_POWER_EXPORT_THRESHOLD_W = 15
LOW_POWER_EXPORT_MIN_DURATION_SECONDS = 15
FLOW_ACTIVE_THRESHOLD_W = 15
FLOW_ZERO_THRESHOLD_W = 5
FLOW_RESTART_WINDOW_SECONDS = 30
WITHHELD_LOCAL_PV_IMPORT_THRESHOLD_W = 30
CHARGE_CAPABLE_STATES = {"normal", "nearly_full", "reserve", "reserve_recovery", "empty"}

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
                "devices": {device_id: _device_from_row(raw_row, device_id) for device_id in DEVICE_IDS},
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


def select_management_rows(rows: list[ParsedRow], *, unmanaged_devices: tuple[str, ...] = ()) -> list[ParsedRow]:
    """Select rows by explicit manager participation and recalculate intervals."""
    invalid_devices = set(unmanaged_devices) - set(DEVICE_IDS)
    if invalid_devices:
        invalid_list = ", ".join(sorted(invalid_devices))
        raise ValueError(f"unknown device IDs: {invalid_list}")

    selected = [
        {**row} for row in rows if all(row["devices"][device_id]["managed"] is False for device_id in unmanaged_devices)
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
        "energy_kwh": sum(row["sml"] * duration for row, duration in zip(period_rows, durations, strict=True))
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


def find_low_power_export_periods(rows: list[ParsedRow]) -> list[dict[str, Any]]:
    """Return export periods above 15 W that last longer than 15 seconds."""
    periods = find_sustained_periods(
        rows,
        lambda row: row["sml"] is not None and row["sml"] < -LOW_POWER_EXPORT_THRESHOLD_W,
        gap_allowance_sec=MAX_INTEGRATION_GAP_SECONDS,
    )
    return [period for period in periods if period["duration"] > LOW_POWER_EXPORT_MIN_DURATION_SECONDS]


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


def find_large_swings(parsed_rows: list[ParsedRow], window_sec: float = 120, limit: int = 5) -> list[dict[str, Any]]:
    """Return the largest distinct grid-power ranges in rolling windows."""
    swings: list[dict[str, Any]] = []
    minimum: deque[int] = deque()
    maximum: deque[int] = deque()
    valid: deque[int] = deque()
    window_end = 0

    for index, start_row in enumerate(parsed_rows):
        while valid and valid[0] < index:
            valid.popleft()
        while minimum and minimum[0] < index:
            minimum.popleft()
        while maximum and maximum[0] < index:
            maximum.popleft()

        while window_end < len(parsed_rows):
            row = parsed_rows[window_end]
            if (row["time"] - start_row["time"]).total_seconds() > window_sec:
                break
            if row["sml"] is not None:
                valid.append(window_end)
                while minimum and parsed_rows[minimum[-1]]["sml"] > row["sml"]:
                    minimum.pop()
                minimum.append(window_end)
                while maximum and parsed_rows[maximum[-1]]["sml"] < row["sml"]:
                    maximum.pop()
                maximum.append(window_end)
            window_end += 1

        if len(valid) < 2:
            continue
        min_sml = parsed_rows[minimum[0]]["sml"]
        max_sml = parsed_rows[maximum[0]]["sml"]
        swings.append(
            {
                "start_time": start_row["time"],
                "end_time": parsed_rows[valid[-1]]["time"],
                "swing": max_sml - min_sml,
                "min_sml": min_sml,
                "max_sml": max_sml,
                "start_index": index,
                "end_index": valid[-1],
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
    for swing in distinct_swings:
        swing["rows"] = [
            row for row in parsed_rows[swing.pop("start_index") : swing.pop("end_index") + 1] if row["sml"] is not None
        ]
    return distinct_swings


def estimate_ac_input(device: Mapping[str, Any], *, solar_is_external: bool = False) -> float | None:
    """Estimate actual AC intake, preferring an explicit measurement."""
    if device["mode"] != "input":
        return 0.0
    input_power = device["input_power"]
    if input_power is not None:
        return max(float(input_power), 0.0)
    battery_flow = device["battery_flow"]
    if battery_flow is None:
        return None
    solar = device["solar"]
    if not solar_is_external and solar is None:
        return None
    local_solar = 0.0 if solar_is_external else float(solar)
    output = float(device["output"] or 0.0)
    return max(0.0, -float(battery_flow) + output - local_solar)


def _flow_power(
    device: Mapping[str, Any],
    direction: str,
    *,
    solar_is_external: bool,
) -> float | None:
    """Return actual input or output flow for interruption detection."""
    if direction == "input":
        input_power = device["input_power"]
        if input_power is not None:
            return max(0.0, float(input_power))
        return estimate_ac_input(device, solar_is_external=solar_is_external)
    output = device["output"]
    return None if output is None else max(0.0, float(output))


def _grid_impact(row: ParsedRow, direction: str) -> float:
    """Return grid power in the direction expected after one flow stops."""
    sml = row["sml"]
    if sml is None:
        return 0.0
    return max(0.0, -float(sml) if direction == "input" else float(sml))


def find_flow_interruptions(
    rows: list[ParsedRow],
    direction: str,
    *,
    external_solar_devices: tuple[str, ...] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Find actual-flow stop edges and short same-mode restarts per device."""
    external_solar = set(external_solar_devices)
    interruptions: dict[str, list[dict[str, Any]]] = {device_id: [] for device_id in DEVICE_IDS}

    for device_id in DEVICE_IDS:
        armed_power: float | None = None
        armed_mode: str | None = None
        previous_time: datetime | None = None
        pending: dict[str, Any] | None = None

        for row in rows:
            device = row["devices"][device_id]
            timestamp = row["time"]
            gap = 0.0 if previous_time is None else (timestamp - previous_time).total_seconds()
            previous_time = timestamp

            if device["managed"] is not True or gap > MAX_INTEGRATION_GAP_SECONDS:
                armed_power = None
                armed_mode = None
                pending = None
                if device["managed"] is not True:
                    continue

            flow = _flow_power(
                device,
                direction,
                solar_is_external=device_id in external_solar,
            )
            mode = device["mode"] if isinstance(device["mode"], str) else None
            if flow is None:
                armed_power = None
                armed_mode = None
                pending = None
                continue

            if pending is not None:
                restart_elapsed = (timestamp - pending["stop"]).total_seconds()
                if flow > FLOW_ACTIVE_THRESHOLD_W and mode == pending["mode"]:
                    pending["restart"] = timestamp
                    pending["end"] = timestamp
                    pending["duration"] = restart_elapsed
                    pending["same_mode_restart"] = restart_elapsed <= FLOW_RESTART_WINDOW_SECONDS
                    pending = None
                elif mode != pending["mode"]:
                    pending = None
                elif flow <= FLOW_ZERO_THRESHOLD_W:
                    impact = _grid_impact(row, direction)
                    pending["end"] = timestamp
                    pending["duration"] = restart_elapsed
                    pending["peak_grid_impact_w"] = max(
                        pending["peak_grid_impact_w"],
                        impact,
                    )
                    pending["grid_impact_samples"].append(impact)
                    pending["avg_grid_impact_w"] = sum(pending["grid_impact_samples"]) / len(
                        pending["grid_impact_samples"]
                    )
                    pending["rows"].append(row)
                    command_limit = device[f"{direction}_limit"]
                    if command_limit is not None:
                        pending["command_limit_cleared"] = (
                            pending["command_limit_cleared"] is True or command_limit <= FLOW_ZERO_THRESHOLD_W
                        )

            if flow > FLOW_ACTIVE_THRESHOLD_W:
                armed_power = flow
                armed_mode = mode
                continue
            if flow > FLOW_ZERO_THRESHOLD_W or armed_power is None:
                continue

            command_limit = device[f"{direction}_limit"]
            impact = _grid_impact(row, direction)
            pending = {
                "device_id": device_id,
                "direction": direction,
                "mode": armed_mode,
                "stop": timestamp,
                "end": timestamp,
                "restart": None,
                "duration": 0.0,
                "power_before_w": armed_power,
                "peak_grid_impact_w": impact,
                "avg_grid_impact_w": impact,
                "grid_impact_samples": [impact],
                "command_limit_cleared": (None if command_limit is None else command_limit <= FLOW_ZERO_THRESHOLD_W),
                "same_mode_stop": mode == armed_mode,
                "same_mode_restart": False,
                "rows": [row],
            }
            interruptions[device_id].append(pending)
            armed_power = None
            armed_mode = None

    for episodes in interruptions.values():
        for episode in episodes:
            episode.pop("grid_impact_samples")
    return interruptions


def find_local_pv_withheld_import_periods(
    rows: list[ParsedRow],
    *,
    external_solar_devices: tuple[str, ...] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Find output-mode periods that store local PV while importing household demand."""
    external_solar = set(external_solar_devices)
    periods: dict[str, list[dict[str, Any]]] = {device_id: [] for device_id in DEVICE_IDS}

    for device_id in DEVICE_IDS:
        if device_id in external_solar:
            continue
        matching: list[tuple[ParsedRow, float, float]] = []
        grouped: list[list[tuple[ParsedRow, float, float]]] = []
        for row in rows:
            device = row["devices"][device_id]
            solar = device["solar"]
            battery_flow = device["battery_flow"]
            grid_import = row["sml"]
            local_battery_charge = (
                min(float(solar), -float(battery_flow)) if solar is not None and battery_flow is not None else 0.0
            )
            qualifies = (
                device["managed"] is True
                and device["mode"] == "output"
                and grid_import is not None
                and grid_import > WITHHELD_LOCAL_PV_IMPORT_THRESHOLD_W
                and local_battery_charge > FLOW_ACTIVE_THRESHOLD_W
            )
            if not qualifies:
                if matching:
                    grouped.append(matching)
                    matching = []
                continue
            if matching and (row["time"] - matching[-1][0]["time"]).total_seconds() > MAX_INTEGRATION_GAP_SECONDS:
                grouped.append(matching)
                matching = []
            matching.append(
                (
                    row,
                    local_battery_charge,
                    min(float(grid_import), local_battery_charge),
                )
            )
        if matching:
            grouped.append(matching)

        for group in grouped:
            durations = [0.0]
            durations.extend(
                (sample[0]["time"] - previous[0]["time"]).total_seconds()
                for previous, sample in zip(group, group[1:], strict=False)
            )
            grid_imports = [float(sample[0]["sml"]) for sample in group]
            local_charges = [sample[1] for sample in group]
            withheld = [sample[2] for sample in group]
            periods[device_id].append(
                {
                    "device_id": device_id,
                    "start": group[0][0]["time"],
                    "end": group[-1][0]["time"],
                    "duration": sum(durations),
                    "avg_grid_import_w": sum(grid_imports) / len(grid_imports),
                    "peak_grid_import_w": max(grid_imports),
                    "avg_local_battery_charge_w": sum(local_charges) / len(local_charges),
                    "peak_local_battery_charge_w": max(local_charges),
                    "avg_withheld_w": sum(withheld) / len(withheld),
                    "peak_withheld_w": max(withheld),
                    "withheld_energy_kwh": sum(
                        power * duration for power, duration in zip(withheld, durations, strict=True)
                    )
                    / 3_600_000,
                    "rows": [sample[0] for sample in group],
                }
            )

    return periods


def _has_managed_charge_capable_input(row: ParsedRow) -> bool:
    return any(
        device["managed"] is True and device["state"] in CHARGE_CAPABLE_STATES and device["mode"] == "input"
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
        if sml is None or not _has_managed_charge_capable_input(row):
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


def analyze_rows(rows: list[ParsedRow], *, external_solar_devices: tuple[str, ...] = ()) -> AnalysisResult:
    """Calculate routing-aware metrics from parsed rows."""
    invalid_devices = set(external_solar_devices) - set(DEVICE_IDS)
    if invalid_devices:
        invalid_list = ", ".join(sorted(invalid_devices))
        raise ValueError(f"unknown external-solar device IDs: {invalid_list}")
    external_solar = set(external_solar_devices)
    management_samples = {device_id: {"managed": 0, "unmanaged": 0, "unknown": 0} for device_id in DEVICE_IDS}
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
        managed_devices: list[tuple[str, Mapping[str, Any]]] = []

        for device_id, device in row["devices"].items():
            managed = device["managed"]
            status = "managed" if managed is True else "unmanaged" if managed is False else "unknown"
            management_samples[device_id][status] += 1

            if managed is not True:
                previous_managed_mode[device_id] = None
                continue
            managed_devices.append((device_id, device))
            mode = device["mode"]
            previous_mode = previous_managed_mode[device_id]
            if previous_mode is not None and mode is not None and mode != previous_mode:
                mode_switches[device_id] += 1
            previous_managed_mode[device_id] = mode if isinstance(mode, str) else None

        if sml is None or dt <= 0:
            continue

        ac_inputs = [
            ac_input
            for device_id, device in managed_devices
            if (
                ac_input := estimate_ac_input(
                    device,
                    solar_is_external=device_id in external_solar,
                )
            )
            is not None
        ]
        total_ac_input = sum(ac_inputs)
        if sml >= POWER_THRESHOLD_W and total_ac_input > 0:
            import_ws += min(sml, total_ac_input) * dt
            import_rows.append(row)

        if sml <= -POWER_THRESHOLD_W and any(
            device["battery_flow"] is not None and device["battery_flow"] >= POWER_THRESHOLD_W
            for _device_id, device in managed_devices
        ):
            battery_export_ws += -sml * dt
            battery_export_rows.append(row)

        if sml <= -POWER_THRESHOLD_W and any(device["state"] == "full" for _device_id, device in managed_devices):
            full_export_ws += -sml * dt

    input_interruptions = find_flow_interruptions(
        rows,
        "input",
        external_solar_devices=external_solar_devices,
    )
    output_interruptions = find_flow_interruptions(
        rows,
        "output",
        external_solar_devices=external_solar_devices,
    )
    withheld_periods = find_local_pv_withheld_import_periods(
        rows,
        external_solar_devices=external_solar_devices,
    )

    return {
        "management_samples": management_samples,
        "mode_switches": mode_switches,
        "input_interruption_counts": {device_id: len(input_interruptions[device_id]) for device_id in DEVICE_IDS},
        "output_interruption_counts": {device_id: len(output_interruptions[device_id]) for device_id in DEVICE_IDS},
        "input_interruptions": input_interruptions,
        "output_interruptions": output_interruptions,
        "local_pv_withheld_import_counts": {device_id: len(withheld_periods[device_id]) for device_id in DEVICE_IDS},
        "local_pv_withheld_import_periods": withheld_periods,
        "grid_import_while_charging_kwh": import_ws / 3_600_000,
        "battery_backed_export_kwh": battery_export_ws / 3_600_000,
        "full_export_kwh": full_export_ws / 3_600_000,
        "grid_import_while_charging_rows": import_rows,
        "battery_backed_export_rows": battery_export_rows,
        "overcorrection_cycles": find_overcorrection_cycles(rows),
        "low_power_export_periods": find_low_power_export_periods(rows),
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
    parser.add_argument(
        "--external-solar",
        action="append",
        default=[],
        choices=DEVICE_IDS,
        metavar="DEVICE",
        help="treat DEVICE's solar column as external grid context; may be repeated",
    )
    args = parser.parse_args()
    files = resolve_export_files(args.path)
    if not files:
        parser.error(f"no export CSV files found at {args.path!r}")

    for file_path in files:
        raw_count, all_rows = read_export(file_path)
        rows = select_management_rows(all_rows, unmanaged_devices=tuple(args.only_unmanaged))
        result = analyze_rows(rows, external_solar_devices=tuple(args.external_solar))
        print(file_path)
        print(f"  rows: {len(rows)}/{raw_count}")
        if args.only_unmanaged:
            print(f"  scope: {', '.join(args.only_unmanaged)} unmanaged")
        if args.external_solar:
            print(f"  external solar context: {', '.join(args.external_solar)}")
        print(f"  management samples: {result['management_samples']}")
        print(f"  managed mode switches: {result['mode_switches']}")
        print(f"  managed input interruptions: {result['input_interruption_counts']}")
        print(f"  managed output interruptions: {result['output_interruption_counts']}")
        print(f"  local PV withheld during grid import: {result['local_pv_withheld_import_counts']}")
        print(f"  grid import while managed AC charging: {result['grid_import_while_charging_kwh']:.6f} kWh")
        print(f"  battery-backed export: {result['battery_backed_export_kwh']:.6f} kWh")
        print(f"  export while full: {result['full_export_kwh']:.6f} kWh")
        print(f"  overcorrection cycles: {len(result['overcorrection_cycles'])}")


if __name__ == "__main__":
    _main()
