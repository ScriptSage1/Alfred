"""Spotlight-style building blocks: springs, glass, theme, icons, hotkey."""
import ctypes
import math
import os
import time
from io import BytesIO

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QRadialGradient)
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
QLabel#caption {{ color: {s}; font-size: 8.5pt; }}
QLabel#section {{ color: {s}; font-size: 8.5pt; font-weight: 600; }}
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
QPushButton#source {{ background: {f}; border: 1px solid {sep}; border-radius: 9px; padding: 6px 10px; text-align: left; }}
QPushButton#source:hover {{ background: {fs}; }}
QPushButton#source[used="false"] {{ color: {s}; }}
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


class Spring:
    """Damped spring (semi-implicit Euler). Retargeting mid-flight keeps velocity,
    which is what makes motion feel fluid instead of restarting an easing curve."""

    def __init__(self, value=0.0, stiffness=260.0, damping=0.78, on_change=None):
        self.value = self.target = float(value)
        self.velocity = 0.0
        self.k = stiffness
        self.zeta = damping
        self.on_change = on_change

    def to(self, target, impulse=0.0):
        self.target = float(target)
        self.velocity += impulse
        if Motion.reduced:
            self.snap(target)
        else:
            DRIVER.add(self)

    def snap(self, value):
        self.value = self.target = float(value)
        self.velocity = 0.0
        if self.on_change:
            self.on_change(self.value)

    def step(self, dt):
        c = 2 * math.sqrt(self.k) * self.zeta
        accel = -self.k * (self.value - self.target) - c * self.velocity
        self.velocity += accel * dt
        self.value += self.velocity * dt
        settled = abs(self.velocity) < 0.02 and abs(self.value - self.target) < 0.02
        if settled:
            self.value, self.velocity = self.target, 0.0
        return settled


class _Driver(QObject):
    """One ~120 Hz timer for all springs; it stops when everything has settled."""

    def __init__(self):
        super().__init__()
        self.springs = set()
        self.timer = None
        self.last = 0.0

    def add(self, spring):
        if self.timer is None:
            self.timer = QTimer()
            self.timer.setTimerType(Qt.PreciseTimer)
            self.timer.timeout.connect(self.tick)
        self.springs.add(spring)
        if not self.timer.isActive():
            self.last = time.perf_counter()
            self.timer.start(8)

    def tick(self):
        now = time.perf_counter()
        elapsed = min(now - self.last, 0.05)
        self.last = now
        steps = max(1, int(elapsed / 0.004))
        for spring in list(self.springs):
            settled = False
            for _ in range(steps):
                settled = spring.step(elapsed / steps)
            if settled:
                self.springs.discard(spring)
            if spring.on_change:
                spring.on_change(spring.value)
        if not self.springs:
            self.timer.stop()


DRIVER = _Driver()


class Loop(QObject):
    """Phase 0..1 looping animation that only ticks while running (no idle CPU)."""
    tick = Signal(float)

    def __init__(self, period=2.4, parent=None):
        super().__init__(parent)
        self.period, self.phase = period, 0.0
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.last = 0.0

    def start(self):
        if Motion.reduced:
            return
        if not self.timer.isActive():
            self.last = time.perf_counter()
            self.timer.start(12)

    def stop(self):
        self.timer.stop()

    @property
    def running(self):
        return self.timer.isActive()

    def _tick(self):
        now = time.perf_counter()
        self.phase = (self.phase + (now - self.last) / self.period) % 1.0
        self.last = now
        self.tick.emit(self.phase)


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
    'plus': '<path d="M12 5v14M5 12h14" stroke="C" stroke-width="2.2" stroke-linecap="round"/>',
    'note': '<path d="M6 3h8l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" fill="C"/><path d="M8.5 12h7M8.5 15.5h7" stroke="#000" stroke-opacity=".35" stroke-width="1.5" stroke-linecap="round"/>',
}
_icon_cache = {}


