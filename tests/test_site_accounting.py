"""Regression tests for whole-site and EV submeter accounting."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "energy_optimizer"
    / "site_accounting.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "energy_optimizer_site_accounting_under_test", _MODULE_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


class SitePowerAccountingTests(unittest.TestCase):
    """Live values must be fresh, aligned and physically consistent."""

    def _split(self, site_w: float, ev_w: float, **overrides):
        values = {
            "site_meter_includes_ev": True,
            "site_age_seconds": 3,
            "ev_age_seconds": 5,
        }
        values.update(overrides)
        return _MODULE.split_site_power_w(site_w, ev_w, **values)

    def test_site_is_split_into_house_and_ev(self) -> None:
        result = self._split(4_600, 3_600)
        self.assertEqual(result.site_w, 4_600)
        self.assertEqual(result.ev_w, 3_600)
        self.assertEqual(result.house_w, 1_000)
        self.assertFalse(result.tolerance_adjusted)

    def test_unconfirmed_topology_is_rejected(self) -> None:
        with self.assertRaises(_MODULE.UnsupportedMeterTopologyError):
            self._split(4_600, 3_600, site_meter_includes_ev=False)

    def test_small_negative_residual_is_absorbed_as_meter_tolerance(self) -> None:
        result = self._split(3_550, 3_600)
        self.assertEqual(result.house_w, 0)
        self.assertTrue(result.tolerance_adjusted)

    def test_negative_residual_beyond_tolerance_is_rejected(self) -> None:
        with self.assertRaises(_MODULE.InvalidMeasurementError):
            self._split(3_499, 3_600)

    def test_stale_reading_is_rejected(self) -> None:
        with self.assertRaises(_MODULE.InvalidMeasurementError):
            self._split(4_600, 3_600, ev_age_seconds=61)

    def test_readings_with_excessive_time_skew_are_rejected(self) -> None:
        with self.assertRaises(_MODULE.InvalidMeasurementError):
            self._split(4_600, 3_600, site_age_seconds=1, ev_age_seconds=32)

    def test_non_finite_and_negative_values_are_rejected(self) -> None:
        for site, ev in ((float("nan"), 0), (0, float("inf")), (-1, 0), (0, -1)):
            with self.subTest(site=site, ev=ev):
                with self.assertRaises(_MODULE.InvalidMeasurementError):
                    self._split(site, ev)


class SiteEnergyAccountingTests(unittest.TestCase):
    """EV energy is removed before applying the household plausibility cap."""

    def _split(self, site_kwh: float, ev_kwh: float, **overrides):
        values = {"site_meter_includes_ev": True}
        values.update(overrides)
        return _MODULE.split_site_energy_kwh(site_kwh, ev_kwh, **values)

    def test_eleven_kw_ev_hour_is_not_rejected_by_house_limit(self) -> None:
        result = self._split(12.0, 11.0)
        self.assertEqual(result.site_kwh, 12.0)
        self.assertEqual(result.ev_kwh, 11.0)
        self.assertEqual(result.house_kwh, 1.0)

    def test_house_limit_is_applied_after_ev_subtraction(self) -> None:
        with self.assertRaises(_MODULE.InvalidMeasurementError):
            self._split(17.5, 11.0)

    def test_manual_twenty_two_kw_ev_hour_remains_accountable(self) -> None:
        result = self._split(23.2, 22.0)
        self.assertAlmostEqual(result.house_kwh, 1.2)

    def test_small_energy_counter_mismatch_is_tolerated(self) -> None:
        result = self._split(0.98, 1.0)
        self.assertEqual(result.house_kwh, 0)
        self.assertTrue(result.tolerance_adjusted)

    def test_large_energy_counter_mismatch_is_rejected(self) -> None:
        with self.assertRaises(_MODULE.InvalidMeasurementError):
            self._split(0.94, 1.0)

    def test_energy_topology_must_be_confirmed(self) -> None:
        with self.assertRaises(_MODULE.UnsupportedMeterTopologyError):
            self._split(12.0, 11.0, site_meter_includes_ev=False)

    def test_implausible_raw_ev_energy_is_rejected(self) -> None:
        with self.assertRaises(_MODULE.InvalidMeasurementError):
            self._split(27.0, 26.0)


if __name__ == "__main__":
    unittest.main()
