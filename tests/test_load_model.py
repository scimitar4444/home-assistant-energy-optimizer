"""Regression tests for the deterministic load-model helpers."""

from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import types
import unittest


_PACKAGE = "arbolito_energy_optimizer"
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


if __name__ == "__main__":
    unittest.main()
