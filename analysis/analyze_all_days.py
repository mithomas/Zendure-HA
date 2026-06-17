import csv
import glob
import os
from datetime import datetime


def parse_float(val):
    if not val or val.strip() == "":
        return 0.0
    try:
        return float(val)
    except ValueError:
        return 0.0


def format_duration(seconds):
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def print_row_details(r, indent="    "):
    sml = r["sml"]
    prim = r["primary"]
    if prim == "wz_balkon":
        prim_lbl, sec_lbl = "wz_balkon(P)", "k_balkon(S)"
        p_mode, p_sol, p_out, p_in_l, p_out_l, p_bat, p_state = (
            r["wz_mode"],
            r["wz_sol"],
            r["wz_out"],
            r["wz_in_l"],
            r["wz_out_l"],
            r["wz_bat"],
            r["wz_state"],
        )
        s_mode, s_sol, s_out, s_in_l, s_out_l, s_bat, s_state = (
            r["k_mode"],
            r["k_sol"],
            r["k_out"],
            r["k_in_l"],
            r["k_out_l"],
            r["k_bat"],
            r["k_state"],
        )
    else:
        prim_lbl, sec_lbl = "k_balkon(P)", "wz_balkon(S)"
        p_mode, p_sol, p_out, p_in_l, p_out_l, p_bat, p_state = (
            r["k_mode"],
            r["k_sol"],
            r["k_out"],
            r["k_in_l"],
            r["k_out_l"],
            r["k_bat"],
            r["k_state"],
        )
        s_mode, s_sol, s_out, s_in_l, s_out_l, s_bat, s_state = (
            r["wz_mode"],
            r["wz_sol"],
            r["wz_out"],
            r["wz_in_l"],
            r["wz_out_l"],
            r["wz_bat"],
            r["wz_state"],
        )

    print(f"{indent}Time: {r['time'].strftime('%H:%M:%S')} | Grid SML: {sml} W")
    print(
        f"{indent}Primary {prim_lbl}: Mode={p_mode}, State={p_state}, Solar={p_sol}W, Output={p_out}W (Limit={p_out_l}W), Bat={p_bat}W"
    )
    print(
        f"{indent}Secondary {sec_lbl}: Mode={s_mode}, State={s_state}, Solar={s_sol}W, Output={s_out}W (Limit={s_out_l}W), Bat={s_bat}W"
    )


def find_sustained_periods(rows_list, condition_fn, gap_allowance_sec=30):
    matching_rows = [r for r in rows_list if condition_fn(r)]
    if not matching_rows:
        return []

    periods = []
    current_period = [matching_rows[0]]

    for r in matching_rows[1:]:
        prev_r = current_period[-1]
        time_diff = (r["time"] - prev_r["time"]).total_seconds()

        if time_diff <= gap_allowance_sec:
            current_period.append(r)
        else:
            periods.append(current_period)
            current_period = [r]
    periods.append(current_period)

    period_stats = []
    for p in periods:
        p_start = p[0]["time"]
        p_end = p[-1]["time"]
        p_duration = (p_end - p_start).total_seconds()

        p_all_rows = [r for r in rows_list if p_start <= r["time"] <= p_end]
        if not p_all_rows:
            continue

        total_duration_all = sum(r["dt"] for r in p_all_rows)
        avg_sml_p = sum(r["sml"] for r in p_all_rows) / len(p_all_rows)
        energy_kwh_p = sum(r["sml"] * r["dt"] for r in p_all_rows) / 3600.0 / 1000.0

        period_stats.append(
            {
                "start": p_start,
                "end": p_end,
                "duration": total_duration_all,
                "avg_sml": avg_sml_p,
                "energy_kwh": energy_kwh_p,
                "rows": p_all_rows,
            }
        )

    return period_stats


def group_episodes(rows_list, gap_allowance_sec=30):
    if not rows_list:
        return []
    episodes = []
    current_ep = [rows_list[0]]
    for r in rows_list[1:]:
        if (r["time"] - current_ep[-1]["time"]).total_seconds() <= gap_allowance_sec:
            current_ep.append(r)
        else:
            episodes.append(current_ep)
            current_ep = [r]
    episodes.append(current_ep)
    return episodes


