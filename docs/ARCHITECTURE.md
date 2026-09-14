# Architecture

```text
HA entities and recorder statistics
              │
              ▼
   adapters + robust load/PV forecast
              │
              ▼
 deterministic quarter-hour optimizer
              │
              ▼
 localized sensors + expiring command
              │
              ▼
 optional user-owned actuator automation
```

The dynamic program minimizes grid energy cost and optional battery wear while respecting SoC, efficiency and power limits. It has no learned weights, neural network or external inference endpoint.

The measured SoC is the initial energy state, not merely a display value. Each
quarter-hour then balances PV to load, PV to battery, battery to load, grid to
load and optional confirmed grid charging. Historical load and weather data are
cached; the normal path performs one dynamic-programming pass. Only a flexible
load supplied by an adapter requires a second pass over the combined load.

Where a trusted daily PV total exists, weather redistributes it between the
quarter-hours. Beyond the available daily forecast, the integration takes a
seasonal historical plant baseline and corrects its total with future sun
position, cloud cover and rain. Missing weather is blended back
toward history instead of being interpreted as clear sky.

## Generic flexible-load interface

Future adapters for EVs, climate systems and appliances can describe an energy
budget, minimum and maximum power, earliest start and completion deadline. A
calendar occurrence can supply or move that deadline. The interface only
returns eligible planning slots; it never authorizes a device by itself.
Measurement validity, confirmed prices and an adapter-specific safety layer
remain mandatory before actuation.

## Optional EV observation adapter

The optional EV subsystem is deliberately outside the authoritative dispatch
path in the current beta. A pure departure planner produces recommendations,
and a guarded submeter adapter removes measured EV energy from learned
household history. Actual EV demand remains in gross whole-site live load. An
unexecuted recommendation is never inserted into battery optimization and no
charger command is emitted.

## Firm-price boundary

Tariff providers commonly publish only a finite known horizon. The integration may fill the rest of its 48-hour display with seven-day historical averages, but active storage actions are constrained at the boundary to the state of a passive reference plan. Therefore additional energy cannot be bought or diverted merely to carry it into an estimated-price period. Intraday discharge → cheap recharge → later discharge remains possible when all relevant prices are known.

## Metered grid-charge blocks

Grid charging runs as one small state machine instead of a sequence of
independent replanning decisions. A block can start only for a slot aligned to
an exact quarter-hour boundary. At that point the integration freezes the
contiguous confirmed-price charging slots, the cumulative DC battery-charge
counter baseline, and a DC energy target derived from the planned AC charging
energy multiplied by the configured charging efficiency. Later load or PV
forecast changes do not alter an active block.

The block stops when the measured target energy is reached, its frozen end is
reached, SoC reaches 100%, or control is disabled. An invalid, reset or stalled
cumulative charge counter aborts the block fail-safe. Vendor-specific BMS
charge-current signals are deliberately outside the generic integration and
must be enforced by the downstream actuator when available.

## Failure model

- invalid SoC values, including the common `65535` sentinel, are rejected;
- data coverage is diagnostic and never acts as a percentage master switch;
- each action requires its concrete live and firm-price inputs, otherwise it
  yields `DEGRADED` rather than retaining an old command;
- discharge requires a firm current price, PV surplus requires current live
  power, and deliberate PV storage or grid charging requires both plus a
  later higher-price discharge inside the contiguous firm-price horizon;
- reserve requires that same confirmed price basis, except that retaining
  energy at a non-positive current price is valid on its own;
- positive grid setpoints require a firm current price and bounded live measurements;
- ordinary commands expire no later than the current quarter-hour boundary;
- while a grid-charge block is active, its command is refreshed with a rolling
  validity of at most seven minutes and never beyond the frozen block end;
- planning and writing are separate, so installing the integration alone cannot switch the inverter.
