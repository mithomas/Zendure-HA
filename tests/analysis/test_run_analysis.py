from datetime import datetime, timedelta

import pytest

from analysis.run_analysis import (
    POWER_THRESHOLD_W,
    analyze_rows,
    estimate_ac_input,
    find_sustained_periods,
    parse_float,
    parse_rows,
    select_management_rows,
)


START = datetime(2026, 9, 6, 12, 0, 0)


def _raw_row(second: int, **values: object) -> dict[str, str]:
    row: dict[str, object] = {
        "time": (START + timedelta(seconds=second)).strftime("%Y-%m-%d %H:%M:%S"),
        "sml_power": 0,
        "wz_balkon_solar_power": 50,
        "wz_balkon_output_power": 0,
        "wz_balkon_device_state": "normal",
        "wz_balkon_ac_mode": "input",
        "wz_balkon_input_limit": 250,
        "wz_balkon_output_limit": 0,
        "wz_balkon_bat_flow": -300,
        "wz_balkon_fusegroup": "managed",
        "k_balkon_solar_power": 400,
        "k_balkon_output_power": 400,
        "k_balkon_device_state": "normal",
        "k_balkon_ac_mode": "output",
        "k_balkon_input_limit": 0,
        "k_balkon_output_limit": 400,
        "k_balkon_bat_flow": 0,
        "k_balkon_fusegroup": "unmanaged",
    }
    row.update(values)
    return {key: str(value) for key, value in row.items()}


@pytest.mark.parametrize("value", [None, "", "unknown", "unavailable"])
def test_parse_float_preserves_unknown_values(value: str | None) -> None:
    assert parse_float(value) is None


def test_analysis_ignores_unmanaged_device_routing_activity() -> None:
    rows = parse_rows(
        [
            _raw_row(0),
            _raw_row(
                1,
                sml_power=100,
                wz_balkon_ac_mode="output",
                wz_balkon_bat_flow=0,
                k_balkon_ac_mode="input",
                k_balkon_input_limit=600,
                k_balkon_bat_flow=-600,
            ),
            _raw_row(2, sml_power=100, k_balkon_ac_mode="output"),
        ]
    )

    result = analyze_rows(rows)

    assert result["mode_switches"] == {"wz_balkon": 2, "k_balkon": 0}
    assert result["grid_import_while_charging_kwh"] == pytest.approx(100 / 3_600_000)


def test_management_filter_selects_only_explicitly_unmanaged_rows() -> None:
    rows = parse_rows(
        [
            _raw_row(0, k_balkon_fusegroup="managed"),
            _raw_row(1),
            _raw_row(2),
        ]
    )

    selected = select_management_rows(rows, unmanaged_devices=("k_balkon",))

    assert [row["time"] for row in selected] == [
        START + timedelta(seconds=1),
        START + timedelta(seconds=2),
    ]
    assert [row["dt"] for row in selected] == [0, 1]


def test_sustained_period_stops_when_condition_becomes_false() -> None:
    rows = parse_rows(
        [
            _raw_row(0, sml_power=150),
            _raw_row(1, sml_power=160),
            _raw_row(2, sml_power=-200),
            _raw_row(3, sml_power=170),
            _raw_row(4, sml_power=180),
        ]
    )

    periods = find_sustained_periods(rows, lambda row: row["sml"] >= 100)

    assert [[row["sml"] for row in period["rows"]] for period in periods] == [
        [150, 160],
        [170, 180],
    ]
    assert all(period["avg_sml"] >= 100 for period in periods)


def test_overcorrection_cycle_excludes_full_input_samples() -> None:
    rows = parse_rows(
        [
            _raw_row(0, sml_power=-150),
            _raw_row(1, sml_power=160),
            _raw_row(2, sml_power=-170),
            _raw_row(3, sml_power=180, wz_balkon_device_state="full"),
            _raw_row(4, sml_power=-190, wz_balkon_device_state="full"),
        ]
    )

    result = analyze_rows(rows)

    assert len(result["overcorrection_cycles"]) == 1
    assert result["overcorrection_cycles"][0]["start"] == START
    assert result["overcorrection_cycles"][0]["end"] == START + timedelta(seconds=2)


