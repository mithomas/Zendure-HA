import csv
from datetime import datetime

def deep_dive_patterns(file_path):
    print(f"\n==================================================")
    print(f"Deep-Dive Patterns: {file_path}")
    print(f"==================================================")
    
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
            wz_in_l = float(r["wz_balkon_input_limit"]) if r["wz_balkon_input_limit"] else 0.0
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
                "wz_mode": r.get("wz_balkon_ac_mode"),
                "k_mode": r.get("k_balkon_ac_mode"),
                "wz_in_l": wz_in_l,
                "k_in_l": k_in_l,
                "wz_out_l": wz_out_l,
                "k_out_l": k_out_l,
                "wz_bat": wz_bat,
                "k_bat": k_bat,
                "wz_state": r.get("wz_balkon_device_state"),
                "k_state": r.get("k_balkon_device_state"),
                "primary": r.get("primary_device")
            })
        except:
            pass
            
    print(f"Total parsed rows: {len(parsed_rows)}")
    
    # 1. State occurrences
    wz_state_counts = {}
    k_state_counts = {}
    for r in parsed_rows:
        wz_state_counts[r["wz_state"]] = wz_state_counts.get(r["wz_state"], 0) + 1
        k_state_counts[r["k_state"]] = k_state_counts.get(r["k_state"], 0) + 1
        
    print("\nwz_balkon state counts:")
    for state, count in wz_state_counts.items():
        print(f"  {state}: {count} ({(count/len(parsed_rows))*100:.1f}%)")
        
    print("k_balkon state counts:")
    for state, count in k_state_counts.items():
        print(f"  {state}: {count} ({(count/len(parsed_rows))*100:.1f}%)")
        
    # 2. Primary Device switches
    primary_switches = []
    prev_primary = None
    for r in parsed_rows:
        if prev_primary is not None and r["primary"] != prev_primary:
            primary_switches.append((r["time"], prev_primary, r["primary"]))
        prev_primary = r["primary"]
        
    print(f"\nTotal primary device changes: {len(primary_switches)}")
    for t, p1, p2 in primary_switches:
        print(f"  {t.strftime('%Y-%m-%d %H:%M:%S')}: {p1} -> {p2}")
        
    # 3. wz_balkon_ac_mode switches (Primary device AC mode switching)
    wz_mode_switches = []
    prev_wz_mode = None
    for r in parsed_rows:
        if prev_wz_mode is not None and r["wz_mode"] != prev_wz_mode:
            wz_mode_switches.append((r["time"], prev_wz_mode, r["wz_mode"]))
        prev_wz_mode = r["wz_mode"]
        
    print(f"\nTotal primary AC mode switches (wz_balkon): {len(wz_mode_switches)}")
    if wz_mode_switches:
        print("First 5 primary AC mode switches:")
        for t, m1, m2 in wz_mode_switches[:5]:
            print(f"  {t.strftime('%Y-%m-%d %H:%M:%S')}: {m1} -> {m2}")
            
    # 4. Analyze non-normal states (e.g. reserve, empty, reserve_recovery) and see if they block useful behavior
    # Specifically, do we see grid import while battery has solar?
    # Let's check what happens when wz_state is 'reserve' or 'empty'.
    # In 'reserve' or 'empty', battery discharge is blocked.
    # But does PV still serve home?
    # Let's inspect some rows where state is 'reserve' or 'empty' and check if we are importing from the grid while solar is being stored or curtailed.
    # In 'empty' state: PV routing should be "Battery-first" (PV -> own battery before home load)
    # In 'reserve' state: PV routing should be "Normal" (household-load-first; PV still serves home).
    # Let's find some examples.
    empty_rows = [r for r in parsed_rows if r["wz_state"] == "empty" or r["k_state"] == "empty"]
    print(f"\nFound {len(empty_rows)} rows where at least one device is in 'empty' state.")
    if empty_rows:
        print("Sample row in empty state:")
        sample = empty_rows[len(empty_rows)//2]
        print(f"  Time: {sample['time']} | wz_state: {sample['wz_state']} k_state: {sample['k_state']} | wz_sol: {sample['wz_sol']} k_sol: {sample['k_sol']} | wz_out: {sample['wz_out']} | sml: {sample['sml']} | k_mode: {sample['k_mode']}")
        
    reserve_rows = [r for r in parsed_rows if r["wz_state"] == "reserve" or r["k_state"] == "reserve"]
    print(f"Found {len(reserve_rows)} rows where at least one device is in 'reserve' state.")
    if reserve_rows:
        print("Sample row in reserve state:")
        sample = reserve_rows[len(reserve_rows)//2]
        print(f"  Time: {sample['time']} | wz_state: {sample['wz_state']} k_state: {sample['k_state']} | wz_sol: {sample['wz_sol']} k_sol: {sample['k_sol']} | wz_out: {sample['wz_out']} | sml: {sample['sml']} | k_mode: {sample['k_mode']}")

deep_dive_patterns("export2026-06-02.csv")
deep_dive_patterns("export2026-06-03.csv")
