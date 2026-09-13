"""Tests for the metered grid-charge block state machine."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "grid_charge_session_under_test",
    Path(__file__).parents[1]
    / "custom_components"
    / "energy_optimizer"
    / "grid_charge_session.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_grid_charge_session = _MODULE.build_grid_charge_session
evaluate_grid_charge_session = _MODULE.evaluate_grid_charge_session
grid_charge_start_allowed = _MODULE.grid_charge_start_allowed
terminal_session_may_clear = _MODULE.terminal_session_may_clear


START = datetime(2030, 1, 2, 14, 0, tzinfo=timezone.utc)


def _plan() -> list[dict[str, object]]:
    return [
        {
            "start": (START + timedelta(minutes=15 * index)).isoformat(),
            "grid_to_battery_kwh": flow,
            "grid_import_kwh": flow + 0.025,
            "price_is_forecast": forecast,
        }
        for index, flow, forecast in (
            (0, 0.20, False),
            (1, 0.18, False),
            (2, 0.15, False),
            (3, 0.14, True),
        )
    ]


def _session():
    session = build_grid_charge_session(
        now=START + timedelta(seconds=45),
        plan=_plan(),
        baseline_charge_counter_kwh=100.0,
        first_grid_setpoint_w=900,
        target_minimum_soc=70,
        reason="confirmed cheap block",
        charge_efficiency=0.94,
        maximum_grid_setpoint_w=3000,
    )
    assert session is not None
    return session


class GridChargeSessionTests(unittest.TestCase):
    def test_start_is_limited_to_boundary_grace(self) -> None:
        self.assertTrue(grid_charge_start_allowed(START + timedelta(seconds=120), START))
        self.assertFalse(grid_charge_start_allowed(START + timedelta(seconds=121), START))
        self.assertFalse(
            grid_charge_start_allowed(
                START + timedelta(minutes=1),
                START + timedelta(minutes=1),
            )
        )
        self.assertIsNone(
            build_grid_charge_session(
                now=START + timedelta(minutes=8),
                plan=_plan(),
                baseline_charge_counter_kwh=100,
                first_grid_setpoint_w=900,
                target_minimum_soc=70,
                reason="late",
                charge_efficiency=0.94,
                maximum_grid_setpoint_w=3000,
            )
        )

    def test_contiguous_firm_block_is_frozen(self) -> None:
        session = _session()
        self.assertEqual(len(session.slices), 3)
        self.assertEqual(session.end, START + timedelta(minutes=45))
        self.assertAlmostEqual(session.target_stored_kwh, (0.20 + 0.18 + 0.15) * 0.94)
        self.assertEqual(session.slices[0].grid_setpoint_w, 900)
        self.assertEqual(session.slices[1].grid_setpoint_w, 820)

    def test_measurement_progress_keeps_block_active(self) -> None:
        session = evaluate_grid_charge_session(
            _session(),
            now=START + timedelta(minutes=6),
            charge_counter_kwh=100.20,
            battery_soc=55,
            bms_max_charge_current_a=25,
            require_bms_current=True,
        )
        self.assertTrue(session.active)
        self.assertAlmostEqual(session.delivered_stored_kwh, 0.20)

    def test_energy_target_stops_and_locks_until_real_gap(self) -> None:
        session = _session()
        session = evaluate_grid_charge_session(
            session,
            now=START + timedelta(minutes=20),
            charge_counter_kwh=100 + session.target_stored_kwh,
            battery_soc=55,
            bms_max_charge_current_a=25,
            require_bms_current=True,
        )
        self.assertEqual(session.state, "SATISFIED")
        self.assertEqual(session.stop_reason, "energy_target_reached")
        self.assertFalse(
            terminal_session_may_clear(
                session,
                proposed_grid_charge_now=False,
                now=START + timedelta(minutes=30),
            )
        )
        self.assertFalse(
            terminal_session_may_clear(
                session,
                proposed_grid_charge_now=True,
                now=START + timedelta(minutes=45),
            )
        )
        self.assertTrue(
            terminal_session_may_clear(
                session,
                proposed_grid_charge_now=False,
                now=START + timedelta(minutes=45),
            )
        )

    def test_full_battery_and_zero_bms_limit_stop(self) -> None:
        full = evaluate_grid_charge_session(
            _session(),
            now=START + timedelta(minutes=5),
            charge_counter_kwh=100,
            battery_soc=100,
            bms_max_charge_current_a=25,
            require_bms_current=True,
        )
        blocked = evaluate_grid_charge_session(
            _session(),
            now=START + timedelta(minutes=5),
            charge_counter_kwh=100,
            battery_soc=60,
            bms_max_charge_current_a=0,
            require_bms_current=True,
        )
        self.assertEqual((full.state, full.stop_reason), ("SATISFIED", "battery_full"))
        self.assertEqual(
            (blocked.state, blocked.stop_reason),
            ("SATISFIED", "bms_charge_blocked"),
        )

    def test_invalid_or_reset_counter_aborts(self) -> None:
        invalid = evaluate_grid_charge_session(
            _session(),
            now=START + timedelta(minutes=5),
            charge_counter_kwh=None,
            battery_soc=60,
            bms_max_charge_current_a=25,
            require_bms_current=True,
        )
        reset_seed = replace(_session(), last_charge_counter_kwh=100.2)
        reset = evaluate_grid_charge_session(
            reset_seed,
            now=START + timedelta(minutes=5),
            charge_counter_kwh=99,
            battery_soc=60,
            bms_max_charge_current_a=25,
            require_bms_current=True,
        )
        self.assertEqual(invalid.stop_reason, "charge_counter_invalid")
        self.assertEqual(reset.stop_reason, "charge_counter_reset")

    def test_missing_bms_value_fails_closed_when_required(self) -> None:
        session = evaluate_grid_charge_session(
            _session(),
            now=START + timedelta(minutes=5),
            charge_counter_kwh=100,
            battery_soc=60,
            bms_max_charge_current_a=None,
            require_bms_current=True,
        )
        self.assertEqual((session.state, session.stop_reason), ("ABORTED", "bms_current_invalid"))

        sentinel = evaluate_grid_charge_session(
            _session(),
            now=START + timedelta(minutes=5),
            charge_counter_kwh=100,
            battery_soc=60,
            bms_max_charge_current_a=65535,
            require_bms_current=True,
        )
        self.assertEqual(sentinel.stop_reason, "bms_current_invalid")

    def test_disabled_control_aborts_the_active_block(self) -> None:
        session = evaluate_grid_charge_session(
            _session(),
            now=START + timedelta(minutes=5),
            charge_counter_kwh=100,
            battery_soc=60,
            bms_max_charge_current_a=25,
            require_bms_current=True,
            control_enabled=False,
        )
        self.assertEqual((session.state, session.stop_reason), ("ABORTED", "control_disabled"))

    def test_block_end_and_stalled_counter_abort(self) -> None:
        ended = evaluate_grid_charge_session(
            _session(),
            now=START + timedelta(minutes=45),
            charge_counter_kwh=100.1,
            battery_soc=60,
            bms_max_charge_current_a=25,
            require_bms_current=True,
        )
        stalled = evaluate_grid_charge_session(
            _session(),
            now=START + timedelta(minutes=7),
            charge_counter_kwh=100,
            battery_soc=60,
            bms_max_charge_current_a=25,
            require_bms_current=True,
            no_progress_seconds=360,
        )
        self.assertEqual(ended.stop_reason, "block_ended")
        self.assertEqual(stalled.stop_reason, "charge_counter_stalled")
        self.assertFalse(
            terminal_session_may_clear(
                ended,
                proposed_grid_charge_now=True,
                now=START + timedelta(minutes=45),
            )
        )


if __name__ == "__main__":
    unittest.main()
