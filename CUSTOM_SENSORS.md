# Custom Sensors and Entities

This document provides a comprehensive guide to the custom sensors and entities introduced in this fork compared to the upstream Zendure integration baseline.

---

## 1. True Grid Loading Counter (`aggr_grid_charge`)

### Overview
- **Entity ID**: `sensor.<device_id>_total_grid_charge_energy` (translation key: `aggr_grid_charge`)
- **Name**: Total Grid Charge Energy / Gesamte Netz-Ladeenergie
- **Unit & Classes**: `kWh`, `device_class: energy`, `state_class: total_increasing`, precision `2`
- **Scope**: AC-capable Zendure devices

### Motivation & Problem
Standard Zendure devices report raw AC socket intake under `gridInputPower`, which is accumulated into `aggrGridInputPower` ("Total Grid Energy"). However, when a Zendure battery AC-charges from on-site solar power (e.g., rooftop solar or other balcony PV systems connected to the household AC circuit), that power is clean solar energy, not energy purchased from the public utility grid.

`aggr_grid_charge` measures only the **true energy drawn from the public grid**, ignoring solar-backed AC charging.

### Device Eligibility
Exclusively instantiated on devices with AC-charging hardware (`charge_limit < 0`):
- **Supported:** Hyper 2000, SolarFlow 800, SolarFlow 1600, SolarFlow 2400, SolarFlow 4000, Ace 1500, SuperBase V.
- **Excluded:** DC-only devices (Hub 1200, Hub 2000, AIO 2400), which operate with `charge_limit == 0` and cannot AC-charge.

### Accounting & Calculation Rules
1. **Total Solar Generation (`CONF_TOTAL_SOLAR`):**
   - Users can link an optional **Total Solar Power Sensor** in the integration configuration or options flow (`device_class: power`).
   - Must be an instantaneous power sensor (W or kW), not a cumulative energy meter (kWh).
   - Automatically supports both `W` and `kW` units (values in `kW` are converted to Watts).
   - If not configured, unavailable, or an energy sensor is selected, the manager falls back to the sum of all online Zendure devices' active solar generation.
2. **Gross Solar (Battery-First) Policy:**
   - On-site solar generation backs AC battery charging first up to total solar power.
   - Unbacked charging demand is calculated per device as:
     $$P_{\text{demand}}(d) = \max(0, P_{\text{ac\_input}}(d) - P_{\text{solar\_allocated}})$$
3. **Priority Distribution:**
   - **Solar Allocation:** The Selected Primary device has first claim on available solar generation. Remaining solar is allocated to Secondary devices.
   - **Grid Import Attribution:** Secondary devices absorb available grid import first. The Selected Primary device absorbs grid import only if remaining grid import exceeds secondary demand.
4. **Physical Grid Conservation Bound:**
   - Total attributed grid charging power is strictly capped by real-time net P1 grid import:
     $$\sum P_{\text{grid\_charge}} \le \max(0, P1)$$
   - When the home is net exporting ($P1 \le 0$), true grid charging power is guaranteed to be 0 W across all devices.
5. **Energy Integration:**
   - Integrates continuously via Riemann sum into cumulative kWh, persisting across Home Assistant restarts and suitable for the Home Assistant Energy Dashboard.

---

## 2. Manager-Level Capacity & Aggregation Sensors

These sensors provide system-wide insights across all managed devices and are exposed by the Zendure Manager coordinator:

| Sensor Entity | Translation Key | Unit | Description |
|---|---|---|---|
| `available_kwh` | `available_kwh` | kWh | Available usable battery energy currently above the configured minimum SoC and discharge recovery margins. Excludes offline devices. |
| `total_available_kwh` | `total_available_kwh` | kWh | Total usable battery capacity across all configured devices, retaining offline devices so system capacity does not vanish during transient drops. |
| `global_soc` | `global_soc` | % | Capacity-weighted aggregate battery state-of-charge across all online batteries ($\sum \text{level} \times \text{capacity} / \text{total capacity}$). |
| `managed_device_count` | `managed_device_count` | int | Number of online devices currently managed by the active routing coordinator. |

