import csv
from datetime import datetime

def analyze_file(file_path):
    print(f"\n==================================================")
    print(f"Analyzing: {file_path}")
    print(f"==================================================")
    
    with open(file_path, mode="r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        
    print(f"Total rows: {len(rows)}")
    if not rows:
        return
        
    # Categoricals and states
    wz_states = set()
    k_states = set()
    wz_modes = set()
    k_modes = set()
    prim_devices = set()
    
    parsed_rows = []
    
    for idx, r in enumerate(rows):
        wz_states.add(r.get("wz_balkon_device_state"))
        k_states.add(r.get("k_balkon_device_state"))
        wz_mode = r.get("wz_balkon_ac_mode")
        k_mode = r.get("k_balkon_ac_mode")
        wz_modes.add(wz_mode)
        k_modes.add(k_mode)
        prim_devices.add(r.get("primary_device"))
        
        # Clean and parse numerical values
        # Since these files might have empty fields when devices are offline or starting up,
        # we handle empty strings gracefully and fallback to 0.0.
        try:
            t = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S")
            sml = float(r["sml_power"]) if r["sml_power"] else 0.0
            wz_sol = float(r["wz_balkon_solar_power"]) if r["wz_balkon_solar_power"] else 0.0
            k_sol = float(r["k_balkon_solar_power"]) if r["k_balkon_solar_power"] else 0.0
            wz_out = float(r["wz_balkon_output_power"]) if r["wz_balkon_output_power"] else 0.0
            k_out = float(r["k_balkon_output_power"]) if r["k_balkon_output_power"] else 0.0
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
                "k_out": k_out,
                "wz_mode": wz_mode,
                "k_mode": k_mode,
                "wz_in_l": wz_in_l,
                "k_in_l": k_in_l,
                "wz_out_l": wz_out_l,
                "k_out_l": k_out_l,
                "wz_bat": wz_bat,
                "k_bat": k_bat,
                "wz_state": r.get("wz_balkon_device_state"),
                "k_state": r.get("k_balkon_device_state")
            })
        except Exception as e:
            pass
            
    print(f"Successfully parsed {len(parsed_rows)} rows.")
    print(f"Unique wz_balkon states: {wz_states}")
    print(f"Unique k_balkon states: {k_states}")
    print(f"Unique wz_balkon modes: {wz_modes}")
    print(f"Unique k_balkon modes: {k_modes}")
    print(f"Unique primary devices: {prim_devices}")
    
    # Calculate intervals
    for idx, pr in enumerate(parsed_rows):
        if idx == 0:
            pr["dt"] = 1.0
        else:
            pr["dt"] = (pr["time"] - parsed_rows[idx-1]["time"]).total_seconds()
            
    # Find mode switches on k_balkon
    switches = []
    prev_mode = None
    for pr in parsed_rows:
        if prev_mode is not None and pr["k_mode"] != prev_mode:
            switches.append((pr["time"], prev_mode, pr["k_mode"]))
        prev_mode = pr["k_mode"]
        
    print(f"Total k_balkon mode switches: {len(switches)}")
    
    # Compute avoidable import and export
    # Category 1: Import while charging secondary from AC
    # Avoidable import = min(sml, k_in_l) when sml > 0, k_mode == 'input', k_in_l > 0, and k_state == 'normal' (above reserve)
    # Category 2: Export while secondary blocked
    # Avoidable export = -sml when sml < 0, k_mode == 'output', and k_state == 'normal' (above reserve/normal range)
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
        
        # Grid import while AC charging (we only count when device is normal, but let's check)
        if sml > 0 and k_mode == "input" and k_in_l > 0:
            avoidable_w = min(sml, k_in_l)
            import_ac_charge_w.append(avoidable_w * dt)
            reason = "Grid Import while charging secondary AC"
            kwh = (avoidable_w * dt) / 3600.0 / 1000.0
            
        # Grid export while blocked in output mode (charging blocked)
        elif sml < 0 and k_mode == "output":
            avoidable_w = -sml
            export_blocked_w.append(avoidable_w * dt)
            reason = "Grid Export while secondary blocked"
            kwh = (avoidable_w * dt) / 3600.0 / 1000.0
            
        if reason:
            suspicious_episodes.append({
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
                "kwh": kwh
            })
            
    total_import_ac_charge_kwh = sum(import_ac_charge_w) / 3600.0 / 1000.0
    total_export_blocked_kwh = sum(export_blocked_w) / 3600.0 / 1000.0
    
    print(f"Total Avoidable Grid Import: {total_import_ac_charge_kwh:.6f} kWh")
    print(f"Total Avoidable Grid Export: {total_export_blocked_kwh:.6f} kWh")
    print(f"Combined Avoidable Energy: {total_import_ac_charge_kwh + total_export_blocked_kwh:.6f} kWh")
    
    # Analyze transitions and list them
    print("\nFirst 10 mode switches:")
    for t, m1, m2 in switches[:10]:
        print(f"  {t.strftime('%Y-%m-%d %H:%M:%S')}: {m1} -> {m2}")
    if len(switches) > 10:
        print(f"  ... and {len(switches) - 10} more switches.")
        
    return {
        "file": file_path,
        "total_rows": len(rows),
        "parsed_rows": len(parsed_rows),
        "switches": len(switches),
        "import_kwh": total_import_ac_charge_kwh,
        "export_kwh": total_export_blocked_kwh,
        "combined_kwh": total_import_ac_charge_kwh + total_export_blocked_kwh,
        "suspicious_count": len(suspicious_episodes),
        "suspicious_episodes": suspicious_episodes
    }

results_02 = analyze_file("export2026-06-02.csv")
results_03 = analyze_file("export2026-06-03.csv")