def find_large_swings(parsed_rows, window_sec=120):
    swings = []
    for idx, r_start in enumerate(parsed_rows):
        start_t = r_start["time"]
        r_in_window = []
        for r in parsed_rows[idx:]:
            if (r["time"] - start_t).total_seconds() <= window_sec:
                r_in_window.append(r)
            else:
                break

        if len(r_in_window) < 2:
            continue

        sml_vals_w = [x["sml"] for x in r_in_window]
        min_sml = min(sml_vals_w)
        max_sml = max(sml_vals_w)
        swing_val = max_sml - min_sml

        swings.append(
            {
                "start_time": start_t,
                "end_time": r_in_window[-1]["time"],
                "swing": swing_val,
                "min_sml": min_sml,
                "max_sml": max_sml,
                "rows": r_in_window,
            }
        )

    swings.sort(key=lambda x: x["swing"], reverse=True)

    distinct_swings = []
    for sw in swings:
        overlap = False
        for d_sw in distinct_swings:
            if abs((sw["start_time"] - d_sw["start_time"]).total_seconds()) < window_sec:
                overlap = True
                break
        if not overlap:
            distinct_swings.append(sw)
            if len(distinct_swings) >= 5:
                break
    return distinct_swings


def analyze_file(file_path):
    print("\n==================================================")
    print(f"Analyzing: {file_path}")
    print("==================================================")

    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return None

    with open(file_path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Total rows: {len(rows)}")
    if not rows:
        return None

    wz_states = set()
    k_states = set()
    wz_modes = set()
    k_modes = set()
    prim_devices = set()

    parsed_rows = []

    for idx, r in enumerate(rows):
        if not r.get("time"):
            continue
        try:
            t = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S")
            sml = parse_float(r.get("sml_power"))
            wz_sol = parse_float(r.get("wz_balkon_solar_power"))
            k_sol = parse_float(r.get("k_balkon_solar_power"))
            wz_out = parse_float(r.get("wz_balkon_output_power"))
            k_out = parse_float(r.get("k_balkon_output_power"))
            wz_in_l = parse_float(r.get("wz_balkon_input_limit"))
            k_in_l = parse_float(r.get("k_balkon_input_limit"))
            wz_out_l = parse_float(r.get("wz_balkon_output_limit"))
            k_out_l = parse_float(r.get("k_balkon_output_limit"))
            wz_bat = parse_float(r.get("wz_balkon_bat_flow"))
            k_bat = parse_float(r.get("k_balkon_bat_flow"))

            wz_state = r.get("wz_balkon_device_state")
            k_state = r.get("k_balkon_device_state")
            wz_mode = r.get("wz_balkon_ac_mode")
            k_mode = r.get("k_balkon_ac_mode")
            prim_dev = r.get("primary_device")

            wz_states.add(wz_state)
            k_states.add(k_state)
            wz_modes.add(wz_mode)
            k_modes.add(k_mode)
            prim_devices.add(prim_dev)

            parsed_rows.append(
                {
                    "idx": idx,
                    "time": t,
                    "sml": sml,
                    "wz_sol": wz_sol,
                    "k_sol": k_sol,
                    "wz_out": wz_out,
                    "k_out": k_out,
                    "wz_mode": wz_mode,
                    "k_mode": k_mode,
                    "wz_in_l": wz_in_l,
                    "k_in_l": k_in_l,
                    "wz_out_l": wz_out_l,
                    "k_out_l": k_out_l,
                    "wz_bat": wz_bat,
                    "k_bat": k_bat,
                    "wz_state": wz_state,
                    "k_state": k_state,
                    "primary": prim_dev,
                }
            )
        except Exception:
            pass

    parsed_rows.sort(key=lambda x: x["time"])

    print(f"Successfully parsed {len(parsed_rows)} rows.")
    print(f"Unique wz_balkon states: {wz_states}")
    print(f"Unique k_balkon states: {k_states}")
    print(f"Unique wz_balkon modes: {wz_modes}")
    print(f"Unique k_balkon modes: {k_modes}")
    print(f"Unique primary devices: {prim_devices}")

    if not parsed_rows:
        return None

    for idx, pr in enumerate(parsed_rows):
        if idx == 0:
            pr["dt"] = 0.0
        else:
            pr["dt"] = (pr["time"] - parsed_rows[idx - 1]["time"]).total_seconds()

    switches = []
    prev_mode = None
    for pr in parsed_rows:
        if prev_mode is not None and pr["k_mode"] != prev_mode:
            switches.append((pr["time"], prev_mode, pr["k_mode"]))
        prev_mode = pr["k_mode"]

    print(f"Total k_balkon mode switches: {len(switches)}")

    import_ac_charge_w = []
    export_blocked_w = []
    suspicious_episodes = []

    for pr in parsed_rows:
        sml = pr["sml"]
        dt = pr["dt"]
        k_mode = pr["k_mode"]
        k_in_l = pr["k_in_l"]
        k_state = pr["k_state"]

        reason = None
        kwh = 0.0

        if sml > 0 and k_mode == "input" and k_in_l > 0:
            avoidable_w = min(sml, k_in_l)
            import_ac_charge_w.append(avoidable_w * dt)
            reason = "Grid Import while charging secondary AC"
            kwh = (avoidable_w * dt) / 3600.0 / 1000.0

        elif sml < 0 and k_mode == "output":
            avoidable_w = -sml
            export_blocked_w.append(avoidable_w * dt)
            reason = "Grid Export while secondary blocked"
            kwh = (avoidable_w * dt) / 3600.0 / 1000.0

        if reason:
            suspicious_episodes.append(
                {
                    "time": pr["time"],
                    "dt": dt,
                    "sml": sml,
                    "wz_sol": pr["wz_sol"],
                    "k_sol": pr["k_sol"],
                    "wz_out": pr["wz_out"],
                    "k_mode": k_mode,
                    "k_bat": pr["k_bat"],
                    "wz_bat": pr["wz_bat"],
                    "k_in_l": k_in_l,
                    "k_state": k_state,
                    "reason": reason,
                    "kwh": kwh,
                }
            )

    total_import_ac_charge_kwh = sum(import_ac_charge_w) / 3600.0 / 1000.0
    total_export_blocked_kwh = sum(export_blocked_w) / 3600.0 / 1000.0

    print(f"Total Avoidable Grid Import: {total_import_ac_charge_kwh:.6f} kWh")
    print(f"Total Avoidable Grid Export: {total_export_blocked_kwh:.6f} kWh")
    print(f"Combined Avoidable Energy: {total_import_ac_charge_kwh + total_export_blocked_kwh:.6f} kWh")

    import_periods = find_sustained_periods(parsed_rows, lambda r: r["sml"] >= 100, gap_allowance_sec=60)
    export_periods = find_sustained_periods(parsed_rows, lambda r: r["sml"] <= -100, gap_allowance_sec=60)
    import_periods = [p for p in import_periods if p["duration"] >= 60]
    export_periods = [p for p in export_periods if p["duration"] >= 60]
    import_periods.sort(key=lambda x: x["duration"], reverse=True)
    export_periods.sort(key=lambda x: x["duration"], reverse=True)

    print("\nTop 3 Sustained High Import Periods (SML >= 100W, >= 1m):")
    for p in import_periods[:3]:
        print(
            f"  {p['start'].strftime('%H:%M:%S')} to {p['end'].strftime('%H:%M:%S')} ({format_duration(p['duration'])}) - Avg: {p['avg_sml']:.1f}W"
        )

    print("\nTop 3 Sustained High Export Periods (SML <= -100W, >= 1m):")
    for p in export_periods[:3]:
        print(
            f"  {p['start'].strftime('%H:%M:%S')} to {p['end'].strftime('%H:%M:%S')} ({format_duration(p['duration'])}) - Avg: {p['avg_sml']:.1f}W"
        )

    swings = find_large_swings(parsed_rows)
    print("\nTop 3 SML Power Swings within any 2-minute window:")
    for sw in swings[:3]:
        print(
            f"  {sw['start_time'].strftime('%H:%M:%S')} to {sw['end_time'].strftime('%H:%M:%S')} - Magnitude: {sw['swing']:.1f} W ({sw['min_sml']:.1f} to {sw['max_sml']:.1f})"
        )

    return {
        "file": file_path,
        "total_rows": len(rows),
        "parsed_rows": len(parsed_rows),
        "switches": len(switches),
        "import_kwh": total_import_ac_charge_kwh,
        "export_kwh": total_export_blocked_kwh,
        "combined_kwh": total_import_ac_charge_kwh + total_export_blocked_kwh,
        "suspicious_count": len(suspicious_episodes),
    }


if __name__ == "__main__":
    import sys

    search_dir = "." if len(sys.argv) == 1 else sys.argv[1]
    csv_files = glob.glob(os.path.join(search_dir, "export*.csv"))
    csv_files.sort()
    if not csv_files:
        print(f"No export files found in '{search_dir}'.")
    else:
        for f in csv_files:
            analyze_file(f)
