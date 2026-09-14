# Home Assistant Energy Optimizer for Home Assistant

[Deutsch](README.de.md) · English

Home Assistant Energy Optimizer is a local, deterministic 48-hour energy optimizer for Home Assistant. It combines dynamic electricity prices, PV forecasts, household history and battery state of charge to minimize expected grid cost while preserving energy for the most valuable intervals.

It does **not** use an LLM, cloud AI or online inference. All calculations run locally in Home Assistant and remain reproducible.

> **Beta and safety notice**  
> The integration starts as an observation/simulation tool. Never connect its commands to an inverter without checking signs, units, limits and fail-safe behavior for your installation. Grid charging is disabled by default.

## What it does

- plans 192 quarter-hour intervals (48 hours);
- uses firm tariff data first and a seven-day historical fallback only as a trend;
- prevents energy bought or diverted now from being justified solely by uncertain tariff slots;
- models PV-to-load, PV-to-battery, battery-to-load and grid flows separately;
- includes configurable battery capacity, minimum SoC, efficiency, charge power and optional battery wear;
- learns a robust non-shiftable load profile from Home Assistant long-term statistics;
- starts every dispatch from the measured battery SoC and models its usable
  energy, efficiency and power limits across the full horizon;
- uses remaining-today and tomorrow PV forecasts; missing later daily totals
  use a seasonal history baseline corrected by forecast sun position, cloud,
  and rain;
- provides a non-actuating, vendor-neutral interface for interruptible EV,
  climate and appliance loads with power limits and calendar deadlines;
- optionally plans vendor-neutral EV charging against calendar deadlines;
- exposes localized English and German sensors and diagnostics;
- optionally exposes verified Victron Modbus TCP write services for advanced users.

## Requirements

- Home Assistant 2026.8 or newer (the beta is tested against the current 2026 generation);
- a battery SoC sensor in percent;
- a dynamic-price sensor whose attributes contain prices in **EUR/kWh**;
- remaining PV forecast sensors for today and tomorrow in kWh;
- live PV, grid-import and house-load power sensors in W;
- five monotonically increasing energy counters in kWh: grid import, grid export, PV yield, battery charge and battery discharge.

Supported tariff attribute layouts include:

- `data`: `start_time` + `price_per_kwh`;
- Tibber-style `today` / `tomorrow`: `startsAt` + `total`;
- `raw_today` / `raw_tomorrow`: `start` + `value`.

## Installation

### HACS custom repository

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/scimitar4444/home-assistant-energy-optimizer` as category **Integration**.
3. Install **Home Assistant Energy Optimizer** and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration** and select **Home Assistant Energy Optimizer**.

### Manual installation

Copy `custom_components/energy_optimizer` into your Home Assistant `config/custom_components/` directory and restart Home Assistant.

## Configuration

The UI guides you through five required groups and one optional EV step:

1. battery and tariff policy;
2. prices, SoC and PV forecasts;
3. live power sensors;
4. long-term energy counters;
5. optional weather and advanced Victron settings;
6. optional, observation-only EV planning.

Start with grid charging disabled and leave both the control helper and Victron host empty. Observe at least several days, compare forecast and measured grid import, then decide whether to add an automation that consumes the short-lived command attributes.

The main status sensor uses these stable states:

| State | Meaning |
|---|---|
| `DISCHARGE` | battery use is economical now |
| `RESERVE` | retain energy for a more valuable known interval |
| `PV_SURPLUS` | PV covers the house and surplus charges the battery |
| `PV_STORE` | known cheap grid supplies the house so PV can be stored |
| `GRID_CHARGE` | known cheap grid energy is charged for a known expensive interval |
| `DEGRADED` | an action-specific input is missing or invalid; safe fallback command |

## Design and safety

The optimizer deliberately separates planning from actuation. Ordinary recommendations expire at the next quarter-hour boundary; an active metered grid-charge block instead uses a rolling short expiry that never exceeds its frozen end. Estimated future prices may influence the trend, but cannot on their own authorize grid charging or PV diversion. The data-coverage percentage is diagnostic; command authorization uses the current SoC, live measurements and confirmed prices required by each action. See [Architecture](docs/ARCHITECTURE.md) and the example dashboards in [`examples/`](examples/).

Grid charging runs as one metered block made from contiguous, confirmed quarter-hours. Once started, forecast changes cannot resize or interrupt it. The cumulative battery-charge counter stops it when the planned stored energy has arrived; until the original block end the order remains locked so a delayed SoC cannot buy the same energy twice. A downstream hardware adapter must additionally enforce any vendor-specific BMS interlocks.

The default quiet grid-charge profile limits planned battery charging to **0.8 kW** and the actuator's charge-current command to **15 A** from 23:00 until 06:30 on weekdays and until 09:30 on Saturday and Sunday mornings. Calendar-based one-off exceptions are planned but are not interpreted yet.

Victron services are advanced building blocks, not an automatic installer. Register defaults match one tested GX setup but may differ on yours. A positive grid setpoint is never emitted for an unknown current price, and `0 W` releases it.

## Optional EV planning

The EV module is disabled by default and is observation-only in this beta. It
uses vehicle SoC and a calendar departure to calculate a charging
recommendation, but it does not control a charger. An unexecuted EV schedule
therefore cannot change the battery command. Actual measured EV demand remains
part of the live whole-site load.

The metering topology must be correct: the selected gross whole-site load
sensors must include **both household and charger consumption**; they are not
raw grid-import sensors. Historical site energy is reconstructed from the five
flow counters before a separate charger submeter removes EV energy from the
learned household base load.

Use a dedicated EV departure calendar. Every timed event with
`distance_km: 80` in its description is a trip; an optional `reserve_km: 40`
overrides the configured reserve. All-day events are ignored. The nearest trip
enters planning inside the 48-hour horizon. The recommendation defaults to
3.6 kW and may suggest no more than 11 kW automatically when normal power
cannot meet the deadline. A 22 kW emergency charge remains outside the beta.
The required sensor units and calendar format are documented in [Optional EV
planning](docs/EV_PLANNING.md).

## Development

```bash
python -m unittest discover -s tests -v
python -m compileall -q custom_components tests
```

GitHub Actions also run Home Assistant `hassfest` and HACS validation.

## License

MIT. See [LICENSE](LICENSE).
