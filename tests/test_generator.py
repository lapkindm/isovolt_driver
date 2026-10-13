import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from isovolt_driver.generator import ramp_steps  # noqa: E402


class Ramp(unittest.TestCase):
    def test_spec_generator_off(self):
        # site.mac generator_off from 40 kV / 30 mA: SC 25,20,15,10 then SV 30,20,10
        self.assertEqual(ramp_steps(40, 30, 10, 10, 10, 5),
                         [("SC", 25), ("SC", 20), ("SC", 15), ("SC", 10), ("SV", 30), ("SV", 20), ("SV", 10)])

    def test_ramp_up_voltage_first(self):
        self.assertEqual(ramp_steps(10, 10, 40, 30, 10, 5),
                         [("SV", 20), ("SV", 30), ("SV", 40), ("SC", 15), ("SC", 20), ("SC", 25), ("SC", 30)])

    def test_odd_values_end_on_target(self):
        self.assertEqual(ramp_steps(37, 28, 40, 30, 10, 5), [("SV", 40), ("SC", 30)])
        self.assertEqual(ramp_steps(40, 30, 35, 22, 10, 5), [("SC", 25), ("SC", 22), ("SV", 35)])

    def test_no_change(self):
        self.assertEqual(ramp_steps(40, 30, 40, 30, 10, 5), [])


if __name__ == "__main__":
    unittest.main()
