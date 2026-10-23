"""Spotlight-style building blocks: springs, glass, theme, icons, hotkey."""
import ctypes
import math
import os
import time
from io import BytesIO

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QFont, QGuiApplication, QIcon, QImage, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap, QTransform)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton

# --------------------------------------------------------------------- theme
# Apple's system colours (macOS label/separator/systemBlue values) for Spotlight's look.
PALETTES = {
    'dark': dict(tint=QColor(32, 32, 35, 178), tint_bottom=QColor(24, 24, 27, 196), text=QColor(255, 255, 255, 235),
                 secondary=QColor(235, 235, 245, 150), tertiary=QColor(235, 235, 245, 77), separator=QColor(255, 255, 255, 26),
                 hover=QColor(255, 255, 255, 16), fill=QColor(255, 255, 255, 18), fill_strong=QColor(255, 255, 255, 34),
                 accent=QColor(10, 132, 255), accent_text=QColor(255, 255, 255), rim_top=QColor(255, 255, 255, 64),
                 rim_bottom=QColor(255, 255, 255, 14), sheen=QColor(255, 255, 255, 22), shadow=QColor(0, 0, 0, 120),
                 green=QColor(48, 209, 88), orange=QColor(255, 159, 10), red=QColor(255, 69, 58), name='dark'),
    'light': dict(tint=QColor(246, 246, 248, 190), tint_bottom=QColor(238, 238, 242, 205), text=QColor(0, 0, 0, 222),
                  secondary=QColor(60, 60, 67, 155), tertiary=QColor(60, 60, 67, 77), separator=QColor(0, 0, 0, 24),
                  hover=QColor(0, 0, 0, 10), fill=QColor(0, 0, 0, 12), fill_strong=QColor(0, 0, 0, 22),
                  accent=QColor(0, 122, 255), accent_text=QColor(255, 255, 255), rim_top=QColor(255, 255, 255, 200),
                  rim_bottom=QColor(0, 0, 0, 20), sheen=QColor(255, 255, 255, 90), shadow=QColor(0, 0, 0, 70),
                  green=QColor(52, 199, 89), orange=QColor(255, 149, 0), red=QColor(255, 59, 48), name='light'),
}
GLOW = [QColor('#2E9BFF'), QColor('#8E5CFF'), QColor('#FF4FA3'), QColor('#FF8A3D'), QColor('#FFD23F'), QColor('#2E9BFF')]


def system_dark():
    if os.name != 'nt':
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
            return winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] == 0
    except OSError:
        return True


def palette_for(setting):
    name = setting if setting in ('dark', 'light') else ('dark' if system_dark() else 'light')
    return PALETTES[name]


def rgba(color, alpha=None):
    a = color.alphaF() if alpha is None else alpha
    return f'rgba({color.red()},{color.green()},{color.blue()},{a:.3f})'


