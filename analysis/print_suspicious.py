import csv
from datetime import datetime

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

print(f"{'Idx':<4s} | {'Time':<8s} | {'dt':<3s} | {'sml':<4s} | {'wz_sol':<6s} | {'k_sol':<5s} | {'wz_out':<6s} | {'k_mode':<6s} | {'k_in_l':<5s} | {'k_out_l':<5s} | {'Reason':<40s} | {'kWh':<9s}")
print("-" * 125)

for idx, pr in enumerate(parsed_rows):
    sml = pr["sml"]
    dt = pr["dt"]
    k_mode = pr["k_mode"]
    k_in_l = pr["k_in_l"]
    k_out_l = pr["k_out_l"]
    k_sol = pr["k_sol"]
    wz_sol = pr["wz_sol"]
    wz_out = pr["wz_out"]
    
    reason = None
    kwh = 0.0
    
    if sml > 0 and k_mode == "input" and k_in_l > 0:
        reason = "Importing while charging secondary AC"
        kwh = (min(sml, k_in_l) * dt) / 3600.0 / 1000.0
    elif sml < 0 and k_mode == "output":
        reason = "Exporting while secondary blocked"
        kwh = (-sml * dt) / 3600.0 / 1000.0
    
    if reason:
        time_str = pr["time"].strftime("%H:%M:%S")
        print(f"{pr['idx']:3d}  | {time_str} | {int(dt):3d} | {sml:4.0f} | {wz_sol:6.1f} | {k_sol:5.1f} | {wz_out:6.1f} | {k_mode:6s} | {k_in_l:5.1f} | {k_out_l:5.1f} | {reason:<40s} | {kwh:9.7f}")
