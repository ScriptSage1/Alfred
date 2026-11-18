"""Live glass backdrop (juno/glass.py): pure functions on plain arrays and numbers, and the Backdrop engine driven with
a fake grabber, fake display affinity, fake clock, a schedule callable and a fake frame clock. No screen, no window
handles, no hooks. Only the worker-thread regression test waits on real time (a few tenths of a second at most)."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _scratch, mock.patch.dict(os.environ, {'JUNO_HOME': _scratch}):
    from juno import glass  # noqa: E402

# A small window keeps every job fast: the panel sits 8 px inside a 136 x 200 window and is 120 px wide.
M, PW, FULL = 8, 120, 120
RECT = (0, 0, 136, 200)
SHAPE = 62


def bgra(h, w, value=0, alpha=255):
    a = np.full((h, w, 4), value, np.uint8)
    a[..., 3] = alpha
    return a


# ------------------------------------------------------------------ fakes
class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class Frames:
    def __init__(self):
        self.loops = set()

    def add_loop(self, obj):
        self.loops.add(obj)


class FakeAff:
    def __init__(self):
        self.v, self.history = {}, []

    def set(self, hwnd, flag):
        self.history.append((hwnd, flag))
        self.v[hwnd] = flag
        return True

    def get(self, hwnd):
        return self.v.get(hwnd, 0)


class FakeGrab:
    """Returns a flat picture of ``value`` (an int, or a callable of the call count); records, per call, whether the
    main window was excluded from capture at that moment and the engine state."""

    def __init__(self, aff, clock, hwnd=1):
        self.aff, self.clock, self.hwnd = aff, clock, hwnd
        self.value, self.fail, self.cost, self.eng = 120, False, 0.0, None
        self.log = []

    def _level(self):
        return self.value(len(self.log)) if callable(self.value) else self.value

    def _note(self, kind, halftone=False):
        self.log.append(dict(kind=kind, excluded=self.aff.get(self.hwnd) == glass.WDA_EXCLUDE, halftone=halftone,
                             state=self.eng.state if self.eng else None))
        self.clock.t += self.cost
        if self.fail:
            raise OSError('fake grab failure')

    def grab(self, x, y, w, h):
        self._note('grab')
        return bgra(h, w, self._level())

    def thumb(self, x, y, w, h, tw, th, halftone=False):
        self._note('thumb', halftone)
        return bgra(th, tw, self._level())

    def count(self, kind=None, excluded=None, halftone=None):
        return sum(1 for e in self.log if (kind is None or e['kind'] == kind) and (excluded is None or e['excluded'] == excluded)
                   and (halftone is None or e['halftone'] == halftone))


def rig(battery=False, low_end=False, reduced=False, **kw):
    clock, aff = Clock(), FakeAff()
    grab = FakeGrab(aff, clock)
    frames, due, themes, changes = Frames(), [], [], []
    eng = glass.Backdrop(clock=clock, grabber=lambda: grab, affinity=aff, sync=True, schedule=due.append, frame_clock=frames,
                         hook=False, on_change=lambda: changes.append(1), on_theme=themes.append, on_battery=lambda: battery,
                         low_end=lambda: low_end, reduced=lambda: reduced, margin=M, width=PW, full_shape=FULL, **kw)
    grab.eng = eng
    return SimpleNamespace(eng=eng, clock=clock, grab=grab, aff=aff, frames=frames, due=due, themes=themes, changes=changes)


def opened(r=None, **kw):
    r = r or rig()
    kw.setdefault('hidden', True)
    assert r.eng.open(1, RECT, 1.0, SHAPE, **kw)
    return r


def step(r, dt=0.1):
    r.clock.t += dt
    for obj in list(r.frames.loops):
        obj.advance(dt)
    r.eng.tick()


def run(r, seconds, dt=0.1):
    for _ in range(int(round(seconds / dt))):
        step(r, dt)


def settle(r, limit=60):
    """Step until the engine is idle (one settled picture made, blend over)."""
    for _ in range(limit):
        step(r)
        if r.eng.state == 'idle' and not r.eng.blending():
            return
    raise AssertionError('never became idle: %s %r' % (r.eng.state, r.eng.counters))


# ------------------------------------------------------------------ pure functions
class ThemeTests(unittest.TestCase):
    def test_bright_background_goes_light(self):
        self.assertEqual(glass.resolve_theme(0.80, 'dark'), 'light')

    def test_dark_background_goes_dark(self):
        self.assertEqual(glass.resolve_theme(0.10, 'light'), 'dark')

    def test_between_thresholds_keeps_current(self):
        for lum in (0.46, 0.52, 0.59):
            self.assertEqual(glass.resolve_theme(lum, 'dark'), 'dark')
            self.assertEqual(glass.resolve_theme(lum, 'light'), 'light')

    def test_thresholds_are_exclusive(self):
        self.assertEqual(glass.resolve_theme(0.60, 'dark'), 'dark')
        self.assertEqual(glass.resolve_theme(0.45, 'light'), 'light')
        self.assertEqual(glass.resolve_theme(0.601, 'dark'), 'light')
        self.assertEqual(glass.resolve_theme(0.449, 'light'), 'dark')

    def test_fixed_setting_always_wins(self):
        self.assertEqual(glass.resolve_theme(0.99, 'dark', 'dark'), 'dark')
        self.assertEqual(glass.resolve_theme(0.01, 'light', 'light'), 'light')


class AppearanceHoldTests(unittest.TestCase):
    def test_no_change_returns_current(self):
        h = glass.AppearanceHold(1.0)
        self.assertEqual(h.update('dark', 'dark', 0.0), 'dark')
        self.assertIsNone(h.candidate)

    def test_change_applies_only_after_hold(self):
        h = glass.AppearanceHold(1.0)
        self.assertEqual(h.update('light', 'dark', 0.0), 'dark')
        self.assertEqual(h.update('light', 'dark', 0.99), 'dark')
        self.assertEqual(h.update('light', 'dark', 1.0), 'light')

    def test_flip_back_cancels(self):
        h = glass.AppearanceHold(1.0)
        h.update('light', 'dark', 0.0)
        self.assertEqual(h.update('dark', 'dark', 0.6), 'dark')
        self.assertIsNone(h.candidate)
        self.assertEqual(h.update('light', 'dark', 0.9), 'dark')   # the hold starts again at 0.9
        self.assertEqual(h.update('light', 'dark', 1.5), 'dark')
        self.assertEqual(h.update('light', 'dark', 2.0), 'light')

    def test_default_hold_is_theme_hold(self):
        self.assertEqual(glass.AppearanceHold().hold, glass.THEME_HOLD)


class SmoothTests(unittest.TestCase):
    def test_none_previous_returns_target(self):
        self.assertEqual(glass.smooth(None, 0.7, 0.1, 0.15, 0.5), 0.7)

    def test_rise_uses_rise_constant(self):
        self.assertAlmostEqual(glass.smooth(0.0, 1.0, 0.15, 0.15, 0.5), 1 - 1 / 2.718281828, places=4)

    def test_fall_uses_fall_constant(self):
        self.assertAlmostEqual(glass.smooth(1.0, 0.0, 0.5, 0.15, 0.5), 1 / 2.718281828, places=4)

    def test_rises_faster_than_falls(self):
        up = glass.smooth(0.0, 1.0, 0.1, glass.TINT_TAU_RISE, glass.TINT_TAU_FALL)
        down = 1.0 - glass.smooth(1.0, 0.0, 0.1, glass.TINT_TAU_RISE, glass.TINT_TAU_FALL)
        self.assertGreater(up, down)

    def test_zero_or_negative_dt_keeps_previous(self):
        self.assertEqual(glass.smooth(0.3, 1.0, 0.0, 0.15, 0.5), 0.3)
        self.assertEqual(glass.smooth(0.3, 1.0, -1.0, 0.15, 0.5), 0.3)


class DetailAndAlphaTests(unittest.TestCase):
    RECT = (0, 0, 48, 48)

    def test_flat_picture_scores_zero(self):
        self.assertAlmostEqual(glass.detail_score(bgra(48, 48, 120), self.RECT), 0.0, places=3)

    def test_fine_stripes_score_high(self):
        a = bgra(48, 48, 0)
        a[:, ::2, :3] = 255
        self.assertGreater(glass.detail_score(a, self.RECT), 40.0)

    def test_text_like_noise_scores_high(self):
        a = bgra(48, 48, 0)
        a[..., :3] = np.random.default_rng(3).integers(0, 256, (48, 48, 1), dtype=np.uint8)
        self.assertGreater(glass.detail_score(a, self.RECT), 20.0)

    def test_empty_rectangle_scores_zero(self):
        self.assertEqual(glass.detail_score(bgra(48, 48, 120), (500, 500, 10, 10)), 0.0)

    def test_body_alpha_floor_and_ceiling(self):
        self.assertEqual(glass.body_alpha_for(0.0), glass.BODY_ALPHA)
        self.assertEqual(glass.body_alpha_for(glass.DETAIL_S0), glass.BODY_ALPHA)
        self.assertAlmostEqual(glass.body_alpha_for(glass.DETAIL_S1), glass.BODY_ALPHA_MAX)
        self.assertAlmostEqual(glass.body_alpha_for(500.0), glass.BODY_ALPHA_MAX)

    def test_body_alpha_linear_between(self):
        mid = (glass.DETAIL_S0 + glass.DETAIL_S1) / 2
        self.assertAlmostEqual(glass.body_alpha_for(mid), (glass.BODY_ALPHA + glass.BODY_ALPHA_MAX) / 2)

    def test_body_alpha_monotonic(self):
        values = [glass.body_alpha_for(s) for s in np.linspace(0, 40, 41)]
        self.assertEqual(values, sorted(values))

    def test_non_adaptive_is_fixed(self):
        self.assertEqual(glass.body_alpha_for(500.0, adaptive=False), glass.BODY_ALPHA)

    def test_adaptive_flag_default_is_used(self):
        with mock.patch.object(glass, 'ADAPTIVE_ALPHA', False):
            self.assertEqual(glass.body_alpha_for(500.0), glass.BODY_ALPHA)


class TintAndContrastTests(unittest.TestCase):
    def pics(self, level, size=60):
        img = Image.new('RGB', (size, size), (level, level, level))
        return img, img

    def test_black_on_white_is_21(self):
        self.assertAlmostEqual(float(glass.contrast(np.array([[255, 255, 255]]), (0, 0, 0, 1.0))[0]), 21.0, places=3)

    def test_same_colour_is_1_and_qcolor_accepted(self):
        self.assertAlmostEqual(float(glass.contrast(np.array([[10, 10, 10]]), QColor(10, 10, 10))[0]), 1.0, places=3)

    def test_luma_of_known_colours(self):
        self.assertAlmostEqual(glass.luma(bgra(8, 8, 255), (0, 0, 8, 8)), 1.0, places=3)
        self.assertAlmostEqual(glass.luma(bgra(8, 8, 0), (0, 0, 8, 8)), 0.0, places=3)
        red = bgra(8, 8, 0)
        red[..., 2] = 255   # BGRA: index 2 is red
        self.assertAlmostEqual(glass.luma(red, (0, 0, 8, 8)), 0.299, places=3)

    def test_luma_outside_rect_falls_back_to_whole_picture(self):
        self.assertAlmostEqual(glass.luma(bgra(8, 8, 255), (100, 100, 4, 4)), 1.0, places=3)

    def test_dark_theme_over_black_needs_little_tint(self):
        live, wash = self.pics(0)
        self.assertLess(glass.tint_for(live, wash, 'dark', 0.80, (5, 5, 50, 50)), 0.1)

    def test_dark_theme_over_white_needs_a_lot(self):
        live, wash = self.pics(255)
        t = glass.tint_for(live, wash, 'dark', 0.80, (5, 5, 50, 50))
        self.assertGreater(t, 0.5)
        self.assertLessEqual(t, glass.TINT_MAX)

    def test_light_theme_over_white_needs_little_tint(self):
        live, wash = self.pics(255)
        self.assertLess(glass.tint_for(live, wash, 'light', 0.80, (5, 5, 50, 50)), 0.1)

    def test_never_above_tint_max(self):
        live, wash = self.pics(255)
        for cap in (glass.TINT_MAX, 0.5):
            self.assertLessEqual(glass.tint_for(live, wash, 'dark', 0.80, (5, 5, 50, 50), tint_max=cap), cap)

    def test_report_returns_tint_and_met(self):
        live, wash = self.pics(0)
        tint, met = glass.tint_for(live, wash, 'dark', 0.80, (5, 5, 50, 50), report=True)
        self.assertLess(tint, 0.1)
        self.assertTrue(met)

    def test_report_not_met_for_light_theme_over_black(self):
        live, wash = self.pics(0)
        tint, met = glass.tint_for(live, wash, 'light', 0.80, (5, 5, 50, 50), report=True)
        self.assertAlmostEqual(tint, glass.TINT_MAX, places=6)
        self.assertFalse(met)

    def test_higher_body_alpha_needs_no_more_tint_over_busy_live(self):
        live, wash = self.pics(255)
        low = glass.tint_for(live, wash, 'dark', 0.80, (5, 5, 50, 50))
        high = glass.tint_for(live, wash, 'dark', 0.92, (5, 5, 50, 50))
        self.assertLessEqual(high, low)


class ChangeCheckTests(unittest.TestCase):
    def means(self, value=100.0, shape=(3, 4)):
        return np.full(shape + (3,), value, np.float32)

    def test_block_means_shape_and_value(self):
        a = bgra(32, 48, 0)
        a[:16, :16, :3] = 200
        m = glass.block_means(a)
        self.assertEqual(m.shape, (2, 3, 3))
        self.assertAlmostEqual(float(m[0, 0, 0]), 200.0)
        self.assertAlmostEqual(float(m[1, 2, 1]), 0.0)

    def test_block_means_tiny_picture_is_one_block(self):
        self.assertEqual(glass.block_means(bgra(5, 5, 90)).shape, (1, 1, 3))

    def test_gain_inside_and_outside_panel(self):
        g = glass.gain_map((4, 4), (16, 16, 32, 32), 0.80)
        self.assertEqual(g.shape, (4, 4))
        self.assertAlmostEqual(float(g[1, 1]), 5.0, places=4)
        self.assertAlmostEqual(float(g[2, 2]), 5.0, places=4)
        self.assertEqual(float(g[0, 0]), 1.0)
        self.assertEqual(float(g[3, 3]), 1.0)
        self.assertEqual(float(g[1, 3]), 1.0)

    def test_gain_follows_body_alpha(self):
        g = glass.gain_map((2, 2), (0, 0, 32, 32), 0.92)
        self.assertAlmostEqual(float(g[0, 0]), 1 / 0.08, places=3)

    def test_none_or_shape_mismatch_is_changed(self):
        m = self.means()
        self.assertTrue(glass.changed(None, m))
        self.assertTrue(glass.changed(m, None))
        self.assertTrue(glass.changed(m, self.means(shape=(2, 2))))

    def test_below_and_above_levels(self):
        a = self.means(100)
        self.assertFalse(glass.changed(a, self.means(100 + glass.CHANGE_LEVELS - 0.5)))
        self.assertTrue(glass.changed(a, self.means(100 + glass.CHANGE_LEVELS)))

    def test_one_moving_block_is_a_change(self):
        a, b = self.means(), self.means()
        b[1, 2] += 30
        self.assertTrue(glass.changed(a, b))

    def test_skip_masks_a_block(self):
        a, b = self.means(), self.means()
        b[1, 2] += 30
        skip = np.zeros((3, 4), bool)
        self.assertTrue(glass.changed(a, b, skip=skip))
        skip[1, 2] = True
        self.assertFalse(glass.changed(a, b, skip=skip))

    def test_gain_amplifies(self):
        a, b = self.means(), self.means(101.5)
        self.assertFalse(glass.changed(a, b))
        self.assertTrue(glass.changed(a, b, gains=np.full((3, 4), 5.0, np.float32)))


class RateTests(unittest.TestCase):
    def test_base_rates(self):
        self.assertEqual(glass.base_rate(), 4.0)
        self.assertEqual(glass.base_rate(on_battery=True), 2.0)
        self.assertEqual(glass.base_rate(low_end_start=True), 2.0)
        self.assertEqual(glass.base_rate(covered=True), 1.0)
        self.assertEqual(glass.base_rate(on_battery=True, covered=True), 1.0)

    def test_still_interval(self):
        self.assertEqual(glass.still_interval(), 1.0)
        self.assertEqual(glass.still_interval(on_battery=True), 2.0)

    def test_capture_interval_fifteen_percent_rule(self):
        self.assertAlmostEqual(glass.capture_interval(4.0), 0.25)
        self.assertAlmostEqual(glass.capture_interval(4.0, 0.02), 0.25)      # fast job: the rate decides
        self.assertAlmostEqual(glass.capture_interval(4.0, 0.06), 0.4)       # 60 ms / 0.15
        self.assertAlmostEqual(glass.capture_interval(2.0, 0.03), 0.5)
        self.assertAlmostEqual(glass.capture_interval(4.0, 0.03, share=0.5), 0.25)

    def test_blend_factor(self):
        self.assertEqual(glass.blend_factor(0.0), 0.0)
        self.assertEqual(glass.blend_factor(-1.0), 0.0)
        self.assertAlmostEqual(glass.blend_factor(glass.BLEND_TAU), 1 - 1 / 2.718281828, places=4)
        self.assertGreater(glass.blend_factor(10.0), 0.999)

    def test_blend_factor_same_total_at_any_frame_rate(self):
        k60 = 1 - (1 - glass.blend_factor(1 / 60)) ** 24
        k144 = 1 - (1 - glass.blend_factor(1 / 144)) ** 57.6
        self.assertAlmostEqual(k60, k144, places=4)


class LensMapTests(unittest.TestCase):
    SIZE, RECT_ = (136, 200), (8.0, 8.0, 120.0, 62.0)

    def maps(self, rect=None, div=4):
        sw, sh = self.SIZE[0] // div, self.SIZE[1] // div
        return glass.lens_maps(self.SIZE, rect or self.RECT_, 26.0, 26.0, 22.0, div, sw, sh)

    def test_cached_same_key_same_object(self):
        self.assertIs(self.maps(), self.maps())

    def test_different_key_different_object(self):
        self.assertIsNot(self.maps(), self.maps((8.0, 8.0, 120.0, 100.0)))

    def test_indices_inside_the_picture(self):
        m = self.maps()
        W, H = self.SIZE
        self.assertGreater(len(m['idx']), 0)
        self.assertTrue(((m['idx'] >= 0) & (m['idx'] < W * H)).all())
        sw, sh = W // 4, H // 4
        self.assertTrue(((m['i00'] >= 0) & (m['i00'] + sw + 1 < sw * sh)).all())
        self.assertTrue(((m['near'] >= 0) & (m['near'] < sw * sh)).all())
        self.assertEqual(m['w'].shape, (len(m['idx']), 4))
        self.assertTrue(np.allclose(m['w'].sum(axis=1), 1.0, atol=1e-4))

    def test_band_pixels_lie_inside_the_strips(self):
        m = self.maps()
        W = self.SIZE[0]
        ys, xs = m['idx'] // W, m['idx'] % W
        inside = np.zeros(len(xs), bool)
        for x, y, w, h in m['strips']:
            inside |= (xs >= x) & (xs < x + w) & (ys >= y) & (ys < y + h)
        self.assertTrue(inside.all())

    def test_strips_tall_panel_has_four(self):
        strips = glass.ring_strips(self.RECT_, 26.0)
        self.assertEqual(len(strips), 4)
        for x, y, w, h in strips:
            self.assertGreater(w, 0)
            self.assertGreater(h, 0)

    def test_strips_short_panel_has_two(self):
        self.assertEqual(len(glass.ring_strips((8.0, 8.0, 120.0, 40.0), 26.0)), 2)

    def test_strips_cover_the_rectangle_edges(self):
        x0, y0, w, h = self.RECT_
        strips = glass.ring_strips(self.RECT_, 26.0)
        self.assertLessEqual(min(s[0] for s in strips), x0 - 1)
        self.assertLessEqual(min(s[1] for s in strips), y0 - 1)
        self.assertGreaterEqual(max(s[0] + s[2] for s in strips), x0 + w + 1)
        self.assertGreaterEqual(max(s[1] + s[3] for s in strips), y0 + h + 1)


class PlainReasonTests(unittest.TestCase):
    def patched(self, windows=True, transparency=True, contrast_=False, battery_saver=False, remote=False):
        stack = [mock.patch.object(glass, 'WINDOWS', windows), mock.patch.object(glass, 'transparency_effects', return_value=transparency),
                 mock.patch.object(glass, 'high_contrast', return_value=contrast_), mock.patch.object(glass.power, 'battery_saver', return_value=battery_saver),
                 mock.patch.object(glass, 'remote_session', return_value=remote)]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)

    def test_live_glass_when_nothing_blocks(self):
        self.patched()
        self.assertIsNone(glass.plain_reason('windows'))

    def test_offscreen_and_minimal_platforms_are_plain(self):
        self.patched()
        self.assertEqual(glass.plain_reason('offscreen'), 'platform')
        self.assertEqual(glass.plain_reason('minimal'), 'platform')

    def test_not_windows_is_plain(self):
        self.patched(windows=False)
        self.assertEqual(glass.plain_reason('windows'), 'platform')

    def test_see_through_off_is_plain(self):
        self.patched()
        self.assertEqual(glass.plain_reason('windows', see_through=False), 'setting')

    def test_each_system_condition(self):
        for kw, reason in ((dict(transparency=False), 'transparency effects off'), (dict(contrast_=True), 'high contrast'),
                           (dict(battery_saver=True), 'battery saver'), (dict(remote=True), 'remote session')):
            with self.subTest(reason=reason):
                with mock.patch.object(glass, 'WINDOWS', True), \
                        mock.patch.object(glass, 'transparency_effects', return_value=kw.get('transparency', True)), \
                        mock.patch.object(glass, 'high_contrast', return_value=kw.get('contrast_', False)), \
                        mock.patch.object(glass.power, 'battery_saver', return_value=kw.get('battery_saver', False)), \
                        mock.patch.object(glass, 'remote_session', return_value=kw.get('remote', False)):
                    self.assertEqual(glass.plain_reason('windows'), reason)

    def test_refused_exclusion_is_plain(self):
        self.patched()
        with mock.patch.object(glass, 'affinity_supported', return_value=False):
            self.assertEqual(glass.plain_reason('windows', hwnd=5), 'capture exclusion refused')


class ComposeTests(unittest.TestCase):
    def compose(self, kind, theme='dark', scale=1.0, shape=SHAPE, value=120, size=None, **kw):
        size = size or glass.device_size(136, 200, scale)
        W, H = size
        QW, QH = glass.quarter(size)
        src = bgra(H, W, value) if kind == 'full' else bgra(QH, QW, value)
        return glass.compose(src, kind, theme, size, scale, shape, margin=M, width=PW, full_shape=FULL, **kw), size

    def check_sizes(self, kind, scale):
        out, (W, H) = self.compose(kind, scale=scale)
        for name in ('wash', 'ring'):
            self.assertEqual((out[name].width(), out[name].height()), (W, H))
            self.assertEqual(out[name].devicePixelRatio(), scale)
        self.assertEqual(out['wash'].format(), QImage.Format_ARGB32_Premultiplied)
        self.assertEqual(out['ring'].format(), QImage.Format_ARGB32_Premultiplied)
        self.assertEqual(out['shape'], SHAPE)

    def test_cheap_sizes_at_scale_1(self):
        self.check_sizes('cheap', 1.0)

    def test_cheap_sizes_at_scale_1_5(self):
        self.check_sizes('cheap', 1.5)

    def test_full_sizes_at_scale_1(self):
        self.check_sizes('full', 1.0)

    def test_full_sizes_at_scale_1_5(self):
        self.check_sizes('full', 1.5)

    def test_default_geometry_cheap(self):
        size = glass.device_size(872, 692, 1.5)
        QW, QH = glass.quarter(size)
        out = glass.compose(bgra(QH, QW, 90), 'cheap', 'dark', size, 1.5, 62)
        self.assertEqual((out['wash'].width(), out['wash'].height()), size)
        self.assertEqual(out['ring'].devicePixelRatio(), 1.5)

    def test_wash_is_opaque_and_ring_only_in_band(self):
        out, (W, H) = self.compose('cheap')
        wash = out['wash'].convertToFormat(QImage.Format_ARGB32)
        self.assertEqual(wash.pixelColor(W // 2, H // 2).alpha(), 255)
        ring = out['ring']
        self.assertEqual(ring.pixelColor(W // 2, M + 31).alpha(), 0)      # panel centre: no lens band
        self.assertEqual(ring.pixelColor(2, H - 2).alpha(), 0)            # far corner outside the panel
        self.assertGreater(ring.pixelColor(W // 2, M + 1).alpha(), 0)     # just inside the top edge

    def test_wrong_size_input_raises(self):
        size = (136, 200)
        with self.assertRaises(ValueError):
            glass.compose(bgra(10, 10), 'cheap', 'dark', size, 1.0, SHAPE, margin=M, width=PW)
        with self.assertRaises(ValueError):
            glass.compose(bgra(50, 34), 'full', 'dark', size, 1.0, SHAPE, margin=M, width=PW)

    def test_dark_over_black_keeps_body_alpha(self):
        out, _ = self.compose('cheap', 'dark', value=0)
        self.assertEqual(out['body_alpha'], glass.BODY_ALPHA)
        self.assertTrue(out['contrast_met'])

    def test_contrast_floor_raises_body_alpha_for_light_over_black(self):
        out, _ = self.compose('cheap', 'light', value=0)
        self.assertGreater(out['body_alpha'], glass.BODY_ALPHA)
        self.assertLessEqual(out['body_alpha'], glass.BODY_ALPHA_MAX)

    def test_body_alpha_never_above_max(self):
        for theme, value in (('light', 0), ('dark', 255), ('light', 40)):
            out, _ = self.compose('cheap', theme, value=value)
            self.assertLessEqual(out['body_alpha'], glass.BODY_ALPHA_MAX + 1e-9)

    def test_contrast_floor_can_be_switched_off(self):
        with mock.patch.object(glass, 'CONTRAST_FLOOR', False):
            out, _ = self.compose('cheap', 'light', value=0)
        self.assertEqual(out['body_alpha'], glass.BODY_ALPHA)

    def test_fixed_tint_and_callable_tint(self):
        out, _ = self.compose('cheap', value=0, tint=0.4)
        self.assertAlmostEqual(out['tint'], 0.4)
        out, _ = self.compose('cheap', value=0, tint=lambda raw: raw + 0.1)
        self.assertAlmostEqual(out['tint'], out['tint_raw'] + 0.1)
        out, _ = self.compose('cheap', value=0, tint=5.0)
        self.assertEqual(out['tint'], glass.TINT_MAX)


# ------------------------------------------------------------------ the engine
class OpenAndCaptureTests(unittest.TestCase):
    def test_open_returns_true_with_a_picture(self):
        r = rig()
        self.assertTrue(r.eng.open(1, RECT, 1.0, SHAPE))
        self.assertIsNotNone(r.eng.picture())
        self.assertEqual(r.eng.state, 'active')
        self.assertFalse(r.eng.failed)
        self.assertEqual(r.eng.size, (136, 200))
        self.assertEqual(r.eng.counters['arrivals'], 1)

    def test_first_capture_of_a_hidden_window_uses_no_exclusion(self):
        r = opened()
        self.assertEqual(r.grab.count('thumb'), 1)
        self.assertEqual(r.grab.count(excluded=True), 0)
        self.assertEqual(r.aff.history, [])
        self.assertEqual(r.eng.counters['excluded_grabs'], 0)

    def test_first_capture_of_a_visible_window_is_excluded(self):
        r = opened(hidden=False)
        self.assertEqual(r.grab.count(excluded=True), 1)
        self.assertEqual(r.aff.history, [(1, glass.WDA_EXCLUDE), (1, glass.WDA_NONE)])

    def test_later_captures_set_and_clear_the_exclusion(self):
        r = opened()
        run(r, 0.3)
        self.assertEqual(r.grab.count(excluded=True), 1)
        self.assertEqual(r.aff.history, [(1, glass.WDA_EXCLUDE), (1, glass.WDA_NONE)])
        self.assertEqual(r.aff.get(1), glass.WDA_NONE)

    def test_other_juno_windows_are_excluded_too(self):
        r = opened(others=lambda: [1, 2, 0])
        run(r, 0.3)
        flags = [e for e in r.aff.history if e[1] == glass.WDA_EXCLUDE]
        self.assertEqual(sorted(h for h, _ in flags), [1, 2])
        self.assertEqual(r.aff.get(2), glass.WDA_NONE)

    def test_exclusion_cleared_when_the_grab_raises(self):
        r = opened()
        r.grab.fail = True
        run(r, 0.3)
        self.assertEqual(r.eng.counters['failures'], 1)
        self.assertEqual(r.aff.history[-1], (1, glass.WDA_NONE))
        self.assertEqual(r.aff.get(1), glass.WDA_NONE)

    def test_refused_exclusion_of_main_window_fails_the_job(self):
        r = rig()
        r.aff.set = lambda hwnd, flag: flag == glass.WDA_NONE
        self.assertFalse(r.eng.open(1, RECT, 1.0, SHAPE, hidden=False))
        self.assertTrue(r.eng.failed)

    def test_wrong_size_grab_counts_and_fails(self):
        r = rig()
        r.grab.thumb = lambda x, y, w, h, tw, th, halftone=False: bgra(3, 3)
        self.assertFalse(r.eng.open(1, RECT, 1.0, SHAPE))
        self.assertEqual(r.eng.counters['wrong_size'], 1)
        self.assertTrue(r.eng.failed)

    def test_first_capture_failing_sets_failed(self):
        r = rig()
        r.grab.fail = True
        self.assertFalse(r.eng.open(1, RECT, 1.0, SHAPE))
        self.assertTrue(r.eng.failed)
        self.assertEqual(r.eng.state, 'stopped')
        self.assertIsNone(r.eng.picture())

    def test_three_failures_stop_but_keep_the_last_picture(self):
        r = opened()
        r.grab.fail = True
        for _ in range(6):
            run(r, 0.3)
        self.assertEqual(r.eng.state, 'stopped')
        self.assertEqual(r.eng.counters['failures'], glass.MAX_FAILURES)
        self.assertIsNotNone(r.eng.picture())
        self.assertFalse(r.eng.failed)

    def test_a_success_resets_the_failure_count(self):
        r = opened()
        r.grab.fail = True
        run(r, 0.3)
        run(r, 0.3)
        self.assertEqual(r.eng.failures, 2)
        r.grab.fail = False
        run(r, 0.3)
        self.assertEqual(r.eng.failures, 0)
        self.assertNotEqual(r.eng.state, 'stopped')

    def test_close_drops_a_late_result(self):
        r = rig()
        original = r.grab.thumb

        def closing_thumb(*a, **k):
            r.eng.close()           # the window closes while the job is on the worker
            return original(*a, **k)
        r.grab.thumb = closing_thumb
        self.assertFalse(r.eng.open(1, RECT, 1.0, SHAPE))
        self.assertEqual(r.eng.counters['arrivals'], 0)
        self.assertEqual(r.eng.counters['stale'], 1)
        self.assertIsNone(r.eng.picture())
        self.assertEqual(r.eng.state, 'closed')

    def test_close_clears_state_and_loops(self):
        r = opened()
        run(r, 0.3)
        self.assertTrue(r.eng.blending())
        r.eng.close()
        self.assertEqual(r.eng.state, 'closed')
        self.assertFalse(r.eng.blending())
        self.assertNotIn(r.eng, r.frames.loops)
        self.assertIsNone(r.eng.picture())

    def test_close_without_release_keeps_the_picture(self):
        r = opened()
        r.eng.close(release=False)
        self.assertIsNotNone(r.eng.picture())

    def test_tick_after_close_does_nothing(self):
        r = opened()
        r.eng.close()
        jobs = r.eng.counters['jobs']
        step(r, 1.0)
        r.eng.event()
        self.assertEqual(r.eng.counters['jobs'], jobs)

    def test_picture_edge_zero_is_the_plain_wash(self):
        r = opened()
        r.eng.set_animating(False)
        self.assertIs(r.eng.picture(edge=0.0), r.eng.wash_shown)

    def test_picture_with_edge_is_cached_and_sized(self):
        r = opened()
        body = r.eng.picture()
        self.assertEqual((body.width(), body.height()), (136, 200))
        self.assertIs(r.eng.picture(), body)


class StateMachineTests(unittest.TestCase):
    def test_still_background_active_settling_idle(self):
        r = opened()
        self.assertEqual(r.eng.state, 'active')
        run(r, 0.4)                                     # cheap captures only: not still for 0.5 s yet
        self.assertEqual(r.eng.counters['full_grabs'], 0)
        settle(r)
        self.assertEqual(r.eng.counters['full_grabs'], 1)
        self.assertEqual([e['state'] for e in r.grab.log if e['kind'] == 'grab'], ['settling'])
        self.assertEqual(r.eng.state, 'idle')

    def test_settling_waits_at_least_settle_after(self):
        r = opened()
        start = r.clock.t
        while r.eng.counters['full_grabs'] == 0:
            step(r)
            self.assertLess(r.clock.t, start + 5)
        self.assertGreaterEqual(r.clock.t - start, glass.SETTLE_AFTER)

    def test_one_job_in_flight_at_most(self):
        r = rig()
        seen = []
        original = r.grab.thumb

        def spying(*a, **k):
            seen.append(r.eng.in_flight)
            return original(*a, **k)
        r.grab.thumb = spying
        r.eng.open(1, RECT, 1.0, SHAPE)
        run(r, 2.0)
        self.assertTrue(all(i is not None for i in seen))
        self.assertEqual(r.eng.counters['jobs'], len(seen))   # every job grabbed exactly once, and had the slot

    def test_idle_makes_checks_only_no_excluded_grabs(self):
        r = opened()
        settle(r)
        excluded, full = r.grab.count(excluded=True), r.eng.counters['full_grabs']
        run(r, 5.0)
        self.assertEqual(r.eng.state, 'idle')
        self.assertGreaterEqual(r.eng.counters['live_checks'], 3)
        self.assertEqual(r.grab.count(excluded=True), excluded)
        self.assertEqual(r.eng.counters['full_grabs'], full)
        self.assertTrue(all(e['halftone'] for e in r.grab.log[-3:]))

    def test_changed_check_goes_back_to_active(self):
        r = opened()
        settle(r)
        run(r, 2.0)                                     # baseline, then compares
        self.assertEqual(r.eng.state, 'idle')
        r.grab.value = 250
        for _ in range(30):
            step(r)
            if r.eng.state != 'idle':
                break
        self.assertEqual(r.eng.state, 'active')

    def test_event_in_idle_goes_back_to_active_and_captures(self):
        r = opened()
        settle(r)
        jobs, excluded = r.eng.counters['jobs'], r.grab.count(excluded=True)
        r.eng.event()
        self.assertEqual(r.eng.state, 'active')
        self.assertEqual(r.eng.counters['events'], 1)
        self.assertEqual(r.eng.counters['jobs'], jobs + 1)
        self.assertEqual(r.grab.count(excluded=True), excluded + 1)

    def test_event_while_active_restarts_the_still_clock(self):
        r = opened()
        run(r, 0.3)
        r.eng.event()
        self.assertEqual(r.eng.equal_streak, 0)
        self.assertEqual(r.eng.last_change, r.clock.t)

    def test_animating_postpones_idle_checks(self):
        r = opened()
        settle(r)
        r.eng.set_animating(True)
        checks = r.eng.counters['live_checks']
        run(r, 3.0)
        self.assertEqual(r.eng.counters['live_checks'], checks)
        r.eng.set_animating(False)
        run(r, 3.0)
        self.assertGreater(r.eng.counters['live_checks'], checks)

    def test_move_to_a_new_rect_captures_again(self):
        r = opened()
        settle(r)
        r.eng.move((10, 20, 136, 200))
        self.assertEqual(r.eng.rect, (10, 20, 136, 200))
        self.assertEqual(r.eng.state, 'active')
        r.eng.move((10, 20, 136, 200))                  # same rectangle: no second event
        self.assertEqual(r.eng.counters['events'], 1)

    def test_mark_dirty_skips_repainted_blocks(self):
        r = opened()
        settle(r)
        run(r, 1.5)                                     # baseline taken, compares are clean
        # Juno repaints the whole panel: logical block size is 4 * 16 / scale = 64
        r.eng.mark_dirty(0, 0, 136, 200)
        r.grab.value = lambda n: 250 if n % 2 else 120   # would read as a change
        before = r.eng.state
        step(r, 1.1)
        self.assertEqual((before, r.eng.state), ('idle', 'idle'))

    def test_mark_dirty_when_closed_is_ignored(self):
        r = rig()
        r.eng.mark_dirty(0, 0, 10, 10)
        self.assertEqual(r.eng.dirty_cur, set())

    def test_set_shape_requests_a_picture_of_the_new_shape(self):
        r = opened()
        settle(r)
        jobs = r.eng.counters['jobs']
        r.eng.set_shape(120)
        self.assertEqual(r.eng.counters['jobs'], jobs + 1)
        self.assertEqual(r.eng.latest_shape, 120)
        run(r, 0.5)
        self.assertEqual(r.eng.ring_shape, 120)
        self.assertEqual(r.eng.shape, 120)

    def test_set_shape_same_shape_does_nothing(self):
        r = opened()
        events = r.eng.counters['events']
        r.eng.set_shape(SHAPE)
        self.assertEqual(r.eng.counters['events'], events)

    def test_reduced_motion_makes_live_checks_then_one_settled_picture(self):
        r = opened(rig(reduced=True))
        settle(r)
        self.assertEqual(r.eng.counters['full_grabs'], 1)
        self.assertGreaterEqual(r.eng.counters['live_checks'], 1)

    def test_schedule_is_called_with_a_non_negative_delay(self):
        r = opened()
        self.assertTrue(r.due)
        self.assertTrue(all(d >= 0 for d in r.due))


class BlendTests(unittest.TestCase):
    def test_second_capture_starts_a_blend(self):
        r = opened()
        self.assertFalse(r.eng.blending())
        self.assertEqual(r.eng.counters['snaps'], 1)
        r.clock.t += 0.3
        r.eng.tick()
        self.assertTrue(r.eng.blending())
        self.assertIn(r.eng, r.frames.loops)

    def test_advance_steps_at_most_once_per_blend_step(self):
        r = opened()
        r.clock.t += 0.3
        r.eng.tick()
        r.eng.advance(0.0)
        self.assertEqual(r.eng.counters['blend_steps'], 0)
        r.clock.t += glass.BLEND_STEP * 1.1
        r.eng.advance(0.016)
        self.assertEqual(r.eng.counters['blend_steps'], 1)
        r.clock.t += glass.BLEND_STEP * 0.4
        r.eng.advance(0.016)
        self.assertEqual(r.eng.counters['blend_steps'], 1)

    def test_blend_snaps_after_blend_time_and_leaves_the_loops(self):
        r = opened()
        r.clock.t += 0.3
        r.eng.tick()
        snaps = r.eng.counters['snaps']
        r.clock.t += glass.BLEND_TIME
        r.eng.advance(0.016)
        self.assertEqual(r.eng.counters['snaps'], snaps + 1)
        self.assertFalse(r.eng.blending())
        self.assertNotIn(r.eng, r.frames.loops)
        self.assertIs(r.eng.wash_shown, r.eng.latest_wash)

    def test_reduced_motion_snaps_at_once(self):
        r = opened(rig(reduced=True))
        r.clock.t += 0.3
        r.eng._request('cheap')    # reduced mode asks for a cheap picture only through the settled path; take one directly
        self.assertGreaterEqual(r.eng.counters['snaps'], 2)
        self.assertFalse(r.eng.blending())
        self.assertNotIn(r.eng, r.frames.loops)

    def test_advance_without_a_blend_leaves_the_loops(self):
        r = opened()
        r.frames.add_loop(r.eng)
        r.eng.advance(0.016)
        self.assertNotIn(r.eng, r.frames.loops)

    def test_blend_moves_the_shown_picture_toward_the_latest(self):
        r = opened()
        r.grab.value = 250
        r.eng.set_appearance('dark')                    # keep the theme fixed: a blend, not a snap
        r.clock.t += 0.3
        r.eng.tick()
        before = r.eng.wash_shown.pixelColor(100, 100).red()
        r.clock.t += glass.BLEND_STEP * 2
        r.eng.advance(0.03)
        mid = r.eng.wash_shown.pixelColor(100, 100).red()
        r.clock.t += glass.BLEND_TIME
        r.eng.advance(0.03)
        after = r.eng.wash_shown.pixelColor(100, 100).red()
        self.assertNotEqual(before, after)
        self.assertTrue(min(before, after) <= mid <= max(before, after))


class ThemeEngineTests(unittest.TestCase):
    def test_first_capture_applies_a_new_appearance_at_once(self):
        r = rig()
        r.grab.value = 250
        r.eng.open(1, RECT, 1.0, SHAPE, system_theme='dark')
        self.assertEqual(r.themes, ['light'])
        self.assertEqual(r.eng.theme, 'light')

    def test_between_thresholds_keeps_the_system_theme(self):
        r = opened(system_theme='light')
        self.assertEqual(r.themes, [])
        self.assertEqual(r.eng.theme, 'light')

    def test_change_while_open_is_held_for_theme_hold(self):
        r = opened()
        r.grab.value = 250
        seen = {}
        for _ in range(40):
            r.clock.t += 0.3
            r.eng.tick()
            if r.themes:
                seen['at'] = r.clock.t
                break
        self.assertEqual(r.themes, ['light'])
        self.assertGreaterEqual(seen['at'] - 100.3, glass.THEME_HOLD - 1e-6)   # the first light capture was at 100.3

    def test_flip_back_before_the_hold_never_calls_on_theme(self):
        r = opened()
        r.grab.value = 250
        for _ in range(2):
            r.clock.t += 0.3
            r.eng.tick()
        r.grab.value = 120
        for _ in range(6):
            r.clock.t += 0.3
            r.eng.tick()
        self.assertEqual(r.themes, [])

    def test_fixed_setting_applies_at_once_on_change(self):
        r = opened()
        r.eng.set_appearance('light')
        self.assertEqual(r.eng.theme, 'light')          # the caller re-themes itself for a setting change
        run(r, 0.4)
        self.assertEqual(r.eng.theme, 'light')
        self.assertEqual(r.themes, [])


class RateEngineTests(unittest.TestCase):
    def test_mains_interval(self):
        r = opened()
        self.assertAlmostEqual(r.eng.interval(), 0.25)

    def test_on_battery_two_a_second(self):
        r = opened(rig(battery=True))
        self.assertAlmostEqual(r.eng.interval(), 0.5)

    def test_low_end_start_two_a_second_until_five_jobs(self):
        r = opened(rig(low_end=True))
        self.assertAlmostEqual(r.eng.interval(), 0.5)
        r.eng.job_ms = [1.0] * 5
        self.assertAlmostEqual(r.eng.interval(), 0.25)
        self.assertFalse(r.eng.low_end_start)

    def test_slow_jobs_obey_the_fifteen_percent_rule(self):
        r = rig()
        r.grab.cost = 0.1                               # each job takes 100 ms of the fake clock
        r.eng.open(1, RECT, 1.0, SHAPE)
        self.assertAlmostEqual(r.eng.job_ms[-1], 100.0, places=3)
        self.assertAlmostEqual(r.eng.interval(), 0.1 / glass.CPU_SHARE, places=3)

    def test_covered_wash_captures_at_a_quarter_rate(self):
        r = opened()
        r.eng.covered = True
        self.assertAlmostEqual(r.eng.interval(), 1.0)

    def test_slow_settled_picture_is_skipped(self):
        r = opened()
        r.eng.settled_ms = glass.SETTLED_MAX_MS + 1
        run(r, 1.0)
        self.assertEqual(r.eng.counters['full_grabs'], 0)
        self.assertEqual(r.eng.state, 'idle')


# ------------------------------------------------------------------ real worker thread
class WorkerThreadTests(unittest.TestCase):
    def test_first_picture_is_applied_inline_and_its_queued_copy_is_dropped(self):
        """Regression: with the real worker, open(wait=...) applies the first result inline; the same result also comes
        back through the queued signal and must not be counted again."""
        clock, aff = Clock(), FakeAff()
        grab = FakeGrab(aff, clock)
        eng = glass.Backdrop(grabber=lambda: grab, affinity=aff, sync=False, schedule=lambda d: None, frame_clock=Frames(), hook=False,
                             on_battery=lambda: False, low_end=lambda: False, reduced=lambda: False,
                             margin=M, width=PW, full_shape=FULL)
        self.addCleanup(eng.close)
        ok = eng.open(1, RECT, 1.0, SHAPE, wait=5.0)
        self.assertTrue(ok)
        self.assertIsNotNone(eng.picture())
        self.assertEqual(eng.counters['arrivals'], 1)
        self.assertIn('juno-glass', [t.name for t in threading.enumerate()])
        deadline = time.perf_counter() + 5.0
        while eng.consumed and time.perf_counter() < deadline:
            _APP.processEvents()
            time.sleep(0.01)
        _APP.processEvents()
        self.assertEqual(eng.consumed, set())
        self.assertEqual(eng.counters['arrivals'], 1)


if __name__ == '__main__':
    unittest.main()
