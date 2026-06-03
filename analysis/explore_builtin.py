import csv
from datetime import datetime

with open("export2026-05.27.csv", mode="r") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

print("Total rows:", len(rows))
if len(rows) > 0:
    print("Columns:", list(rows[0].keys()))

# Analyze columns
wz_states = set()
k_states = set()
wz_modes = set()
k_modes = set()
prim_devices = set()
spike_filters = set()

null_counts = {col: 0 for col in rows[0].keys()} if rows else {}

for r in rows:
    wz_states.add(r.get("wz_balkon_device_state"))
    k_states.add(r.get("k_balkon_device_state"))
    wz_modes.add(r.get("wz_balkon_ac_mode"))
    k_modes.add(r.get("k_balkon_ac_mode"))
    prim_devices.add(r.get("primary_device"))
    spike_filters.add(r.get("spike_filter"))
    
    for col, val in r.items():
        if val is None or val == "":
            null_counts[col] += 1

print("\nUnique wz_balkon_device_state:", wz_states)
print("Unique k_balkon_device_state:", k_states)
print("Unique wz_balkon_ac_mode:", wz_modes)
print("Unique k_balkon_ac_mode:", k_modes)
print("Unique primary_device:", prim_devices)
print("Unique spike_filter:", spike_filters)

print("\nMissing values per column:")
for col, count in null_counts.items():
    print(f"  {col}: {count}")

# Print first 5 rows to inspect formatting
print("\nFirst 3 rows:")
for r in rows[:3]:
    print(dict(r))
