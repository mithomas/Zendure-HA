import csv
from datetime import datetime

with open("export2026-05.27.csv", mode="r") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

print("Row index, time, sml_power, wz_solar, k_solar, wz_out, wz_mode, k_mode, wz_in_lim, k_in_lim, wz_out_lim, k_out_lim, wz_bat, k_bat")
prev_k_mode = None
for idx, r in enumerate(rows):
    t = r["time"]
    sml = r["sml_power"]
    wz_s = r["wz_balkon_solar_power"]
    k_s = r["k_balkon_solar_power"]
    wz_out = r["wz_balkon_output_power"]
    wz_mode = r["wz_balkon_ac_mode"]
    k_mode = r["k_balkon_ac_mode"]
    wz_in_l = r["wz_balkon_input_limit"]
    k_in_l = r["k_balkon_input_limit"]
    wz_out_l = r["wz_balkon_output_limit"]
    k_out_l = r["k_balkon_output_limit"]
    wz_bat = r["wz_balkon_bat_flow"]
    k_bat = r["k_balkon_bat_flow"]
    
    # Let's print rows where mode changes or some interesting things happen
    mode_changed = prev_k_mode is not None and k_mode != prev_k_mode
    prev_k_mode = k_mode
    
    # We want to inspect the data, so let's print a selected subset of rows, e.g. every 10th row, plus rows where k_mode changes
    if idx % 10 == 0 or mode_changed or (sml and abs(float(sml)) > 100):
        print(f"{idx:3d}: {t} | sml:{sml:>4s} | wz_sol:{wz_s:>5s} k_sol:{k_s:>5s} | wz_out:{wz_out:>4s} | wz_mode:{wz_mode} k_mode:{k_mode} | wz_in_l:{wz_in_l} k_in_l:{k_in_l} | wz_out_l:{wz_out_l} k_out_l:{k_out_l} | wz_bat:{wz_bat:>4s} k_bat:{k_bat:>4s}")
