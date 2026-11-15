"""Liquid glass with a live background (M12): what is behind Alfred's panel, captured, washed and kept fresh.

The panel body is painted at a body alpha of about 0.80, so Windows shows the live desktop through the rest on every
frame. Under it sits a captured layer: a heavy colour wash of what is behind the panel (blur sigma 72) mixed with a
tint that keeps text readable, and a lens band at the edge. After the background has been still for 0.5 s one sharper
settled picture is blended in.

This module has no widget code. Pure functions take plain arrays and numbers (unit-testable without a display); the
``Backdrop`` engine takes its clock, grabber, display-affinity calls and worker mode as parameters, so a test can drive
it with fakes. Windows-only calls are guarded: the module imports, and plain mode works, on any platform and under
QT_QPA_PLATFORM=offscreen.

Threads: one daemon worker thread (``alfred-glass``, started on first use) sets and clears the display affinity, grabs
the screen and does every numpy, Pillow and QImage step. The GUI thread decides what to request, receives finished
results through a queued signal, advances the blend and paints. Every picture is in device pixels and carries the
screen's device pixel ratio."""
import ctypes
import logging
import math
import os
import queue
import threading
import time

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
from PySide6.QtCore import QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QConicalGradient, QImage, QPainter, QPainterPath, QPen

from . import power
from .ui_kit import DRIVER, PALETTES, RIM, Motion

log = logging.getLogger('alfred')
WINDOWS = os.name == 'nt'

# ------------------------------------------------------------------ constants (logical pixels unless said otherwise)
MARGIN, PANEL_W, RADIUS = 46, 780, 26   # mirror ui.py's M, W and R: the panel sits MARGIN inside its window
BODY_ALPHA = 0.80          # the body is painted at this alpha: Windows shows the live desktop through the rest
ADAPTIVE_ALPHA = True      # section 10: body alpha rises over busy backgrounds (False = a fixed 0.80, as approved)
BODY_ALPHA_MAX = 0.92
CONTRAST_FLOOR = True      # raise the body alpha (up to BODY_ALPHA_MAX) when the tint alone cannot hold the targets
# Detail score (levels) where the body alpha starts rising / reaches BODY_ALPHA_MAX. Measured on the spike's four
# backgrounds under the expanded panel, from the real quarter-size grab (StretchBlt): photo 0.9, white text page 22.3,
# black 0.0, coloured text 21.9 (every 4th pixel of an offscreen render gives 0.97, 49.6, 0.0, 42.3). With these the
# photo stays at 0.80 and the text pages reach 0.92.
DETAIL_S0, DETAIL_S1 = 6.0, 20.0
WASH_SIGMA, SETTLED_SIGMA, LENS_SIGMA = 72.0, 12.0, 2.5
SAT, MAG = 1.4, 1.03       # saturation boost, slight magnification of what is behind
BAND, AMP, POWER, SIGN = 26, 22.0, 3.0, -1  # lens band width, shift, profile and direction
TINT_EDGE = 0.9            # the lens band is tinted 10% less than the body
TINT_MAX = 0.97
TARGET_PRIMARY, TARGET_SECONDARY = 7.5, 5.0
TINT = {name: (PALETTES[name]['body'].red(), PALETTES[name]['body'].green(), PALETTES[name]['body'].blue()) for name in PALETTES}
LIGHT_ABOVE, DARK_BELOW = 0.60, 0.45  # mean luminance of the background: appearance thresholds
THEME_HOLD = 1.0           # seconds a new appearance must hold before it is applied
SETTLE_AFTER = 0.5         # seconds still before the settled picture
# The running blend: time constant, swap after BLEND_TIME, at most one step (and one panel repaint) per BLEND_STEP.
# Each step repaints the whole see-through panel, the largest CPU cost of the glass: 30 steps a second, not 60.
BLEND_TAU, BLEND_TIME, BLEND_STEP = 0.095, 0.4, 1 / 30
FIRST_WAIT = 0.08          # show_panel waits this long for the first picture
CHANGE_LEVELS = 4.0        # a block's mean colour must move this much (after gain) to count as a change
BLOCK = 16                 # change-check block, in quarter-size pixels (64 device pixels)
TINT_TAU_FALL, TINT_TAU_RISE, ALPHA_TAU = 0.5, 0.15, 0.5
CPU_SHARE = 0.15           # the worker may use at most 15% of one core
SETTLED_MAX_MS = 150.0     # a settled job slower than this is skipped (the wash stays)
COVERED = 0.90             # smoothed tint at or above this: the wash is covered, capture at a quarter of the rate
MAX_FAILURES = 3
WDA_NONE, WDA_EXCLUDE = 0x0, 0x11
SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
COLORONCOLOR, HALFTONE = 3, 4


# ------------------------------------------------------------------ small pure helpers
def scaled(value, scale):
    return value * scale


def device_size(width, height, scale):
    return int(round(width * scale)), int(round(height * scale))


def panel_rect(scale, shape, margin=MARGIN, width=PANEL_W):
    """The panel's rectangle (x, y, w, h) in device pixels for a panel ``shape`` logical pixels tall."""
    return (margin * scale, margin * scale, width * scale, shape * scale)


