"""Regression tests for the single Victron control command."""

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
for module_name in ("const", "control"):
    spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE}.{module_name}", _ROOT / f"{module_name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

build_control_command = sys.modules[
    f"{_PACKAGE}.control"
].build_control_command
bounded_pv_store_grid_setpoint_w = sys.modules[
    f"{_PACKAGE}.control"
].bounded_pv_store_grid_setpoint_w
NOW = datetime.fromisoformat("2026-09-13T00:00:00+02:00")


class ControlCommandTests(unittest.TestCase):
    def _command(self, **overrides):
        values = {
            "now": NOW,
            "action": "RESERVE",
            "model_minimum_soc": 86,
            "current_soc": 90,
            "data_quality_percent": 85,
            "requested_charge_current_a": 50,
            "requested_grid_setpoint_w": 0,
            "current_price_is_known": True,
            "reason": "Test",
        }
        values.update(overrides)
        return build_control_command(**values)

    def test_reserve_uses_unmodified_model_target(self) -> None:
        command = self._command()
        self.assertEqual(command.minimum_soc, 86)
        self.assertEqual(command.action, "RESERVE")

    def test_discharge_applies_release_headroom_exactly_once(self) -> None:
        command = self._command(action="DISCHARGE")
        self.assertEqual(command.minimum_soc, 82)

    def test_pv_surplus_keeps_headroom_for_dc_passthrough(self) -> None:
        command = self._command(
            action="PV_SURPLUS", model_minimum_soc=95, current_soc=90
        )
        self.assertEqual(command.minimum_soc, 86)

    def test_low_coverage_remains_diagnostic(self) -> None:
        for value in (0, 69):
            with self.subTest(value=value):
                command = self._command(
                    data_quality_percent=value,
                    model_minimum_soc=90,
                )
                self.assertTrue(command.quality_ok)
                self.assertEqual(command.action, "RESERVE")
                self.assertEqual(command.minimum_soc, 90)
                self.assertEqual(command.charge_current_a, 50)
                self.assertEqual(command.grid_setpoint_w, 0)

    def test_malformed_coverage_is_rejected(self) -> None:
        for value in (-1, 101, float("nan")):
            with self.subTest(value=value):
                command = self._command(data_quality_percent=value)
                self.assertFalse(command.quality_ok)
                self.assertEqual(command.action, "DEGRADED")
                self.assertEqual(command.minimum_soc, 12)

    def test_invalid_65535_soc_is_rejected(self) -> None:
        command = self._command(current_soc=65535)
        self.assertEqual(command.action, "DEGRADED")
        self.assertEqual(command.minimum_soc, 12)

    def test_command_expires_shortly_after_one_missed_refresh(self) -> None:
        command = self._command()
        self.assertEqual(
            command.valid_until, "2026-09-13T00:07:00+02:00"
        )

    def test_command_never_crosses_quarter_hour_boundary(self) -> None:
        command = self._command(
            now=datetime.fromisoformat("2026-09-13T00:14:56+02:00")
        )
        self.assertEqual(
            command.valid_until, "2026-09-13T00:15:00+02:00"
        )

    def test_non_grid_action_always_uses_zero_grid_setpoint(self) -> None:
        command = self._command(requested_grid_setpoint_w=1200)
        self.assertEqual(command.action, "RESERVE")
        self.assertEqual(command.grid_setpoint_w, 0)

    def test_pv_store_uses_known_price_positive_setpoint(self) -> None:
        command = self._command(
            action="PV_STORE",
            requested_grid_setpoint_w=780,
        )
        self.assertEqual(command.action, "PV_STORE")
        self.assertEqual(command.grid_setpoint_w, 780)

    def test_pv_store_without_known_price_is_degraded(self) -> None:
        command = self._command(
            action="PV_STORE",
            requested_grid_setpoint_w=780,
            current_price_is_known=False,
        )
        self.assertFalse(command.quality_ok)
        self.assertEqual(command.action, "DEGRADED")
        self.assertEqual(command.grid_setpoint_w, 0)

    def test_pv_store_without_safe_setpoint_is_degraded(self) -> None:
        command = self._command(action="PV_STORE")
        self.assertFalse(command.quality_ok)
        self.assertEqual(command.action, "DEGRADED")
        self.assertEqual(command.grid_setpoint_w, 0)

    def test_each_action_enforces_only_its_required_inputs(self) -> None:
        blocked_cases = (
            ("DISCHARGE", {"current_price_is_known": False}),
            ("RESERVE", {"current_price_is_known": False}),
            ("RESERVE", {"action_has_firm_price_basis": False}),
            ("PV_SURPLUS", {"live_power_is_valid": False}),
            (
                "PV_STORE",
                {"requested_grid_setpoint_w": 780, "live_power_is_valid": False},
            ),
            (
                "PV_STORE",
                {
                    "requested_grid_setpoint_w": 780,
                    "action_has_firm_price_basis": False,
                },
            ),
            (
                "GRID_CHARGE",
                {"requested_grid_setpoint_w": 780, "live_power_is_valid": False},
            ),
            (
                "GRID_CHARGE",
                {
                    "requested_grid_setpoint_w": 780,
                    "current_price_is_known": False,
                },
            ),
            (
                "GRID_CHARGE",
                {
                    "requested_grid_setpoint_w": 780,
                    "action_has_firm_price_basis": False,
                },
            ),
            ("GRID_CHARGE", {}),
        )
        for action, overrides in blocked_cases:
            with self.subTest(action=action, overrides=overrides):
                command = self._command(action=action, **overrides)
                self.assertFalse(command.quality_ok)
                self.assertEqual(command.action, "DEGRADED")
                self.assertEqual(command.minimum_soc, 12)
                self.assertEqual(command.grid_setpoint_w, 0)

        allowed_cases = (
            (
                "DISCHARGE",
                {"live_power_is_valid": False, "action_has_firm_price_basis": False},
            ),
            ("RESERVE", {"live_power_is_valid": False}),
            (
                "PV_SURPLUS",
                {
                    "current_price_is_known": False,
                    "action_has_firm_price_basis": False,
                },
            ),
        )
        for action, overrides in allowed_cases:
            with self.subTest(action=action, overrides=overrides):
                command = self._command(action=action, **overrides)
                self.assertTrue(command.quality_ok)
                self.assertEqual(command.action, action)

    def test_grid_charge_setpoint_is_conservatively_limited(self) -> None:
        command = self._command(
            action="GRID_CHARGE",
            requested_grid_setpoint_w=4500,
        )
        self.assertEqual(command.action, "GRID_CHARGE")
        self.assertEqual(command.grid_setpoint_w, 3000)

    def test_negative_grid_setpoint_is_never_accepted(self) -> None:
        command = self._command(
            action="GRID_CHARGE",
            requested_grid_setpoint_w=-1,
        )
        self.assertFalse(command.quality_ok)
        self.assertEqual(command.action, "DEGRADED")
        self.assertEqual(command.grid_setpoint_w, 0)

    def test_pv_store_is_capped_by_real_house_load_not_battery_charge(self) -> None:
        self.assertEqual(
            bounded_pv_store_grid_setpoint_w(
                planned_grid_to_house_w=2392,
                live_house_w=438,
                live_pv_w=457,
            ),
            408,
        )

    def test_pv_store_preserves_unavoidable_live_deficit(self) -> None:
        self.assertEqual(
            bounded_pv_store_grid_setpoint_w(
                planned_grid_to_house_w=100,
                live_house_w=500,
                live_pv_w=200,
            ),
            300,
        )


if __name__ == "__main__":
    unittest.main()
