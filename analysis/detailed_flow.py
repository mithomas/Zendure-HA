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

# Let's print out the exact values around the first transition (idx 30 to 45) with calculated setpoint
print("Detailed Flow analysis (idx 30 to 45):")
print(f"{'Idx':<3s} | {'Time':<8s} | {'sml':<4s} | {'wz_out':<6s} | {'k_in_l':<6s} | {'k_mode':<6s} | {'k_bat':<5s} | {'Calc Setpoint':<13s} | {'Mode Decision':<15s}")
print("-" * 85)

for idx, pr in enumerate(parsed_rows):
    if pr["idx"] < 25 or pr["idx"] > 50:
        continue
    
    # Calculate setpoint like _poll_devices_and_prepare_routing_state
    # If k_balkon is in input mode (charging), homeInput is k_in_l (or we can approximate using k_bat if we want)
    # Actually, in _poll_devices_and_prepare_routing_state, d.homeInput.asInt is used.
    # In input mode, homeInput is the AC charging power. Let's see if we can find its value.
    # Let's approximate: if k_mode == 'input', homeInput = k_in_l.
    # If d is in discharge (output mode), homeOutput = wz_out.
    home_wz = pr["wz_out"]
    home_k = -pr["k_in_l"] if pr["k_mode"] == "input" else 0.0
    
    # setpoint = sml + home Output of discharging devices + home Input of charging devices (which is negative)
    # wait, in _poll_devices_and_prepare_routing_state:
    # home = -d.homeInput.asInt + max(0, d.pwr_offgrid)
    # if home < 0 (charging): setpoint += home
    # home = d.homeOutput.asInt
    # if home > 0 (discharging): setpoint += home
    calc_setpoint = pr["sml"] + home_wz + home_k
    
    # Decision: if calc_setpoint < 0, we route input (charge). If calc_setpoint >= 0, we route output (discharge).
    # Wait, in MATCHING mode, the manager clamps the setpoint.
    decision = "CHARGE" if calc_setpoint < 0 else "DISCHARGE"
    
    time_str = pr["time"].strftime("%H:%M:%S")
    print(f"{pr['idx']:3d} | {time_str} | {pr['sml']:4.0f} | {pr['wz_out']:6.1f} | {pr['k_in_l']:6.1f} | {pr['k_mode']:6s} | {pr['k_bat']:5.0f} | {calc_setpoint:13.1f} | {decision:<15s}")
