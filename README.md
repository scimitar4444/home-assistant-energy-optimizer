# Arbolito Energy Optimizer for Home Assistant

[Deutsch](README.de.md) · English

Arbolito is a local, deterministic 48-hour energy optimizer for Home Assistant. It combines dynamic electricity prices, PV forecasts, household history and battery state of charge to minimize expected grid cost while preserving energy for the most valuable intervals.

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
- uses remaining-today and tomorrow PV forecasts, with historical seasonal fallback;
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
3. Install **Arbolito Energy Optimizer** and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration** and select **Arbolito Energy Optimizer**.

### Manual installation

Copy `custom_components/arbolito_energy_optimizer` into your Home Assistant `config/custom_components/` directory and restart Home Assistant.

## Configuration

The UI guides you through five groups:

1. battery and tariff policy;
2. prices, SoC and PV forecasts;
3. live power sensors;
4. long-term energy counters;
5. optional weather and advanced Victron settings.

Start with grid charging disabled and leave both the control helper and Victron host empty. Observe at least several days, compare forecast and measured grid import, then decide whether to add an automation that consumes the short-lived command attributes.

The main status sensor uses these stable states:

| State | Meaning |
|---|---|
| `DISCHARGE` | battery use is economical now |
| `RESERVE` | retain energy for a more valuable known interval |
| `PV_SURPLUS` | PV covers the house and surplus charges the battery |
| `PV_STORE` | known cheap grid supplies the house so PV can be stored |
| `GRID_CHARGE` | known cheap grid energy is charged for a known expensive interval |
| `DEGRADED` | input quality is insufficient; safe fallback command |

## Design and safety

The optimizer deliberately separates planning from actuation. Recommendations expire at the next quarter-hour boundary and estimated future prices may influence the trend, but cannot on their own authorize grid charging or PV diversion. See [Architecture](docs/ARCHITECTURE.md) and the example dashboards in [`examples/`](examples/).

Victron services are advanced building blocks, not an automatic installer. Register defaults match one tested GX setup but may differ on yours. A positive grid setpoint is never emitted for an unknown current price, and `0 W` releases it.

## Development

```bash
python -m unittest discover -s tests -v
python -m compileall -q custom_components tests
```

GitHub Actions also run Home Assistant `hassfest` and HACS validation.

## License

MIT. See [LICENSE](LICENSE).