def stylesheet(p):
    t, s, f, fs, sep, acc = rgba(p['text']), rgba(p['secondary']), rgba(p['fill']), rgba(p['fill_strong']), rgba(p['separator']), rgba(p['accent'])
    return f"""
* {{ font-family: 'Segoe UI Variable Text', 'Segoe UI'; font-size: 10pt; color: {t}; }}
QWidget#clear, QScrollArea, QScrollArea > QWidget > QWidget, QStackedWidget, QWidget#qt_scrollarea_viewport {{ background: transparent; border: none; }}
QLabel#secondary {{ color: {s}; }}
QLabel#caption {{ color: {s}; font-size: 9pt; }}
QLabel#section {{ color: {s}; font-size: 9pt; font-weight: 600; }}
QLabel#title {{ font-size: 13.5pt; font-weight: 600; }}
QLabel#hero {{ font-size: 15pt; font-weight: 600; }}
QLineEdit#search {{ background: transparent; border: none; font-family: 'Segoe UI Variable Display', 'Segoe UI'; font-size: 17pt;
                   font-weight: 300; selection-background-color: {rgba(p['accent'], .45)}; padding: 0; }}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextBrowser {{ background: {f}; border: 1px solid {sep}; border-radius: 8px;
     padding: 5px 9px; selection-background-color: {rgba(p['accent'], .45)}; }}
QTextBrowser#excerpt, QTextBrowser#plain {{ background: transparent; border: none; padding: 0; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border: 1px solid {acc}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {rgba(p['tint'], 1)}; border: 1px solid {sep}; selection-background-color: {acc}; outline: none; }}
QPushButton {{ background: {f}; border: 1px solid {sep}; border-radius: 13px; padding: 5px 14px; }}
QPushButton:hover {{ background: {fs}; }}
QPushButton:pressed {{ background: {sep}; }}
QPushButton:focus {{ border: 1px solid {acc}; }}
QPushButton:disabled {{ color: {rgba(p['tertiary'])}; }}
QPushButton#primary {{ background: {acc}; border: none; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: {rgba(p['accent'].lighter(112))}; }}
QPushButton#primary:disabled {{ background: {fs}; color: {rgba(p['tertiary'])}; }}
QPushButton#link {{ background: transparent; border: none; color: {acc}; padding: 2px 4px; }}
QPushButton#link:hover {{ text-decoration: underline; }}
QPushButton#chip {{ background: {f}; border: 1px solid {sep}; border-radius: 13px; padding: 4px 11px 4px 8px; }}
QPushButton#chip:hover {{ background: {fs}; border-color: {acc}; }}
QPushButton#ghost {{ background: transparent; border: none; border-radius: 13px; padding: 0; }}
QPushButton#ghost:hover {{ background: {fs}; }}
QLabel#name {{ font-weight: 600; }}
QLabel#badge {{ background: {fs}; border-radius: 7px; padding: 1px 7px; color: {s}; font-size: 8.5pt; font-weight: 600; }}
QLabel#bubble {{ background: {rgba(p['accent'], .20)}; border-radius: 14px; padding: 8px 13px; font-size: 10.5pt; }}
QPushButton#pill {{ background: {f}; border: 1px solid {sep}; border-radius: 14px; padding: 5px 13px; }}
QPushButton#pill:hover {{ background: {fs}; }}
QLabel#tag {{ background: {fs}; border-radius: 9px; padding: 2px 9px; color: {s}; font-size: 9pt; }}
QPushButton#source {{ background: {f}; border: 1px solid {sep}; border-radius: 9px; padding: 6px 10px; text-align: left; }}
QPushButton#source:hover {{ background: {fs}; }}
QPushButton#source[used="false"] {{ color: {s}; }}
QFrame#sourcerow {{ background: {f}; border: 1px solid {sep}; border-radius: 9px; }}
QFrame#sourcerow:hover {{ background: {fs}; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border-radius: 4px; border: 1px solid {rgba(p['tertiary'])}; background: {f}; }}
QCheckBox::indicator:checked {{ background: {acc}; border-color: {acc}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px 1px; }}
QScrollBar::handle:vertical {{ background: {rgba(p['tertiary'], .55)}; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {s}; }}
QScrollBar:horizontal {{ height: 0; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; width: 0; background: transparent; }}
QToolTip {{ background: {rgba(p['tint'], 1)}; color: {t}; border: 1px solid {sep}; border-radius: 6px; padding: 4px 8px; }}
QMenu {{ background: {rgba(p['tint'], 1)}; border: 1px solid {sep}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
QMenu::item:selected {{ background: {acc}; color: white; }}
"""


# -------------------------------------------------------------------- motion
class Motion:
    reduced = False
    instant = False  # True while a view is rebuilt behind a transition: springs started then land at once

    @classmethod
    def detect(cls, settings):
        system = False
        if os.name == 'nt':
            value = ctypes.c_bool(True)
            try:  # SPI_GETCLIENTAREAANIMATION: Windows "Animation effects"
                ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0)
                system = not value.value
            except Exception:
                pass
        cls.reduced = bool(settings.get('reduce_motion')) or system


def refresh_rate(screen=None):
    """Refresh rate (Hz) of ``screen`` (the primary one by default), 60 if the system doesn't say."""
    screen = screen or QGuiApplication.primaryScreen()
    hz = screen.refreshRate() if screen else 0.0
    return hz if 23.0 <= hz <= 1000.0 else 60.0


def frame_interval(screen=None):
    """Milliseconds between two refreshes of ``screen`` (4.17 at 240 Hz, 16.7 at 60 Hz)."""
    return 1000.0 / refresh_rate(screen)


class Spring:
    """Damped spring (semi-implicit Euler). Retargeting mid-flight keeps velocity,
    which is what makes motion feel fluid instead of restarting an easing curve.

    It integrates over measured time, not per frame, so a spring takes the same time and follows the same curve at
    60, 144 or 240 Hz, and when frames are dropped. ``precision`` is how close (in the spring's own units) counts as
    arrived: for pixels a fraction of a device pixel, so nobody waits on motion too small to see."""
    SUBSTEP = 0.002  # seconds: small enough that the curve doesn't depend on the frame rate

    def __init__(self, value=0.0, stiffness=260.0, damping=0.78, on_change=None, precision=0.02):
        self.value = self.target = float(value)
        self.velocity = 0.0
        self.k = stiffness
        self.zeta = damping
        self.on_change = on_change
        self.precision = precision

    def to(self, target, impulse=0.0):
        self.target = float(target)
        self.velocity += impulse
        if Motion.reduced or Motion.instant:
            self.snap(target)
        else:
            DRIVER.add(self)

    def snap(self, value):
        DRIVER.springs.discard(self)
        self.value = self.target = float(value)
        self.velocity = 0.0
        if self.on_change:
            self.on_change(self.value)

    def advance(self, elapsed):
        """Move ``elapsed`` seconds forward; True once settled."""
        steps = max(1, math.ceil(elapsed / self.SUBSTEP - 1e-9))
        dt = elapsed / steps
        c = 2 * math.sqrt(self.k) * self.zeta
        for _ in range(steps):
            accel = -self.k * (self.value - self.target) - c * self.velocity
            self.velocity += accel * dt
            self.value += self.velocity * dt
        settled = abs(self.value - self.target) < self.precision and abs(self.velocity) < self.precision * 10
        if settled:
            self.value, self.velocity = self.target, 0.0
        return settled


