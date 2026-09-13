"""Tests for dynamic-tariff attribute adapters."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest


_PATH = Path(__file__).parents[1] / "custom_components" / "energy_optimizer" / "price_adapter.py"
_SPEC = importlib.util.spec_from_file_location("price_adapter_under_test", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class PriceAdapterTests(unittest.TestCase):
    def test_native_data_layout(self) -> None:
        result = _MODULE.extract_price_timeline(
            {"data": [{"start_time": "2026-09-13T10:07:00+00:00", "price_per_kwh": 0.21}]},
            lambda value: value,
        )
        self.assertEqual(result[datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)], 0.21)

    def test_tibber_today_and_tomorrow_layout(self) -> None:
        result = _MODULE.extract_price_timeline(
            {
                "today": [{"startsAt": "2026-09-13T12:00:00Z", "total": 0.18}],
                "tomorrow": [{"startsAt": "2026-09-14T00:00:00Z", "total": 0.25}],
            },
            lambda value: value,
        )
        self.assertEqual(len(result), 2)
        self.assertIn(datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc), result)

    def test_invalid_and_cent_values_are_rejected(self) -> None:
        result = _MODULE.extract_price_timeline(
            {"raw_today": [{"start": "bad", "value": 0.2}, {"start": "2026-09-13T10:00:00Z", "value": 21.0}]},
            lambda value: value,
        )
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
