# Manager Routing Preference Rules

This document summarizes the intended routing preferences for the Zendure manager based on the current implementation and manager tests. It focuses on single-primary routing logic and the primary/secondary relationship. Fusegroup capacity sharing, multi-device load balancing within a pool, and offline device recovery sequencing are not covered here.

## Grid Meter Sign Convention

| Grid reading | Meaning                           |
|--------------|-----------------------------------|
| Positive | Household needs power (grid demand) |
| Negative | Surplus power available |

The manager accounts for power devices are already providing or consuming before issuing new commands. This prevents overreaction to telemetry lag, already-active PV pass-through, or in-flight charge/discharge activity.

## Device States

Device reserve and recovery state is owned by the device; the manager consumes it and must not recompute it from back-references.

| State        | SoC condition                      | Battery discharge | Available energy                               | PV routing                                        |
|--------------|------------------------------------|-------------------|------------------------------------------------|---------------------------------------------------|
| Empty        | At or below minimum SoC            | Blocked           | Excluded                                       | Battery-first (PV → own battery before home load) |
| At reserve   | Above minimum, at or below reserve | Blocked           | Excluded                                       | Normal (household-load-first); current PV still serves home |
| Recovering   | Any                                | Blocked           | Excluded                                       | Pass-through preserved for home demand            |
| Offline      | Any                                | Blocked           | Excluded from *available*; included in *total* | —                                                 |
| Near-full    | In device-specific taper range     | Allowed           | Included                                       | Normal; charge capped by taper (see below)        |
| Normal       | Above reserve, not recovering      | Allowed           | Included                                       | Normal                                            |

> **Note:** *Available energy* excludes offline devices. *Total available energy* retains offline devices so the aggregate does not vanish when a device is temporarily unreachable. Negative contributions are clamped to zero.

> **Note:** Blocking battery discharge does not block current PV or off-grid output. Devices in any blocked state can still pass whatever power they are currently producing to the home.

> **Note:** Evidence that current home output is battery-backed is owned by the device. The manager may ask a device whether its reported home output appears to include battery power, but must not duplicate that inference from raw telemetry.

## Routing Pools

Each cycle the manager assigns every online device to exactly one pool. This is separate from device state — state constrains what a device *can* do; the pool reflects what the manager has *assigned* it this cycle.

| Pool        | Meaning                                                        |
|-------------|----------------------------------------------------------------|
| Discharging | Currently commanded to deliver power to the home               |
| Charging    | Currently commanded to store surplus                           |
| Idle        | Online and eligible, but not yet assigned to charge or discharge; including devices currently passing solar or off-grid power to the home |

A device in Normal state can be in any pool.

## Mode Behavior

Primary-aware modes follow the same rules but prefer the selected primary device over fallback devices where safe and useful.

| Mode                 | Positive reading (demand)                  | Negative reading (surplus)         |
|----------------------|--------------------------------------------|------------------------------------|
| `MATCHING`           | Discharge                                  | Charge; strong battery-backed export may trim home output only |
| `MATCHING_DISCHARGE` | Discharge                                  | No action; strong battery-backed export may trim home output only |
| `MATCHING_CHARGE`    | PV pass-through only; no battery discharge | Charge                             |
| `STORE_SOLAR`        | Home output stopped for non-full devices; full devices pass through | Charge                             |
| `MANUAL`             | Use configured manual power target         | Use configured manual power target |

## Discharge Routing (Grid Demand)

Apply sources in priority order, stopping when demand is covered:

| Priority | Source                                       | Condition                                                                      |
|----------|----------------------------------------------|--------------------------------------------------------------------------------|
| 1        | Selected primary device PV / off-grid / bypass PV | Primary online; bypass PV counts as primary PV when the primary is full |
| 2        | Secondary device PV / off-grid / bypass PV   | Secondary PV is used before any battery-backed discharge                       |
| 3        | Selected primary battery                     | Primary online, above all SoC/reserve/recovery floors, within discharge limit; full bypass-capable primary may discharge only after PV cannot cover demand |
| 4        | Secondary device battery                     | Primary unavailable, at discharge limit, or unable to cover the remainder      |

**Primary unavailability:** if the primary is offline, empty, at reserve, recovering, or at its discharge limit, remaining demand falls through to secondary devices.

> **Note:** recovering devices are excluded from the produced-floor allocation in discharge routing to prevent them from holding a floor they might not sustain.

> **Edge case — full-device bypass:** a full device that supports bypass stays in bypass while PV can cover the household demand. If PV from the primary and secondaries cannot cover demand, the selected primary may discharge before secondary batteries are used. A bypass-capable full device still receives a bypass command instead of a zero-watt discharge command when no battery-backed output is needed from it.