class _VBlank:
    """Waits for the vertical blank of one display, so animation frames land on the screen's own refresh (60, 75, 144,
    240 Hzâ€¦) instead of a timer that drifts against it. Windows only (D3DKMTWaitForVerticalBlankEvent, part of every
    WDDM display driver, whatever the graphics card); ``wait`` returns False when it can't be used."""

    class _Open(ctypes.Structure):
        _fields_ = [('hDc', ctypes.c_void_p), ('hAdapter', ctypes.c_uint), ('luid_low', ctypes.c_uint), ('luid_high', ctypes.c_int),
                    ('source', ctypes.c_uint)]

    class _Wait(ctypes.Structure):
        _fields_ = [('hAdapter', ctypes.c_uint), ('hDevice', ctypes.c_uint), ('source', ctypes.c_uint)]

    class _Close(ctypes.Structure):
        _fields_ = [('hAdapter', ctypes.c_uint)]

    def __init__(self):
        self.device, self.adapter = None, None
        self.ok = os.name == 'nt'
        if self.ok:
            try:
                self.gdi = ctypes.WinDLL('gdi32')
                self.gdi.CreateDCW.restype = ctypes.c_void_p
                self.gdi.CreateDCW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p]
                self.gdi.DeleteDC.argtypes = [ctypes.c_void_p]
                self.gdi.D3DKMTWaitForVerticalBlankEvent.argtypes = [ctypes.c_void_p]
            except (OSError, AttributeError):  # not a WDDM system
                self.ok = False

    def _open(self, device):
        self._close()
        dc = self.gdi.CreateDCW(device, None, None, None) if device else None
        if not dc:
            return False
        request = self._Open(hDc=dc)
        status = self.gdi.D3DKMTOpenAdapterFromHdc(ctypes.byref(request))
        self.gdi.DeleteDC(dc)
        if status != 0:
            return False
        self.adapter, self.device = request, device
        return True

    def _close(self):
        if self.adapter is not None:
            self.gdi.D3DKMTCloseAdapter(ctypes.byref(self._Close(self.adapter.hAdapter)))
        self.adapter = self.device = None

    def wait(self, device):
        """Block until ``device`` (a GDI display name, see display_device) next refreshes; False if it can't. A display that
        can't be opened turns this off (``ok``), and the frame clock falls back to its timer."""
        if not self.ok:
            return False
        if device != self.device and not self._open(device):
            self.ok = False
            return False
        request = self._Wait(self.adapter.hAdapter, 0, self.adapter.source)
        if self.gdi.D3DKMTWaitForVerticalBlankEvent(ctypes.byref(request)) != 0:
            self._close()  # the display changed (unplugged, mode switch): reopen on the next wait
            return False
        return True


def display_device(hwnd=None):
    """GDI name (such as DISPLAY1) of the display showing window ``hwnd`` (the primary display without one); '' off Windows."""
    if os.name != 'nt':
        return ''
    from ctypes import wintypes

    class Info(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD),
                    ('szDevice', ctypes.c_wchar * 32)]
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.restype = user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        monitor = user32.MonitorFromWindow(hwnd, 2) if hwnd else user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)
        info = Info(cbSize=ctypes.sizeof(Info))
        return info.szDevice if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)) else ''
    except Exception:
        return ''


