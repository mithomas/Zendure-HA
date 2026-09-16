from datetime import datetime, timedelta

import pytest

from analysis.run_analysis import (
    POWER_THRESHOLD_W,
    analyze_rows,
    estimate_ac_input,
    find_actuation_lag,
    find_charge_overshoot_events,
    find_large_swings,
    find_neutral_grid_profile,
    find_sustained_periods,
    neutral_grid_power,
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


@pytest.mark.parametrize(
    ("grid_power", "duration_seconds", "expected_periods"),
    [
        (-16, 16, 1),
        (-16, 15, 0),
        (-15, 16, 0),
    ],
)
def test_low_power_export_periods_use_strict_thresholds(
    grid_power: int,
    duration_seconds: int,
    expected_periods: int,
) -> None:
    rows = parse_rows([_raw_row(second, sml_power=grid_power) for second in range(duration_seconds + 1)])

    result = analyze_rows(rows)

    assert len(result["low_power_export_periods"]) == expected_periods


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


def test_actual_input_stop_and_same_mode_restart_are_correlated_with_command() -> None:
    rows = parse_rows(
        [
            _raw_row(0, sml_power=10, wz_balkon_input_power=40, wz_balkon_input_limit=40),
            _raw_row(1, sml_power=-50, wz_balkon_input_power=0, wz_balkon_input_limit=0),
            _raw_row(4, sml_power=5, wz_balkon_input_power=40, wz_balkon_input_limit=40),
        ]
    )

    result = analyze_rows(rows)

    assert result["input_interruption_counts"] == {"wz_balkon": 1, "k_balkon": 0}
    episode = result["input_interruptions"]["wz_balkon"][0]
    assert episode["stop"] == START + timedelta(seconds=1)
    assert episode["restart"] == START + timedelta(seconds=4)
    assert episode["duration"] == 3
    assert episode["power_before_w"] == 40
    assert episode["peak_grid_impact_w"] == 50
    assert episode["command_limit_cleared"] is True
    assert episode["same_mode_restart"] is True


def test_stale_input_limit_without_actual_intake_is_not_an_interruption() -> None:
    rows = parse_rows(
        [
            _raw_row(0, wz_balkon_input_power=0, wz_balkon_input_limit=100),
            _raw_row(1, wz_balkon_input_power=0, wz_balkon_input_limit=0),
        ]
    )

    result = analyze_rows(rows)

    assert result["input_interruption_counts"]["wz_balkon"] == 0
    assert result["input_interruptions"]["wz_balkon"] == []


def test_secondary_output_stop_and_restart_are_detected_without_mode_switch() -> None:
    rows = parse_rows(
        [
            _raw_row(
                0,
                wz_balkon_fusegroup="unmanaged",
                k_balkon_fusegroup="managed",
                k_balkon_output_power=60,
                k_balkon_output_limit=60,
            ),
            _raw_row(
                1,
                sml_power=80,
                wz_balkon_fusegroup="unmanaged",
                k_balkon_fusegroup="managed",
                k_balkon_output_power=0,
                k_balkon_output_limit=0,
            ),
            _raw_row(
                5,
                wz_balkon_fusegroup="unmanaged",
                k_balkon_fusegroup="managed",
                k_balkon_output_power=60,
                k_balkon_output_limit=60,
            ),
        ]
    )

    result = analyze_rows(rows)

    assert result["mode_switches"]["k_balkon"] == 0
    assert result["output_interruption_counts"]["k_balkon"] == 1
    episode = result["output_interruptions"]["k_balkon"][0]
    assert episode["restart"] == START + timedelta(seconds=5)
    assert episode["same_mode_restart"] is True
    assert episode["peak_grid_impact_w"] == 80


def test_interruption_detection_ignores_repeated_zeroes_and_sample_gaps() -> None:
    rows = parse_rows(
        [
            _raw_row(0, wz_balkon_input_power=40),
            _raw_row(10, wz_balkon_input_power=0),
            _raw_row(11, wz_balkon_input_power=0),
            _raw_row(12, wz_balkon_input_power=40),
            _raw_row(13, wz_balkon_input_power=0),
            _raw_row(14, wz_balkon_input_power=0),
        ]
    )

    result = analyze_rows(rows)

    assert result["input_interruption_counts"]["wz_balkon"] == 1


def test_local_pv_withheld_during_grid_import_is_device_scoped() -> None:
    rows = parse_rows(
        [
            _raw_row(
                0,
                sml_power=100,
                wz_balkon_ac_mode="output",
                wz_balkon_solar_power=500,
                wz_balkon_output_power=300,
                wz_balkon_bat_flow=-200,
            ),
            _raw_row(
                1,
                sml_power=120,
                wz_balkon_ac_mode="output",
                wz_balkon_solar_power=500,
                wz_balkon_output_power=280,
                wz_balkon_bat_flow=-220,
            ),
        ]
    )

    result = analyze_rows(rows)

    assert result["local_pv_withheld_import_counts"] == {"wz_balkon": 1, "k_balkon": 0}
    period = result["local_pv_withheld_import_periods"]["wz_balkon"][0]
    assert period["duration"] == 1
    assert period["peak_grid_import_w"] == 120
    assert period["peak_local_battery_charge_w"] == 220
    assert period["withheld_energy_kwh"] == pytest.approx(120 / 3_600_000)


@pytest.mark.parametrize(
    ("managed", "external_solar_devices"),
    [
        ("unmanaged", ()),
        ("managed", ("wz_balkon",)),
    ],
)
def test_local_pv_withheld_detection_excludes_unmanaged_and_external_solar(
    managed: str,
    external_solar_devices: tuple[str, ...],
) -> None:
    rows = parse_rows(
        [
            _raw_row(
                0,
                sml_power=100,
                wz_balkon_fusegroup=managed,
                wz_balkon_ac_mode="output",
                wz_balkon_solar_power=500,
                wz_balkon_output_power=300,
                wz_balkon_bat_flow=-200,
            )
        ]
    )

    result = analyze_rows(rows, external_solar_devices=external_solar_devices)

    assert result["local_pv_withheld_import_counts"]["wz_balkon"] == 0


def test_large_swings_sliding_window_matches_reference_implementation() -> None:
    rows = parse_rows(
        [
            _raw_row(second, sml_power="unknown" if second in {3, 9} else ((second * 37) % 101) - 50)
            for second in range(0, 361, 3)
        ]
    )

    expected = []
    for index, start_row in enumerate(rows):
        window = [
            row
            for row in rows[index:]
            if (row["time"] - start_row["time"]).total_seconds() <= 120 and row["sml"] is not None
        ]
        if len(window) < 2:
            continue
        values = [row["sml"] for row in window]
        expected.append(
            {
                "start_time": start_row["time"],
                "end_time": window[-1]["time"],
                "swing": max(values) - min(values),
                "min_sml": min(values),
                "max_sml": max(values),
                "rows": window,
            }
        )
    expected.sort(key=lambda swing: swing["swing"], reverse=True)
    distinct = []
    for swing in expected:
        if all(abs((swing["start_time"] - existing["start_time"]).total_seconds()) >= 120 for existing in distinct):
            distinct.append(swing)
            if len(distinct) == 3:
                break

    actual = find_large_swings(rows, limit=3)

    assert [
        (
            swing["start_time"],
            swing["end_time"],
            swing["swing"],
            swing["min_sml"],
            swing["max_sml"],
            [row["idx"] for row in swing["rows"]],
        )
        for swing in actual
    ] == [
        (
            swing["start_time"],
            swing["end_time"],
            swing["swing"],
            swing["min_sml"],
            swing["max_sml"],
            [row["idx"] for row in swing["rows"]],
        )
        for swing in distinct
    ]


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


def _idle_row(second: int, sml: float, **values: object) -> dict[str, str]:
    """Build a row where the managed device moves no power, so neutral grid equals the meter."""
    return _raw_row(
        second,
        sml_power=sml,
        wz_balkon_ac_mode="output",
        wz_balkon_output_power=0,
        wz_balkon_bat_flow=0,
        wz_balkon_solar_power=0,
        **values,
    )


@pytest.mark.parametrize(
    ("overrides", "sml", "expected"),
    [
        (
            {
                "wz_balkon_ac_mode": "output",
                "wz_balkon_output_power": 40,
                "wz_balkon_bat_flow": 40,
                "wz_balkon_solar_power": 0,
            },
            -10,
            30,
        ),
        (
            {
                "wz_balkon_ac_mode": "input",
                "wz_balkon_output_power": 0,
                "wz_balkon_bat_flow": -100,
                "wz_balkon_solar_power": 0,
            },
            50,
            -50,
        ),
        (
            {
                "wz_balkon_ac_mode": "input",
                "wz_balkon_output_power": 25,
                "wz_balkon_bat_flow": -100,
                "wz_balkon_solar_power": 0,
            },
            50,
            -50,
        ),
        ({"wz_balkon_fusegroup": "unmanaged"}, 77, 77),
    ],
)
def test_neutral_grid_power_removes_only_the_devices_own_ac_flows(
    overrides: dict[str, object], sml: float, expected: float
) -> None:
    rows = parse_rows([_raw_row(0, sml_power=sml, **overrides)])

    assert neutral_grid_power(rows[0], "wz_balkon") == pytest.approx(expected)


def test_neutral_grid_power_is_unknown_without_a_grid_reading() -> None:
    rows = parse_rows([_raw_row(0, sml_power="unknown")])

    assert neutral_grid_power(rows[0], "wz_balkon") is None


def test_neutral_grid_profile_reports_persistent_surplus() -> None:
    rows = parse_rows([_idle_row(second, -100) for second in range(2400)])

    profile = find_neutral_grid_profile(rows, device_id="wz_balkon")

    assert profile["mean_w"] == pytest.approx(-100)
    assert profile["export_fraction"] == pytest.approx(1.0)
    assert profile["crossings"] == 0


def test_neutral_grid_profile_reports_alternation_as_deadband_crossings() -> None:
    rows = parse_rows([_idle_row(second, -100 if (second // 600) % 2 == 0 else 100) for second in range(2400)])

    profile = find_neutral_grid_profile(rows, device_id="wz_balkon")

    assert profile["crossings"] >= 3
    assert profile["export_fraction"] < 1.0
    assert profile["import_fraction"] > 0.0


def test_actuation_lag_measures_delay_between_raised_limit_and_intake() -> None:
    rows = parse_rows(
        [
            _raw_row(
                second,
                wz_balkon_input_limit=350 if second >= 10 else 250,
                wz_balkon_bat_flow=-360 if second >= 14 else -300,
            )
            for second in range(20)
        ]
    )

    lag = find_actuation_lag(rows, "wz_balkon")

    assert lag["samples"] == 1
    assert lag["median_seconds"] == pytest.approx(4)
    assert lag["events"][0]["step_w"] == pytest.approx(100)


def test_charge_overshoot_event_relates_commanded_limit_to_preceding_export() -> None:
    rows = parse_rows(
        [_idle_row(second, -60) for second in range(6)]
        + [
            _raw_row(
                second,
                sml_power=30 if second >= 8 else -60,
                wz_balkon_ac_mode="input",
                wz_balkon_input_limit=150,
                wz_balkon_output_power=0,
                wz_balkon_bat_flow=0,
                wz_balkon_solar_power=0,
            )
            for second in range(6, 20)
        ]
    )

    events = find_charge_overshoot_events(rows, "wz_balkon")

    assert len(events) == 1
    assert events[0]["export_before_w"] == pytest.approx(60)
    assert events[0]["excursion_seconds"] == pytest.approx(6)
    assert events[0]["peak_input_limit_w"] == pytest.approx(150)
    assert events[0]["seconds_to_import"] == pytest.approx(2)
