"""Model picker placement on multi-monitor setups.  Run:  .venv\\Scripts\\python -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alfred.picker import SHADOW, place  # noqa: E402

# This PC: a 150% laptop as the primary screen, and a 100% monitor to its left (negative coordinates).
LAPTOP = (0, 0, 1706, 911)            # available geometry as (left, top, right, bottom)
MONITOR = (-2560, 163, -1, 1554)
W, H = 340 + 2 * SHADOW, 400 + 2 * SHADOW


def panel(x, y):
    """The visible popover rectangle (without its shadow margin)."""
    return x + SHADOW, y + SHADOW, x + W - SHADOW, y + H - SHADOW


class PlacementTests(unittest.TestCase):
    def test_opens_under_a_button_on_the_secondary_monitor(self):
        x, y = place(-700, 300, 270, W, H, MONITOR)
        left, top, right, bottom = panel(x, y)
        self.assertEqual((right, top), (-700, 306))  # right-aligned, just under the button
        self.assertTrue(MONITOR[0] <= left and right <= MONITOR[2] + 1)  # stays on the monitor, not the laptop

    def test_kept_inside_the_monitor_near_its_left_edge(self):
        x, y = place(-2500, 300, 270, W, H, MONITOR)
        self.assertGreaterEqual(panel(x, y)[0], MONITOR[0])

    def test_flips_above_the_button_near_the_bottom(self):
        x, y = place(-700, 1400, 1370, W, H, MONITOR)
        left, top, right, bottom = panel(x, y)
        self.assertEqual(bottom, 1370 - 6)
        self.assertGreaterEqual(top, MONITOR[1])

    def test_laptop_screen_unchanged(self):
        x, y = place(1600, 120, 90, W, H, LAPTOP)
        self.assertEqual(panel(x, y)[2], 1600)
        self.assertTrue(0 <= panel(x, y)[0])

    def test_no_screen_information(self):
        self.assertEqual(panel(*place(500, 100, 70, W, H))[2:3], (500,))


if __name__ == '__main__':
    unittest.main()
