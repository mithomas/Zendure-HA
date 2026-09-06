from datetime import datetime, timedelta

import pytest

from analysis.run_analysis import (
    analyze_rows,
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


def test_overcorrection_cycle_uses_only_managed_normal_input_samples() -> None:
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