class _Driver(QObject):
    """One clock for every animation: springs and loops all advance in the same frame, once per refresh of the display
    the visible Alfred window is on. A background thread waits for that display's vertical blank and wakes the UI thread;
    if that isn't available, a precise timer runs at the display's refresh interval. Each frame advances everything by
    the time that really passed (clamped, so a stall slows motion instead of making it jump), and the clock stops when
    nothing moves."""
    frame = Signal()
    fallback = Signal()
    MAX_STEP = 1 / 30  # seconds: the most one frame may advance

    def __init__(self):
        super().__init__()
        self.springs, self.loops = set(), set()
        self.timer = None
        self.last = 0.0
        self.screen = None
        self.device, self.interval = '', 1000.0 / 60
        self.vsync = _VBlank()
        self.running = False
        self.pending = False
        self.thread = None
        self.wake = None
        self.frames = 0
        self.paced = 0.0
        self.frame.connect(self.tick, Qt.QueuedConnection)
        self.fallback.connect(self._fall_back, Qt.QueuedConnection)

    def follow(self, screen, window=None):
        """Pace frames by ``screen``, the display ``window`` is on (call again when it moves to another display)."""
        self.screen = screen
        self.device = display_device(int(window.winId()) if window is not None else None)
        self.interval = frame_interval(screen)
        if self.timer is not None and self.timer.isActive():
            self.timer.setInterval(max(1, round(self.interval)))

    def add(self, spring):
        self.springs.add(spring)
        self._start()

    def add_loop(self, loop):
        self.loops.add(loop)
        self._start()

    def _start(self):
        if self.running:
            return
        self.running = True
        self.last = time.perf_counter()
        if self.screen is None:
            self.follow(QGuiApplication.primaryScreen())
        if self.vsync.ok:
            import threading
            if self.thread is None:
                self.wake = threading.Condition()
                self.thread = threading.Thread(target=self._pace, name='alfred-vsync', daemon=True)
                self.thread.start()
            with self.wake:
                self.wake.notify()
        else:
            self._start_timer()

    def _fall_back(self):
        if self.running:
            self._start_timer()

    def _start_timer(self):
        if self.timer is None:
            self.timer = QTimer()
            self.timer.setTimerType(Qt.PreciseTimer)
            self.timer.timeout.connect(self.tick)
        if not self.timer.isActive():
            self.timer.start(max(1, round(self.interval)))

    def _pace(self):
        """Background thread: one ``frame`` signal per vertical blank while anything moves."""
        while True:
            with self.wake:
                while not self.running:
                    self.wake.wait()
            if not self.vsync.wait(self.device) and not self.vsync.ok:
                self.fallback.emit()  # not usable here: the timer on the UI thread takes over
                return
            # Never faster than the display refreshes (a wait that fails, or returns at once while the display sleeps).
            early = self.interval / 1000 - (time.perf_counter() - self.paced)
            if early > self.interval / 2000:
                time.sleep(early)
            self.paced = time.perf_counter()
            if self.running and not self.pending:  # the UI thread hasn't drawn the last frame: skip, don't queue up
                self.pending = True
                self.frame.emit()

    def tick(self):
        self.pending = False
        if not self.running:
            return
        now = time.perf_counter()
        elapsed = min(max(now - self.last, 0.0), self.MAX_STEP)
        self.last = now
        self.frames += 1
        for spring in list(self.springs):
            settled = spring.advance(elapsed)
            if settled:
                self.springs.discard(spring)
            if spring.on_change:
                spring.on_change(spring.value)
        for loop in list(self.loops):
            loop.advance(elapsed)
        if not self.springs and not self.loops:
            self.running = False
            if self.timer is not None:
                self.timer.stop()


DRIVER = _Driver()


class Loop(QObject):
    """Phase 0..1 looping animation on the shared clock that only ticks while running (no idle CPU)."""
    tick = Signal(float)

    def __init__(self, period=2.4, parent=None):
        super().__init__(parent)
        self.period, self.phase = period, 0.0
        self.destroyed.connect(lambda: DRIVER.loops.discard(self))

    def start(self):
        if Motion.reduced:
            return
        DRIVER.add_loop(self)

    def stop(self):
        DRIVER.loops.discard(self)

    @property
    def running(self):
        return self in DRIVER.loops

    def advance(self, elapsed):
        self.phase = (self.phase + elapsed / self.period) % 1.0
        try:
            self.tick.emit(self.phase)
        except RuntimeError:  # its widget is gone
            DRIVER.loops.discard(self)