---

## 3. Device-Level Energy & Cumulative Counters

In addition to standard charging and discharging aggregates, the following cumulative energy counters are provided:

| Sensor Entity | Translation Key | Unit | Device Scope | Description |
|---|---|---|---|---|
| `aggr_grid_charge` | `aggr_grid_charge` | kWh | AC-capable devices | True energy drawn from the grid during AC charging (excluding solar-backed AC charge). |
| `aggr_grid_input_power` | `aggr_grid_input_power` | kWh | AC-capable devices | Total raw energy fed into the AC port of the device. |
| `aggr_grid_off_power` | `aggr_grid_off_power` | kWh | SF800, SF1600, SF2400, SF4000 | Total energy supplied through the dedicated off-grid AC socket. |
| `aggr_solar1` .. `aggr_solar6` | `aggr_solar1` .. `aggr_solar6` | kWh | Multi-PV devices | Cumulative solar energy generated per individual PV string/port. |

---

## 4. Diagnostics, Health & Status Entities

| Entity | Translation Key | Domain | Description |
|---|---|---|---|
| `last_http_report` | `last_http_report` | Sensor (timestamp) | Timestamp of the most recent valid telemetry payload received via local ZenSDK HTTP. |
| `last_mqtt_report` | `last_mqtt_report` | Sensor (timestamp) | Timestamp of the most recent valid telemetry payload received via local or cloud MQTT. |
| `soc_limit` | `soc_limit` | Sensor (enum) | Detailed classification of device SoC state: `Normal`, `Full`, `Empty`, `Reserve recovery`, and `Safety margin`. |

---

## 5. Comparison Matrix: Fork vs Upstream Baseline

Comparison against upstream commit `8d59bac4` (tag `1.4.4+`):

| Entity Name | Domain | Scope | In Upstream? | In This Fork? | Status / Purpose |
|---|---|---|---|---|---|
| `Total Grid Charge Energy` (`aggr_grid_charge`) | Sensor | Device (AC-capable) | No | **Yes** | **New:** True grid AC charging energy excluding clean solar |
| `Total Solar Power Sensor` (`total_solar`) | Config/Option | Manager | No | **Yes** | **New:** Config option to link external rooftop/balcony solar |
| `Available Energy` (`available_kwh`) | Sensor | Manager | Partial (buggy) | **Yes** | Net available discharge energy considering recovery margins |
| `Total Available Energy` (`total_available_kwh`) | Sensor | Manager | No | **Yes** | Preserves aggregate capacity even if a device drops offline |
| `Global Battery SOC` (`global_soc`) | Sensor | Manager | No | **Yes** | Accurate capacity-weighted system SoC percentage |
| `Managed Device Count` (`managed_device_count`) | Sensor | Manager | No | **Yes** | Active count of managed devices |
| `PV1..PV6 Total Solar` (`aggr_solar1..6`) | Sensor | Device | No | **Yes** | Per-string solar energy integration |
| `Total Off-Grid Energy` (`aggr_grid_off_power`) | Sensor | Device | No | **Yes** | Off-grid port cumulative energy meter |
| `Last HTTP Report` (`last_http_report`) | Sensor | Device | No | **Yes** | Telemetry freshness & connectivity diagnostic |
| `Last MQTT Report` (`last_mqtt_report`) | Sensor | Device | No | **Yes** | Telemetry freshness & connectivity diagnostic |
| `Battery Heating` (`heat_state`) | Binary Sensor | Device | No | **Yes** | Active battery heating indication |
| `Bypass` (`pass`) | Binary Sensor | Device | No | **Yes** | Hardware bypass state indication |
| `SoC Limit` (`soc_limit`) | Sensor | Device | Simplified | **Extended** | Extended states (`Reserve recovery`, `Safety margin`) |
| `Discharge Recovery Margin` | Number | Manager | No | **Yes** | Configurable hysteresis floor before discharge resumes |
| `Min SoC Safety Margin` | Number | Manager | No | **Yes** | Safety buffer protecting minimum reserve floor |
