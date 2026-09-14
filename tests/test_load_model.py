"""Regression tests for the deterministic load-model helpers."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

_PACKAGE = "energy_optimizer"
_ROOT = Path(__file__).parents[1] / "custom_components" / _PACKAGE
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    sys.modules[_PACKAGE] = package
_SPEC = importlib.util.spec_from_file_location(
    f"{_PACKAGE}.load_model", _ROOT / "load_model.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


class LoadModelTests(unittest.TestCase):
    """The robust helpers must stay conservative and deterministic."""

    def test_shiftable_jobs_are_removed_from_base_load(self) -> None:
        value = _MODULE.non_shiftable_load(
            2.0,
            {
                "waschmaschine": 0.7,
                "spuelmaschine": 0.4,
                "kuehlschrank": 0.2,
            },
        )
        self.assertAlmostEqual(value, 0.9)

    def test_level_calibration_only_reduces_high_forecast(self) -> None:
        factor, ceiling = _MODULE.forecast_level_calibration(17.0, 5.65)
        self.assertAlmostEqual(ceiling, 12.995)
        self.assertLess(factor, 1.0)
        self.assertEqual(_MODULE.forecast_level_calibration(10.0, 5.65)[0], 1.0)

    def test_pv_brightness_is_zero_at_night(self) -> None:
        moment = datetime.fromisoformat("2026-09-09T00:00:00+02:00")
        weather = _MODULE.WeatherSample(cloud_percent=0)
        self.assertEqual(_MODULE.solar_brightness(moment, weather, 50.1, 8.7), 0.0)

    def test_future_clear_weather_raises_historical_pv_baseline(self) -> None:
        adjusted, factor, coverage = _MODULE.weather_adjusted_daily_pv(
            10.0, [(1.0, 1.0)] * 8
        )
        self.assertGreater(adjusted, 10.0)
        self.assertAlmostEqual(adjusted, 10.0 * factor)
        self.assertEqual(coverage, 1.0)

    def test_future_overcast_weather_reduces_historical_pv_baseline(self) -> None:
        adjusted, factor, coverage = _MODULE.weather_adjusted_daily_pv(
            10.0, [(1.0, 0.2)] * 8
        )
        self.assertLess(adjusted, 10.0)
        self.assertLess(factor, 1.0)
        self.assertEqual(coverage, 1.0)

    def test_partial_weather_coverage_blends_toward_history(self) -> None:
        full, full_factor, _ = _MODULE.weather_adjusted_daily_pv(
            10.0, [(1.0, 1.0)] * 8
        )
        partial, partial_factor, coverage = _MODULE.weather_adjusted_daily_pv(
            10.0, [(1.0, 1.0)] * 4 + [(1.0, None)] * 4
        )
        self.assertGreater(partial, 10.0)
        self.assertLess(partial, full)
        self.assertLess(partial_factor, full_factor)
        self.assertEqual(coverage, 0.5)

    def test_rain_reduces_brightness_when_lux_is_unavailable(self) -> None:
        moment = datetime.fromisoformat("2026-06-21T12:00:00+02:00")
        dry = _MODULE.WeatherSample(cloud_percent=20, rain_fraction=0.0)
        wet = _MODULE.WeatherSample(cloud_percent=20, rain_fraction=1.0)

        self.assertLess(
            _MODULE.solar_brightness(moment, wet, 50.1, 8.7),
            _MODULE.solar_brightness(moment, dry, 50.1, 8.7),
        )


if __name__ == "__main__":
    unittest.main()