# --------------------------------------------------------------------- icons
SVG = {
    'search': '<circle cx="10.5" cy="10.5" r="6.5" fill="none" stroke="C" stroke-width="2.2"/><path d="M15.5 15.5 L21 21" stroke="C" stroke-width="2.4" stroke-linecap="round"/>',
    'sparkle': '<path d="M12 2.5 C12.8 7.6 14.6 9.6 20 10.5 C14.6 11.4 12.8 13.4 12 18.5 C11.2 13.4 9.4 11.4 4 10.5 C9.4 9.6 11.2 7.6 12 2.5Z" fill="C"/>'
               '<path d="M19 15 C19.3 17 20 17.7 22 18 C20 18.3 19.3 19 19 21 C18.7 19 18 18.3 16 18 C18 17.7 18.7 17 19 15Z" fill="C"/>',
    'graph': '<circle cx="6" cy="17" r="2.6" fill="C"/><circle cx="18" cy="17" r="2.6" fill="C"/><circle cx="12" cy="6" r="2.8" fill="C"/>'
             '<path d="M12 6 L6 17 M12 6 L18 17 M6 17 L18 17" stroke="C" stroke-width="1.6" fill="none"/>',
    'gear': '<path d="M12 8.6a3.4 3.4 0 1 0 0 6.8a3.4 3.4 0 0 0 0-6.8Zm8.2 4.6l1.9 1.5-1.9 3.3-2.3-.9a7.4 7.4 0 0 1-1.8 1l-.3 2.4h-3.8l-.4-2.4a7 7 0 0 1-1.7-1l-2.3.9-1.9-3.3 1.9-1.5a7 7 0 0 1 0-2.1L3.7 9.7l1.9-3.3 2.3.9a7.4 7.4 0 0 1 1.8-1l.3-2.4h3.8l.4 2.4a7 7 0 0 1 1.7 1l2.3-.9 1.9 3.3-1.9 1.5a7 7 0 0 1 0 2.1Z" fill="C" fill-rule="evenodd"/>',
    'folder': '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.6l2 2h8.4A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5Z" fill="C"/>',
    'open': '<path d="M14 4h6v6M20 4l-9 9M18 14v5H5V6h5" fill="none" stroke="C" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    'copy': '<rect x="8" y="8" width="11" height="12" rx="2" fill="none" stroke="C" stroke-width="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8" fill="none" stroke="C" stroke-width="2"/>',
    'bookmark': '<path d="M7 3.5h10a1 1 0 0 1 1 1V21l-6-4-6 4V4.5a1 1 0 0 1 1-1Z" fill="C"/>',
    'chevron': '<path d="M9 6l6 6-6 6" fill="none" stroke="C" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    'warning': '<path d="M12 3 22 20H2Z" fill="C"/><path d="M12 9v5" stroke="#000" stroke-opacity=".7" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="17" r="1.2" fill="#000" fill-opacity=".7"/>',
    'refresh': '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" fill="none" stroke="C" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    'close': '<path d="M6 6l12 12M18 6L6 18" stroke="C" stroke-width="2.4" stroke-linecap="round"/>',
    'sidebar': '<rect x="3.5" y="5" width="17" height="14" rx="2.5" fill="none" stroke="C" stroke-width="1.9"/><path d="M9 5.5v13" stroke="C" stroke-width="1.9"/>',
    'pencil': '<path d="M5 19l1-4L16 5l3 3L9 18Z" fill="none" stroke="C" stroke-width="2" stroke-linejoin="round"/>',
    'server': '<rect x="4" y="4" width="16" height="6.5" rx="2" fill="none" stroke="C" stroke-width="1.9"/><rect x="4" y="13.5" width="16" height="6.5" rx="2" fill="none" stroke="C" stroke-width="1.9"/><circle cx="8" cy="7.25" r="1.2" fill="C"/><circle cx="8" cy="16.75" r="1.2" fill="C"/>',
    'check': '<path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="C" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>',
    'chevron_down': '<path d="M6 9l6 6 6-6" fill="none" stroke="C" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    'plus': '<path d="M12 5v14M5 12h14" stroke="C" stroke-width="2.2" stroke-linecap="round"/>',
    'note': '<path d="M6 3h8l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" fill="C"/><path d="M8.5 12h7M8.5 15.5h7" stroke="#000" stroke-opacity=".35" stroke-width="1.5" stroke-linecap="round"/>',
}
_icon_cache = {}


def icon_pixmap(name, size, color, dpr=None):
    dpr = dpr or screen_dpr()
    key = (name, size, color.rgba(), dpr)
    if key not in _icon_cache:
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{SVG[name]}</svg>'.replace('"C"', f'"{color.name()}"')
        renderer = QSvgRenderer(QByteArray(svg.encode()))
        image = QImage(int(size * dpr), int(size * dpr), QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setOpacity(color.alphaF())
        renderer.render(painter)
        painter.end()
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(dpr)
        _icon_cache[key] = pixmap
    return _icon_cache[key]


def icon(name, size, color, dpr=None):
    return QIcon(icon_pixmap(name, size, color, dpr))


# --------------------------------------------------------------------- logos
LOGO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'logos')
# Brand colours that stay as they are; every other mark is tinted to the theme's text colour.
BRAND_COLOURS = {'claude': '#D97757'}
_logo_cache = {}


TEXT_FAMILIES = ['Segoe UI Variable Text', 'Segoe UI']  # Windows 11's UI font, Windows 10's otherwise


