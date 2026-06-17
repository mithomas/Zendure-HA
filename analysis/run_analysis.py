import csv
from datetime import datetime

# Load CSV
with open("export2026-05.27.csv") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

# Clean and parse rows starting from index 2 to avoid initial missing values
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
    except Exception as e:
        print(f"Error parsing row {idx}: {e}")

print(f"Parsed {len(parsed_rows)} rows out of {len(rows)} total.")

# Compute intervals and analyze
mode_switches = []
prev_mode = None
for idx, pr in enumerate(parsed_rows):
    if idx == 0:
        pr["dt"] = 1.0  # assume 1s for the first parsed row
    else:
        pr["dt"] = (pr["time"] - parsed_rows[idx - 1]["time"]).total_seconds()

    if prev_mode is not None and pr["k_mode"] != prev_mode:
        mode_switches.append((idx, pr["time"], prev_mode, pr["k_mode"]))
    prev_mode = pr["k_mode"]

print(f"Total k_balkon mode switches: {len(mode_switches)}")
print("Switches:")
for s in mode_switches:
    print(f"  At idx {s[0]} ({s[1]}): {s[2]} -> {s[3]}")

# 1. Total Avoidable Grid Import (kWh)
# Grid import is positive sml. When we import from the grid while charging k_balkon from AC,
# that grid import is avoidable if we reduced k_balkon's charge rate.
# Avoidable import = min(sml, k_in_l) when sml > 0 and k_mode == 'input' and k_in_l > 0.
# Wait! Let's check if wz_balkon is also charging. It's in output mode with wz_in_l = 0, so no.
# What if sml > 0, and k_balkon is in output mode? In output mode, k_out_limit is 0, so it's not discharging.
# Why is k_balkon in output mode with 0 limit when we are importing from the grid?
# If we are importing from the grid and k_balkon has solar and battery capacity, why is its output limit 0?
# Under the routing rules, secondary batteries can discharge to cover household load if primary cannot cover it.
# Why is k_balkon's output limit 0?
# In MATCHING mode:
# "Secondary device battery: Primary unavailable, at discharge limit, or unable to cover the remainder."
# Is the primary at its discharge limit?
# Let's check! In Row 70: sml = 58 (import). wz_solar = 552, wz_out = 367. wz_out_limit = 368.
# Wait! The primary output power (367 W) is at its limit (368 W).
# Why is the primary's limit 368 W? Is that the maximum discharge limit of the primary, or did the manager set it there?
# Let's check what the max limit is in other rows. In Row 115, wz_out_limit is 481 W. In Row 130 (let's check), it might be higher.
# If the primary output limit was 368 W, and household demand was higher (367 + 58 = 425 W), why didn't the manager set the primary's output limit higher?
# Let's check if the primary was limited by its own solar or battery discharge limits.
# But also, why didn't the secondary (k_balkon) discharge?
# Because the primary output was not yet at its absolute maximum (which is at least 481 W), or maybe there's a delay.
# Let's write code to compute avoidable import and export and classify them.

avoidable_import_ac_charge_k = 0.0  # kWh
avoidable_export_k_mode_output = 0.0  # kWh
avoidable_export_general = 0.0  # kWh

suspicious_intervals = []

for idx, pr in enumerate(parsed_rows):
    sml = pr["sml"]
    dt = pr["dt"]
    k_mode = pr["k_mode"]
    k_in_l = pr["k_in_l"]
    k_sol = pr["k_sol"]
    k_bat = pr["k_bat"]
    wz_sol = pr["wz_sol"]
    wz_out = pr["wz_out"]
    wz_out_l = pr["wz_out_l"]

    # Category 1: Grid import while charging k_balkon from AC
    # If sml > 0 and k_mode == 'input' and k_in_l > 0:
    # We are importing from grid, but also commanding k_balkon to charge from AC (input limit).
    # If we reduced the AC charge limit, we would import less.
    if sml > 0 and k_mode == "input" and k_in_l > 0:
        avoidable_w = min(sml, k_in_l)
        avoidable_kwh = (avoidable_w * dt) / 3600.0 / 1000.0
        avoidable_import_ac_charge_k += avoidable_kwh
        suspicious_intervals.append(
            {
                "idx": pr["idx"],
                "time": pr["time"],
                "dt": dt,
                "sml": sml,
                "wz_sol": wz_sol,
                "k_sol": k_sol,
                "wz_out": wz_out,
                "k_mode": k_mode,
                "k_in_l": k_in_l,
                "k_out_l": pr["k_out_l"],
                "wz_bat": pr["wz_bat"],
                "k_bat": k_bat,
                "reason": "Importing from grid while AC-charging secondary device",
                "kwh": avoidable_kwh,
            }
        )

    # Category 2: Grid export while k_balkon is in output mode with 0 limit (so not charging from grid)
    # If sml < 0 (export) and k_mode == 'output':
    # Since sml < 0, we have surplus power on the grid. We should be charging.
    # But k_balkon is in output mode (with output limit 0). In output mode, it cannot charge from AC.
    # If we put k_balkon in input mode, we could charge its battery with the surplus.
    # The surplus is -sml. The potential charge power we could have used is min(-sml, max_charge_rate - current_charge_rate).
    # Since k_bat is -80W (charging from its own solar), its AC input is 0.
    # It can easily absorb up to its max charge rate. Let's assume it can absorb at least up to 500W.
    # So the avoidable export is -sml.
    if sml < 0 and k_mode == "output":
        avoidable_w = -sml
        avoidable_kwh = (avoidable_w * dt) / 3600.0 / 1000.0
        avoidable_export_k_mode_output += avoidable_kwh
        suspicious_intervals.append(
            {
                "idx": pr["idx"],
                "time": pr["time"],
                "dt": dt,
                "sml": sml,
                "wz_sol": wz_sol,
                "k_sol": k_sol,
                "wz_out": wz_out,
                "k_mode": k_mode,
                "k_in_l": k_in_l,
                "k_out_l": pr["k_out_l"],
                "wz_bat": pr["wz_bat"],
                "k_bat": k_bat,
                "reason": "Exporting to grid while secondary is in output mode (charging blocked)",
                "kwh": avoidable_kwh,
            }
        )

print(f"\nAvoidable Grid Import due to AC charging k_balkon: {avoidable_import_ac_charge_k:.6f} kWh")
print(f"Avoidable Grid Export due to k_balkon being in output mode: {avoidable_export_k_mode_output:.6f} kWh")
