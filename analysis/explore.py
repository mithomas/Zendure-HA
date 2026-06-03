import pandas as pd

df = pd.read_csv("export2026-05.27.csv")
print("Columns:", df.columns.tolist())
print("\nShape:", df.shape)
print("\nData Types:")
print(df.dtypes)
print("\nMissing values:")
print(df.isnull().sum())
print("\nDevice states (wz_balkon):", df["wz_balkon_device_state"].unique())
print("Device states (k_balkon):", df["k_balkon_device_state"].unique())
print("\nAC modes (wz_balkon):", df["wz_balkon_ac_mode"].unique())
print("AC modes (k_balkon):", df["k_balkon_ac_mode"].unique())
print("\nPrimary device unique values:", df["primary_device"].unique())
print("Spike filter unique values:", df["spike_filter"].unique())
print("\nMin/Max/Mean of numeric columns:")
numeric_cols = ["sml_power", "wz_balkon_solar_power", "k_balkon_solar_power", 
                "wz_balkon_output_power", "k_balkon_output_power",
                "wz_balkon_input_limit", "k_balkon_input_limit",
                "wz_balkon_output_limit", "k_balkon_output_limit",
                "wz_balkon_bat_flow", "k_balkon_bat_flow"]
print(df[numeric_cols].describe())