def tune_text(dpr):
    """Set the application font's rendering for a screen with pixel ratio ``dpr``.

    Alfred's panel is a translucent window, which Windows draws without ClearType, so text is grey-scale
    anti-aliased. At 100 % (and 200 %) full hinting snaps stems to whole pixels and keeps small text crisp; at fractional
    ratios (125 %, 150 %) full hinting distorts glyph shapes, so only vertical metrics are snapped. Returns True if
    anything changed (widgets then re-render their text)."""
    font = QGuiApplication.font()
    hinting = QFont.PreferFullHinting if abs(dpr - round(dpr)) < 0.01 else QFont.PreferVerticalHinting
    strategy = QFont.StyleStrategy(QFont.PreferAntialias | QFont.PreferQuality)
    if font.families()[:2] == TEXT_FAMILIES and font.hintingPreference() == hinting and font.styleStrategy() == strategy:
        return False
    font.setFamilies(TEXT_FAMILIES)
    font.setHintingPreference(hinting)
    font.setStyleStrategy(strategy)
    QGuiApplication.setFont(font)
    return True


def text_font(points, weight=None):
    """A font for text drawn by hand in a paintEvent, rendered like the widgets' text (see tune_text)."""
    font = QFont(QGuiApplication.font())
    font.setPointSizeF(points)
    if weight is not None:
        font.setWeight(weight)
    return font


def screen_dpr():
    """Render scale for crisp vector art: exactly the screen's pixel ratio, so a pixmap lands 1:1 on device pixels
    (a 2x pixmap shown at 1.5x is resampled and looks soft)."""
    screen = QGuiApplication.primaryScreen()
    return max(1.0, round((screen.devicePixelRatio() if screen else 1.0) * 4) / 4)


def snapped(rect, widget):
    """``rect`` (in ``widget``'s coordinates) moved onto the window's device-pixel grid, so a pixmap drawn there is a 1:1
    copy instead of a blurry resample. At 150 % scaling an odd logical coordinate falls between two device pixels."""
    dpr = widget.devicePixelRatioF()
    origin = widget.mapTo(widget.window(), QPointF(0, 0)) if widget.window() is not widget else QPointF(0, 0)
    x, y = origin.x() + rect.x(), origin.y() + rect.y()
    return QRectF(round(x * dpr) / dpr - origin.x(), round(y * dpr) / dpr - origin.y(), rect.width(), rect.height())


def brand_pixmap(name, size, color, dpr=None):
    """A provider logo from assets/logos/<name>.svg, drawn from its vector at the screen's resolution
    (never scaled from a bitmap). Falls back to a neutral server glyph when the file is missing."""
    dpr = dpr or screen_dpr()
    key = (name, size, color.rgba(), dpr)
    if key in _logo_cache:
        return _logo_cache[key]
    path = os.path.join(LOGO_DIR, f'{name}.svg')
    renderer = QSvgRenderer(path) if os.path.isfile(path) else None
    if renderer is None or not renderer.isValid():
        _logo_cache[key] = icon_pixmap('server', size, color, dpr)
        return _logo_cache[key]
    side = int(round(size * dpr))
    image = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    box = renderer.viewBoxF()
    scale = side / max(box.width(), box.height(), 1)
    w, h = box.width() * scale, box.height() * scale
    renderer.render(painter, QRectF((side - w) / 2, (side - h) / 2, w, h))
    tint = QColor(BRAND_COLOURS.get(name) or color)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(image.rect(), tint)
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    _logo_cache[key] = pixmap
    return pixmap


def dot_pixmap(color, size=8, dpr=None):
    """A small status dot, drawn (not a text character)."""
    dpr = dpr or screen_dpr()
    side = int(round(size * dpr))
    image = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QRectF(0.5, 0.5, side - 1, side - 1))
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def sparkle_pixmap(size, dpr=None):
    """Gradient sparkle used for Ask Alfred (Apple-Intelligence-like colouring)."""
    dpr = dpr or screen_dpr()
    key = ('sparkle', size, dpr)
    if key in _icon_cache:
        return _icon_cache[key]
    base = icon_pixmap('sparkle', size, QColor('white'), dpr).toImage()
    base.setDevicePixelRatio(1)  # work in device pixels so the mask covers the whole icon
    image = QImage(base.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, image.width(), image.height())
    for i, color in enumerate(GLOW[:5]):
        gradient.setColorAt(i / 4, color)
    painter.fillRect(image.rect(), gradient)
    painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, base)
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    _icon_cache[key] = pixmap  # drawn on every frame of the mode switch and window paint
    return pixmap


APP_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'alfred.svg')
_app_logo_cache = {}


def logo_pixmap(size=64, dpr=None):
    """Alfred's logo, drawn from assets/alfred.svg at the screen's resolution."""
    dpr = dpr or screen_dpr()
    key = (size, dpr)
    if key not in _app_logo_cache:
        side = int(round(size * dpr))
        image = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        QSvgRenderer(APP_LOGO).render(painter, QRectF(0, 0, side, side))
        painter.end()
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(dpr)
        _app_logo_cache[key] = pixmap
    return _app_logo_cache[key]


