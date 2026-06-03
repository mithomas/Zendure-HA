import csv

with open("export2026-05.27.csv", mode="r") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

def print_range(start, end):
    print(f"\n--- Range {start} to {end} ---")
    for idx in range(start, min(end, len(rows))):
        r = rows[idx]
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
        print(f"{idx:3d}: {t} | sml:{sml:>4s} | wz_sol:{wz_s:>5s} k_sol:{k_s:>5s} | wz_out:{wz_out:>4s} | wz_mode:{wz_mode} k_mode:{k_mode} | wz_in_l:{wz_in_l} k_in_l:{k_in_l} | wz_out_l:{wz_out_l} k_out_l:{k_out_l} | wz_bat:{wz_bat:>4s} k_bat:{k_bat:>4s}")

print_range(30, 45)
print_range(60, 80)
print_range(90, 125)