## Charge Routing (Grid Surplus)

1. **Primary first:** charge the selected primary if it is online and can accept charge.
2. **Secondary fallback:** if the primary cannot use the surplus, route to secondary devices.
3. **Home-serving PV is not reassigned:** if the primary is actively passing PV to the home and has no local surplus beyond that home output, that PV must not be redirected to primary charging — *unless* the device is empty.
4. **Empty device exception:** empty devices in primary-aware automatic modes are charge-first. Their current PV is redirected to their own battery before being preserved for home load. Once the device reaches at-reserve state it returns to normal household-load-first routing.
5. **Keep local PV local:** prefer charging a secondary device with its own PV over causing the primary to discharge or increasing grid consumption. A secondary that is both serving the home and charging should keep its PV locally when the primary can cover remaining demand, either by reducing active primary PV charging or by increasing PV-backed primary home output.
6. **Primary with no local solar defers to secondaries:** if the primary is charging but has no solar of its own to contribute (it would draw from the grid to charge), and a secondary has its own solar available, the charge allocation shifts to that secondary instead.
7. **Full primary hands off to secondaries:** when the primary battery is full and has entered bypass, idle secondary devices that have solar available are promoted to charging so that surplus is not wasted.

> **Anti-oscillation:** entering charge mode sets a 2 s hold timer that suppresses any immediate flip back to discharge. In primary-aware mode an additional 4 s delay also applies before switching into charge mode if doing so would stop PV that is currently serving the home. At zero/export in `MATCHING`, a charging selected primary may preserve its current output and replace measured non-primary PV floors, but must not grow output simply because more local PV is available; that surplus remains available for charging.

## Anti-oscillation Controls

The manager deliberately slows P1 convergence to prevent hunting. Each control protects against a specific failure mode at the cost of slower response.

| Control | Default | Purpose | Trade-off of Reducing |
|---------|---------|---------|----------------------|
| Charge holdoff | 2 s | Prevents rapid charge↔discharge flipping | More oscillation; devices may ping-pong between modes |
| Charge debounce | 4 s | Delays charge mode when it would zero active PV floor, without growing charging selected-primary output during export | PV floor may drop briefly before recovery; visible power dips |
| Selected-primary export cap | P1 ≤ 0 in `MATCHING` | Preserves current primary output and measured non-primary PV floors while stopping PV-only output growth into grid export | Primary PV may cover import only after a positive P1 reading |
| Battery-export trim threshold | 100 W export | Lets battery-backed export trim home output without waiting for normal debounce | More zero-flow noise can trigger output trims; stale telemetry can over-trim near zero |
| Spike filter threshold | 800 W | Ignores sudden P1 spikes from appliance inrush | False positives cause overcorrection to transient loads |

### Adjustment guidance

**Lower risk:**
- Reducing charge holdoff from 2 s to 1 s — safe if load transients are infrequent.
- Reducing charge debounce from 4 s to 2 s — minor risk of PV floor zeroing.

**Higher risk:**
- Lowering spike filter threshold below typical appliance inrush (kettles, AC compressors).

**No effect:**
- Faster P1 polling — commands are already sent immediately once the setpoint is computed. Delays are intentional, not latency.

### Strong Export From Battery Output

*Battery-backed export* is the situation where the grid meter shows a large export while at least one device reports home output that appears to include battery power. This is distinct from PV-only export: PV-only export should not bypass the normal grid-meter debounce because trimming it can create grid import or drop useful self-consumption.

In selected-primary `MATCHING` and `MATCHING_DISCHARGE`, battery-backed export stronger than 100 W may skip the normal fast-delay and debounce so the manager can trim output promptly. This fast path:

1. Uses device-owned battery-backed-output evidence.
2. Still respects the minimum grid-meter update interval.
3. Never starts or continues the charge/input path for the fast-track cycle.
4. Clamps the cycle to a non-negative home-output target.
5. Sends output-side zero commands when the trim target reaches zero, so an actively discharging device stays in discharge/home-output handling until the next normal calculation.

The fast path is not active in `MATCHING_CHARGE`, `STORE_SOLAR`, `MANUAL`, or `OFF`, and it is not triggered by PV-only home output.

## Near-full Charge Taper

To prevent hardware-level PV curtailment near the configured target SoC, devices with charge taper support apply a software charge cap once SoC enters the taper range. The cap reduces the charge rate in steps as SoC rises:

| SoC band relative to target | Example at target = 80 % | Max charge rate |
|-----------------------------|--------------------------|------------------|
| target − 6 through target − 5 | 74 – 75 %                | 200 W            |
| target − 4 through target − 3 | 76 – 77 %                | 150 W            |
| target − 2 through target − 1 | 78 – 79 %                | 100 W            |
| At or above target            | 80 % or above            | Bypass (full)    |