def icon_pixmap(name, size, color, dpr=2.0):
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


def icon(name, size, color, dpr=2.0):
    return QIcon(icon_pixmap(name, size, color, dpr))


def sparkle_pixmap(size, dpr=2.0):
    """Gradient sparkle used for Ask Alfred (Apple-Intelligence-like colouring)."""
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
    return pixmap


def logo_pixmap(size=64, dpr=2.0):
    """Alfred mark: a glass orb with a gradient sparkle (Spotlight-adjacent, original)."""
    image = QImage(int(size * dpr), int(size * dpr), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(dpr, dpr)
    rect = QRectF(1, 1, size - 2, size - 2)
    orb = QRadialGradient(QPointF(size * 0.35, size * 0.3), size * 0.85)
    orb.setColorAt(0, QColor('#5AC8FA'))
    orb.setColorAt(0.55, QColor('#0A84FF'))
    orb.setColorAt(1, QColor('#5E5CE6'))
    p.setPen(Qt.NoPen)
    p.setBrush(orb)
    p.drawRoundedRect(rect, size * 0.28, size * 0.28)
    sheen = QLinearGradient(0, 0, 0, size * 0.55)
    sheen.setColorAt(0, QColor(255, 255, 255, 120))
    sheen.setColorAt(1, QColor(255, 255, 255, 0))
    p.setBrush(sheen)
    p.drawRoundedRect(QRectF(3, 2, size - 6, size * 0.5), size * 0.24, size * 0.24)
    p.drawPixmap(QRectF(size * 0.2, size * 0.2, size * 0.6, size * 0.6), icon_pixmap('sparkle', size * 0.6, QColor('white'), dpr), QRectF())
    p.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


# --------------------------------------------------------------------- glass
def blurred_backdrop(pixmap, strength=10):
    """Frosted-glass backdrop: downscale, gaussian blur, boost saturation (vibrancy)."""
    from PIL import Image, ImageEnhance, ImageFilter
    if pixmap is None or pixmap.isNull():
        return None
    image = pixmap.toImage().convertToFormat(QImage.Format_RGBA8888)
    w, h = image.width(), image.height()
    data = bytes(image.constBits())[:image.bytesPerLine() * h]
    pil = Image.frombuffer('RGBA', (w, h), data, 'raw', 'RGBA', image.bytesPerLine(), 1).convert('RGB')
    small = pil.resize((max(1, w // 8), max(1, h // 8)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(strength / 2.5))
    small = ImageEnhance.Color(small).enhance(1.7)
    large = small.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(strength))
    buffer = BytesIO()
    large.save(buffer, 'PNG')
    result = QPixmap()
    result.loadFromData(buffer.getvalue(), 'PNG')
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


def paint_shadow(p, rect, radius, color, spread=26):
    p.save()
    p.setPen(Qt.NoPen)
    for i in range(spread, 0, -2):
        c = QColor(color)
        c.setAlphaF(color.alphaF() * (1 - i / spread) ** 2 * 0.16)
        p.setBrush(c)
        p.drawRoundedRect(rect.adjusted(-i, -i + 10, i, i + 10), radius + i, radius + i)
    p.restore()


def paint_glass(p, rect, radius, palette, backdrop, backdrop_origin):
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.save()
    p.setClipPath(path)
    if backdrop is not None:
        p.drawPixmap(backdrop_origin, backdrop)
    tint = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    tint.setColorAt(0, palette['tint'])
    tint.setColorAt(1, palette['tint_bottom'])
    p.fillRect(rect, tint)
    # Liquid-glass sheen: a soft specular band across the top.
    sheen = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + min(90, rect.height())))
    sheen.setColorAt(0, palette['sheen'])
    sheen.setColorAt(1, QColor(255, 255, 255, 0))
    p.fillRect(rect, sheen)
    p.restore()
    rim = QLinearGradient(rect.topLeft(), rect.bottomLeft())
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
