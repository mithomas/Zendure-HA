import csv
from datetime import datetime

# Load CSV
with open("export2026-05.27.csv", mode="r") as f:
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
        
        parsed_rows.append({
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
            "k_bat": k_bat
        })
    except Exception as e:
        pass

for idx, pr in enumerate(parsed_rows):
    if idx == 0:
        pr["dt"] = 1.0
    else:
        pr["dt"] = (pr["time"] - parsed_rows[idx-1]["time"]).total_seconds()

print("| Timestamp | Duration | kWh Est | SoC (wz/k) | Solar (W) | Load (W) | Grid (W) | Battery Flow (W) | Reason |")
print("|---|---|---|---|---|---|---|---|---|")

# Let's collect rows that represent continuous intervals or show them as discrete events
for pr in parsed_rows:
    sml = pr["sml"]
    dt = pr["dt"]
    k_mode = pr["k_mode"]
    k_in_l = pr["k_in_l"]
    k_sol = pr["k_sol"]
    wz_sol = pr["wz_sol"]
    wz_out = pr["wz_out"]
    wz_bat = pr["wz_bat"]
    k_bat = pr["k_bat"]
    
    # Calculate load: SML + total output (wz_out) - AC charge power (approx k_in_l if input else 0)
    # Actually, SML + wz_out is the total load when k_balkon is not AC charging.
    # When k_balkon is AC charging, SML + wz_out - k_in_l is the household load excluding charging.
    # Let's represent household load = SML + wz_out - (k_in_l if k_mode == 'input' else 0).
    ac_charge_k = k_in_l if k_mode == "input" else 0.0
    load_w = sml + wz_out - ac_charge_k
    solar_w = wz_sol + k_sol
    
    reason = None
    kwh = 0.0
    
    if sml > 0 and k_mode == "input" and k_in_l > 0:
        reason = "Grid Import while charging secondary AC"
        kwh = (min(sml, k_in_l) * dt) / 3600.0 / 1000.0
    elif sml < 0 and k_mode == "output":
        reason = "Grid Export while secondary blocked"
        kwh = (-sml * dt) / 3600.0 / 1000.0
        
    if reason:
        time_str = pr["time"].strftime("%H:%M:%S")
        print(f"| {time_str} | {int(dt)}s | {kwh:.7f} | Normal/Normal | {solar_w:.1f} | {load_w:.1f} | {sml:.1f} | wz: {wz_bat:.0f}, k: {k_bat:.0f} | {reason} |")
