# Optional EV planning

The optional EV module is a local, vendor-neutral departure planner. It is
disabled by default. The current beta is strictly **observation-only**: it
calculates recommendations but has no charger writer and does not insert an
unexecuted EV schedule into the authoritative battery plan.

## Current beta boundary

The module currently consumes:

- gross whole-site load power, including a charging EV, in W;
- charger power in W and cumulative charger energy in kWh;
- a connected binary sensor;
- vehicle state of charge from 0 to 100 percent;
- a dedicated departure calendar.

Actual charging remains visible in the common site measurement. The existing
central optimizer may therefore supply an EV that is really charging from PV,
the stationary battery and/or the grid, subject to its normal household reserve
and hardware limits. The recommendation itself cannot change that dispatch.

When EV planning is disabled, the established household and battery path is
unchanged and no EV sensors are created.

## Metering model

The selected whole-site load sensors must measure **gross consumption after PV
and battery flows**, not grid import, and must include both household and EV.
The charger meter is only an EV submeter:

```text
household power = gross site-load power - charger power
household energy = reconstructed site energy - charger energy
```

This keeps actual EV demand in live site control while removing EV sessions
from the learned household base load. Values are rejected when units,
freshness, signs or the two measurement paths are inconsistent; they are never
silently forced to zero. High-power EV hours are validated before subtraction,
then the normal household plausibility limit is applied to the remainder.

Required units are W for live power, kWh for the monotonically increasing
charger-energy counter, and percent for vehicle SoC. The beta does not convert
other units automatically.

## Departure calendar

Use a dedicated calendar. The start of each timed event is the departure
deadline. All-day events are ignored. Put the required trip distance in the
event description, one key per line:

```text
distance_km: 80
reserve_km: 40
```

`distance_km` is required and must be greater than 0 and at most 2000.
`reserve_km` is optional, must be between 0 and 1000, and otherwise uses the
configured default. Both `:` and `=` are accepted. The title is for the user
only and is not interpreted.

The beta reads up to 14 days ahead. The earliest timed event is authoritative;
if its distance is missing or invalid, the module asks for corrected data
instead of silently skipping to a later trip. It plans that departure inside
its 48-hour optimization horizon. A departure between 48 hours and 14 days is
reported as waiting for the planning horizon; beyond that it reports no
upcoming trip. It does not yet
combine back-to-back trips; there must be a fresh SoC and a realistic reconnect
opportunity before the following departure.

Recurring-event changes are performed in the calendar provider. If that
provider supports recurrence exceptions, users can move or skip one occurrence
or change the future series; the optimizer only consumes the resulting
individual events.

## Timing and charging power

- normal charging defaults to 3.6 kW and is configurable;
- automatic boost is configurable but hard-capped at 11 kW;
- 22 kW is never requested automatically and remains outside this beta.

The planner first protects aggregate site-import headroom and then chooses
interruptible charging intervals. The configured site limit is an observation
estimate, not a replacement for charger, vehicle, phase or electrical
protection.

An 11 kW recommendation is allowed only when 3.6 kW cannot meet the deadline,
or when a confirmed cheap interval replaces materially dearer **confirmed**
energy. The beta defines “materially” as at least 5 ct/kWh. Confirmed free or
negative prices are preserved. Positive-price
charging is deferred when still-unknown future intervals have enough capacity;
the plan is recalculated after the tariff provider publishes those prices.
Unknown prices cannot justify an economic boost.

Intervals are clipped at the exact departure time. A seven-minute remainder is
never counted as a full quarter hour.

## PV, battery and grid

Residual PV is a timing preference after predicted household demand. It is not
a promise that the EV will receive that source. In the current beta:

- only measured, real EV load participates in central site control;
- a hypothetical EV schedule is not added to the battery optimizer;
- there is no second battery controller and no negative grid-setpoint trick;
- stale or inconsistent EV data blocks a current recommendation without
  stopping the established household optimizer.

The status is provisional while the vehicle is disconnected because future
availability is unknown. A future active charger adapter would need atomic
start/stop, short command expiry, phase-aware limits and manual-override
handling before the recommended EV load could safely be included exactly once
in the central site plan.