@pytest.mark.parametrize("device_state", ["full", "offline"])
def test_overcorrection_cycle_excludes_non_charge_capable_input_states(device_state: str) -> None:
    rows = parse_rows(
        [
            _raw_row(0, sml_power=-150, wz_balkon_device_state=device_state),
            _raw_row(1, sml_power=160, wz_balkon_device_state=device_state),
            _raw_row(2, sml_power=-170, wz_balkon_device_state=device_state),
        ]
    )

    result = analyze_rows(rows)

    assert result["overcorrection_cycles"] == []


@pytest.mark.parametrize("device_state", ["normal", "nearly_full", "reserve", "reserve_recovery", "empty"])
def test_overcorrection_cycle_includes_managed_charge_capable_input_states(device_state: str) -> None:
    rows = parse_rows(
        [
            _raw_row(0, sml_power=-150, wz_balkon_device_state=device_state),
            _raw_row(1, sml_power=160, wz_balkon_device_state=device_state),
            _raw_row(2, sml_power=-170, wz_balkon_device_state=device_state),
        ]
    )

    result = analyze_rows(rows)

    assert len(result["overcorrection_cycles"]) == 1


def test_external_solar_is_not_subtracted_from_ac_input() -> None:
    row = parse_rows(
        [
            _raw_row(
                0,
                k_balkon_ac_mode="input",
                k_balkon_bat_flow=-300,
                k_balkon_output_power=0,
                k_balkon_solar_power=400,
            )
        ]
    )[0]
    device = row["devices"]["k_balkon"]

    assert estimate_ac_input(device) == 0
    assert estimate_ac_input(device, solar_is_external=True) == 300


def test_ac_input_estimate_accounts_for_simultaneous_output_telemetry() -> None:
    row = parse_rows(
        [
            _raw_row(
                0,
                wz_balkon_ac_mode="input",
                wz_balkon_bat_flow=-300,
                wz_balkon_output_power=200,
                wz_balkon_solar_power=400,
            )
        ]
    )[0]

    assert estimate_ac_input(row["devices"]["wz_balkon"]) == 100


def test_external_solar_reserve_charge_contributes_to_grid_import() -> None:
    rows = parse_rows(
        [
            _raw_row(
                0,
                wz_balkon_fusegroup="unmanaged",
                k_balkon_fusegroup="managed",
                k_balkon_device_state="reserve",
                k_balkon_ac_mode="input",
                k_balkon_bat_flow=-300,
            ),
            _raw_row(
                1,
                sml_power=100,
                wz_balkon_fusegroup="unmanaged",
                k_balkon_fusegroup="managed",
                k_balkon_device_state="reserve",
                k_balkon_ac_mode="input",
                k_balkon_bat_flow=-300,
            ),
        ]
    )

    result = analyze_rows(rows, external_solar_devices=("k_balkon",))

    assert result["grid_import_while_charging_kwh"] == pytest.approx(100 / 3_600_000)


def test_nearly_full_battery_output_contributes_to_battery_backed_export() -> None:
    rows = parse_rows(
        [
            _raw_row(
                0,
                sml_power=-100,
                wz_balkon_device_state="nearly_full",
                wz_balkon_ac_mode="output",
                wz_balkon_bat_flow=100,
            ),
            _raw_row(
                1,
                sml_power=-100,
                wz_balkon_device_state="nearly_full",
                wz_balkon_ac_mode="output",
                wz_balkon_bat_flow=100,
            ),
        ]
    )

    result = analyze_rows(rows)

    assert result["battery_backed_export_kwh"] == pytest.approx(100 / 3_600_000)


def test_default_analysis_threshold_includes_30_watt_reversals() -> None:
    rows = parse_rows(
        [
            _raw_row(0, sml_power=-30),
            _raw_row(1, sml_power=30),
            _raw_row(2, sml_power=-30),
        ]
    )

    result = analyze_rows(rows)

    assert POWER_THRESHOLD_W == 30
    assert len(result["overcorrection_cycles"]) == 1
    assert result["grid_import_while_charging_kwh"] == pytest.approx(30 / 3_600_000)


def test_default_analysis_threshold_excludes_29_watt_reversals() -> None:
    rows = parse_rows(
        [
            _raw_row(0, sml_power=-29),
            _raw_row(1, sml_power=29),
            _raw_row(2, sml_power=-29),
        ]
    )

    result = analyze_rows(rows)

    assert result["overcorrection_cycles"] == []
    assert result["grid_import_while_charging_kwh"] == 0