The taper uses the near-full device state, but is otherwise treated as a normal chargeable state:

- **Charge is allowed** but capped at the taper rate via the effective charge limit.
- **Bypass is not triggered.** Bypass is reserved for the full state. A near-full device receives normal charge commands, not a bypass command.
- **Discharge is unrestricted.** The taper does not block battery discharge.
- **Overflow is routed normally.** Capping charge at the taper rate reduces the surplus that the device will absorb. The remaining surplus is distributed to other sinks (home load → other batteries → grid export) through the normal routing logic — no special overflow handling is needed.
- **"Keep local PV local" is suspended.** When a device is near-full, its charge cap may prevent it from absorbing all its own PV. Excess PV is routed outward rather than being withheld.
- **Drop-back.** If SoC falls more than 6 percentage points below the target (e.g. during active discharge), the taper is removed, the state returns to normal, and the full charge rate is restored automatically.

## Mode Mechanics

### Zero-setpoint charge path

When P1 is exactly zero the grid is balanced and there is neither demand nor surplus. `MATCHING` treats this as a discharge situation and dispatches to the home-output executor, which leaves any active charging untouched.

`STORE_SOLAR`, `MATCHING_CHARGE`, and `MANUAL` instead dispatch to the charge executor with a budget of zero. The charge executor explicitly commands every device to stop charging (`power_charge(0)`). This ensures that a device which was actively charging from a previous cycle receives a stop command rather than silently continuing until the next non-zero P1 reading arrives.

### Strict output stop

`STORE_SOLAR` uses strict output stop. In strict mode the manager stops home output for all non-full devices — including devices that are actively bypassing — before issuing any charge commands in the same cycle. Devices in the full state are exempt: their battery cannot accept more charge, so their current PV pass-through is preserved rather than stopped. In non-strict modes, devices already serving home output are only stopped when they need to change direction.

### MANUAL mode is primary-aware

`MANUAL` mode follows the same primary-aware executor ordering as `MATCHING`. When a primary device is selected, charge and home-output commands prefer the primary before falling through to secondaries.

### Off-grid output at zero

When a device with active off-grid production is stopped (commanded to zero home output), it receives a small negative command rather than zero to prevent off-grid production from being drawn from the grid. This is transparent to the routing rules above.

## Grid Demand During Charge Lag

*Charge lag* is the situation where the grid meter shows household demand but devices still report charging, because telemetry updates lag behind reality. Rather than immediately flipping to discharge, the manager first reduces or eliminates the stale charging.

| Step | Action                                                             |
|------|--------------------------------------------------------------------|
| 1    | Reduce primary charging first                                      |
| 2    | Reduce secondary charging only after primary charging reaches zero |
| 3    | If demand still remains, follow the normal discharge priority order above |

> **Constraint:** charge hysteresis must not prevent local PV from being rerouted to household demand in the same cycle.

**Debounce fast-path:** PV-backed active charging and full-device PV bypass charge-lag cases may skip the normal grid-meter debounce when grid deviation is outside the ±20 W zero guard. The fast path uses current device telemetry and is not limited to the selected primary. Readings inside the guard, and active charging without PV evidence, remain debounced to suppress zero-flow noise and non-PV charging churn.

## Startup and Stability

- Idle devices are started only when the remaining target exceeds the startup power threshold. Exception: empty, at-reserve, and recovering devices are promoted to charging immediately, without waiting for the surplus to reach the threshold.
- Fast grid-meter changes are debounced through normal timing windows, except that primary-device changes trigger immediate routing recomputation. A reading is considered fast (and triggers immediate routing) when it deviates from the recent average or from the most recent reading by more than 3.5× the standard deviation of recent readings, with a minimum threshold of 15 W. For example, if recent readings average 100 W with a standard deviation of 10 W, the threshold is 35 W — a new reading of 140 W triggers immediately, a reading of 130 W does not. If readings are very stable (stddev below 15 W), the 15 W minimum applies, giving a fixed threshold of 52 W.
- The optional P1 spike filter can be enabled through the manager switch. While enabled, upward P1 jumps above the configured threshold are held for the configured duration; if the jump falls back before the duration expires, it is ignored and not added to the recent P1 history.
- Active charge-lag corrections that bypass normal timing still respect the minimum grid-meter update interval.
- Around zero grid flow, prefer a small export or missed charge opportunity over switching a home-serving primary into charge mode and causing grid import.
- Starting with no available devices must not produce user-facing noise beyond expected warning or debug log output.
