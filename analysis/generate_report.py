import csv
from datetime import datetime

# Load CSV
with open("export2026-05.27.csv") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

parsed_rows = []
for idx, r in enumerate(rows):
    if idx < 2:
        continue
    try:
        t = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S")
        sml = float(r["sml_power"]) if r["sml_power"] else 0.0
        wz_sol = float(r["wz_balkon_solar_power"]) if r["wz_balkon_solar_power"] else 0.0
        k_sol = float(r["k_balkon_solar_power"]) if r["k_balkon_solar_power"] else 0.0
        wz_out = float(r["wz_balkon_output_power"]) if r["wz_balkon_output_power"] else 0.0
        wz_mode = r["wz_balkon_ac_mode"]
        k_mode = r["k_balkon_ac_mode"]
        wz_in_l = float(r["wz_balkon_input_limit"]) if r["wz_balkon_input_limit"] else 0.0
        k_in_l = float(r["k_balkon_input_limit"]) if r["k_balkon_input_limit"] else 0.0
        wz_out_l = float(r["wz_balkon_output_limit"]) if r["wz_balkon_output_limit"] else 0.0
        k_out_l = float(r["k_balkon_output_limit"]) if r["k_balkon_output_limit"] else 0.0
        wz_bat = float(r["wz_balkon_bat_flow"]) if r["wz_balkon_bat_flow"] else 0.0
        k_bat = float(r["k_balkon_bat_flow"]) if r["k_balkon_bat_flow"] else 0.0

        parsed_rows.append(
            {
                "idx": idx,
                "time": t,
                "sml": sml,
                "wz_sol": wz_sol,
                "k_sol": k_sol,
                "wz_out": wz_out,
                "wz_mode": wz_mode,
                "k_mode": k_mode,
                "wz_in_l": wz_in_l,
                "k_in_l": k_in_l,
                "wz_out_l": wz_out_l,
                "k_out_l": k_out_l,
                "wz_bat": wz_bat,
                "k_bat": k_bat,
            }
        )
    except Exception:
        pass

for idx, pr in enumerate(parsed_rows):
    if idx == 0:
        pr["dt"] = 1.0
    else:
        pr["dt"] = (pr["time"] - parsed_rows[idx - 1]["time"]).total_seconds()

# Analyze in detail
import_ac_charge_w = []
export_blocked_w = []

for pr in parsed_rows:
    sml = pr["sml"]
    dt = pr["dt"]
    k_mode = pr["k_mode"]
    k_in_l = pr["k_in_l"]

    # 1. Import while charging from AC
    if sml > 0 and k_mode == "input" and k_in_l > 0:
        avoidable_w = min(sml, k_in_l)
        import_ac_charge_w.append(avoidable_w * dt)

    # 2. Export while secondary is blocked (in output mode with 0 limit)
    if sml < 0 and k_mode == "output":
        avoidable_w = -sml
        export_blocked_w.append(avoidable_w * dt)

total_import_ac_charge_kwh = sum(import_ac_charge_w) / 3600.0 / 1000.0
total_export_blocked_kwh = sum(export_blocked_w) / 3600.0 / 1000.0

print("Summary:")
print(
    f"  Total Avoidable Grid Import (AC charging): {total_import_ac_charge_kwh:.6f} kWh ({sum(import_ac_charge_w):.1f} W-s)"
)
print(
    f"  Total Avoidable Grid Export (Secondary blocked): {total_export_blocked_kwh:.6f} kWh ({sum(export_blocked_w):.1f} W-s)"
)
print(f"  Combined Avoidable energy: {total_import_ac_charge_kwh + total_export_blocked_kwh:.6f} kWh")

# Let's count mode transitions
transitions = []
prev_mode = None
for pr in parsed_rows:
    if prev_mode is not None and pr["k_mode"] != prev_mode:
        transitions.append((pr["time"], prev_mode, pr["k_mode"]))
    prev_mode = pr["k_mode"]

print(f"\nMode switches count: {len(transitions)}")
for t, m1, m2 in transitions:
    print(f"  {t.strftime('%H:%M:%S')}: {m1} -> {m2}")
