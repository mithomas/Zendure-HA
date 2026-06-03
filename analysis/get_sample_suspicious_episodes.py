import csv
from datetime import datetime

def find_episode(file_path, num_flips_target=4):
    with open(file_path, mode="r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        
    parsed_rows = []
    for idx, r in enumerate(rows):
        try:
            t = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S")
            sml = float(r["sml_power"]) if r["sml_power"] else 0.0
            wz_sol = float(r["wz_balkon_solar_power"]) if r["wz_balkon_solar_power"] else 0.0
            k_sol = float(r["k_balkon_solar_power"]) if r["k_balkon_solar_power"] else 0.0
            wz_out = float(r["wz_balkon_output_power"]) if r["wz_balkon_output_power"] else 0.0
            k_out = float(r["k_balkon_output_power"]) if r["k_balkon_output_power"] else 0.0
            k_in_l = float(r["k_balkon_input_limit"]) if r["k_balkon_input_limit"] else 0.0
            wz_bat = float(r["wz_balkon_bat_flow"]) if r["wz_balkon_bat_flow"] else 0.0
            k_bat = float(r["k_balkon_bat_flow"]) if r["k_balkon_bat_flow"] else 0.0
            
            parsed_rows.append({
                "idx": idx,
                "time": t,
                "sml": sml,
                "wz_sol": wz_sol,
                "k_sol": k_sol,
                "wz_out": wz_out,
                "k_out": k_out,
                "k_mode": r.get("k_balkon_ac_mode"),
                "wz_mode": r.get("wz_balkon_ac_mode"),
                "k_in_l": k_in_l,
                "wz_bat": wz_bat,
                "k_bat": k_bat,
                "wz_state": r.get("wz_balkon_device_state"),
                "k_state": r.get("k_balkon_device_state")
            })
        except:
            pass
            
    # Let's find a sequence of about 15 rows where k_mode switches at least 3-4 times
    for start_idx in range(len(parsed_rows) - 20):
        subset = parsed_rows[start_idx : start_idx + 15]
        switches_count = 0
        prev_mode = None
        for item in subset:
            if prev_mode is not None and item["k_mode"] != prev_mode:
                switches_count += 1
            prev_mode = item["k_mode"]
            
        if switches_count >= num_flips_target:
            # We found an excellent episode!
            print(f"\nFound high-oscillation episode in {file_path} starting at row {start_idx} ({subset[0]['time']}):")
            print(f"| Timestamp | SML | wz_sol | k_sol | wz_out | k_mode | k_in_l | k_bat | Reason |")
            print(f"|---|---|---|---|---|---|---|---|---|")
            for item in subset:
                time_str = item["time"].strftime("%H:%M:%S")
                # reason
                reason = "Idle"
                if item["sml"] > 0 and item["k_mode"] == "input" and item["k_in_l"] > 0:
                    reason = "Avoidable Import"
                elif item["sml"] < 0 and item["k_mode"] == "output":
                    reason = "Avoidable Export"
                print(f"| {time_str} | {item['sml']:4.0f} | {item['wz_sol']:5.1f} | {item['k_sol']:5.1f} | {item['wz_out']:5.1f} | {item['k_mode']:6s} | {item['k_in_l']:5.1f} | {item['k_bat']:5.0f} | {reason} |")
            break

find_episode("export2026-06-02.csv", num_flips_target=4)
find_episode("export2026-06-03.csv", num_flips_target=4)