def app_icon():
    """Alfred's icon at every size Windows asks for (tray, taskbar, notifications), each drawn from the vector."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
        icon.addPixmap(logo_pixmap(size, 1.0))
    return icon


# --------------------------------------------------------------------- glass
def blurred_backdrop(pixmap, strength=10):
    """Frosted-glass backdrop: downscale, gaussian blur, boost saturation (vibrancy).

    Runs before the panel's first frame, so it stays cheap (~20 ms rather than ~200): the heavy blurs work on 1/8 and 1/2
    size images and the result goes straight back to Qt (no PNG round trip). It looks the same as blurring at full size."""
    from PIL import Image, ImageEnhance, ImageFilter
    if pixmap is None or pixmap.isNull():
        return None
    image = pixmap.toImage()
    w, h = image.width(), image.height()
    small = image.scaled(max(1, w // 8), max(1, h // 8), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    small = small.convertToFormat(QImage.Format_RGB888)
    pil = Image.frombuffer('RGB', (small.width(), small.height()), bytes(small.constBits()), 'raw', 'RGB', small.bytesPerLine(), 1)
    pil = ImageEnhance.Color(pil.filter(ImageFilter.GaussianBlur(strength / 2.5))).enhance(1.7)
    half = pil.resize((max(1, w // 2), max(1, h // 2)), Image.BICUBIC).filter(ImageFilter.GaussianBlur(strength / 2))
    data = half.tobytes()
    large = QImage(data, half.width, half.height, half.width * 3, QImage.Format_RGB888)
    result = QPixmap.fromImage(large.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
    result.setDevicePixelRatio(pixmap.devicePixelRatio())
    return result


def fallback_backdrop(size, dark=True):
    """Soft wallpaper-like gradient when the screen cannot be captured."""
    pixmap = QPixmap(size)
    painter = QPainter(pixmap)
    gradient = QLinearGradient(0, 0, size.width(), size.height())
    if dark:
        gradient.setColorAt(0, QColor('#1d2a4a'))
        gradient.setColorAt(0.5, QColor('#2a1f45'))
        gradient.setColorAt(1, QColor('#0f2f3a'))
    else:
        gradient.setColorAt(0, QColor('#cfe3ff'))
        gradient.setColorAt(0.5, QColor('#e9dcff'))
        gradient.setColorAt(1, QColor('#d7f3ee'))
    painter.fillRect(pixmap.rect(), gradient)
    painter.end()
    return pixmap


SHADOW_DROP = 10  # the shadow sits this much lower than the panel
SHADOW_SPREAD = 26  # and reaches this far beyond it
_shadow_cache = {}


def _draw_shadow(p, rect, radius, color, spread):
    p.save()
    p.setPen(Qt.NoPen)
    for i in range(spread, 0, -2):
        c = QColor(color)
        c.setAlphaF(color.alphaF() * (1 - i / spread) ** 2 * 0.16)
        p.setBrush(c)
        p.drawRoundedRect(rect.adjusted(-i, -i + SHADOW_DROP, i, i + SHADOW_DROP), radius + i, radius + i)
    p.restore()


def _shadow_sprite(width, radius, color, spread, dpr):
    """The shadow of a short panel, drawn once at device resolution. Its middle rows are identical, so any taller
    panel's shadow is its top, a stretched middle band and its bottom: three blits instead of 13 large anti-aliased fills."""
    key = (round(width, 2), radius, color.rgba(), spread, dpr)
    if key not in _shadow_cache:
        pad, height = spread + SHADOW_DROP + 2, 2 * radius + 8
        image = QImage(int(math.ceil((width + 2 * pad) * dpr)), int(math.ceil((height + 2 * pad) * dpr)), QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        _draw_shadow(painter, QRectF(pad, pad, width, height), radius, color, spread)
        painter.end()
        pixmap = QPixmap.fromImage(image)
        top_cut = pad + radius + SHADOW_DROP + 2  # inside the band of identical rows
        band = pixmap.copy(0, int(round(top_cut * dpr)), pixmap.width(), int(round(2 * dpr)))
        band.setDevicePixelRatio(dpr)
        _shadow_cache[key] = (pixmap, band, pad, height, top_cut)
    return _shadow_cache[key]


def paint_shadow(p, rect, radius, color, spread=SHADOW_SPREAD):
    pixmap, band, pad, height, top_cut = _shadow_sprite(rect.width(), radius, color, spread, p.device().devicePixelRatioF())
    dpr = pixmap.devicePixelRatio()
    bottom_cut = top_cut + 2
    stretch = rect.height() - height  # extra height of this panel over the sprite's
    if stretch + bottom_cut - top_cut < 0:  # too short to slice (never for Alfred's panel)
        _draw_shadow(p, rect, radius, color, spread)
        return
    full_w = pixmap.width() / dpr
    left, top = rect.left() - pad, rect.top() - pad
    below = pixmap.height() / dpr - bottom_cut
    p.drawPixmap(QRectF(left, top, full_w, top_cut), pixmap, QRectF(0, 0, pixmap.width(), top_cut * dpr))
    # The middle is a tiled fill of identical rows (a scaled blit takes the raster engine's slow path).
    p.drawTiledPixmap(QRectF(left, top + top_cut, full_w, stretch + bottom_cut - top_cut), band)
    p.drawPixmap(QRectF(left, top + bottom_cut + stretch, full_w, below), pixmap,
                 QRectF(0, bottom_cut * dpr, pixmap.width(), below * dpr))


def paint_glass(p, rect, radius, palette, backdrop, backdrop_origin, span=None):
    """``span``: the height the tint and rim gradients stretch over. A fixed span keeps every pixel above the bottom edge
    the same while the panel grows or shrinks, so an animating panel only repaints its moving edge."""
    span = span or rect.height()
    # Fills of the rounded shape (the backdrop as a texture brush) instead of a clip path: an anti-aliased clip makes
    # the raster engine build a mask on every frame.
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.save()
    p.setPen(Qt.NoPen)
    if backdrop is not None:
        brush = QBrush(backdrop)
        brush.setTransform(QTransform.fromTranslate(backdrop_origin.x(), backdrop_origin.y()))
        p.setBrush(brush)
        p.drawPath(path)
    tint = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + span))
    tint.setColorAt(0, palette['tint'])
    tint.setColorAt(1, palette['tint_bottom'])
    p.setBrush(tint)
    p.drawPath(path)
    # Liquid-glass sheen: a soft specular band across the top.
    sheen = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + min(90, rect.height())))
    sheen.setColorAt(0, palette['sheen'])
    sheen.setColorAt(1, QColor(255, 255, 255, 0))
    p.setBrush(sheen)
    p.drawPath(path)
    p.restore()
    rim = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + span))
    rim.setColorAt(0, palette['rim_top'])
    rim.setColorAt(0.35, palette['rim_bottom'])
    rim.setColorAt(1, palette['rim_bottom'])
    p.setPen(QPen(QBrush(rim), 1.0))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


