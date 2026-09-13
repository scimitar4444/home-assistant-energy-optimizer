# Changelog

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
