"""Unit tests for the Core Monitor eviction-distance math (pure functions).

Three synthetic portfolios per the build spec:
  1. single position
  2. mixed maintenance rates (incl. a 100% non-marginable name)
  3. zero loan → buffer 100%, no alert
"""
import os
import sys
import unittest
import importlib.util

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
import core_monitor
from core_monitor import eviction_distance, restore_amounts

# Import the alert script as a module to test its threshold logic.
_spec = importlib.util.spec_from_file_location(
    "core_monitor_alert", os.path.join(_BASE, "scripts", "core_monitor_alert.py")
)
alert = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(alert)

CFG = {"warn_buffer": 0.15, "urgent_buffer": 0.10, "restore_buffer": 0.20}


def simulate_buffer(positions, loan, d):
    """Brute-force check: equity and maintenance after a uniform drop d."""
    equity = sum(p["value"] * (1 - d) for p in positions) - loan
    maint = sum(p["maint"] * p["value"] * (1 - d) for p in positions)
    return equity, maint


class TestEvictionMath(unittest.TestCase):
    def test_single_position(self):
        positions = [{"symbol": "X", "value": 10000.0, "maint": 0.25}]
        loan = 4000.0
        ev = eviction_distance(positions, loan)
        # K = (1-0.25)*10000 = 7500; d* = 1 - 4000/7500
        self.assertAlmostEqual(ev["buffer"], 1 - 4000 / 7500, places=9)
        # At exactly d*, equity == maintenance (the call boundary).
        eq, mt = simulate_buffer(positions, loan, ev["buffer"])
        self.assertAlmostEqual(eq, mt, places=6)
        # One cent before the boundary we are still above maintenance.
        eq, mt = simulate_buffer(positions, loan, ev["buffer"] - 0.01)
        self.assertGreater(eq, mt)

    def test_mixed_maintenance_rates(self):
        # 25% growth name, 100% non-marginable (AMPX-class), 75% high-rate name.
        positions = [
            {"symbol": "GROWTH", "value": 5000.0, "maint": 0.25},
            {"symbol": "NOMARG", "value": 3000.0, "maint": 1.00},
            {"symbol": "HIGHRT", "value": 2000.0, "maint": 0.75},
        ]
        loan = 2000.0
        ev = eviction_distance(positions, loan)
        K = 0.75 * 5000 + 0.0 * 3000 + 0.25 * 2000  # 4250
        self.assertAlmostEqual(ev["loanable_collateral"], K, places=9)
        self.assertAlmostEqual(ev["buffer"], 1 - 2000 / K, places=9)
        eq, mt = simulate_buffer(positions, loan, ev["buffer"])
        self.assertAlmostEqual(eq, mt, places=6)
        # A flat 30% assumption would overstate the buffer badly here.
        flat = [{**p, "maint": 0.30} for p in positions]
        self.assertGreater(eviction_distance(flat, loan)["buffer"], ev["buffer"])

    def test_zero_loan_full_buffer_no_alert(self):
        positions = [{"symbol": "X", "value": 8000.0, "maint": 0.30}]
        ev = eviction_distance(positions, 0.0)
        self.assertEqual(ev["buffer"], 1.0)  # 100%
        # No restore amounts needed…
        r = restore_amounts(positions, 0.0, CFG["restore_buffer"])
        self.assertEqual(r, {"deposit": 0.0, "reduce_positions": 0.0})
        # …and the alert state machine stays quiet.
        self.assertEqual(alert.buffer_state(ev["buffer"] * 100, CFG), "ok")

    def test_alert_thresholds(self):
        self.assertEqual(alert.buffer_state(16.0, CFG), "ok")
        self.assertEqual(alert.buffer_state(14.9, CFG), "warn")
        self.assertEqual(alert.buffer_state(9.9, CFG), "urgent")

    def test_restore_amounts_hit_target_buffer(self):
        positions = [
            {"symbol": "A", "value": 6000.0, "maint": 0.25},
            {"symbol": "B", "value": 4000.0, "maint": 0.50},
        ]
        loan = 5500.0
        r = CFG["restore_buffer"]
        ev = eviction_distance(positions, loan)
        self.assertLess(ev["buffer"], r)  # scenario is genuinely under target

        amounts = restore_amounts(positions, loan, r)
        # Depositing D restores the buffer to exactly r.
        after_deposit = eviction_distance(positions, loan - amounts["deposit"])
        self.assertAlmostEqual(after_deposit["buffer"], r, places=4)
        # Selling Y proportionally (proceeds pay the loan) restores it too.
        gross = sum(p["value"] for p in positions)
        s = amounts["reduce_positions"] / gross
        scaled = [{**p, "value": p["value"] * (1 - s)} for p in positions]
        after_sell = eviction_distance(scaled, loan - amounts["reduce_positions"])
        self.assertAlmostEqual(after_sell["buffer"], r, places=4)

    def test_already_under_maintenance_is_negative(self):
        positions = [{"symbol": "X", "value": 10000.0, "maint": 0.25}]
        ev = eviction_distance(positions, 8000.0)  # K=7500 < loan
        self.assertLess(ev["buffer"], 0)


if __name__ == "__main__":
    unittest.main()