def paint_glow(p, rect, radius, phase, strength):
    """Rotating multi-colour edge glow while Alfred is thinking."""
    if strength <= 0.01:
        return
    gradient = QConicalGradient(rect.center(), -phase * 360)
    for i, color in enumerate(GLOW):
        gradient.setColorAt(i / (len(GLOW) - 1), color)
    p.save()
    p.setBrush(Qt.NoBrush)
    for width, alpha in ((14, 0.10), (8, 0.18), (4, 0.42), (1.6, 0.95)):
        p.setOpacity(alpha * strength)
        p.setPen(QPen(QBrush(gradient), width))
        p.drawRoundedRect(rect.adjusted(0.8, 0.8, -0.8, -0.8), radius, radius)
    p.restore()


# -------------------------------------------------------------------- toggle
class Toggle(QAbstractButton):
    """Apple-style switch with a spring-driven knob."""

    def __init__(self, palette_ref, parent=None):
        super().__init__(parent)
        self.palette_ref = palette_ref
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(40, 24)
        self.knob = Spring(0, 420, 0.72, lambda v: self.update())
        self.toggled.connect(lambda on: self.knob.to(1 if on else 0))

    def setChecked(self, value):
        super().setChecked(value)
        self.knob.snap(1 if value else 0)

    def sizeHint(self):
        return QSize(40, 24)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette_ref()
        t = max(0.0, min(1.0, self.knob.value))
        off, on = QColor(pal['fill_strong']), QColor(pal['green'])
        track = QColor(int(off.red() + (on.red() - off.red()) * t), int(off.green() + (on.green() - off.green()) * t),
                       int(off.blue() + (on.blue() - off.blue()) * t), int(off.alpha() + (255 - off.alpha()) * t))
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, 40, 24), 12, 12)
        stretch = 4 * (1 - abs(2 * t - 1)) if self.isDown() or self.knob.velocity else 0
        x = 2 + t * 16
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawRoundedRect(QRectF(x, 3, 20 + stretch, 20), 10, 10)
        p.setBrush(QColor('white'))
        p.drawRoundedRect(QRectF(x, 2, 20 + stretch, 20), 10, 10)


# -------------------------------------------------------------------- hotkey
class HotkeyFilter(QAbstractNativeEventFilter):
    """Delivers WM_HOTKEY from the GlobalHotkey registration to Qt."""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if os.name == 'nt' and event_type == b'windows_generic_MSG':
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312:  # WM_HOTKEY
                QTimer.singleShot(0, self.callback)
                return True, 0
        return False, 0
