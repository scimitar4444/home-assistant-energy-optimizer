"""Normalize common dynamic-tariff sensor attributes.

All prices are expected in EUR/kWh. The module is deliberately independent of
Home Assistant so it can be tested without an HA runtime.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Callable, Mapping


def _quarter(value: datetime) -> datetime:
    return value.replace(minute=(value.minute // 15) * 15, second=0, microsecond=0)


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def extract_price_timeline(
    attributes: Mapping[str, Any],
    as_local: Callable[[datetime], datetime],
) -> dict[datetime, float]:
    """Return quarter-hour prices from known Home Assistant attribute layouts."""
    rows: list[Any] = []
    for key in ("data", "today", "tomorrow", "raw_today", "raw_tomorrow"):
        value = attributes.get(key)
        if isinstance(value, list):
            rows.extend(value)

    prices: dict[datetime, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        timestamp = next(
            (
                row[key]
                for key in ("start_time", "startsAt", "start", "datetime")
                if row.get(key) is not None
            ),
            None,
        )
        raw_price = next(
            (
                row[key]
                for key in ("price_per_kwh", "total", "value", "price")
                if row.get(key) is not None
            ),
            None,
        )
        try:
            moment = _quarter(as_local(_datetime(timestamp)))
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if isfinite(price) and -0.5 < price < 2:
            prices[moment] = price
    return prices
