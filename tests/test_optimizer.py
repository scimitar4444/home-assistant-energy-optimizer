"""Focused regression tests for the Energy Optimizer."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


_OPTIMIZER_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "energy_optimizer"
    / "optimizer.py"
)
_SPEC = importlib.util.spec_from_file_location("energy_optimizer_under_test", _OPTIMIZER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
pv_headroom_required_percent = _MODULE.pv_headroom_required_percent
ForecastSlot = _MODULE.ForecastSlot
optimize_battery = _MODULE.optimize_battery


class WinterGridChargeTests(unittest.TestCase):
    """Grid energy is bought only when an allowed slot is clearly worthwhile."""

    @staticmethod
    def _result(first_price: float, max_grid_charge_kw: float):
        slots = [
            ForecastSlot(
                "2026-12-01T02:00:00+01:00",
                first_price,
                0.10,
                0.0,
                False,
                max_grid_charge_kw,
            )
        ]
        slots.extend(
            ForecastSlot(
                f"2026-12-01T{hour:02d}:00:00+01:00",
                0.50,
                0.35,
                0.0,
            )
            for hour in range(6, 14)
        )
        return optimize_battery(
            slots,
            20,
            capacity_kwh=5.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.04,
            export_eur_kwh=0.0,
            grid_charge_margin_eur_kwh=0.07,
        )

    def test_cheap_permitted_slot_charges_for_expensive_hours(self) -> None:
        result = self._result(0.20, 1.6)
        self.assertEqual(result.action, "GRID_CHARGE")
        self.assertGreater(result.target_min_soc, 20)
        self.assertGreater(result.expected_grid_charge_kwh, 0)
        self.assertLessEqual(result.plan[0]["grid_to_battery_kwh"], 0.4)

    def test_missing_permission_prevents_grid_charge(self) -> None:
        result = self._result(0.20, 0.0)
        self.assertNotEqual(result.action, "GRID_CHARGE")
        self.assertEqual(result.expected_grid_charge_kwh, 0)

    def test_small_price_advantage_is_rejected(self) -> None:
        result = self._result(0.37, 1.6)
        self.assertNotEqual(result.action, "GRID_CHARGE")
        self.assertEqual(result.expected_grid_charge_kwh, 0)

    def test_negative_price_does_not_receive_extra_arbitrage_hurdle(self) -> None:
        result = self._result(-0.01, 1.6)
        self.assertEqual(result.action, "GRID_CHARGE")
        self.assertGreater(result.expected_grid_charge_kwh, 0)

    def test_subminimum_grid_charge_does_not_accumulate_from_rounding(self) -> None:
        slots = [
            ForecastSlot(
                f"cheap-sun-{index}",
                0.10,
                0.0,
                0.099,
                False,
                1.6,
            )
            for index in range(12)
        ]
        slots.append(ForecastSlot("expensive", 0.70, 1.0, 0.0))
        result = optimize_battery(
            slots,
            12,
            capacity_kwh=2.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.0,
            export_eur_kwh=0.0,
        )
        self.assertEqual(result.expected_grid_charge_kwh, 0.0)
        self.assertTrue(
            all(item["grid_to_battery_kwh"] == 0.0 for item in result.plan)
        )


class ForecastHorizonTests(unittest.TestCase):
    """Estimated prices after the horizon must not create phantom imports."""

    def test_estimated_terminal_prices_do_not_preserve_full_battery(self) -> None:
        slots = [
            ForecastSlot(str(index), 0.20, 0.25, 0.0, True)
            for index in range(8)
        ]
        slots.extend(
            ForecastSlot(str(index), 0.50, 0.0, 0.0, True)
            for index in range(8, 40)
        )
        result = optimize_battery(
            slots,
            90,
            capacity_kwh=5.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.04,
            export_eur_kwh=0.0,
        )
        self.assertLess(result.expected_grid_import_kwh, 0.25)
        self.assertGreater(result.expected_battery_discharge_kwh, 1.75)

    def test_estimated_price_never_triggers_pv_diversion_or_grid_charge(self) -> None:
        slots = [
            ForecastSlot(
                "estimated-cheap-sun",
                0.10,
                0.20,
                0.30,
                True,
                1.6,
            ),
            ForecastSlot("firm-expensive", 0.70, 0.50, 0.0),
        ]
        result = optimize_battery(
            slots,
            12,
            capacity_kwh=1.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.02,
            export_eur_kwh=0.0,
        )
        first = result.plan[0]
        self.assertEqual(result.action, "PV_SURPLUS")
        self.assertEqual(first["grid_to_battery_kwh"], 0.0)
        self.assertEqual(first["grid_import_kwh"], 0.0)
        self.assertAlmostEqual(first["pv_to_load_kwh"], 0.20, places=2)
        self.assertAlmostEqual(first["pv_to_battery_kwh"], 0.10, places=2)
        self.assertAlmostEqual(
            first["pv_kwh"],
            first["pv_to_load_kwh"]
            + first["pv_to_battery_kwh"]
            + first["pv_export_kwh"],
            places=3,
        )

    def test_estimated_negative_price_cannot_trigger_real_grid_charge(self) -> None:
        slots = [
            ForecastSlot("estimated-negative", -0.05, 0.10, 0.0, True, 1.6),
            ForecastSlot("firm-expensive", 0.70, 0.50, 0.0),
        ]
        result = optimize_battery(
            slots,
            12,
            capacity_kwh=1.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.02,
            export_eur_kwh=0.0,
        )
        self.assertNotEqual(result.action, "GRID_CHARGE")
        self.assertEqual(result.plan[0]["grid_to_battery_kwh"], 0.0)
        self.assertEqual(result.expected_grid_charge_kwh, 0.0)

    def test_firm_grid_charge_cannot_be_carried_into_estimated_prices(self) -> None:
        slots = [
            ForecastSlot("firm-cheap", 0.10, 0.05, 0.0, False, 1.6),
            ForecastSlot("firm-boundary", 0.12, 0.05, 0.0),
            ForecastSlot("estimated-expensive", 0.80, 0.50, 0.0, True),
            ForecastSlot("estimated-expensive-2", 0.80, 0.50, 0.0, True),
        ]
        result = optimize_battery(
            slots,
            12,
            capacity_kwh=1.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.0,
            export_eur_kwh=0.0,
            grid_charge_margin_eur_kwh=0.03,
        )
        self.assertNotEqual(result.action, "GRID_CHARGE")
        self.assertEqual(result.expected_grid_charge_kwh, 0.0)
        self.assertEqual(result.plan[0]["grid_to_battery_kwh"], 0.0)

    def test_grid_charge_consumed_inside_firm_price_window_remains_allowed(
        self,
    ) -> None:
        slots = [
            ForecastSlot("firm-cheap", 0.10, 0.0, 0.0, False, 1.6),
            ForecastSlot("firm-expensive", 0.70, 0.30, 0.0),
            ForecastSlot("estimated-tail", 0.80, 0.50, 0.0, True),
        ]
        result = optimize_battery(
            slots,
            12,
            capacity_kwh=1.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.0,
            export_eur_kwh=0.0,
            grid_charge_margin_eur_kwh=0.03,
        )
        self.assertEqual(result.action, "GRID_CHARGE")
        self.assertGreater(result.plan[0]["grid_to_battery_kwh"], 0.20)
        self.assertGreater(result.plan[1]["battery_to_load_kwh"], 0.20)
        self.assertLessEqual(result.plan[1]["soc_end"], 12.1)

    def test_all_firm_horizon_does_not_charge_without_later_load(self) -> None:
        slots = [
            ForecastSlot("firm-cheap", 0.10, 0.0, 0.0, False, 1.6),
            ForecastSlot("firm-high-no-load", 0.70, 0.0, 0.0),
            ForecastSlot("firm-high-no-load-2", 0.70, 0.0, 0.0),
        ]
        result = optimize_battery(
            slots,
            12,
            capacity_kwh=1.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.0,
            export_eur_kwh=0.0,
            grid_charge_margin_eur_kwh=0.03,
        )
        self.assertNotEqual(result.action, "GRID_CHARGE")
        self.assertEqual(result.expected_grid_charge_kwh, 0.0)
        self.assertTrue(
            all(item["grid_to_battery_kwh"] == 0.0 for item in result.plan)
        )

    def test_firm_pv_store_cannot_be_carried_into_estimated_prices(self) -> None:
        slots = [
            ForecastSlot("firm-cheap-sun", 0.10, 0.20, 0.30),
            ForecastSlot("firm-boundary", 0.12, 0.0, 0.0),
            ForecastSlot("estimated-expensive", 0.80, 0.40, 0.0, True),
        ]
        result = optimize_battery(
            slots,
            10,
            capacity_kwh=1.0,
            hard_min_soc=10,
            battery_wear_eur_kwh=0.0,
            export_eur_kwh=0.0,
        )
        first = result.plan[0]
        self.assertEqual(result.action, "PV_SURPLUS")
        self.assertEqual(first["grid_import_kwh"], 0.0)
        self.assertAlmostEqual(first["pv_to_load_kwh"], 0.20, places=2)
        self.assertAlmostEqual(first["pv_to_battery_kwh"], 0.10, places=2)

    def test_pv_store_consumed_inside_firm_price_window_remains_allowed(
        self,
    ) -> None:
        slots = [
            ForecastSlot("firm-cheap-sun", 0.10, 0.20, 0.30),
            ForecastSlot("firm-expensive", 0.70, 0.30, 0.0),
            ForecastSlot("estimated-tail", 0.80, 0.40, 0.0, True),
        ]
        result = optimize_battery(
            slots,
            10,
            capacity_kwh=1.0,
            hard_min_soc=10,
            battery_wear_eur_kwh=0.0,
            export_eur_kwh=0.0,
        )
        first, second = result.plan[:2]
        self.assertEqual(result.action, "PV_STORE")
        self.assertAlmostEqual(first["grid_import_kwh"], 0.20, places=2)
        self.assertAlmostEqual(first["pv_to_battery_kwh"], 0.30, places=2)
        self.assertGreater(second["battery_to_load_kwh"], 0.15)
        # It may retain the same 20% passive-PV reserve as the reference, but
        # the additional diverted PV has been consumed before the boundary.
        self.assertLessEqual(second["soc_end"], 20.1)


class DispatchPriorityTests(unittest.TestCase):
    """Expensive imports come first; PV utilization breaks economic ties."""

    def test_expensive_slot_is_covered_before_cheap_slot(self) -> None:
        slots = [
            ForecastSlot("cheap", 0.20, 0.30, 0.0),
            ForecastSlot("expensive", 0.50, 0.30, 0.0),
        ]
        result = optimize_battery(
            slots,
            20,
            capacity_kwh=5.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.04,
            export_eur_kwh=0.0,
        )
        self.assertEqual(result.action, "RESERVE")
        self.assertEqual(result.plan[0]["battery_to_load_kwh"], 0.0)
        self.assertGreater(result.plan[1]["battery_to_load_kwh"], 0.25)

    def test_tiny_last_minute_load_is_not_deferred_for_sub_cent_gain(self) -> None:
        slots = [
            ForecastSlot("20:59", 0.4597, 0.006, 0.0),
            ForecastSlot("21:00", 0.4634, 0.060, 0.0),
        ]
        result = optimize_battery(
            slots,
            90,
            capacity_kwh=5.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.04,
            export_eur_kwh=0.0,
        )
        self.assertEqual(result.action, "DISCHARGE")
        self.assertGreater(result.plan[0]["battery_to_load_kwh"], 0.005)
        self.assertEqual(result.plan[0]["grid_import_kwh"], 0.0)
        self.assertLess(result.target_min_soc, 90)

    def test_material_next_slot_price_gain_still_reserves_energy(self) -> None:
        slots = [
            ForecastSlot("now", 0.40, 0.30, 0.0),
            ForecastSlot("next", 0.46, 0.30, 0.0),
        ]
        result = optimize_battery(
            slots,
            18.4,
            capacity_kwh=5.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.04,
            export_eur_kwh=0.0,
        )
        self.assertEqual(result.action, "RESERVE")
        self.assertEqual(result.plan[0]["battery_to_load_kwh"], 0.0)
        self.assertGreater(result.plan[0]["grid_import_kwh"], 0.0)

    def test_live_partial_expensive_night_slot_discharges(self) -> None:
        # Regression for the live 01:09 case: 78 % SoC, 42.25 ct/kWh and only
        # 0.009975 kWh left in the current quarter hour.  Exact prices select
        # the current expensive slot before the cheaper following night block.
        slots = [ForecastSlot("01:09", 0.4225, 0.009975, 0.0)]
        slots.extend(
            ForecastSlot(f"cheap-night-{index}", 0.4100, 0.027, 0.0)
            for index in range(19)
        )
        slots.extend(
            ForecastSlot(
                f"morning-{index}",
                0.4132 + index * 0.0014,
                0.060,
                0.0,
            )
            for index in range(20)
        )
        slots.extend(
            ForecastSlot(f"safe-sun-{index}", 0.3300, 0.060, 0.10125)
            for index in range(24)
        )
        slots.extend(
            ForecastSlot(f"expensive-evening-{index}", 0.4900, 0.100, 0.0)
            for index in range(20)
        )
        slots.extend(
            ForecastSlot(
                f"estimated-tail-{index}",
                0.396,
                0.070,
                0.032,
                True,
            )
            for index in range(60)
        )
        result = optimize_battery(
            slots,
            78,
            capacity_kwh=5.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.04,
            export_eur_kwh=0.0,
        )
        self.assertEqual(result.action, "DISCHARGE")
        self.assertAlmostEqual(
            result.plan[0]["battery_to_load_kwh"],
            0.009975,
            places=3,
        )
        self.assertEqual(result.plan[0]["grid_import_kwh"], 0.0)

    def test_first_action_always_matches_first_plan_interval(self) -> None:
        scenarios = [
            [
                ForecastSlot("near", 0.4597, 0.006, 0.0),
                ForecastSlot("next", 0.4634, 0.060, 0.0),
            ],
            [
                ForecastSlot("cheap", 0.20, 0.30, 0.0),
                ForecastSlot("expensive", 0.50, 0.30, 0.0),
            ],
            [ForecastSlot("solar", 0.20, 0.10, 0.30)],
        ]
        for slots in scenarios:
            with self.subTest(slot=slots[0].start):
                result = optimize_battery(
                    slots,
                    20,
                    capacity_kwh=5.0,
                    hard_min_soc=12,
                    battery_wear_eur_kwh=0.04,
                    export_eur_kwh=0.0,
                )
                first = result.plan[0]
                if result.action == "DISCHARGE":
                    self.assertGreater(first["battery_to_load_kwh"], 0.005)
                elif result.action == "RESERVE":
                    self.assertLessEqual(first["battery_to_load_kwh"], 0.005)
                    self.assertGreater(first["grid_import_kwh"], 0.0)
                elif result.action == "PV_SURPLUS":
                    self.assertGreaterEqual(first["pv_kwh"], first["load_kwh"])
                elif result.action == "GRID_CHARGE":
                    self.assertGreater(first["grid_to_battery_kwh"], 0.005)
                elif result.action == "PV_STORE":
                    self.assertGreater(first["pv_to_battery_kwh"], 0.005)
                    self.assertGreater(first["grid_import_kwh"], 0.005)
                else:
                    self.fail(f"Unexpected action: {result.action}")

    def test_pv_utilization_breaks_equal_cost_tie(self) -> None:
        slots = [
            ForecastSlot("before-sun", 0.04, 0.40, 0.0),
            ForecastSlot("sun", 0.00, 0.0, 0.50),
        ]
        result = optimize_battery(
            slots,
            100,
            capacity_kwh=5.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.04,
            export_eur_kwh=0.0,
        )
        self.assertGreater(result.plan[0]["battery_to_load_kwh"], 0.35)
        self.assertLess(result.expected_export_kwh, 0.15)

    def test_bill_only_dispatch_reloads_pv_between_two_expensive_phases(
        self,
    ) -> None:
        slots = [
            ForecastSlot("expensive-night", 0.50, 0.20, 0.0),
            ForecastSlot("cheap-sunny-day", 0.10, 0.20, 0.30),
            ForecastSlot("more-expensive-evening", 0.70, 0.40, 0.0),
        ]
        parameters = {
            "capacity_kwh": 1.0,
            "hard_min_soc": 10,
            "battery_wear_eur_kwh": 0.0,
            "export_eur_kwh": 0.0,
        }

        night_result = optimize_battery(slots, 46, **parameters)
        night, day, evening = night_result.plan
        for item in night_result.plan:
            with self.subTest(balance=item["start"]):
                self.assertAlmostEqual(
                    item["pv_kwh"],
                    item["pv_to_load_kwh"]
                    + item["pv_to_battery_kwh"]
                    + item["pv_export_kwh"],
                    places=3,
                )
                self.assertAlmostEqual(
                    item["grid_import_kwh"],
                    item["load_kwh"]
                    - item["pv_to_load_kwh"]
                    - item["battery_to_load_kwh"]
                    + item["grid_to_battery_kwh"],
                    places=3,
                )
                expected_battery_delta = (
                    (
                        item["pv_to_battery_kwh"]
                        + item["grid_to_battery_kwh"]
                    )
                    * 0.94
                    - item["battery_to_load_kwh"] / 0.94
                )
                actual_battery_delta = (
                    item["soc_end"] - item["soc_start"]
                ) / 100
                self.assertAlmostEqual(
                    actual_battery_delta,
                    expected_battery_delta,
                    delta=0.01,
                )
        self.assertEqual(night_result.action, "DISCHARGE")
        self.assertAlmostEqual(night["battery_to_load_kwh"], 0.20, places=2)
        self.assertEqual(night["grid_import_kwh"], 0.0)

        # The cheap grid supplies only the house.  All available PV is stored;
        # none of it is silently created, exported or labelled as AC charging.
        self.assertAlmostEqual(day["grid_import_kwh"], 0.20, places=2)
        self.assertEqual(day["grid_to_battery_kwh"], 0.0)
        self.assertAlmostEqual(day["pv_to_battery_kwh"], 0.30, places=2)
        self.assertEqual(day["pv_to_load_kwh"], 0.0)
        self.assertEqual(day["pv_export_kwh"], 0.0)
        self.assertGreater(evening["battery_to_load_kwh"], 0.30)
        self.assertLess(evening["grid_import_kwh"], 0.06)
        self.assertEqual(night_result.expected_grid_charge_kwh, 0.0)

        day_result = optimize_battery(
            slots[1:],
            float(night["soc_end"]),
            **parameters,
        )
        self.assertEqual(day_result.action, "PV_STORE")
        self.assertIn("PV lädt", day_result.reason)

        evening_result = optimize_battery(
            slots[2:],
            float(day["soc_end"]),
            **parameters,
        )
        self.assertEqual(evening_result.action, "DISCHARGE")

    def test_expensive_night_is_used_before_cheaper_sunny_recharge(self) -> None:
        night = [
            ForecastSlot(f"night-{index}", 0.405, 0.03, 0.0, False, 1.6)
            for index in range(16)
        ]
        cheap_sun = [
            ForecastSlot(f"day-{index}", 0.33, 0.06, 0.10, False, 2.5)
            for index in range(16)
        ]
        evening = [
            ForecastSlot(f"evening-{index}", 0.49, 0.10, 0.0, False, 2.5)
            for index in range(20)
        ]
        estimated_tail = [
            ForecastSlot(f"estimated-{index}", 0.40, 0.06, 0.02, True)
            for index in range(60)
        ]
        result = optimize_battery(
            night + cheap_sun + evening + estimated_tail,
            40,
            capacity_kwh=5.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.0,
            export_eur_kwh=0.0,
            grid_charge_margin_eur_kwh=0.03,
        )
        self.assertEqual(result.action, "DISCHARGE")
        self.assertAlmostEqual(
            sum(item["grid_import_kwh"] for item in result.plan[:16]),
            0.0,
            places=3,
        )
        self.assertTrue(
            any(
                item["pv_to_battery_kwh"]
                > max(0.0, item["pv_kwh"] - item["load_kwh"]) + 0.005
                for item in result.plan[16:32]
            )
        )
        self.assertGreater(
            sum(item["battery_to_load_kwh"] for item in result.plan[32:52]),
            1.8,
        )

    def test_pv_battery_charge_obeys_input_power_limit(self) -> None:
        slots = [
            ForecastSlot("solar", 0.10, 0.0, 1.0),
            ForecastSlot("later", 0.70, 0.50, 0.0),
        ]
        result = optimize_battery(
            slots,
            12,
            capacity_kwh=1.0,
            hard_min_soc=12,
            battery_wear_eur_kwh=0.02,
            export_eur_kwh=0.0,
            max_charge_kw=0.4,
        )
        first = result.plan[0]
        self.assertLessEqual(first["pv_to_battery_kwh"], 0.4 * 0.25 + 1e-9)
        stored_kwh = (first["soc_end"] - first["soc_start"]) / 100
        self.assertLessEqual(
            stored_kwh,
            first["pv_to_battery_kwh"] * 0.94 + 0.006,
        )
        self.assertGreater(stored_kwh, 0.08)

    def test_headroom_uses_chronological_pv_load_balance(self) -> None:
        slots = [
            ForecastSlot("load-first", 0.20, 0.20, 0.0),
            ForecastSlot("solar", 0.20, 0.0, 0.70),
        ]
        headroom = pv_headroom_required_percent(slots, capacity_kwh=5.0)
        self.assertAlmostEqual(headroom, 8.9, places=1)


if __name__ == "__main__":
    unittest.main()
