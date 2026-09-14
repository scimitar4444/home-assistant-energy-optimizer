# Changelog

## 0.1.0-beta.8 - 2026-09-14

- Added dedicated diagnostic sensors for calculation duration, optimizer-pass
  count and the rolling PV-forecast source.
- Added the three values to the German and English example dashboards as one
  compact diagnostic line.

## 0.1.0-beta.7 - 2026-09-14

- The 48-hour model explicitly starts from the current battery SoC and values
  flexible loads against the same battery/PV dispatch instead of tariff alone.
- When a daily PV forecast is unavailable beyond tomorrow, the seasonal
  historical baseline is corrected with future sun position, cloud-cover and
  rain forecasts.
- Added a vendor-neutral interruptible-load interface with energy budget,
  power limits, earliest start and calendar-compatible completion deadline for
  future EV, climate and appliance adapters. It does not actuate devices.
- Kept the normal calculation path at one dynamic-programming pass; a second
  pass is used only when a flexible appliance is actually scheduled.
- Exposed calculation duration, optimizer-pass count and the source, weather
  coverage and correction factor of each fallback PV day as diagnostics.

## 0.1.0-beta.6 - 2026-09-13

- Grid charging now starts only at a quarter-hour boundary and remains one
  frozen, contiguous block instead of following every rolling forecast update.
- The cumulative battery-charge counter ends a block after its planned stored
  energy has arrived; a stale SoC can no longer repeat the same charge order.
- A full battery, invalid measurements, a reset/stalled counter, disabled
  control or the frozen block end all fail safe to a zero grid setpoint.
- Scale the first, partial quarter-hour's grid-charge allowance to its actual
  remaining duration so the plan cannot exceed the actuator limit.

## 0.1.0-beta.5 - 2026-09-13

- Ongoing charge and discharge blocks are no longer reported as upcoming
  starts.
- Reduced the default quiet grid-charge limit to 15 A / 0.8 kW from 23:00 to
  06:30 on weekdays and 09:30 on weekend mornings.

## 0.1.0-beta.4 - 2026-09-13

- Replaced the 70% command master switch with action-specific live-power and
  firm-price guards; data coverage remains diagnostic.

## 0.1.0-beta.3 - 2026-09-13

- Added an optional, vendor-neutral EV departure planner.
- Added EV observation sensors for deadline, energy, target SoC, recommended
  power, feasibility and data quality.
- Kept EV actuation disabled: hypothetical charging plans do not alter the
  authoritative stationary-battery command.
- Added whole-site/EV-submeter accounting guards so EV charging is not learned
  as household base load.

## 0.1.0-beta.2

- Renamed the integration to **Home Assistant Energy Optimizer**.
- Changed the integration domain to `energy_optimizer`.

## 0.1.0-beta.1

- First public beta.
- Deterministic 48-hour battery and PV dispatch.
- Firm-price boundary for safe tariff fallback handling.
- Robust historical load and seasonal PV fallback.
- English and German setup and sensor names.
- Observation-only default; optional Victron Modbus services.