def quarter(size):
    return max(1, size[0] // 4), max(1, size[1] // 4)


def colour(c):
    """(r, g, b, alpha 0..1) of a QColor or a tuple."""
    if isinstance(c, QColor):
        return (c.red(), c.green(), c.blue(), c.alphaF())
    return tuple(c) if len(c) == 4 else (c[0], c[1], c[2], 1.0)


_LIN = None


def _lum_u8(u8):
    global _LIN
    if _LIN is None:
        c = np.arange(256, dtype=np.float64) / 255.0
        _LIN = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * _LIN[u8[..., 0]] + 0.7152 * _LIN[u8[..., 1]] + 0.0722 * _LIN[u8[..., 2]]


def contrast(background_rgb, text):
    """WCAG contrast of ``text`` (QColor or (r, g, b, a)) drawn over each background pixel (N x 3, 0..255)."""
    r, g, b, a = colour(text)
    fg = np.array([r, g, b], dtype=np.float32)
    bg = np.clip(np.asarray(background_rgb, dtype=np.float32), 0, 255)
    eff = np.clip(a * fg + (1 - a) * bg, 0, 255).astype(np.uint8)
    l1, l2 = _lum_u8(eff), _lum_u8(bg.astype(np.uint8))
    return (np.maximum(l1, l2) + 0.05) / (np.minimum(l1, l2) + 0.05)


def luma(bgra, rect):
    """Mean luminance 0..1 of a BGRA picture inside ``rect`` (x, y, w, h in that picture's pixels)."""
    x0, y0, w, h = (int(round(v)) for v in rect)
    reg = bgra[max(0, y0):max(0, y0 + h), max(0, x0):max(0, x0 + w), :3].astype(np.float32)
    if reg.size == 0:
        reg = bgra[..., :3].astype(np.float32)
    return float((0.114 * reg[..., 0] + 0.587 * reg[..., 1] + 0.299 * reg[..., 2]).mean() / 255.0)


def resolve_theme(lum, current, setting='system'):
    """Appearance for a background of mean luminance ``lum``: light above 0.60, dark below 0.45, otherwise unchanged.
    A fixed setting ('dark' or 'light') always wins."""
    if setting in ('dark', 'light'):
        return setting
    if lum > LIGHT_ABOVE:
        return 'light'
    if lum < DARK_BELOW:
        return 'dark'
    return current


class AppearanceHold:
    """A change of the automatic appearance must hold for THEME_HOLD seconds before it is applied."""

    def __init__(self, hold=THEME_HOLD):
        self.hold, self.candidate, self.since = hold, None, 0.0

    def update(self, want, current, now):
        if want == current:
            self.candidate = None
            return current
        if self.candidate != want:
            self.candidate, self.since = want, now
        return want if now - self.since >= self.hold else current


def smooth(previous, target, dt, tau_rise, tau_fall):
    """Exponential smoothing over measured time; a rising value uses ``tau_rise``, a falling one ``tau_fall``."""
    if previous is None:
        return target
    tau = tau_rise if target > previous else tau_fall
    return previous + (target - previous) * (1 - math.exp(-max(0.0, dt) / tau))


def detail_score(bgra, rect):
    """How busy a picture is inside ``rect``: mean absolute difference (0..255 levels) between its luminance and a
    2-pixel box blur of it. Flat pictures score near 0, small text scores high."""
    x0, y0, w, h = (int(round(v)) for v in rect)
    reg = np.ascontiguousarray(bgra[max(0, y0):max(0, y0 + h), max(0, x0):max(0, x0 + w), :3])
    if reg.size == 0:
        return 0.0
    lum = (0.114 * reg[..., 0] + 0.587 * reg[..., 1] + 0.299 * reg[..., 2]).astype(np.uint8)
    img = Image.fromarray(lum, 'L')
    blur = np.asarray(img.filter(ImageFilter.BoxBlur(2))).astype(np.float32)
    return float(np.abs(lum.astype(np.float32) - blur).mean())


def body_alpha_for(score, s0=DETAIL_S0, s1=DETAIL_S1, adaptive=None):
    """Body alpha for a detail score: BODY_ALPHA up to s0, rising linearly to BODY_ALPHA_MAX at s1."""
    if not (ADAPTIVE_ALPHA if adaptive is None else adaptive):
        return BODY_ALPHA
    t = min(1.0, max(0.0, (score - s0) / max(1e-6, s1 - s0)))
    return BODY_ALPHA + (BODY_ALPHA_MAX - BODY_ALPHA) * t


def rank_filter(arr, size, pick):
    """Square max (``pick`` = np.maximum) or min (np.minimum) filter of an (h, w, c) array, ``size`` odd, edges
    replicated: what Pillow's MaxFilter / MinFilter give, done separably (Pillow's rank filter is slow at this size)."""
    r = size // 2
    a = np.pad(arr, ((r, r), (r, r), (0, 0)), mode='edge')
    h, w = arr.shape[:2]
    rows = a[:, r:r + w].copy()
    for d in range(1, r + 1):
        rows = pick(rows, pick(a[:, r - d:r - d + w], a[:, r + d:r + d + w]))
    out = rows[r:r + h].copy()
    for d in range(1, r + 1):
        out = pick(out, pick(rows[r - d:r - d + h], rows[r + d:r + d + h]))
    return out


def tint_for(live_q, wash_q, theme, body_alpha, rect_q, palette=None, scale=1.0, tint_max=TINT_MAX,
             target_primary=TARGET_PRIMARY, target_secondary=TARGET_SECONDARY, report=False):
    """The smallest tint (at most ``tint_max``) at which the worst 2% of the panel still gives primary text
    ``target_primary`` and secondary text ``target_secondary`` against
    body_alpha * (wash mixed with tint) + (1 - body_alpha) * the most extreme live pixel nearby
    (brightest in the dark appearance, darkest in the light one).
    live_q, wash_q: quarter-size PIL RGB pictures of the window; rect_q: the panel in quarter-size pixels.
    ``report``: return (tint, met) where met says whether the targets hold at that tint."""
    palette = palette or PALETTES[theme]
    size = 5 if scale < 1.25 else 7
    ext = rank_filter(np.asarray(live_q), size, np.maximum if theme == 'dark' else np.minimum)
    x0, y0, w, h = rect_q
    inset = max(1, int(round(4 * scale)))
    r0, r1 = int(y0) + inset, int(y0 + h) - inset
    c0, c1 = int(x0) + inset, int(x0 + w) - inset
    if r1 <= r0:
        r0, r1 = int(y0), max(int(y0) + 1, int(y0 + h))
    if c1 <= c0:
        c0, c1 = int(x0), max(int(x0) + 1, int(x0 + w))
    sl = (slice(r0, r1, 3), slice(c0, c1, 3))
    live = ext[sl].astype(np.float32).reshape(-1, 3)
    wash = np.asarray(wash_q).astype(np.float32)[sl].reshape(-1, 3)
    if live.size == 0:
        return (tint_max, True) if report else tint_max
    tint = np.array(TINT[theme], dtype=np.float32)

    def meets(t):
        eff = body_alpha * ((1 - t) * wash + t * tint) + (1 - body_alpha) * live
        return (np.percentile(contrast(eff, palette['text']), 2) >= target_primary and
                np.percentile(contrast(eff, palette['secondary']), 2) >= target_secondary)
    lo, hi = 0.0, tint_max
    for _ in range(6):
        mid = 0.5 * (lo + hi)
        if meets(mid):
            hi = mid
        else:
            lo = mid
    if report:
        return hi, (hi < tint_max or meets(tint_max))
    return hi


def block_means(bgra, block=BLOCK):
    """Mean colour (B, G, R) of each ``block`` x ``block`` block of a picture: array (rows, cols, 3)."""
    h, w = bgra.shape[:2]
    rows, cols = h // block, w // block
    if rows == 0 or cols == 0:
        return bgra[..., :3].reshape(1, 1, -1, 3).mean(axis=2).astype(np.float32)
    a = bgra[:rows * block, :cols * block, :3].astype(np.float32)
    return a.reshape(rows, block, cols, block, 3).mean(axis=(1, 3))


def gain_map(shape, panel_q, body_alpha, block=BLOCK):
    """Gain per block: inside the panel the desktop is seen at (1 - body_alpha), so 1 / (1 - body_alpha); outside
    (the shadow margin, and below the bar while collapsed) it is seen almost directly: 1. A block counts as inside
    when its centre is."""
    rows, cols = shape
    gains = np.ones((rows, cols), np.float32)
    x0, y0, w, h = panel_q
    cy = (np.arange(rows) + 0.5) * block
    cx = (np.arange(cols) + 0.5) * block
    inside = ((cy >= y0) & (cy < y0 + h))[:, None] & ((cx >= x0) & (cx < x0 + w))[None, :]
    gains[inside] = 1.0 / max(1e-3, 1.0 - body_alpha)
    return gains


def changed(previous, current, gains=None, skip=None, levels=CHANGE_LEVELS):
    """True when some compared block's mean colour moved by at least ``levels`` after gain. ``skip``: boolean array of
    blocks not to compare (Alfred repainted them)."""
    if previous is None or current is None or previous.shape != current.shape:
        return True
    diff = np.abs(current - previous).max(axis=2)
    if gains is not None:
        diff = diff * gains
    if skip is not None:
        diff = np.where(skip, 0.0, diff)
    return bool((diff >= levels).any())


def base_rate(on_battery=False, covered=False, low_end_start=False):
    """Captures a second while the background moves (section 7)."""
    if covered:
        return 1.0
    if on_battery or low_end_start:
        return 2.0
    return 4.0


def still_interval(on_battery=False):
    """Seconds between two still-background checks while idle."""
    return 2.0 if on_battery else 1.0


def capture_interval(rate, job_seconds=0.0, share=CPU_SHARE):
    """interval = max(1 / base_rate, job_time / 0.15): the worker may use at most 15% of one core."""
    return max(1.0 / rate, (job_seconds or 0.0) / share)


def blend_factor(dt, tau=BLEND_TAU):
    """How far a running blend moves toward the newest picture in ``dt`` seconds."""
    return 1.0 - math.exp(-max(0.0, dt) / tau)


# ------------------------------------------------------------------ lens maps (cached per size, shape, scale)
_maps = {}
_maps_lock = threading.Lock()


def lens_maps(size, rect, radius, band, amp, div, sw, sh):
    """Where each pixel of the lens band (a ``band``-wide ring inside the rounded rectangle ``rect``, device pixels)
    samples its source (a picture ``sw`` x ``sh`` at 1/``div`` size): bilinear, magnified, cube profile. Only band pixels
    are computed and kept (int32 indices, float32 weights)."""
    key = (size, tuple(round(v, 2) for v in rect), radius, band, amp, div, sw, sh)
    found = _maps.get(key)
    if found is not None:
        return found
    W, H = size
    x0, y0, w, h = rect
    B, A = float(band), float(amp)
    xa = np.arange(max(0, int(math.floor(x0)) - 1), min(W, int(math.ceil(x0 + w)) + 1))
    ya = np.arange(max(0, int(math.floor(y0)) - 1), min(H, int(math.ceil(y0 + h)) + 1))
    edge = B + 2
    rim_rows = ya[(ya < y0 + edge) | (ya >= y0 + h - edge)]
    mid_rows = ya[(ya >= y0 + edge) & (ya < y0 + h - edge)]
    side_cols = xa[(xa < x0 + edge) | (xa >= x0 + w - edge)]
    gy1, gx1 = np.meshgrid(rim_rows, xa, indexing='ij')
    gy2, gx2 = np.meshgrid(mid_rows, side_cols, indexing='ij')
    ys = np.concatenate([gy1.ravel(), gy2.ravel()]).astype(np.float32)
    xs = np.concatenate([gx1.ravel(), gx2.ravel()]).astype(np.float32)
    cx, cy = x0 + w / 2.0, y0 + h / 2.0
    px, py = xs + 0.5 - cx, ys + 0.5 - cy
    qx, qy = np.abs(px) - (w / 2.0 - radius), np.abs(py) - (h / 2.0 - radius)
    ox, oy = np.maximum(qx, 0), np.maximum(qy, 0)
    outside = np.hypot(ox, oy)
    sdf = outside + np.minimum(np.maximum(qx, qy), 0) - radius
    d = -sdf
    keep = (d > -0.5) & (d < B)
    xs, ys, px, py, qx, qy, ox, oy, outside, sdf, d = (v[keep] for v in (xs, ys, px, py, qx, qy, ox, oy, outside, sdf, d))
    corner = (qx > 0) & (qy > 0)
    inv = 1.0 / np.maximum(outside, 1e-6)
    nx = np.where(corner, ox * inv, np.where(qx > qy, 1.0, 0.0)) * np.sign(px)
    ny = np.where(corner, oy * inv, np.where(qx > qy, 0.0, 1.0)) * np.sign(py)
    s = A * (1 - np.clip(d, 0, B) / B) ** POWER
    srcx = (xs + 0.5) - SIGN * nx * s
    srcy = (ys + 0.5) - SIGN * ny * s
    srcx = cx + (srcx - cx) / MAG
    srcy = cy + (srcy - cy) / MAG
    u = np.clip(srcx / div - 0.5, 0, sw - 1.001)
    v = np.clip(srcy / div - 0.5, 0, sh - 1.001)
    xi, yi = np.floor(u).astype(np.int32), np.floor(v).astype(np.int32)
    fx, fy = (u - xi).astype(np.float32), (v - yi).astype(np.float32)
    out = dict(idx=(ys.astype(np.int32) * W + xs.astype(np.int32)), i00=(yi * sw + xi).astype(np.int32),
               w=np.stack([(1 - fx) * (1 - fy), fx * (1 - fy), (1 - fx) * fy, fx * fy], axis=1).astype(np.float32), sw=sw,
               near=(np.minimum(np.rint(v), sh - 1).astype(np.int32) * sw + np.minimum(np.rint(u), sw - 1).astype(np.int32)),
               alpha=((np.clip(1 - d / B, 0, 1) ** 2) * np.clip(0.5 - sdf, 0, 1)).astype(np.float32),
               edge_f=(TINT_EDGE + (1 - TINT_EDGE) * np.clip(d / B, 0, 1)).astype(np.float32),
               strips=ring_strips(rect, band))
    with _maps_lock:
        _maps[key] = out
    return out


def ring_strips(rect, band):
    """Four device-pixel rectangles (x, y, w, h) that hold every pixel of the lens band of ``rect``."""
    x0, y0, w, h = rect
    l, t = int(math.floor(x0)) - 1, int(math.floor(y0)) - 1
    r, b = int(math.ceil(x0 + w)) + 1, int(math.ceil(y0 + h)) + 1
    e = int(math.ceil(band)) + 2
    strips = [(l, t, r - l, e), (l, b - e, r - l, e)]
    if b - t > 2 * e:
        strips += [(l, t + e, e, b - t - 2 * e), (r - e, t + e, e, b - t - 2 * e)]
    return strips


# ------------------------------------------------------------------ compose
def _bgra_image(arr, scale):
    h, w = arr.shape[:2]
    img = QImage(np.ascontiguousarray(arr).data, w, h, w * 4, QImage.Format_ARGB32_Premultiplied).copy()
    img.setDevicePixelRatio(scale)
    return img


def compose(src, kind, theme, size, scale, shape, body_alpha=BODY_ALPHA, tint=None, margin=MARGIN, width=PANEL_W,
            full_shape=600, radius=RADIUS, timings=None, measured=None, live_src=None):
    """Compose one capture into the panel's pictures.

    src: BGRA uint8 array; kind 'cheap' = a quarter-size capture (size // 4) composed as the slow colour wash,
    'full' = a full-size capture composed as the settled picture (blur sigma 12, sharp lens band).
    size: the window's device size; scale: its device pixel ratio; shape: the panel's resting height (logical) the lens
    band is made for. tint: a fixed tint, or a callable taking the raw tint and returning the one to use (smoothing),
    or None for the raw tint.
    Returns dict(wash=QImage opaque at the window's device size, ring=QImage premultiplied (lens band only, for
    ``shape``), strips, tint_raw, tint, luma, detail, theme, body_alpha (raised when the tint alone cannot hold the
    contrast targets), contrast_met). ``measured``: (luma, detail) already computed. ``live_src`` (kind 'full'): the
    quarter-size grab taken with it, for the tint and detail (so they match the wash's; default: every 4th pixel)."""
    T = time.perf_counter
    tm = timings if timings is not None else {}
    t0 = T()
    W, H = size
    QW, QH = quarter(size)
    src = np.ascontiguousarray(src)
    if kind == 'full':
        if src.shape[:2] != (H, W):
            raise ValueError('capture is %r, expected %r' % (src.shape[:2], (H, W)))
        rgb = Image.frombuffer('RGB', (W, H), src, 'raw', 'BGRX', 0, 1)
        q = rgb.reduce(4).crop((0, 0, QW, QH))
        half = rgb.reduce(2)
        live = np.ascontiguousarray(live_src if live_src is not None else src[::4, ::4][:QH, :QW])
        div = 2
    else:
        if src.shape[:2] != (QH, QW):
            raise ValueError('capture is %r, expected %r' % (src.shape[:2], (QH, QW)))
        q = Image.frombuffer('RGB', (QW, QH), src, 'raw', 'BGRX', 0, 1)
        live = src
        div = 4
    live_q = q if kind != 'full' else Image.frombuffer('RGB', (live.shape[1], live.shape[0]), live, 'raw', 'BGRX', 0, 1)
    shape_q = tuple(v / 4.0 for v in panel_rect(scale, shape, margin, width))
    lum, detail = measured if measured is not None else (luma(live, shape_q), detail_score(live, shape_q))
    if kind == 'full':
        bq = ImageEnhance.Color(q.filter(ImageFilter.GaussianBlur(SETTLED_SIGMA * scale / 4.0))).enhance(SAT)
    else:
        wq = q.reduce(2).filter(ImageFilter.GaussianBlur(WASH_SIGMA * scale / 8.0)).resize((QW, QH), Image.BILINEAR)
        bq = ImageEnhance.Color(wq).enhance(SAT)
    bq0 = bq
    fx0, fy0, fw, fh = panel_rect(scale, full_shape, margin, width)  # magnified about the full panel: height-independent
    c0x, c0y = (fx0 + fw / 2.0) / 4.0, (fy0 + fh / 2.0) / 4.0
    m = MAG
    bq = bq.transform((QW, QH), Image.AFFINE, (1 / m, 0, c0x * (1 - 1 / m), 0, 1 / m, c0y * (1 - 1 / m)), Image.BILINEAR)
    tm['blur'] = (T() - t0) * 1000
    t0 = T()
    raw, met = tint_for(live_q, bq, theme, body_alpha, shape_q, scale=scale, report=True)
    while CONTRAST_FLOOR and not met and body_alpha < BODY_ALPHA_MAX - 1e-6:
        # Even the strongest tint cannot hold the targets (a fixed appearance against its opposite background): only a
        # less see-through body can. Raised at once, up to BODY_ALPHA_MAX.
        body_alpha = min(BODY_ALPHA_MAX, body_alpha + 0.02)
        raw, met = tint_for(live_q, bq, theme, body_alpha, shape_q, scale=scale, report=True)
    used = tint(raw) if callable(tint) else (raw if tint is None else float(tint))
    used = min(TINT_MAX, max(0.0, used))
    tm['tint'] = (T() - t0) * 1000
    t0 = T()
    tcol = np.array(TINT[theme], dtype=np.float32)
    tq = np.ascontiguousarray((np.asarray(bq).astype(np.float32) * (1 - used) + tcol * used).astype(np.uint8))
    wash = QImage(tq.data, QW, QH, QW * 3, QImage.Format_RGB888).scaled(W, H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation) \
        .convertToFormat(QImage.Format_ARGB32_Premultiplied)
    wash.setDevicePixelRatio(scale)
    tm['wash'] = (T() - t0) * 1000
    t0 = T()
    srect = panel_rect(scale, shape, margin, width)
    if kind == 'full':
        source, sdiv = half, 2
    else:
        source, sdiv = bq0, 4
    sw_, sh_ = source.size
    maps = lens_maps(size, srect, radius * scale, BAND * scale, AMP * scale, sdiv, sw_, sh_)
    if kind == 'full':  # the sharp lens: bilinear samples of a lightly blurred half-size picture
        sharp = source.filter(ImageFilter.BoxBlur(LENS_SIGMA * scale / sdiv * 0.65))
        pix = np.asarray(sharp, dtype=np.float32).reshape(-1, 3)
        i00, swm, wts = maps['i00'], maps['sw'], maps['w']
        vals = pix[i00] * wts[:, 0:1] + pix[i00 + 1] * wts[:, 1:2] + pix[i00 + swm] * wts[:, 2:3] + pix[i00 + swm + 1] * wts[:, 3:4]
    else:  # the wash is so smooth (sigma 18 quarter-size pixels) that the nearest sample is the same picture, 4x cheaper
        vals = np.asarray(source, dtype=np.float32).reshape(-1, 3)[maps['near']]
    gray = vals @ np.array([0.299, 0.587, 0.114], np.float32)
    ab = (used * maps['edge_f'])[:, None]
    ar = maps['alpha']
    vals = gray[:, None] + SAT * (vals - gray[:, None])
    vals = (vals * (1 - ab) + tcol[None, :] * ab) * ar[:, None]
    ring_img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
    ring_img.fill(0)
    ring = np.frombuffer(ring_img.bits(), np.uint8).reshape(H, ring_img.bytesPerLine() // 4, 4)[:, :W].reshape(-1, 4) \
        if ring_img.bytesPerLine() == W * 4 else None
    if ring is None:  # never for 32-bit pictures (rows are always whole words); kept for safety
        ring = np.zeros((H * W, 4), np.uint8)
    out = np.empty((len(ar), 4), np.uint8)
    out[:, :3] = np.clip(vals[:, ::-1], 0, 255)  # RGB into BGRA order
    out[:, 3] = np.clip(ar * 255, 0, 255)
    ring[maps['idx']] = out
    if ring_img.bytesPerLine() != W * 4:
        ring_img = _bgra_image(ring.reshape(H, W, 4), scale)
    ring_img.setDevicePixelRatio(scale)
    tm['ring'] = (T() - t0) * 1000
    return dict(wash=wash, ring=ring_img, strips=maps['strips'], shape=shape, tint_raw=raw, tint=used, luma=lum, detail=detail,
                theme=theme, body_alpha=body_alpha, contrast_met=met)


def warm(size, scale, shapes=(62, 600), margin=MARGIN, width=PANEL_W, radius=RADIUS, themes=('dark', 'light')):
    """Build the lens maps and edge layers for one screen ahead of the first showing (76 ms once per size at 100%)."""
    QW, QH = quarter(size)
    half = ((size[0] + 1) // 2, (size[1] + 1) // 2)
    for shape in shapes:
        rect = panel_rect(scale, shape, margin, width)
        lens_maps(size, rect, radius * scale, BAND * scale, AMP * scale, 4, QW, QH)
        lens_maps(size, rect, radius * scale, BAND * scale, AMP * scale, 2, half[0], half[1])
        for theme in themes:
            edge_layer(size, scale, shape, theme, margin, width, radius)


# ------------------------------------------------------------------ edge layers (rim, inner glow; cached per shape)
_layers = {}


def _wrap(a):
    return (a + 180) % 360 - 180


def conical(center, color, base, peaks, width):
    """A conical gradient of ``color`` whose alpha is ``base`` plus gaussian peaks (angle, amplitude) ``width`` wide."""
    g = QConicalGradient(center, 0)
    for k in range(0, 361, 6):
        a = base + sum(amp * math.exp(-(_wrap(k - ang) / width) ** 2) for ang, amp in peaks)
        c = QColor(color)
        c.setAlphaF(max(0.0, min(1.0, a)))
        g.setColorAt(min(1.0, k / 360.0), c)
    return g


def edge_layer(size, scale, shape, theme, margin=MARGIN, width=PANEL_W, radius=RADIUS):
    """The bevel rim of a resting panel ``shape`` logical pixels tall: bright hairline, inner glow strongest top-left,
    dark inner line opposite (and a faint outline in the light appearance). A transparent picture of the whole window
    at device size; everything in it lies within a few pixels of the panel's edge."""
    key = (size, round(scale, 3), shape, theme, margin, width, radius)
    found = _layers.get(key)
    if found is not None:
        return found
    img = QImage(size[0], size[1], QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(scale)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(Qt.NoBrush)
    rect = QRectF(margin, margin, width, shape)
    center = rect.center()
    k = 1.0 if theme == 'dark' else 0.8
    peaks = [(135, RIM['tl'] * k), (315, RIM['br'] * k)]
    white = QColor(255, 255, 255)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.setClipPath(path)
    for pen, sc in ((16, 0.05), (11, 0.07), (6, 0.09)):  # soft inner glow, 6 to 8 px visible
        p.setPen(QPen(QBrush(conical(center, white, 0.0, [(a, v * sc) for a, v in peaks], RIM['width'] * 1.3)), pen))
        p.drawRoundedRect(rect, radius, radius)
    p.setClipping(False)
    p.setPen(QPen(QBrush(conical(center, white, RIM['base'], peaks, RIM['width'])), RIM['pen']))
    p.drawRoundedRect(rect.adjusted(0.6, 0.6, -0.6, -0.6), radius - 0.6, radius - 0.6)
    dl = RIM['dark_line'] * (1.0 if theme == 'dark' else 0.6)
    p.setPen(QPen(QBrush(conical(QPointF(center), QColor(0, 0, 0), dl * 0.25, [(45, dl), (225, dl)], 60)), 1.0))
    p.drawRoundedRect(rect.adjusted(1.9, 1.9, -1.9, -1.9), radius - 1.9, radius - 1.9)
    if theme == 'light':
        p.setPen(QPen(QColor(0, 0, 0, 40), 1.0))
        p.drawRoundedRect(rect.adjusted(-0.5, -0.5, 0.5, 0.5), radius + 0.5, radius + 0.5)
    p.end()
    _layers[key] = img
    return img


# ------------------------------------------------------------------ Windows: capture, affinity, mode checks
if WINDOWS:
    from ctypes import wintypes as _wt

    class _BmpInfo(ctypes.Structure):
        _fields_ = [('biSize', _wt.DWORD), ('biWidth', _wt.LONG), ('biHeight', _wt.LONG), ('biPlanes', _wt.WORD),
                    ('biBitCount', _wt.WORD), ('biCompression', _wt.DWORD), ('biSizeImage', _wt.DWORD), ('biXPels', _wt.LONG),
                    ('biYPels', _wt.LONG), ('biClrUsed', _wt.DWORD), ('biClrImportant', _wt.DWORD)]

    class _HighContrast(ctypes.Structure):
        _fields_ = [('cbSize', _wt.UINT), ('dwFlags', _wt.DWORD), ('lpszDefaultScheme', ctypes.c_wchar_p)]

    class _MonitorInfo(ctypes.Structure):
        _fields_ = [('cbSize', _wt.DWORD), ('rcMonitor', _wt.RECT), ('rcWork', _wt.RECT), ('dwFlags', _wt.DWORD),
                    ('szDevice', ctypes.c_wchar * 32)]

_api = None


def _win():
    """ctypes prototypes, made once (Windows only)."""
    global _api
    if _api is None:
        H = ctypes.c_void_p
        u = ctypes.WinDLL('user32', use_last_error=True)
        g = ctypes.WinDLL('gdi32', use_last_error=True)
        u.GetDC.restype = H
        u.GetDC.argtypes = [H]
        u.ReleaseDC.argtypes = [H, H]
        u.SetWindowDisplayAffinity.argtypes = [H, _wt.DWORD]
        u.SetWindowDisplayAffinity.restype = _wt.BOOL
        u.GetWindowDisplayAffinity.argtypes = [H, ctypes.POINTER(_wt.DWORD)]
        u.GetWindowDisplayAffinity.restype = _wt.BOOL
        u.GetAncestor.argtypes = [H, _wt.UINT]
        u.GetAncestor.restype = H
        u.GetWindowRect.argtypes = [H, ctypes.POINTER(_wt.RECT)]
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.SystemParametersInfoW.argtypes = [_wt.UINT, _wt.UINT, ctypes.c_void_p, _wt.UINT]
        u.SetWinEventHook.restype = H
        u.UnhookWinEvent.argtypes = [H]
        g.CreateCompatibleDC.restype = H
        g.CreateCompatibleDC.argtypes = [H]
        g.CreateDIBSection.restype = H
        g.CreateDIBSection.argtypes = [H, ctypes.c_void_p, _wt.UINT, ctypes.POINTER(ctypes.c_void_p), H, _wt.DWORD]
        g.SelectObject.restype = H
        g.SelectObject.argtypes = [H, H]
        g.BitBlt.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, ctypes.c_int, ctypes.c_int, _wt.DWORD]
        g.StretchBlt.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, _wt.DWORD]
        g.SetStretchBltMode.argtypes = [H, ctypes.c_int]
        g.DeleteDC.argtypes = [H]
        g.DeleteObject.argtypes = [H]
        _api = (u, g)
    return _api


class Grabber:
    """Copies screen rectangles (device pixels, the whole virtual screen) into reusable top-down 32-bit DIBs. Use one
    per thread. ``grab`` is a BitBlt at full size; ``thumb`` a StretchBlt to a smaller size (COLORONCOLOR, or HALFTONE
    for the still-background check). Both return a BGRA copy and raise OSError when Windows refuses."""

    def __init__(self):
        self.user32, self.gdi32 = _win()
        self.screen = self.user32.GetDC(None)
        self.dibs = {}

    def _dib(self, w, h):
        found = self.dibs.get((w, h))
        if found is None:
            for key in [k for k in self.dibs if k != (w, h)][:max(0, len(self.dibs) - 2)]:  # keep at most three sizes
                self._free(key)
            dc = self.gdi32.CreateCompatibleDC(self.screen)
            bits = ctypes.c_void_p()
            bmp = self.gdi32.CreateDIBSection(self.screen, ctypes.byref(_BmpInfo(ctypes.sizeof(_BmpInfo), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)),
                                              0, ctypes.byref(bits), None, 0)
            if not dc or not bmp or not bits.value:
                raise OSError('CreateDIBSection failed %d' % ctypes.get_last_error())
            self.gdi32.SelectObject(dc, bmp)
            arr = np.ctypeslib.as_array((ctypes.c_ubyte * (w * h * 4)).from_address(bits.value)).reshape(h, w, 4)
            found = self.dibs[(w, h)] = (dc, bmp, arr)
        return found

    def _free(self, key):
        dc, bmp, _ = self.dibs.pop(key)
        self.gdi32.DeleteDC(dc)
        self.gdi32.DeleteObject(bmp)

    def grab(self, x, y, w, h):
        dc, _, arr = self._dib(w, h)
        if not self.gdi32.BitBlt(dc, 0, 0, w, h, self.screen, x, y, SRCCOPY | CAPTUREBLT):
            raise OSError('BitBlt failed %d' % ctypes.get_last_error())
        out = arr.copy()
        out[..., 3] = 255
        return out

    def thumb(self, x, y, w, h, tw, th, halftone=False):
        dc, _, arr = self._dib(tw, th)
        self.gdi32.SetStretchBltMode(dc, HALFTONE if halftone else COLORONCOLOR)
        if not self.gdi32.StretchBlt(dc, 0, 0, tw, th, self.screen, x, y, w, h, SRCCOPY | CAPTUREBLT):
            raise OSError('StretchBlt failed %d' % ctypes.get_last_error())
        out = arr.copy()
        out[..., 3] = 255
        return out

    def close(self):
        for key in list(self.dibs):
            self._free(key)
        if self.screen:
            self.user32.ReleaseDC(None, self.screen)
            self.screen = None


class Affinity:
    """SetWindowDisplayAffinity / GetWindowDisplayAffinity (a test passes a fake with the same two methods)."""

    def set(self, hwnd, flag):
        return bool(WINDOWS and _win()[0].SetWindowDisplayAffinity(hwnd, flag))

    def get(self, hwnd):
        if not WINDOWS:
            return 0
        value = _wt.DWORD(0)
        return int(value.value) if _win()[0].GetWindowDisplayAffinity(hwnd, ctypes.byref(value)) else 0


def transparency_effects():
    """Windows "Transparency effects" (Personalize\\EnableTransparency); True when unknown."""
    if not WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
            return winreg.QueryValueEx(key, 'EnableTransparency')[0] != 0
    except OSError:
        return True


def high_contrast():
    if not WINDOWS:
        return False
    try:
        info = _HighContrast(cbSize=ctypes.sizeof(_HighContrast))
        if _win()[0].SystemParametersInfoW(0x0042, ctypes.sizeof(info), ctypes.byref(info), 0):  # SPI_GETHIGHCONTRAST
            return bool(info.dwFlags & 0x1)  # HCF_HIGHCONTRASTON
    except Exception:
        pass
    return False


def remote_session():
    if not WINDOWS:
        return False
    try:
        return bool(_win()[0].GetSystemMetrics(0x1000))  # SM_REMOTESESSION
    except Exception:
        return False


_affinity_ok = None


def affinity_supported(hwnd, affinity=None):
    """True when Windows accepts WDA_EXCLUDEFROMCAPTURE for ``hwnd`` (Windows 10 2004 and later). Asked once."""
    global _affinity_ok
    if _affinity_ok is None:
        affinity = affinity or Affinity()
        try:
            _affinity_ok = bool(affinity.set(hwnd, WDA_EXCLUDE))
        finally:
            affinity.set(hwnd, WDA_NONE)
    return _affinity_ok


def plain_reason(platform, see_through=True, hwnd=None):
    """Why the panel must be plain this showing, or None for live glass (section 3)."""
    if not WINDOWS or platform in ('offscreen', 'minimal'):
        return 'platform'
    if not see_through:
        return 'setting'
    if not transparency_effects():
        return 'transparency effects off'
    if high_contrast():
        return 'high contrast'
    if power.battery_saver():
        return 'battery saver'
    if remote_session():
        return 'remote session'
    if hwnd is not None and not affinity_supported(hwnd):
        return 'capture exclusion refused'
    return None


def native_rect(screen, x, y, width, height):
    """Device-pixel rectangle (x, y, w, h) of a window placed at logical (x, y) on ``screen`` (QScreen), before it is
    shown: the monitor's native rectangle (matched to QScreen.name()) plus the logical offset times the scale."""
    dpr = screen.devicePixelRatio()
    geo = screen.geometry()
    left, top = geo.x(), geo.y()  # Qt keeps each screen's native origin
    if WINDOWS:
        try:
            u = ctypes.WinDLL('user32', use_last_error=True)  # own prototypes: the callback type differs per call site
            found = []
            proc = ctypes.WINFUNCTYPE(_wt.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, _wt.LPARAM)
            u.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p, proc, _wt.LPARAM]
            u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

            def each(monitor, dc, rect, data):
                info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
                if u.GetMonitorInfoW(monitor, ctypes.byref(info)) and info.szDevice == screen.name():
                    found.append((info.rcMonitor.left, info.rcMonitor.top))
                return True
            callback = proc(each)
            u.EnumDisplayMonitors(None, None, callback, 0)
            if found:
                left, top = found[0]
        except Exception:
            pass
    w, h = device_size(width, height, dpr)
    return (int(left + round((x - geo.x()) * dpr)), int(top + round((y - geo.y()) * dpr)), w, h)


def window_rect(hwnd):
    """GetWindowRect as (x, y, w, h), or None."""
    if not WINDOWS:
        return None
    box = _wt.RECT()
    if _win()[0].GetWindowRect(hwnd, ctypes.byref(box)):
        return (box.left, box.top, box.right - box.left, box.bottom - box.top)
    return None


# ------------------------------------------------------------------ the engine
class _Relay(QObject):
    done = Signal(object)


class _WorkerState:
    """Everything the worker thread remembers between jobs of one showing (only the worker touches it)."""

    def __init__(self):
        self.reset()
        self.grabber = None

    def reset(self):
        self.prev_cheap = self.prev_check = None
        self.tint = self.tint_t = None
        self.alpha = self.alpha_t = None
        self.tint_theme = None
        self.hold = AppearanceHold()


class Backdrop:
    """The live glass engine for one window. States while the panel is visible: idle (nothing behind changes), active
    (it does: exclusion grabs at the rate rule), settling (still for 0.5 s: one settled picture is being made); and
    closed / stopped.

    Parameters (all optional; the defaults are the real thing):
      clock()                 seconds, monotonic (time.perf_counter); a test passes a fake.
      grabber()               factory called once on the worker thread, returning an object with grab(x, y, w, h) and
                              thumb(x, y, w, h, tw, th, halftone=False), both returning BGRA arrays (Grabber).
      affinity                object with set(hwnd, flag) -> bool and get(hwnd) -> int (Affinity).
      sync                    True: jobs run inline in the caller's thread and results arrive at once (tests).
      schedule(delay)         called with the seconds until the next tick() is due (default: a single-shot QTimer).
      frame_clock             object with add_loop(obj) and a ``loops`` set; obj.advance(elapsed) is called each frame
                              while a blend runs (ui_kit.DRIVER).
      on_change()             the pictures changed: repaint the panel.
      on_theme(theme)         the automatic appearance changed: re-theme (the new pictures are already composed for it).
      on_battery(), low_end(), reduced()  readers (power.on_battery, 4 or fewer usable CPUs, Motion.reduced).
      hook                    install the window-event hook while open (Windows only).
    """

    def __init__(self, clock=time.perf_counter, grabber=None, affinity=None, sync=False, schedule=None, frame_clock=None,
                 on_change=None, on_theme=None, on_battery=None, low_end=None, reduced=None, hook=True,
                 margin=MARGIN, width=PANEL_W, radius=RADIUS, full_shape=600):
        self.clock = clock
        self.grabber_factory = grabber or Grabber
        self.affinity = affinity or Affinity()
        self.sync = sync
        self.schedule_fn = schedule
        self.frame_clock = frame_clock if frame_clock is not None else DRIVER
        self.on_change = on_change or (lambda: None)
        self.on_theme = on_theme or (lambda theme: None)
        self.on_battery = on_battery or power.on_battery
        self.low_end = low_end or (lambda: power.usable_cpus() <= 4)
        self.reduced = reduced or (lambda: Motion.reduced)
        self.use_hook = hook and WINDOWS
        self.margin, self.width, self.radius, self.full_shape = margin, width, radius, full_shape
        self.relay = None
        self.timer = None
        self.worker = None
        self.jobs = queue.Queue()
        self.ws = _WorkerState()
        self.dirty_lock = threading.Lock()
        self.dirty_cur, self.dirty_prev = set(), set()
        self.hooks, self.hook_proc = [], None
        self.gen, self.job_id = 0, 0
        self.state = 'closed'
        self.in_flight = None
        self.consumed = set()
        self.first_event = threading.Event()
        self.first_result = None
        self.warned = False
        self.counters = dict(jobs=0, excluded_grabs=0, live_checks=0, full_grabs=0, events=0, failures=0, stale=0, arrivals=0,
                             affinity_resets=0, blend_steps=0, snaps=0, wrong_size=0)
        self.gui_ms = []          # how long each GUI-thread step of the engine took (arrive, blend step, body)
        self.theme_ms = []        # how long each appearance switch (on_theme) took
        self.job_ms = []          # recent cheap jobs: grab + compose, ms
        self.settled_ms = 0.0
        self.hwnd, self.rect, self.scale, self.shape = None, None, 1.0, 62
        self.setting, self.theme, self.system_theme = 'system', None, 'dark'
        self.others = lambda: []
        self.animating = False
        self._clear_pictures()
        self._reset_showing()

    # ---------------------------------------------------------------- read by the window
    @property
    def failed(self):
        """True when this showing must be plain: the first capture failed, or capturing stopped with no good picture."""
        return self.first_failed or (self.state == 'stopped' and self.wash_shown is None)

    @property
    def size(self):
        return (self.rect[2], self.rect[3]) if self.rect else (0, 0)

    @property
    def body_alpha(self):
        return self.ba_shown

    def timer_active(self):
        return bool(self.timer is not None and self.timer.isActive())

    def hook_installed(self):
        return bool(self.hooks)

    def blending(self):
        return self.blend_start is not None

    def picture(self, edge=1.0):
        """The body picture to paint (opaque, the window's device size), or None (paint the plain material). With
        ``edge`` > 0 and a lens band for the current resting shape, the band is folded in at that opacity."""
        t0 = time.perf_counter()
        if self.wash_shown is None:
            return None
        if edge <= 0.0 or self.ring_shown is None or self.ring_shape != self.shape:
            return self.wash_shown
        key = (self.wash_ver, self.ring_ver, round(edge, 3))
        if self.body_key != key:
            body = self.wash_shown.copy()
            p = QPainter(body)
            p.setOpacity(edge)
            self._draw_strips(p, self.ring_shown, self.ring_strips)
            p.end()
            self.body_cache, self.body_key = body, key
            self.gui_ms.append((time.perf_counter() - t0) * 1000)
        return self.body_cache

    # ---------------------------------------------------------------- GUI thread: commands
    def warm(self, size, scale, themes=('dark', 'light')):
        """Build lens maps and edge layers for a screen on the worker (start-up, and a new screen)."""
        shapes = (62, self.full_shape)
        self._submit(dict(kind='warm', gen=-1, size=size, scale=scale, shapes=shapes, themes=themes), track=False)

    def open(self, hwnd, rect, scale, shape, setting='system', system_theme='dark', hidden=True, others=None, wait=FIRST_WAIT):
        """Start a showing: capture what is behind the window's resting rectangle ``rect`` (device pixels) at once (no
        exclusion needed while the window is still ``hidden``) and wait up to ``wait`` seconds for the first picture.
        Returns True when a picture is ready."""
        self.close(release=False)
        self.gen += 1
        self.hwnd, self.rect, self.scale, self.shape = hwnd, tuple(int(v) for v in rect), scale, shape
        self.setting, self.system_theme = setting, system_theme
        self.theme = setting if setting in ('dark', 'light') else (self.theme or system_theme)
        self.others = others or (lambda: [])
        self._reset_showing()
        now = self.clock()
        self.battery, self.low_end_start = bool(self.on_battery()), bool(self.low_end())
        self.state = 'active'
        self.last_change = now
        self.next_grab = now + self.interval()
        self._install_hook()
        self.first_event.clear()
        self.first_result = None
        self._request('cheap', exclude=not hidden, reset=True, first=True)
        if not self.sync and wait > 0 and self.first_event.wait(wait) and self.first_result is not None:
            result = self.first_result
            self._arrive(result)
            self.consumed.add(result['id'])  # the same result also comes through the queued signal: dropped there
        self._schedule()
        return self.wash_shown is not None

    def close(self, release=True):
        """End the showing: no hook, no timer, no job; a result still in flight is dropped (generation number).
        ``release`` frees the pictures too (keep them while the panel fades out)."""
        if self.state != 'closed':
            self.gen += 1
        self.state = 'closed'
        self._remove_hook()
        if self.timer is not None:
            self.timer.stop()
        self.blend_start = None
        self.frame_clock.loops.discard(self)
        if release:
            self._clear_pictures()
            if self.worker is not None:
                self._submit(dict(kind='release', gen=-1), track=False)

    def event(self, kind='window'):
        """Something behind the panel may have changed (window event, wallpaper, Alfred's own change)."""
        if self.state in ('closed', 'stopped'):
            return
        self.counters['events'] += 1
        now = self.clock()
        self.last_change = now
        self.equal_streak = 0
        if self.in_flight is not None:
            self.event_flag = True
        if self.state == 'idle':
            self.state = 'active'
            self.battery = bool(self.on_battery())
            self.next_grab = now
            self.tick()

    def set_shape(self, shape):
        """The panel's resting height changed (62 collapsed, 600 expanded): the lens band is made for the new shape."""
        if shape != self.shape:
            self.shape = shape
            self.event('shape')

    def set_appearance(self, setting):
        if setting != self.setting:
            self.setting = setting
            if setting in ('dark', 'light'):
                self.theme = setting
            self.ws_hold_reset = True
            self.event('appearance')

    def move(self, rect):
        """The window's resting rectangle differs from the one first computed: capture again."""
        rect = tuple(int(v) for v in rect)
        if rect != self.rect:
            self.rect = rect
            self.event('move')

    def set_animating(self, on):
        """The height or appear spring is running (or the edge fades): the still-background check waits."""
        if on and not self.animating:
            self.check_reset = True
        self.animating = bool(on)

    def mark_dirty(self, x, y, w, h):
        """Alfred repainted this logical rectangle: the change check skips its blocks."""
        if self.state in ('closed', 'stopped'):
            return
        b = 4.0 * BLOCK / self.scale  # logical pixels per block
        cols = range(max(0, int(x // b)), int((x + w - 1) // b) + 1)
        rows = range(max(0, int(y // b)), int((y + h - 1) // b) + 1)
        with self.dirty_lock:
            self.dirty_cur.update((r, c) for r in rows for c in cols)

    # ---------------------------------------------------------------- GUI thread: the state machine
    def interval(self):
        """Seconds between captures while active (section 7)."""
        if self.low_end_start and len(self.job_ms) >= 5:
            self.low_end_start = False  # the starting guess is over: the 15% rule decides
        rate = base_rate(self.battery, self.covered, self.low_end_start)
        recent = self.job_ms[-5:]
        return capture_interval(rate, (sum(recent) / len(recent) / 1000.0) if recent else 0.0)

    def busy(self):
        return self.animating or self.blend_start is not None

    def tick(self):
        """Decide what to request next (called by the timer, after each result and on events)."""
        if self.state in ('closed', 'stopped') or self.in_flight is not None:
            return
        now = self.clock()
        if self.state == 'idle':
            if now >= self.next_check:
                if self.busy():
                    self.check_reset = True
                    self.next_check = now + 0.25
                else:
                    # a baseline (the first check after Alfred itself moved) is followed by a compare soon after
                    self.next_check = now + (0.5 if self.check_reset else still_interval(self.battery))
                    self._request('check')
                    return
        elif self.state == 'active':
            still = self.equal_streak >= 1 and now - self.last_change >= SETTLE_AFTER and not self.event_flag
            if self.reduced():  # no 4-a-second refresh: live checks until things stop, then one settled picture
                if now >= self.next_grab:
                    if still:
                        self.state = 'settling'
                        self._request('full')
                    else:
                        self.next_grab = now + 0.5
                        self._request('check', reduced=True)
                    return
            elif still:
                if self.settled_ms > SETTLED_MAX_MS:  # too slow here: the wash stays
                    self._go_idle(now)
                else:
                    self.state = 'settling'
                    self._request('full')
                    return
            elif now >= self.next_grab:
                self.next_grab = now + self.interval()
                self._request('cheap')
                return
        self._schedule()

    def _go_idle(self, now):
        self.state = 'idle'
        self.check_reset = True
        self.next_check = now + BLEND_TIME + 0.05  # the baseline check, once the last blend has ended

    def _schedule(self):
        if self.state in ('closed', 'stopped') or self.in_flight is not None:
            if self.timer is not None:
                self.timer.stop()
            return
        now = self.clock()
        if self.state == 'idle':
            due = self.next_check
        else:
            due = self.next_grab
            if self.equal_streak >= 1:
                due = min(due, self.last_change + SETTLE_AFTER)
        delay = max(0.0, due - now)
        if self.schedule_fn is not None:
            self.schedule_fn(delay)
            return
        if self.timer is None:
            self._ensure_relay()
            self.timer = QTimer(self.relay)
            self.timer.setSingleShot(True)
            self.timer.setTimerType(Qt.PreciseTimer)
            self.timer.timeout.connect(self.tick)
        self.timer.start(max(1, int(math.ceil(delay * 1000))))

    def _request(self, kind, exclude=True, reset=False, first=False, reduced=False):
        self.job_id += 1
        hwnds = []
        if kind in ('cheap', 'full') and exclude:
            hwnds = [self.hwnd] + [h for h in self.others() if h and h != self.hwnd]
        spec = dict(kind=kind, id=self.job_id, gen=self.gen, rect=self.rect, scale=self.scale, shape=self.shape,
                    theme=self.theme or self.system_theme, setting=self.setting, system_theme=self.system_theme, exclude=hwnds,
                    hwnd=self.hwnd, reset=reset, first=first, ba=self.ba_shown, check_reset=self.check_reset,
                    hold_reset=getattr(self, 'ws_hold_reset', False), reduced=reduced)
        self.ws_hold_reset = False
        if kind == 'check':
            self.check_reset = False
        self.in_flight = self.job_id
        self.counters['jobs'] += 1
        if self.timer is not None:
            self.timer.stop()  # the result's arrival decides what comes next
        self._submit(spec)

    def _arrive(self, r):
        """A finished job (GUI thread)."""
        t0 = time.perf_counter()
        themed = len(self.theme_ms)
        try:
            self._arrive_inner(r)
        finally:
            self.gui_ms.append((time.perf_counter() - t0) * 1000 - sum(self.theme_ms[themed:]))

    def _arrive_inner(self, r):
        if r.get('kind') in ('warm', 'release'):
            return
        if r['id'] in self.consumed:
            self.consumed.discard(r['id'])
            return
        if r['id'] == self.in_flight:
            self.in_flight = None
        if r['gen'] != self.gen or self.state in ('closed', 'stopped'):
            self.counters['stale'] += 1
            return
        now = self.clock()
        self.counters['arrivals'] += 1
        if r.get('affinity_reset'):
            self.counters['affinity_resets'] += 1
        if not r['ok']:
            self.counters['failures'] += 1
            self.failures += 1
            if r.get('first'):
                self.first_failed = True
            if self.failures >= MAX_FAILURES or self.first_failed:
                self.state = 'stopped'
                if not self.warned:
                    self.warned = True
                    log.warning('Glass capture stopped for this showing: %s', r.get('error'))
                if self.timer is not None:
                    self.timer.stop()
                self.on_change()
                return
            self.tick()
            return
        self.failures = 0
        kind = r['kind']
        if kind in ('cheap', 'full'):
            self._take(r, now)
            if kind == 'cheap':
                self.job_ms = (self.job_ms + [r['job_ms']])[-20:]
                if r['changed'] or self.event_flag or r.get('pending'):  # (a held appearance change keeps capturing)
                    self.last_change = max(self.last_change, now if self.event_flag else r['t_grab'])
                    self.equal_streak = 0
                else:
                    self.equal_streak += 1
                self.event_flag = False
            else:
                self.settled_ms = r['job_ms']
                if self.event_flag:
                    self.event_flag = False
                    self.state = 'active'
                    self.equal_streak = 0
                    self.next_grab = now
                else:
                    self._go_idle(now)
        elif kind == 'check':
            if r['changed']:
                self.last_change = now
                self.equal_streak = 0
                if self.state == 'idle':
                    self.state = 'active'
                    self.battery = bool(self.on_battery())
                    self.next_grab = now
            elif r.get('reduced'):
                self.equal_streak += 1
        self.tick()

    def _take(self, r, now):
        """New pictures: snap or start the running blend."""
        theme_changed = self.theme is not None and r['theme'] != self.theme
        if r['theme'] != self.theme:
            self.theme = r['theme']
            t0 = time.perf_counter()
            self.on_theme(self.theme)
            self.theme_ms.append((time.perf_counter() - t0) * 1000)  # reported apart from the glass's own GUI time
        first = self.wash_shown is None
        self.latest_wash, self.latest_ring, self.latest_shape = r['wash'], r['ring'], r['shape']
        self.latest_strips = r['strips']
        self.ba_latest = r['ba']
        self.tint, self.detail, self.luma = r['tint'], r['detail'], r['luma']
        self.covered = r['tint'] >= COVERED
        if first or theme_changed or self.reduced():
            self._snap()
        else:
            if self.blend_start is None:
                self.blend_last = now
            self.blend_start = now
            self.frame_clock.add_loop(self)
        self.on_change()

    def _snap(self):
        self.counters['snaps'] += 1
        self.wash_shown, self.ba_shown = self.latest_wash, self.ba_latest
        self.ring_shown, self.ring_shape, self.ring_strips = self.latest_ring, self.latest_shape, self.latest_strips
        self.wash_ver += 1
        self.ring_ver += 1
        self.blend_start = None
        self.frame_clock.loops.discard(self)

    def advance(self, elapsed):
        """One frame of the running blend (frame clock): shown += (latest - shown) * (1 - exp(-dt / tau)), by measured
        time and at most one step per BLEND_STEP; a swap to the latest picture after BLEND_TIME."""
        if self.blend_start is None:
            self.frame_clock.loops.discard(self)
            return
        now = self.clock()
        dt = now - self.blend_last
        done = now - self.blend_start >= BLEND_TIME
        if dt < BLEND_STEP and not done:
            return
        t0 = time.perf_counter()
        if done:
            self._snap()
        else:
            k = blend_factor(dt)
            self.blend_last = now
            p = QPainter(self.wash_shown)
            p.setOpacity(k)
            p.drawImage(0, 0, self.latest_wash)  # both opaque: an exact blend
            p.end()
            self.wash_ver += 1
            if self.ring_shown is None or self.ring_shape != self.latest_shape:
                self.ring_shown, self.ring_shape, self.ring_strips = self.latest_ring, self.latest_shape, self.latest_strips
                self.ring_ver += 1
            elif self.ring_shown is not self.latest_ring:
                p = QPainter(self.ring_shown)
                p.setCompositionMode(QPainter.CompositionMode_Source)  # with opacity: a linear blend, alpha included
                p.setOpacity(k)
                self._draw_strips(p, self.latest_ring, self.latest_strips)
                p.end()
                self.ring_ver += 1
            self.ba_shown += (self.ba_latest - self.ba_shown) * k
            self.counters['blend_steps'] += 1
        self.gui_ms.append((time.perf_counter() - t0) * 1000)
        self.on_change()

    def _draw_strips(self, p, image, strips):
        s = image.devicePixelRatio()
        bound = QRect(0, 0, image.width(), image.height())
        for x, y, w, h in strips:
            r = QRect(x, y, w, h).intersected(bound)
            if not r.isEmpty():
                p.drawImage(QRectF(r.x() / s, r.y() / s, r.width() / s, r.height() / s), image, QRectF(r))

    # ---------------------------------------------------------------- internals
    def _clear_pictures(self):
        self.wash_shown = self.ring_shown = self.latest_wash = self.latest_ring = self.body_cache = None
        self.ring_shape = self.latest_shape = None
        self.ring_strips = self.latest_strips = []
        self.body_key = None
        self.wash_ver = self.ring_ver = 0
        self.blend_start, self.blend_last = None, 0.0
        self.ba_shown = self.ba_latest = BODY_ALPHA
        self.tint, self.detail, self.luma = None, None, None
        self.covered = False

    def _reset_showing(self):
        self.failures, self.first_failed = 0, False
        self.event_flag = False
        self.equal_streak = 0
        self.next_grab = self.next_check = 0.0
        self.last_change = 0.0
        self.check_reset = True
        self.ws_hold_reset = False
        self.in_flight = None
        self.battery = self.low_end_start = False
        with self.dirty_lock:
            self.dirty_cur, self.dirty_prev = set(), set()

    def _ensure_relay(self):
        if self.relay is None:
            self.relay = _Relay()
            self.relay.done.connect(self._arrive, Qt.QueuedConnection)

    def _submit(self, spec, track=True):
        if self.sync:
            result = self._run(spec)
            if spec['kind'] not in ('warm', 'release'):
                self._arrive(result)
            return
        self._ensure_relay()
        if self.worker is None:
            self.worker = threading.Thread(target=self._loop, name='alfred-glass', daemon=True)
            self.worker.start()
        self.jobs.put(spec)

    def _loop(self):
        while True:
            spec = self.jobs.get()
            result = self._run(spec)
            if spec.get('first') and spec['gen'] == self.gen:
                self.first_result = result
                self.first_event.set()
            if spec['kind'] not in ('warm', 'release'):
                try:
                    self.relay.done.emit(result)
                except RuntimeError:  # the application is shutting down
                    return

    # ---------------------------------------------------------------- worker thread
    def _run(self, spec):
        """One job on the worker thread. Never raises: a failure comes back as ok=False."""
        kind = spec['kind']
        base = dict(kind=kind, id=spec.get('id'), gen=spec['gen'], first=spec.get('first', False), reduced=spec.get('reduced', False))
        try:
            if kind == 'warm':
                warm(spec['size'], spec['scale'], spec['shapes'], self.margin, self.width, self.radius, spec['themes'])
                return dict(base, ok=True)
            if kind == 'release':
                self.ws.reset()
                if self.ws.grabber is not None and hasattr(self.ws.grabber, 'close'):
                    self.ws.grabber.close()
                self.ws.grabber = None
                return dict(base, ok=True)
            if spec.get('reset'):
                self.ws.reset()
            if self.ws.grabber is None:
                self.ws.grabber = self.grabber_factory()
            if kind == 'check':
                return dict(base, **self._check(spec))
            return dict(base, **self._capture(spec))
        except Exception as error:
            return dict(base, ok=False, error=repr(error))

    def _excluded(self, spec, grab):
        """Run ``grab`` with every listed Alfred window excluded from capture; the exclusion is cleared in finally."""
        done = []
        try:
            for hwnd in spec['exclude']:
                if self.affinity.set(hwnd, WDA_EXCLUDE):
                    done.append(hwnd)
                elif hwnd == spec['hwnd']:
                    raise OSError('SetWindowDisplayAffinity refused')
            return grab()
        finally:
            for hwnd in done:
                self.affinity.set(hwnd, WDA_NONE)

    def _capture(self, spec):
        ws, g = self.ws, self.ws.grabber
        x, y, w, h = spec['rect']
        size, scale = (w, h), spec['scale']
        qw, qh = quarter(size)
        t0 = self.clock()
        quarter_src = None
        if spec['kind'] == 'full':  # with a quarter-size grab beside it, so tint and detail match the wash's
            both = lambda: (g.grab(x, y, w, h), g.thumb(x, y, w, h, qw, qh))
            src, quarter_src = self._excluded(spec, both) if spec['exclude'] else both()
            self.counters['full_grabs'] += 1
        else:
            src = self._excluded(spec, lambda: g.thumb(x, y, w, h, qw, qh)) if spec['exclude'] else g.thumb(x, y, w, h, qw, qh)
        if spec['exclude']:
            self.counters['excluded_grabs'] += 1
        t1 = self.clock()
        expect = (h, w) if spec['kind'] == 'full' else (qh, qw)
        if src is None or tuple(src.shape[:2]) != expect:
            self.counters['wrong_size'] += 1
            raise ValueError('capture size %r, expected %r' % (None if src is None else src.shape[:2], expect))
        if quarter_src is None or tuple(quarter_src.shape[:2]) != (qh, qw):
            quarter_src = src if spec['kind'] != 'full' else np.ascontiguousarray(src[::4, ::4][:qh, :qw])
        shape_q = tuple(v / 4.0 for v in panel_rect(scale, spec['shape'], self.margin, self.width))
        # appearance: at the first capture of a showing (or a setting change) at once, otherwise held for 1 s
        lum = luma(quarter_src, shape_q)
        current = spec['system_theme'] if spec.get('first') else spec['theme']
        want = resolve_theme(lum, current, spec['setting'])
        if spec.get('first') or spec.get('hold_reset') or spec['setting'] in ('dark', 'light'):
            ws.hold.candidate = None
            theme = want
        else:
            theme = ws.hold.update(want, spec['theme'], t1)
        pending = ws.hold.candidate is not None and ws.hold.candidate != theme  # a new appearance is being held
        if theme != ws.tint_theme:
            ws.tint = None  # a new appearance starts its tint afresh
            ws.tint_theme = theme
        detail = detail_score(quarter_src, shape_q)
        target_alpha = body_alpha_for(detail)
        ws.alpha = smooth(ws.alpha, target_alpha, (t1 - ws.alpha_t) if ws.alpha_t is not None else 0.0, ALPHA_TAU, ALPHA_TAU)
        ws.alpha_t = t1

        def tint(raw):
            ws.tint = smooth(ws.tint, raw, (t1 - ws.tint_t) if ws.tint_t is not None else 0.0, TINT_TAU_RISE, TINT_TAU_FALL)
            ws.tint_t = t1
            return ws.tint
        out = compose(src, spec['kind'], theme, size, scale, spec['shape'], ws.alpha, tint, self.margin, self.width,
                      self.full_shape, self.radius, measured=(lum, detail), live_src=quarter_src if spec['kind'] == 'full' else None)
        ws.alpha = out['body_alpha']  # raised at once when the tint alone cannot hold the contrast targets
        moved = None
        if spec['kind'] == 'cheap':
            means = block_means(src)
            moved = changed(ws.prev_cheap, means)
            ws.prev_cheap = means
        t2 = self.clock()
        return dict(ok=True, wash=out['wash'], ring=out['ring'], strips=out['strips'], shape=spec['shape'], theme=theme,
                    tint=out['tint'], tint_raw=out['tint_raw'], ba=ws.alpha, detail=detail, luma=lum, changed=moved, pending=pending,
                    t_grab=t0, grab_ms=(t1 - t0) * 1000, job_ms=(t2 - t0) * 1000)

    def _check(self, spec):
        """The still-background (and video) check: a quarter-size grab WITHOUT exclusion (it contains Alfred's own
        panel), compared block by block with the previous one; inside the panel with gain 1 / (1 - body alpha),
        skipping blocks Alfred repainted. Also resets a stuck display affinity."""
        ws, g = self.ws, self.ws.grabber
        reset = False
        if spec['hwnd'] and self.affinity.get(spec['hwnd']) != 0:  # no exclusion job runs now: nothing may be excluded
            self.affinity.set(spec['hwnd'], WDA_NONE)
            reset = True
            log.info('Glass: a stuck capture exclusion was reset')
        x, y, w, h = spec['rect']
        qw, qh = quarter((w, h))
        t0 = self.clock()
        src = g.thumb(x, y, w, h, qw, qh, halftone=True)
        self.counters['live_checks'] += 1
        if src is None or tuple(src.shape[:2]) != (qh, qw):
            raise ValueError('check size %r, expected %r' % (None if src is None else src.shape[:2], (qh, qw)))
        means = block_means(src)
        with self.dirty_lock:
            dirty = self.dirty_cur | self.dirty_prev
            self.dirty_prev, self.dirty_cur = self.dirty_cur, set()
        skip = np.zeros(means.shape[:2], bool)
        for r, c in dirty:
            if r < skip.shape[0] and c < skip.shape[1]:
                skip[r, c] = True
        panel_q = tuple(v / 4.0 for v in panel_rect(spec['scale'], spec['shape'], self.margin, self.width))
        gains = gain_map(means.shape[:2], panel_q, spec['ba'])
        moved = False if (spec.get('check_reset') or ws.prev_check is None) else changed(ws.prev_check, means, gains, skip)
        ws.prev_check = means
        return dict(ok=True, changed=moved, affinity_reset=reset, job_ms=(self.clock() - t0) * 1000)

    # ---------------------------------------------------------------- the window-event hook (GUI thread)
    def _install_hook(self):
        if not self.use_hook or self.hooks:
            return
        u = _win()[0]
        proc_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, _wt.DWORD, ctypes.c_void_p, _wt.LONG, _wt.LONG, _wt.DWORD, _wt.DWORD)
        u.SetWinEventHook.argtypes = [_wt.DWORD, _wt.DWORD, ctypes.c_void_p, proc_type, _wt.DWORD, _wt.DWORD, _wt.DWORD]
        if self.hook_proc is None:
            box = _wt.RECT()

            def callback(hook, event, hwnd, id_object, id_child, thread, when):
                try:
                    if id_object != 0 or id_child != 0 or not hwnd or self.rect is None:
                        return
                    if u.GetAncestor(hwnd, 2) != hwnd:  # GA_ROOT: top-level windows only
                        return
                    if u.GetWindowRect(hwnd, ctypes.byref(box)):
                        x, y, w, h = self.rect
                        if not (box.left < x + w and box.right > x and box.top < y + h and box.bottom > y):
                            return
                    self.event('window')
                except Exception:
                    pass
            self.hook_proc = proc_type(callback)
        # foreground, move/size end, minimise start/end, destroy/show/hide, location change
        for lo, hi in ((0x0003, 0x0003), (0x000B, 0x000B), (0x0016, 0x0017), (0x8001, 0x8003), (0x800B, 0x800B)):
            handle = u.SetWinEventHook(lo, hi, None, self.hook_proc, 0, 0, 0x0000 | 0x0002)  # OUTOFCONTEXT | SKIPOWNPROCESS
            if handle:
                self.hooks.append(handle)

    def _remove_hook(self):
        if self.hooks:
            u = _win()[0]
            for handle in self.hooks:
                u.UnhookWinEvent(handle)
            self.hooks = []
