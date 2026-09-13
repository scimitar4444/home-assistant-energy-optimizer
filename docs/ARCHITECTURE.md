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

## Optional EV observation adapter

The optional EV subsystem is deliberately outside the authoritative dispatch
path in the current beta. A pure departure planner produces recommendations,
and a guarded submeter adapter removes measured EV energy from learned
household history. Actual EV demand remains in gross whole-site live load. An
unexecuted recommendation is never inserted into battery optimization and no
charger command is emitted.

## Firm-price boundary

Tariff providers commonly publish only a finite known horizon. The integration may fill the rest of its 48-hour display with seven-day historical averages, but active storage actions are constrained at the boundary to the state of a passive reference plan. Therefore additional energy cannot be bought or diverted merely to carry it into an estimated-price period. Intraday discharge → cheap recharge → later discharge remains possible when all relevant prices are known.

## Failure model

- invalid SoC values, including the common `65535` sentinel, are rejected;
- low data coverage yields `DEGRADED` rather than retaining an old command;
- positive grid setpoints require a firm current price and bounded live measurements;
- every command expires no later than the current quarter-hour boundary;
- planning and writing are separate, so installing the integration alone cannot switch the inverter.
