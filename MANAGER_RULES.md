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
| `MATCHING`           | Discharge                                  | Charge                             |
| `MATCHING_DISCHARGE` | Discharge                                  | No action                          |
| `MATCHING_CHARGE`    | PV pass-through only; no battery discharge | Charge                             |
| `STORE_SOLAR`        | PV pass-through only; no battery discharge | Charge                             |
| `MANUAL`             | Use configured manual power target         | Use configured manual power target |

## Discharge Routing (Grid Demand)

Apply sources in priority order, stopping when demand is covered:

| Priority | Source                                       | Condition                                                                      |
|----------|----------------------------------------------|--------------------------------------------------------------------------------|
| 1        | PV / off-grid power already serving the home | Always                                                                         |
| 2        | Selected primary device PV                   | Primary online and not empty/at-reserve/recovering                             |
| 3        | Secondary device PV                          | —                                                                              |
| 4        | Selected primary battery                     | Primary online, above all SoC/reserve/recovery floors, within discharge limit  |
| 5        | Secondary device battery                     | Primary unavailable or at discharge limit                                      |

**Primary unavailability:** if the primary is offline, empty, at reserve, recovering, or at its discharge limit, remaining demand falls through to secondary devices.

> **Edge case — SF800 Pro at zero:** an SF800 Pro with a full battery receives a bypass command instead of a zero-watt discharge command when no output is needed from it.

## Charge Routing (Grid Surplus)

1. **Primary first:** charge the selected primary if it is online and can accept charge.
2. **Secondary fallback:** if the primary cannot use the surplus, route to secondary devices.
3. **Home-serving PV is not reassigned:** if the primary is actively passing PV to the home and has no local surplus beyond that home output, that PV must not be redirected to primary charging — *unless* the device is empty.
4. **Empty device exception:** empty devices in primary-aware automatic modes are charge-first. Their current PV is redirected to their own battery before being preserved for home load. Once the device reaches at-reserve state it returns to normal household-load-first routing.
5. **Keep local PV local:** prefer charging a secondary device with its own PV over causing the primary to discharge or increasing grid consumption. A secondary that is both serving the home and charging should keep its PV locally when the primary can cover remaining demand.
6. **Primary with no local solar defers to secondaries:** if the primary is charging but has no solar of its own to contribute (it would draw from the grid to charge), and a secondary has its own solar available, the charge allocation shifts to that secondary instead.
7. **Full primary hands off to secondaries:** when the primary battery is full and has entered bypass, idle secondary devices that have solar available are promoted to charging so that surplus is not wasted.

> **Anti-oscillation:** entering charge mode sets a hold timer that suppresses any immediate flip back to discharge. The timer is 2 s if the previous charge session ended more than 5 minutes ago, or 60 s otherwise. In primary-aware mode an additional short delay also applies before switching into charge mode if doing so would stop PV that is currently serving the home.

## Near-full Charge Taper

To prevent hardware-level PV curtailment near the configured target, the SF800 Pro applies a software charge cap once SoC enters the taper range below `socSet`. The cap reduces the charge rate in steps as SoC rises:

| SoC band relative to `socSet` | Example at `socSet = 80 %` | Max charge rate  |
|--------------------------------|----------------------------|------------------|
| `socSet - 6` through `socSet - 5` | 74 – 75 %                  | 200 W            |
| `socSet - 4` through `socSet - 3` | 76 – 77 %                  | 150 W            |
| `socSet - 2` through `socSet - 1` | 78 – 79 %                  | 100 W            |
| At or above `socSet`             | 80 % or above              | Bypass (SOCFULL) |

The taper uses the `SOCNEARLYFULL` device state (rather than `ACTIVE`/`INACTIVE`), but is otherwise treated as a normal chargeable state:

- **Charge is allowed** but capped at the taper rate via `effective_charge_limit`.
- **Bypass is not triggered.** Bypass (pass-through via `power_bypass`) is reserved for `SOCFULL`. A near-full device receives normal charge commands, not a bypass command.
- **Sensors expose near-full distinctly.** The device state sensor reports `SOCNEARLYFULL`, and the SoC Limit sensor reports `Nearly full` rather than `Full`.
- **Discharge is unrestricted.** The taper does not block battery discharge.
- **Overflow is routed normally.** Capping charge at the taper rate reduces the surplus that the device will absorb. The remaining surplus is distributed to other sinks (home load → other batteries → grid export) through the normal routing logic — no special overflow handling is needed.
- **"Keep local PV local" is suspended.** When a device is near-full, its charge cap may prevent it from absorbing all its own PV. Excess PV is routed outward rather than being withheld.
- **Drop-back.** If SoC falls more than 6 percentage points below `socSet` (e.g. during active discharge), `taper_charge_limit` returns `None`, the state returns to `INACTIVE` or `ACTIVE`, and the full charge rate is restored automatically.

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
- Fast grid-meter changes are debounced through normal timing windows, except that primary-device changes trigger immediate routing recomputation. A reading is considered fast (and triggers immediate routing) when it deviates from the recent average by more than 3.5× the standard deviation of recent readings, with a minimum threshold of 15 W. For example, if recent readings average 100 W with a standard deviation of 10 W, the threshold is 35 W — a new reading of 140 W triggers immediately, a reading of 130 W does not. If readings are very stable (stddev below 15 W), the 15 W minimum applies, giving a fixed threshold of 52 W.
- Active charge-lag corrections that bypass normal timing still respect the minimum grid-meter update interval.
- Around zero grid flow, prefer a small export or missed charge opportunity over switching a home-serving primary into charge mode and causing grid import.
- Starting with no available devices must not produce user-facing noise beyond expected warning or debug log output.
