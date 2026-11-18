"""Juno desktop UI: a Spotlight-style floating glass panel.

Search · Ask Juno · Knowledge · Settings live in one panel that springs open
from a single search bar. A global shortcut (default Ctrl+Alt+L) shows/hides
it; Esc hides it.
"""
import html
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import (QBrush, QColor, QKeySequence, QCursor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QIcon, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QRegion, QShortcut)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFileIconProvider, QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QKeySequenceEdit, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QRadioButton, QScrollArea, QSpinBox, QStackedWidget,
                               QSystemTrayIcon, QTextBrowser, QToolTip, QVBoxLayout, QWidget)

from . import APP_NAME, __version__, ai, chats, config, connect, glass, keys, mcp_server, models, opener, paths, picker, power, providers, rag, render, rerank, startup
from .index import IMAGE_SUFFIXES, VIDEO_SUFFIXES, Index, IndexBusy, discover, video_frame_png
from .watcher import FolderWatcher
from .ui_kit import (DRIVER, Loop, Motion, PALETTES, PANEL_SHADOW, Spring, Toggle, app_icon, brand_pixmap, dot_pixmap, icon,
                     icon_pixmap, logo_pixmap,
                     paint_glass, paint_glow, paint_shadow, palette_for, rgba, screen_dpr, snapped, sparkle_pixmap, stylesheet,
                     system_dark, HotkeyFilter, text_font, tune_text)

W, BAR, MAXH, M, R, FOOT = 780, 62, 600, 46, 26, 34
EDGE_FADE = 0.12  # seconds: the resting shape's rim and lens band fade in after the height animation
SEPARATOR_FADE = 60  # the line under the search bar fades in over the panel's first pixels of growth
DEFAULT_HOTKEY, FALLBACK_HOTKEYS = 'Ctrl+Alt+L', ['Ctrl+Alt+L', 'Ctrl+Alt+K', 'Ctrl+Alt+A']
SEARCH, ASK, SETTINGS, SETUP = range(4)  # SETUP: the first-run setup (SetupView)
PLACEHOLDERS = {SEARCH: 'Search your files', ASK: 'Ask anything about your files', SETTINGS: 'Settings', SETUP: ''}
PENDING_REUSE_SECONDS = 5
KIND_FILTERS = [('documents', 'Documents'), ('pdfs', 'PDFs'), ('slides', 'Slides'), ('spreadsheets', 'Sheets'), ('images', 'Images'),
                ('videos', 'Videos'), ('text', 'Text & data')]
DATE_FILTERS = [('Any time', 0), ('Past week', 7), ('Past month', 30), ('Past year', 365)]
LAYERS = ('names', 'text', 'meaning', 'pictures')  # the index's layers, in the order they become searchable
PHASE_LABELS = {'scanning': 'Looking for files', 'names': 'Listing files', 'indexing': 'Reading', 'embedding': 'Learning meaning',
                'reading pictures': 'Reading pictures', 'embedding pictures': 'Learning pictures', 'reading video': 'Reading video',
                'reading tables': 'Reading tables', 'adding context': 'Adding context',
                'reading code': 'Reading code', 'learning code': 'Learning code',
                'done': 'Finishing'}
BATTERY_TEXT = 'Paused on battery — names and text are searchable'


def phase_label(phase):
    return PHASE_LABELS.get(phase, phase or 'Indexing')


def layer_done(pair):
    """True when a [done, total] pair is complete (an empty layer has nothing left to do)."""
    done, total = pair
    return total <= 0 or done >= total


def layer_percent(pair):
    """Whole percent of a [done, total] pair, rounded down: 100 only when the layer is truly complete."""
    done, total = pair
    return 100 if layer_done(pair) else max(0, min(99, int(done * 100 // total)))


def layers_text(layers, unfinished_only=False):
    """'names ✓ · text ✓ · meaning 62% · pictures 10%' from {'names': [done, total], ...}; '' when unknown.
    ``unfinished_only`` leaves the finished layers out ('meaning 62% · pictures 10%'), for the narrow status line."""
    parts = [f"{name} {'✓' if layer_done(layers[name]) else str(layer_percent(layers[name])) + '%'}" for name in LAYERS
             if layers and name in layers and not (unfinished_only and layer_done(layers[name]))]
    return ' · '.join(parts)


def layers_incomplete(layers):
    return any(not layer_done(layers[name]) for name in LAYERS if layers and name in layers)


def layers_notice(layers):
    """The quiet line under the search box while indexing is unfinished; '' when every layer is complete."""
    for name in LAYERS:
        if layers and name in layers and not layer_done(layers[name]):
            return f'Still indexing — {name} {layer_percent(layers[name])}%. Results may be incomplete.'
    return ''


def result_subtitle(result):
    """A result row's second line: where it sits, then the matching text (picture hits have none)."""
    return ' — '.join(part for part in (result.get('location'), (result.get('snippet') or '').strip()) if part)
GROUPS = [('PDF Documents', {'pdf'}), ('Presentations', {'pptx'}), ('Spreadsheets', {'xlsx', 'csv'}),
          ('Images', {s.lstrip('.') for s in IMAGE_SUFFIXES}), ('Videos', {s.lstrip('.') for s in VIDEO_SUFFIXES})]


# ------------------------------------------------------------------ workers
class Signals(QObject):
    progress = Signal(object)
    token = Signal(str)
    done = Signal(object)
    failed = Signal(object)


class Job(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.setAutoDelete(False)
        self.fn, self.signals = fn, Signals()

    def run(self):
        try:
            self.signals.done.emit(self.fn(self.signals))
        except Exception as error:
            self.signals.failed.emit(error)


_JOBS = set()  # strong refs until the result reaches the UI thread


def run_job(fn, done=None, failed=None, progress=None, token=None):
    job = Job(fn)
    for signal, slot in ((job.signals.done, done), (job.signals.failed, failed), (job.signals.progress, progress), (job.signals.token, token)):
        if slot:
            signal.connect(slot)
    job.signals.done.connect(lambda _: _JOBS.discard(job))
    job.signals.failed.connect(lambda _: _JOBS.discard(job))
    _JOBS.add(job)
    QThreadPool.globalInstance().start(job)
    return job


class WheelGuard(QObject):
    """Drop-downs/number boxes ignore the mouse wheel unless focused, so scrolling a page
    over them can't silently change a setting."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and not obj.hasFocus():
            event.ignore()
            return True
        return False


WHEEL_GUARD = None


def guard_wheel(root):
    global WHEEL_GUARD
    WHEEL_GUARD = WHEEL_GUARD or WheelGuard()
    for widget in root.findChildren(QComboBox) + root.findChildren(QSpinBox):
        widget.setFocusPolicy(Qt.StrongFocus)
        widget.installEventFilter(WHEEL_GUARD)


class NumberBox(QSpinBox):
    """A number box whose value is selected when it gets focus, so typing replaces it (in a plain QSpinBox the digits
    land wherever the click put the cursor, next to the old number or the unit), and input outside its range is
    corrected to the nearest allowed value instead of being dropped."""

    def __init__(self):
        super().__init__()
        self.setCorrectionMode(QSpinBox.CorrectToNearestValue)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)  # after the click has placed the cursor


# How long a local model stays loaded after its last use (minutes; ai.keep_alive_minutes caps it at a day).
KEEP_LOADED = (5, 10, 15, 30, 60, 120, 240, 480, 1440)


def duration_text(minutes):
    if minutes < 60 or minutes % 60:
        return f'{minutes} minutes'
    hours = minutes // 60
    return f"{hours} hour{'s' * (hours != 1)}"


def fill_effort(combo, note, options, explanation, current):
    """Fill a thinking-effort drop-down with what the selected model supports."""
    combo.blockSignals(True)
    combo.clear()
    for value, text in options:
        combo.addItem(text, value)
    if not options:
        combo.addItem('Not available', None)
    values = [value for value, _ in options]
    if current in values:
        combo.setCurrentIndex(values.index(current))
    elif 'medium' in values:
        combo.setCurrentIndex(values.index('medium'))
    elif current not in ('off', None) and 'on' in values:
        combo.setCurrentIndex(values.index('on'))
    combo.setEnabled(bool(options))
    combo.setToolTip(explanation)
    combo.blockSignals(False)
    if note is not None:
        note.setText(explanation)


def open_path(path, loc=None):
    """Open a file; with ``loc`` (a result's page / seconds) at that spot when its default app supports it."""
    if loc and (loc.get('page') or loc.get('seconds') is not None):
        opener.open_at(path, loc, plain=_open_plain)
    else:
        _open_plain(path)


def _open_plain(path):
    if os.name == 'nt':
        os.startfile(str(path))
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def reveal_in_explorer(path):
    if os.name == 'nt':
        subprocess.Popen(['explorer', '/select,', str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))


def label(text='', name=None, wrap=False, selectable=False):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return widget


def button(text, slot=None, name=None, tip=None, icon_value=None):
    widget = QPushButton(text)
    if name:
        widget.setObjectName(name)
    if slot:
        widget.clicked.connect(slot)
    if tip:
        widget.setToolTip(tip)
    if icon_value is not None:
        widget.setIcon(icon_value)
        widget.setIconSize(QSize(14, 14))
    widget.setCursor(Qt.PointingHandCursor)
    return widget


def _above_panel(dialog):
    """Dialogs must sit above Juno's always-on-top panel, or they open behind it and can't be clicked."""
    dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    dialog.setWindowModality(Qt.ApplicationModal)
    QTimer.singleShot(0, lambda: _lift(dialog))
    QTimer.singleShot(200, lambda: _lift(dialog))
    return dialog


def _lift(dialog):
    """Raise a shown dialog above the panel. Qt's stay-on-top flag does not reach Windows for these dialogs (measured:
    not topmost, not owned by the panel, below it), so the window is made topmost through Windows itself."""
    try:
        if not dialog.isVisible():
            return
        dialog.raise_()
        dialog.activateWindow()
        if os.name == 'nt':
            import ctypes
            # HWND_TOPMOST; SWP_NOSIZE | SWP_NOMOVE
            ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(int(dialog.winId())), ctypes.c_void_p(-1), 0, 0, 0, 0, 0x1 | 0x2)
    except RuntimeError:  # the dialog was closed and deleted meanwhile
        pass


def ask_box(parent, title, text, buttons, default):
    """QMessageBox.question that stays above the panel; returns the button chosen (Esc counts as the escape button)."""
    box = _above_panel(QMessageBox(QMessageBox.Question, title, text, buttons, parent.window() if parent else None))
    box.setDefaultButton(default)
    box.exec()
    return box.standardButton(box.clickedButton()) if box.clickedButton() else QMessageBox.Cancel


def ask_text(parent, title, prompt, echo=QLineEdit.Normal):
    """QInputDialog.getText that stays above the panel; returns (text, ok)."""
    from PySide6.QtWidgets import QDialog, QInputDialog
    dialog = _above_panel(QInputDialog(parent.window() if parent else None))
    dialog.setWindowTitle(title)
    dialog.setLabelText(prompt)
    dialog.setTextEchoMode(echo)
    ok = dialog.exec() == QDialog.Accepted
    return dialog.textValue(), ok


def fade_in(widget, duration=0.22, dy=0):
    """Opacity spring for content changes (removed afterwards to keep text crisp)."""
    if Motion.reduced or Motion.instant:
        return
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    effect.setOpacity(0)
    spring = Spring(0, 160 / duration, 1.0)
    spring.fade = True  # a transition's snapshot finishes it first

    def change(value):
        try:
            effect.setOpacity(max(0.0, min(1.0, value)))
            if value >= 0.999:
                widget.setGraphicsEffect(None)
        except RuntimeError:
            DRIVER.springs.discard(spring)
    spring.on_change = change
    spring.to(1)
    widget._fade = spring


class FileIcons:
    """High-resolution Windows shell icons (cached per type) and image thumbnails."""
    provider = None
    cache = {}

    @classmethod
    def get(cls, path, size=28):
        path = Path(path)
        suffix = path.suffix.lower()
        dpr = screen_dpr()  # exactly the screen's ratio: no resampling when drawn
        if suffix in IMAGE_SUFFIXES and path.is_file():
            key = ('thumb', str(path), size, dpr)
            if key not in cls.cache:
                pixmap = QPixmap(str(path))
                if not pixmap.isNull():
                    side = int(round(size * dpr))
                    pixmap = pixmap.scaled(side, side, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    pixmap.setDevicePixelRatio(dpr)
                cls.cache[key] = pixmap
            return cls.cache[key]
        key = ('type', suffix, size, dpr)
        if key not in cls.cache:
            from PySide6.QtCore import QFileInfo
            cls.provider = cls.provider or QFileIconProvider()
            cls.cache[key] = cls.provider.icon(QFileInfo(str(path))).pixmap(QSize(size, size), dpr)
        return cls.cache[key]


# ------------------------------------------------------------------ widgets
class ModeSwitch(QWidget):
    """Glass icon buttons, one per mode; a liquid pill glides (and stretches) to the active one."""
    changed = Signal(int)
    ITEMS = [('search', 'Search  (Ctrl+1)'), ('sparkle', 'Ask Juno  (Ctrl+2 · Tab)'), ('gear', 'Settings  (Ctrl+3)')]
    SIZE = 34

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.index, self.hover = 0, -1
        self.setFixedSize(self.SIZE * len(self.ITEMS) + 8, self.SIZE + 6)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.left = Spring(4, 380, 0.8, lambda v: self.update(), precision=0.1)
        self.right = Spring(4 + self.SIZE, 380, 0.8, lambda v: self.update(), precision=0.1)

    def set(self, index, animate=True):
        moving_right = index > self.index
        self.index = index
        x = 4 + index * self.SIZE
        # Leading edge springs faster than the trailing edge → liquid stretch.
        self.left.k, self.right.k = (200, 520) if moving_right else (520, 200)
        if animate:
            self.left.to(x)
            self.right.to(x + self.SIZE)
        else:
            self.left.snap(x)
            self.right.snap(x + self.SIZE)
        self.update()

    def mouseMoveEvent(self, event):
        hover = int((event.position().x() - 4) // self.SIZE)
        hover = hover if 0 <= hover < len(self.ITEMS) else -1
        if hover != self.hover:
            self.hover = hover
            self.setToolTip(self.ITEMS[hover][1] if hover >= 0 else '')
            self.update()

    def leaveEvent(self, event):
        self.hover = -1
        self.update()

    def mousePressEvent(self, event):
        index = int((event.position().x() - 4) // self.SIZE)
        if 0 <= index < len(self.ITEMS):
            self.changed.emit(index)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.win.pal
        p.setPen(Qt.NoPen)
        p.setBrush(pal['fill'])
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), self.height() / 2, self.height() / 2)
        if self.hover >= 0 and self.hover != self.index:
            p.setBrush(pal['hover'])
            p.drawRoundedRect(QRectF(4 + self.hover * self.SIZE, 3, self.SIZE, self.SIZE), self.SIZE / 2, self.SIZE / 2)
        pill = QRectF(self.left.value, 3, self.right.value - self.left.value, self.SIZE)
        p.setBrush(pal['fill_strong'])
        p.setPen(QPen(pal['rim_top'], 0.8))
        p.drawRoundedRect(pill, self.SIZE / 2, self.SIZE / 2)
        for i, (name, _) in enumerate(self.ITEMS):
            rect = snapped(QRectF(4 + i * self.SIZE + 9, 12, 16, 16), self)
            if name == 'sparkle' and i == self.index:
                p.drawPixmap(rect, sparkle_pixmap(16), QRectF())
            else:
                p.drawPixmap(rect, icon_pixmap(name, 16, pal['text'] if i == self.index else pal['secondary']), QRectF())


class ShimmerLabel(QWidget):
    """Status text with a moving highlight sweep (only animates while active)."""

    def __init__(self, win, text=''):
        super().__init__()
        self.win, self.text, self.active = win, text, False
        self.loop = Loop(1.6, self)
        self.loop.tick.connect(lambda _: self.update())
        self.setFixedHeight(22)

    def set(self, text, active=True):
        self.text, self.active = text, active
        if active and self.isVisible():
            self.loop.start()
        else:
            self.loop.stop()
        self.update()

    def showEvent(self, event):
        if self.active:
            self.loop.start()

    def hideEvent(self, event):
        self.loop.stop()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        pal = self.win.pal
        font = text_font(10)
        p.setFont(font)
        if self.active and not Motion.reduced:
            width = max(1, QFontMetrics(font).horizontalAdvance(self.text))
            x = -120 + self.loop.phase * (width + 240)
            gradient = QLinearGradient(x - 60, 0, x + 60, 0)
            gradient.setColorAt(0, pal['tertiary'])
            gradient.setColorAt(0.5, pal['text'])
            gradient.setColorAt(1, pal['tertiary'])
            p.setPen(QPen(gradient, 1))
        else:
            p.setPen(pal['secondary'])
        p.drawText(QRectF(0, 0, self.width(), self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text)


class ResultList(QWidget):
    """Custom-painted result rows with a spring-animated, stretchy selection highlight."""
    selected = Signal(object)
    activated = Signal(object)
    HEADER, ROW = 28, 46

    def __init__(self, win, scroll):
        super().__init__()
        self.win, self.scroll = win, scroll
        self.items, self.rows, self.current, self.hover = [], [], -1, -1
        self.setMouseTracking(True)
        self.top = Spring(0, 500, 0.82, lambda v: self.update(), precision=0.1)
        self.bottom = Spring(0, 500, 0.82, lambda v: self.update(), precision=0.1)
        self.reveal = Spring(1, 90, 1.0, lambda v: self.update())
        self.scroll_spring = Spring(0, 300, 1.0, lambda v: self.scroll.verticalScrollBar().setValue(int(v)), precision=0.1)

    def set_items(self, items):
        """items: list of ('header', text) or ('row', dict)."""
        self.items = items
        self.rows, y = [], 6
        self.layout_y = []
        for kind, value in items:
            height = self.HEADER if kind == 'header' else self.ROW
            self.layout_y.append((y, height))
            if kind == 'row':
                self.rows.append(len(self.layout_y) - 1)
            y += height
        self.setFixedHeight(max(y + 8, 10))
        self.current = -1
        self.reveal.snap(0)
        self.reveal.to(1)
        if self.rows:
            self.select(0, animate=False)
        self.update()

    def item(self, row=None):
        row = self.current if row is None else row
        if 0 <= row < len(self.rows):
            return self.items[self.rows[row]][1]
        return None

    def select(self, row, animate=True):
        if not self.rows:
            return
        row = max(0, min(row, len(self.rows) - 1))
        down = row > self.current
        self.current = row
        y, h = self.layout_y[self.rows[row]]
        self.top.k, self.bottom.k = (230, 560) if down else (560, 230)
        if animate and not Motion.reduced:
            self.top.to(y)
            self.bottom.to(y + h)
        else:
            self.top.snap(y)
            self.bottom.snap(y + h)
        self.ensure_visible(y, h)
        self.selected.emit(self.item())
        self.update()

    def move(self, delta):
        if self.rows:
            self.select(self.current + delta)

    def ensure_visible(self, y, h):
        bar = self.scroll.verticalScrollBar()
        view = self.scroll.viewport().height()
        target = bar.value()
        if y - 30 < target:
            target = max(0, y - 30)
        elif y + h + 12 > target + view:
            target = y + h + 12 - view
        if target != bar.value():
            self.scroll_spring.snap(bar.value())
            self.scroll_spring.to(target)

    def row_at(self, y):
        for row, index in enumerate(self.rows):
            top, height = self.layout_y[index]
            if top <= y < top + height:
                return row
        return -1

    def mouseMoveEvent(self, event):
        hover = self.row_at(event.position().y())
        if hover != self.hover:
            self.hover = hover
            self.setCursor(Qt.PointingHandCursor if hover >= 0 else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, event):
        self.hover = -1
        self.update()

    def mousePressEvent(self, event):
        row = self.row_at(event.position().y())
        if row >= 0:
            self.select(row)

    def mouseDoubleClickEvent(self, event):
        row = self.row_at(event.position().y())
        if row >= 0:
            self.activated.emit(self.item(row))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        pal = self.win.pal
        width = self.width()
        clip = event.rect()
        if self.rows and self.current >= 0:
            top, bottom = min(self.top.value, self.bottom.value), max(self.top.value, self.bottom.value)
            p.setPen(Qt.NoPen)
            p.setBrush(pal['accent'])
            p.drawRoundedRect(QRectF(6, top + 1, width - 12, bottom - top - 2), 9, 9)
        title_font = text_font(10, QFont.Medium)
        sub_font = text_font(9)
        head_font = text_font(9, QFont.DemiBold)
        row_number = -1
        for index, (kind, value) in enumerate(self.items):
            y, h = self.layout_y[index]
            if y > clip.bottom() + 2 or y + h < clip.top() - 2:
                if kind == 'row':
                    row_number += 1
                continue
            t = max(0.0, min(1.0, (self.reveal.value * 1.6 - index * 0.06) / 0.5))  # stagger
            p.setOpacity(t)
            offset = (1 - t) * 8
            if kind == 'header':
                p.setFont(head_font)
                p.setPen(pal['secondary'])
                p.drawText(QRectF(16, y + offset + 8, width - 32, h - 8), Qt.AlignLeft | Qt.AlignVCenter, value)
                continue
            row_number += 1
            top, bottom = min(self.top.value, self.bottom.value), max(self.top.value, self.bottom.value)
            cover = max(0.0, min(1.0, (min(bottom, y + h) - max(top, y)) / h)) if self.current >= 0 else 0
            if row_number == self.hover and row_number != self.current:
                p.setPen(Qt.NoPen)
                p.setBrush(pal['hover'])
                p.drawRoundedRect(QRectF(6, y + 1 + offset, width - 12, h - 2), 9, 9)
            text = _mix(pal['text'], pal['accent_text'], cover)
            if value.get('muted'):
                text = _mix(text, pal['secondary'], 0.6 * (1 - cover))  # weaker matches read quieter
            on_accent = QColor(pal['accent_text'])
            on_accent.setAlpha(200)
            secondary = _mix(pal['secondary'], on_accent, cover)
            pixmap = value.get('pixmap')
            if pixmap is not None and not pixmap.isNull():
                target = snapped(QRectF(16, y + offset + (h - 30) / 2, 30, 30), self)
                if value.get('rounded'):
                    path = QPainterPath()
                    path.addRoundedRect(target, 6, 6)
                    p.save()
                    p.setClipPath(path)
                    p.drawPixmap(target, pixmap, QRectF())
                    p.restore()
                else:
                    p.drawPixmap(target, pixmap, QRectF())
            p.setFont(title_font)
            p.setPen(text)
            right = value.get('right', '')
            right_w = QFontMetrics(sub_font).horizontalAdvance(right) + 18 if right else 0
            p.drawText(QRectF(58, y + offset + 5, width - 72 - right_w, 20), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(title_font).elidedText(value['title'], Qt.ElideMiddle, int(width - 72 - right_w)))
            p.setFont(sub_font)
            p.setPen(secondary)
            p.drawText(QRectF(58, y + offset + 24, width - 74, 17), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(sub_font).elidedText(value.get('subtitle', ''), Qt.ElideRight, int(width - 74)))
            if right:
                p.drawText(QRectF(width - right_w - 14, y + offset + 5, right_w, 20), Qt.AlignRight | Qt.AlignVCenter, right)
        p.setOpacity(1)


def _mix(a, b, t):
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


class EmptyState(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(8)
        self.art = QLabel()
        self.art.setAlignment(Qt.AlignCenter)
        self.title = label('', 'title')
        self.title.setAlignment(Qt.AlignCenter)
        self.text = label('', 'secondary', wrap=True)
        self.text.setAlignment(Qt.AlignCenter)
        self.text.setFixedWidth(440)
        self.buttons = QHBoxLayout()
        self.buttons.setAlignment(Qt.AlignCenter)
        for widget in (self.art, self.title, self.text):
            layout.addWidget(widget, 0, Qt.AlignCenter)
        layout.addLayout(self.buttons)

    def blank(self):
        """Show nothing (while the panel collapses, or until results for a new query arrive)."""
        for widget in (self.art, self.title, self.text):
            widget.hide()
        while self.buttons.count():
            item = self.buttons.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def show_state(self, title, text, actions=(), art='search'):
        for widget in (self.art, self.title, self.text):
            widget.show()
        self.art.setPixmap(sparkle_pixmap(44) if art == 'sparkle' else logo_pixmap(48) if art == 'logo'
                           else icon_pixmap(art, 40, self.win.pal['tertiary']))
        self.title.setText(title)
        self.text.setText(text)
        while self.buttons.count():
            item = self.buttons.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for number, (caption, slot) in enumerate(actions):
            self.buttons.addWidget(button(caption, slot, 'primary' if number == 0 else None))
        fade_in(self)


# ------------------------------------------------------------------ views
class ImageTextCard(QFrame):
    """The text OCR found in a picture, in a block under its preview."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName('group')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 11)
        layout.setSpacing(5)
        self.ocr_title = label('TEXT IN IMAGE', 'section')
        self.ocr = label('', 'secondary', wrap=True, selectable=True)
        for widget in (self.ocr_title, self.ocr):
            layout.addWidget(widget)

    def show_state(self, ocr_text=''):
        """Hides itself when there's no text to show."""
        has_ocr = bool(ocr_text.strip())
        self.ocr.setText(ocr_text.strip()[:900])
        self.setVisible(has_ocr)
        if has_ocr:
            fade_in(self)


class SearchView(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.generation = 0
        self.query = ''
        self.shown = None  # the query whose results (or "no results") are showing
        self.weak_open, self.weak_for = False, None  # weaker matches revealed for this query?
        self.last = None
        self.pending_seen = (0.0, None)  # (when, answer) of the last disk walk for index changes
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.kinds, self.age = set(), 0  # result filters: file kinds chosen, and the index into DATE_FILTERS
        self.filters = QWidget()
        self.filters.setObjectName('clear')
        bar = QHBoxLayout(self.filters)
        bar.setContentsMargins(16, 2, 16, 6)
        bar.setSpacing(6)
        self.kind_chips = {}
        for number, (key, name) in enumerate(KIND_FILTERS, 1):
            chip = button(name, None, 'filter', f'Only {name.lower()}  (Alt+{number})')
            chip.setCheckable(True)
            chip.setFocusPolicy(Qt.NoFocus)  # typing stays in the search box; Alt+number toggles from the keyboard
            chip.toggled.connect(lambda on, k=key: self.set_kind(k, on))
            self.kind_chips[key] = chip
            bar.addWidget(chip)
            QShortcut(QKeySequence(f'Alt+{number}'), win, activated=lambda c=chip: self.keyed(c.toggle))
        bar.addStretch()
        self.age_chip = button(DATE_FILTERS[0][0], self.cycle_age, 'filter', 'Modified: click to cycle  (Alt+D)')
        self.age_chip.setCheckable(True)
        self.age_chip.setFocusPolicy(Qt.NoFocus)
        QShortcut(QKeySequence('Alt+D'), win, activated=lambda: self.keyed(self.cycle_age))
        bar.addWidget(self.age_chip)
        self.filters.hide()
        outer.addWidget(self.filters)
        self.notice = label('', 'caption')  # "Still indexing — …": quiet, only while a layer is unfinished
        self.notice.setContentsMargins(18, 0, 16, 6)
        self.notice.hide()
        outer.addWidget(self.notice)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.empty = EmptyState(win)
        self.stack.addWidget(self.empty)
        results = QWidget()
        row = QHBoxLayout(results)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list = ResultList(win, self.scroll)
        self.scroll.setWidget(self.list)
        self.scroll.setFixedWidth(372)
        self.list.selected.connect(self.preview)
        self.list.activated.connect(self.activate_item)
        row.addWidget(self.scroll)
        self.divider = QFrame()
        self.divider.setFixedWidth(1)
        row.addWidget(self.divider)
        self.pane = QWidget()
        pane = QVBoxLayout(self.pane)
        pane.setContentsMargins(24, 18, 22, 12)
        pane.setSpacing(6)
        head = QHBoxLayout()
        self.big_icon = QLabel()
        self.big_icon.setFixedSize(56, 56)
        head.addWidget(self.big_icon)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.p_title = label('', 'hero', wrap=True)
        self.p_meta = label('', 'caption', wrap=True)
        titles.addWidget(self.p_title)
        titles.addWidget(self.p_meta)
        head.addLayout(titles, 1)
        pane.addLayout(head)
        # A picture or video: its image and any text read from it, in one column that scrolls as a
        # whole only when it doesn't fit. Every part takes the height its content needs at the pane's width, nothing
        # empty holds space, and the buttons below never get pushed out of the panel.
        media = QWidget()
        media.setObjectName('clear')
        column = QVBoxLayout(media)
        column.setContentsMargins(0, 2, 6, 2)
        column.setSpacing(8)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.hide()
        self.image_source, self.image_height = None, 0
        column.addWidget(self.image)
        self.card = ImageTextCard(win)
        self.card.hide()
        column.addWidget(self.card)
        self.media_text = label('', 'secondary', wrap=True, selectable=True)
        self.media_text.setTextFormat(Qt.RichText)
        self.media_text.hide()
        column.addWidget(self.media_text)
        column.addStretch()
        self.media_area = QScrollArea()
        self.media_area.setWidgetResizable(True)
        self.media_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.media_area.setWidget(media)
        self.media_area.hide()
        pane.addWidget(self.media_area, 1)
        self.excerpt = QTextBrowser()
        self.excerpt.setObjectName('excerpt')
        self.excerpt.setOpenLinks(False)
        pane.addWidget(self.excerpt, 1)
        actions = QHBoxLayout()
        self.open_button = button('Open', self.open_current, 'primary')
        self.reveal_button = button('Show in folder', self.reveal_current)
        self.ask_button = button('Ask about this', self.ask_current, None, None)
        for widget in (self.open_button, self.reveal_button, self.ask_button):
            actions.addWidget(widget)
        actions.addStretch()
        pane.addLayout(actions)
        row.addWidget(self.pane, 1)
        self.stack.addWidget(results)
        self.debounce = QTimer(self, singleShot=True, timeout=self.run)

    def themed(self):
        self.divider.setStyleSheet(f"background: rgba({self.win.pal['separator'].red()},{self.win.pal['separator'].green()},"
                                   f"{self.win.pal['separator'].blue()},{self.win.pal['separator'].alphaF():.3f});")

    def keyed(self, action):
        """A filter shortcut: only while the search is showing its filters."""
        if self.win.mode == SEARCH and len(self.query) >= 2:
            action()

    def set_kind(self, key, on):
        self.kinds.discard(key)
        if on:
            self.kinds.add(key)
        self.refilter()

    def cycle_age(self):
        self.age = (self.age + 1) % len(DATE_FILTERS)
        self.age_chip.setText(DATE_FILTERS[self.age][0])
        self.age_chip.setChecked(self.age > 0)
        self.refilter()

    def refilter(self):
        if len(self.query) >= 2:
            self.run()  # the same search under the new filters

    def clear_filters(self):
        for chip in self.kind_chips.values():
            chip.blockSignals(True)
            chip.setChecked(False)
            chip.blockSignals(False)
        self.kinds, self.age = set(), 0
        self.age_chip.setText(DATE_FILTERS[0][0])
        self.age_chip.setChecked(False)
        self.refilter()

    def filter_options(self):
        days = DATE_FILTERS[self.age][1]
        return {'kinds': set(self.kinds) or None, 'modified_after': time.time() - days * 86400 if days else None}

    def wants_expanded(self):
        return len(self.query) >= 2 or self.win.problem_state() is not None

    def update_notice(self):
        text = layers_notice(self.win.layers) if len(self.query) >= 2 else ''
        if text != self.notice.text():
            self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def set_query(self, text):
        self.query = text.strip()
        self.update_notice()
        self.filters.setVisible(len(self.query) >= 2)
        if len(self.query) < 2:
            self.generation += 1
            self.show_problem_or_collapse()
            return
        self.debounce.start(70)

    def show_problem_or_collapse(self):
        problem = self.win.problem_state()
        if problem and not self.query:
            self.empty.show_state(*problem)
            self.stack.setCurrentIndex(0)
        elif self.shown is not None:
            self.blank()  # the old results must not shrink away with the panel
        self.win.update_height()

    def blank(self):
        self.shown = None
        self.empty.blank()
        self.stack.setCurrentIndex(0)

    def run(self):
        self.generation += 1
        generation, query = self.generation, self.query
        settings, index = self.win.settings, self.win.index
        options = self.filter_options()
        def work(signals):
            embedder = ai.Embedder.from_settings(settings)
            results = index.search(query, limit=24, mode='hybrid', embedder=embedder, include_weak=True, **options)
            pending = None
            if not any(not r.get('weak') for r in results) and not options['kinds'] and not options['modified_after']:
                when, pending = self.pending_seen
                if time.monotonic() - when > PENDING_REUSE_SECONDS:  # a disk walk: not once per keystroke
                    pending = index.pending_changes()
                    self.pending_seen = (time.monotonic(), pending)
            return results, pending
        run_job(work, lambda value: self.show(value[0], generation, query, value[1]),
                lambda error: generation == self.generation and self.show_error(error))

    def show_error(self, error):
        self.shown = None
        self.empty.show_state('Search unavailable', str(error), [('Rescan', self.win.start_index)], 'warning')
        self.stack.setCurrentIndex(0)
        self.win.update_height()

    def show(self, results, generation, query, pending=None):
        if generation != self.generation:
            return
        self.shown = query
        if query != self.weak_for:
            self.weak_open, self.weak_for = False, query
        self.last = (results, generation, query, pending)
        strong = [r for r in results if not r.get('weak')]
        weak = [r for r in results if r.get('weak')]
        results = strong + (weak if self.weak_open else [])
        if not strong and pending and any(pending.values()):
            count = sum(pending.values())
            self.empty.show_state('Your folder has changed', f'{count} file{"s" * (count != 1)} added, changed or removed since the last '
                                  'index, so results may be missing.', [('Index now', self.win.start_index)], 'refresh')
            self.stack.setCurrentIndex(0)
            self.win.update_height()
            return
        if not results and weak:
            count = len(weak)
            self.empty.show_state(f'No strong matches for “{query}”', f'{count} weaker match{"es" * (count != 1)} found, which may not be what you meant.',
                                  [(f'Show {count} weaker match{"es" * (count != 1)}', self.show_weak), ('Ask Juno  ⇥', lambda: self.win.set_mode(ASK))], 'search')
            self.stack.setCurrentIndex(0)
            self.win.update_height()
            return
        if not results and (self.kinds or self.age):
            self.empty.show_state(f'No results for “{query}”', 'Nothing matches with the filters on above.',
                                  [('Clear filters', self.clear_filters)], 'search')
            self.stack.setCurrentIndex(0)
            self.win.update_height()
            return
        if not results:
            self.empty.show_state(f'No results for “{query}”', 'Nothing in your indexed files matches. Ask Juno to reason over your documents instead.',
                                  [('Ask Juno  ⇥', lambda: self.win.set_mode(ASK))], 'search')
            self.stack.setCurrentIndex(0)
            self.win.update_height()
            return
        items, seen = [], set()
        results = [r for r in results if not (r['path'] in seen or seen.add(r['path']))]

        def row(result):
            return ('row', dict(result, kind_row='file', muted=bool(result.get('weak')), title=result['filename'], pixmap=FileIcons.get(result['path']),
                                rounded=Path(result['path']).suffix.lower() in IMAGE_SUFFIXES,
                                subtitle=result_subtitle(result), right=result.get('folder', '')))
        shown_strong = [r for r in results if not r.get('weak')]
        shown_weak = [r for r in results if r.get('weak')]
        if shown_strong:
            items.append(('header', 'Top Hit'))
            items.append(row(shown_strong[0]))
        grouped = {}
        for result in shown_strong[1:]:
            group = next((name for name, types in GROUPS if result['file_type'] in types), 'Documents')
            grouped.setdefault(group, []).append(result)
        for name in ['Documents', 'PDF Documents', 'Presentations', 'Spreadsheets', 'Images', 'Videos']:
            if grouped.get(name):
                items.append(('header', name))
                items.extend(row(result) for result in grouped[name])
        if shown_weak:
            items.append(('header', 'Weaker matches'))
            items.extend(row(result) for result in shown_weak)
        elif weak and not self.weak_open:
            items.append(('row', dict(kind_row='weak', title=f'Show {len(weak)} weaker match{"es" * (len(weak) != 1)}', pixmap=None,
                                      subtitle='Partial matches, below the usual confidence', right='')))
        items.append(('header', 'Ask Juno'))
        items.append(('row', dict(kind_row='ask', title=f'Ask Juno: “{query}”', pixmap=sparkle_pixmap(30),
                                  subtitle='Get an answer grounded in your files, with citations', right='Tab')))
        self.list.set_items(items)
        self.list.scroll_spring.snap(0)
        self.stack.setCurrentIndex(1)
        self.win.update_height()

    def show_weak(self):
        if self.last and self.last[1] == self.generation:
            self.weak_open = True
            self.show(*self.last)

    def preview(self, item):
        if not item:
            return
        if item.get('kind_row') == 'weak':
            self.big_icon.setPixmap(sparkle_pixmap(52))
            self.set_title('Weaker matches')
            self.media_area.hide()
            self.excerpt.show()
            self.p_meta.setText('Below the usual confidence')
            self.excerpt.setHtml("<p style='font-size:11pt'>These passages match only part of what you typed, or only by meaning. "
                                 "They are listed apart so they don't look like strong results.</p><p>Press <b>Enter</b> to show them.</p>")
            for widget in (self.open_button, self.reveal_button, self.ask_button):
                widget.hide()
            fade_in(self.pane)
            return
        if item.get('kind_row') == 'ask':
            self.big_icon.setPixmap(sparkle_pixmap(52))
            self.set_title('Ask Juno')
            self.media_area.hide()
            self.excerpt.show()
            self.p_meta.setText(f"{ai.ChatProvider(self.win.settings).describe()['summary']}")
            self.excerpt.setHtml(f"<p style='font-size:11pt'>Juno searches your indexed files for “{html.escape(self.query)}”, "
                                 "reads the most relevant passages, and writes an answer that cites each source.</p>"
                                 "<p>Press <b>Enter</b> or <b>Tab</b> to ask.</p>")
            for widget in (self.open_button, self.reveal_button, self.ask_button):
                widget.setVisible(widget is self.ask_button)
            fade_in(self.pane)
            return
        for widget in (self.open_button, self.reveal_button, self.ask_button):
            widget.setVisible(True)
        absolute = Path(item['path'])
        self.big_icon.setPixmap(FileIcons.get(absolute, 52))
        self.set_title(item['filename'])
        size = item.get('size') or 0
        nice = f'{size / 1024:.0f} KB' if size >= 1024 else f'{size} bytes'
        self.p_meta.setText(' · '.join(part for part in (item['file_type'].upper(), nice, item['location'], f"modified {item['modified']}") if part))
        if Path(item['path']).suffix.lower() in IMAGE_SUFFIXES and absolute.is_file():
            pixmap = QPixmap(str(absolute))
            if not pixmap.isNull():
                self.show_image(pixmap, 150)
        elif Path(item['path']).suffix.lower() in VIDEO_SUFFIXES and absolute.is_file():
            self.image.hide()
            self.image_source = None

            def show_frame(data):
                if self.list.item() is not item or not data:
                    return
                pixmap = QPixmap()
                if pixmap.loadFromData(data, 'PNG'):
                    self.show_image(pixmap, 170)
                    fade_in(self.image)
            moment = item.get('seconds') or 0
            run_job(lambda s: video_frame_png(absolute, moment), show_frame)
        else:
            self.image.hide()
            self.image_source = None
        self.excerpt.setHtml('')
        self.media_text.clear()
        self.media_text.hide()
        media = Path(item['path']).suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES
        picture = Path(item['path']).suffix.lower() in IMAGE_SUFFIXES
        self.card.setVisible(False)
        self.excerpt.setVisible(not media)  # a picture's or video's text goes in its column
        self.media_area.setVisible(media)
        self.media_area.verticalScrollBar().setValue(0)
        self.load_details(item, media, picture)
        fade_in(self.pane)

    def load_details(self, item, media, picture):
        query, index = self.query, self.win.index

        def show(ex):
            if self.list.item() is not item:
                return
            if not media:
                self.excerpt.setHtml(_highlight(ex['excerpt'], query, self.win.pal) if ex['excerpt'] else '')
            elif not picture:  # a video: the text read from its frames, under the frame
                self.media_text.setText(_highlight(ex['excerpt'], query, self.win.pal) if ex['excerpt'] else '')
                self.media_text.setVisible(bool(ex['excerpt']))
            if media:
                self.card.show_state(ex.get('ocr_text', '') if picture else '')
                self.fit_image()
        run_job(lambda s: index.read_excerpt(item['path'], segment_id=item.get('segment_id'), max_chars=2400), show,
                lambda error: self.list.item() is item and self.excerpt.setPlainText(f'Preview unavailable: {error}'))

    IMAGE_WIDTH, IMAGE_MIN_HEIGHT = 330, 96

    def show_image(self, pixmap, height):
        """Show a picture (or a video frame) at most ``height`` tall; fit_image may shrink it to leave room for its text."""
        self.image_source, self.image_height = pixmap, height
        self.image.show()
        self.fit_image()

    def fit_image(self):
        """Size the image so the image and its text fit the column without scrolling when they can: the
        image gives up height first (down to IMAGE_MIN_HEIGHT); only text that still doesn't fit scrolls."""
        source = self.image_source
        if source is None or self.image.isHidden():
            return
        column = self.media_area.widget().layout()
        width = self.media_area.viewport().width() - column.contentsMargins().left() - column.contentsMargins().right()
        others = sum(w.heightForWidth(width) if w.hasHeightForWidth() else w.sizeHint().height()
                     for w in (self.card, self.media_text) if not w.isHidden())
        shown = sum(1 for w in (self.card, self.media_text) if not w.isHidden())
        room = (self.media_area.viewport().height() - column.contentsMargins().top() - column.contentsMargins().bottom()
                - others - column.spacing() * shown)
        height = max(self.IMAGE_MIN_HEIGHT, min(self.image_height, room))
        dpr = self.devicePixelRatioF()  # exactly the screen's ratio: shown 1:1, not resampled
        scaled = source.scaled(round(self.IMAGE_WIDTH * dpr), round(height * dpr), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self.image.setPixmap(_rounded(scaled, 10))

    def set_title(self, name):
        """The file name on at most two lines, broken at a natural point and elided in the middle;
        the full name is in the tooltip."""
        metrics, width = self.p_title.fontMetrics(), max(180, self.pane.width() - 24 - 22 - 56 - 14)
        self.p_title.setToolTip(name)
        self.p_title.setWordWrap(False)
        if metrics.horizontalAdvance(name) <= width:
            self.p_title.setText(name)
            return
        cut = len(name)
        while cut > 1 and metrics.horizontalAdvance(name[:cut]) > width:
            cut -= 1
        breaks = [i + 1 for i in range(max(1, cut // 2), cut) if name[i] in ' -_.,']
        cut = breaks[-1] if breaks else cut
        self.p_title.setText(name[:cut].rstrip() + '\n' + metrics.elidedText(name[cut:].lstrip(), Qt.ElideMiddle, width))

    def activate(self):
        item = self.list.item()
        if item:
            self.activate_item(item)

    def activate_item(self, item):
        if item.get('kind_row') == 'ask':
            self.win.set_mode(ASK)  # carries the typed text over; nothing is sent until Enter
        elif item.get('kind_row') == 'weak':
            self.show_weak()
        else:
            self.open_current()

    def current_path(self):
        item = self.list.item()
        if not item or item.get('kind_row') != 'file':
            return None
        return self.win.index.resolve(item['path'])

    def open_current(self):
        try:
            target = self.current_path()
            if target:
                open_path(target, self.list.item())
                self.win.hide_panel()
        except (paths.PathError, OSError) as error:
            self.win.toast(f'Cannot open: {error}', 'red')

    def reveal_current(self):
        try:
            target = self.current_path()
            if target:
                reveal_in_explorer(target)
        except paths.PathError as error:
            self.win.toast(str(error), 'red')

    def ask_current(self):
        item = self.list.item()
        if item and item.get('kind_row') == 'file':
            self.win.search.clear()
            self.win.set_mode(ASK)
            self.win.ask_view.set_scope(item['path'])
        else:
            self.win.set_mode(ASK)


def _rounded(pixmap, radius):
    result = QPixmap(pixmap.size())
    result.setDevicePixelRatio(pixmap.devicePixelRatio())
    result.fill(Qt.transparent)
    p = QPainter(result)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    size = pixmap.deviceIndependentSize()
    path.addRoundedRect(QRectF(0, 0, size.width(), size.height()), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pixmap)
    p.end()
    return result


def _highlight(text, query, pal):
    escaped = html.escape(text).replace('\n', '<br>')
    words = [w for w in re.findall(r'\w[\w.-]*\w|\w', query.casefold()) if len(w) > 2][:8]
    accent = pal['accent']
    for word in sorted(set(words), key=len, reverse=True):
        escaped = re.sub(r'(?i)(?<![\w>])(' + re.escape(html.escape(word)) + r')',
                         rf"<span style='background: rgba({accent.red()},{accent.green()},{accent.blue()},0.30); border-radius: 3px'>\1</span>", escaped)
    return f"<div style='font-size:10pt; line-height:150%'>{escaped}</div>"


def chip_colors(pal):
    return {'accent': pal['accent'].name(), 'chip': rgba(pal['accent'], .16), 'fill': rgba(pal['fill_strong']),
            'tag': rgba(pal['fill_strong']), 'secondary': rgba(pal['secondary'])}


def passage_html(text, sentence, pal, limit=1400):
    """A cited passage with the sentence that supports the answer highlighted (trimmed around it when long)."""
    text = text or ''
    start = text.find(sentence) if sentence else -1
    if len(text) > limit:
        begin = max(0, min(start - limit // 3, len(text) - limit)) if start >= 0 else 0
        text = ('…' if begin else '') + text[begin:begin + limit] + ('…' if begin + limit < len(text) else '')
        start = text.find(sentence) if sentence else -1
    if start < 0:
        return html.escape(text).replace('\n', '<br>')
    accent = pal['accent']
    mark = f"<span style='background-color: rgba({accent.red()},{accent.green()},{accent.blue()},0.28);'>"
    return (html.escape(text[:start]) + mark + html.escape(sentence) + '</span>' + html.escape(text[start + len(sentence):])).replace('\n', '<br>')


def cite_preview(source, pal):
    """Tooltip for a citation chip: file, place, and the supporting sentence."""
    quote = source.get('highlight') or (source.get('text') or '')[:260]
    if len(quote) > 320:
        quote = quote[:320].rsplit(' ', 1)[0] + '…'
    return (f"<b>{html.escape(source.get('filename', ''))}</b> <span style='color:{rgba(pal['secondary'])}'>· "
            f"{html.escape(source.get('location', ''))}</span><br><div style='max-width: 360px'>{html.escape(quote)}</div>")


class Turn(QWidget):
    """One exchange: your question in a bubble on the right, Juno's answer on the left with numbered citation chips and,
    under it, one row per cited file."""

    def __init__(self, view, question, scope=None):
        super().__init__()
        self.view, self.win, self.question = view, view.win, question
        self.text, self.result, self.cited, self.rows = '', None, {}, []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 12)
        layout.setSpacing(8)
        ask = QHBoxLayout()
        ask.addStretch()
        bubble = label(question, 'bubble', wrap=True, selectable=True)
        bubble.ensurePolished()  # measure with the bubble's own (stylesheet) font
        widest = max((bubble.fontMetrics().horizontalAdvance(line) for line in question.splitlines() or ['']), default=0)
        bubble.setFixedWidth(min(430, widest + 40))  # short questions stay on one line; long ones wrap at 430
        ask.addWidget(bubble)
        layout.addLayout(ask)
        if scope:
            note = label(f'Only from {Path(scope).name}', 'caption')
            note.setAlignment(Qt.AlignRight)
            layout.addWidget(note)
        head = QHBoxLayout()
        head.setSpacing(6)
        mark = QLabel()
        mark.setPixmap(sparkle_pixmap(16))
        head.addWidget(mark)
        self.status = ShimmerLabel(self.win, 'Searching your files…')
        head.addWidget(self.status, 1)
        layout.addLayout(head)
        self.body = QLabel()
        self.body.setTextFormat(Qt.RichText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.body.setStyleSheet('font-size: 10.5pt;')
        self.body.linkHovered.connect(self.hover_cite)
        self.body.linkActivated.connect(self.open_cite)
        self.body.hide()
        layout.addWidget(self.body)
        self.sources = QVBoxLayout()
        self.sources.setSpacing(5)
        layout.addLayout(self.sources)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(2)
        layout.addLayout(self.actions)
        self.flush = QTimer(self, interval=50, timeout=self.render)
        self.status.set('Searching your files…', True)

    def evidence(self, items):
        if isinstance(items, tuple) and items and items[0] == 'status':
            self.status.set(items[1], True)
            return
        if isinstance(items, tuple) and items and items[0] == 'cite':
            self.cited[items[1]] = items[2]  # a new citation while streaming: its chip can preview it now
            return
        self.status.set(f'Reading {len(items)} source{"s" * (len(items) != 1)}…' if items else 'No matching passages', True)

    def add_token(self, token):
        if not self.text:
            self.status.set('Writing…', True)
            self.body.show()
            fade_in(self.body)
            self.flush.start()
        self.text += token

    def render(self):
        self.body.setText(render.answer_html(self.text, chip_colors(self.win.pal)))
        self.view.follow()

    def hover_cite(self, link):
        source = self.cited.get(int(link[5:])) if link.startswith('cite:') and link[5:].isdigit() else None
        if source is None:
            QToolTip.hideText()
            return
        QToolTip.showText(QCursor.pos(), cite_preview(source, self.win.pal), self.body)

    def open_cite(self, link):
        if not (link.startswith('cite:') and link[5:].isdigit()):
            return
        number = int(link[5:])
        for row in self.rows:
            if any(item.get('marker') == number for item in row.items):
                row.toggle(True, focus=number)
                return

    def _show_sources(self, citations, animate=True):
        groups = {}
        for item in citations:
            groups.setdefault(item.get('path', ''), []).append(item)
        for number, items in enumerate(groups.values()):
            row = SourceRow(self, items)
            self.rows.append(row)
            self.sources.addWidget(row)
            if animate:
                row.hide()
                QTimer.singleShot(0 if Motion.reduced else 45 * number, lambda r=row: (r.show(), fade_in(r)))

    def copy(self):
        lines = [self.text.strip()]
        if self.cited:
            lines.append('\nSources')
            lines += [f"[{n}] {s.get('filename', '')} — {s.get('location', '')}" for n, s in sorted(self.cited.items())]
        QApplication.clipboard().setText('\n'.join(lines))
        self.win.toast('Answer copied', 'green')

    def _add_actions(self):
        self.actions.addWidget(button('Copy', self.copy, 'link', 'Copy the answer and its sources'))
        self.actions.addStretch()

    def status_text(self, result, citations, model):
        files = len({c.get('path') for c in citations})
        if result.get('small_talk'):
            return model
        if citations:
            count = len(citations)
            text = (f"From {files} file{'s' * (files != 1)}" if count == files else
                    f"From {count} source{'s' * (count != 1)} in {files} file{'s' * (files != 1)}")
            if result.get('beyond'):
                text += ' · plus general knowledge'
        elif result.get('beyond'):
            text = 'Not in your files · general knowledge'
        else:
            text = 'Not found in your files'
        return f'{text} · {model}'

    def finish(self, result):
        self.flush.stop()
        self.result = result
        self.text = result['answer'] or self.text
        citations = result.get('citations') or []
        self.cited = {c['marker']: c for c in citations}
        self.render()
        self.body.show()
        info = result.get('provider', {})
        model = info.get('name') or info.get('model') or ''
        model += f" via {info['group']}" if info.get('cloud') else ' on this PC'
        status = self.status_text(result, citations, model)
        if result.get('cancelled'):
            status += ' · stopped'
        self.status.set(status, False)
        self._show_sources(citations)
        self._add_actions()
        self.view.follow()

    def restore(self, message):
        """Show a saved answer (reopened from history) without animating it in."""
        if message.get('error'):
            self.fail(message['error'], setup=message.get('setup', False))
            return
        self.text = message.get('content') or ''
        sources = [dict(s, marker=s['marker'] if isinstance(s.get('marker'), int) else n) for n, s in enumerate(message.get('sources') or [], 1)]
        self.cited = {s['marker']: s for s in sources}
        self.render()
        self.body.show()
        model = message.get('model') or ''
        self.status.set(self.status_text({'beyond': message.get('beyond'), 'small_talk': message.get('small_talk')}, sources, model)
                        if model else '', False)
        self._show_sources(sources, animate=False)
        self._add_actions()

    def fail(self, error, setup=None):
        self.flush.stop()
        setup = isinstance(error, ai.NotConfigured) if setup is None else setup
        self.status.set('Not available yet' if setup else 'Something went wrong', False)
        self.body.setTextFormat(Qt.PlainText)
        self.body.setText(str(error))
        self.body.setStyleSheet(f"color: {self.win.pal['secondary' if setup else 'red'].name()}; font-size: 10pt;")
        self.body.show()


class Drawer(QWidget):
    """Reveals its content by clipping it rather than squeezing it. The content is laid out once at its full height,
    so wrapped text never reflows or overlaps while the drawer springs open or shut."""

    def __init__(self, content):
        super().__init__()
        self.content = content
        content.setParent(self)
        self.setFixedHeight(0)
        self.opened = False
        self.spring = Spring(0, 330, 0.96, self._height, precision=0.1)

    def full_height(self):
        layout = self.content.layout()
        return layout.totalHeightForWidth(self.width()) if layout.hasHeightForWidth() else self.content.sizeHint().height()

    def resizeEvent(self, event):
        self.content.setGeometry(0, 0, self.width(), self.full_height())
        if self.opened and self.spring.value == self.spring.target:
            self.spring.snap(self.full_height())  # the panel changed width while open: follow the reflowed text

    def _height(self, value):
        try:
            self.setFixedHeight(max(0, round(value)))
        except RuntimeError:
            DRIVER.springs.discard(self.spring)

    def set_open(self, on):
        self.opened = on
        self.content.setGeometry(0, 0, self.width(), self.full_height())
        self.spring.to(self.full_height() if on else 0)


class SourceRow(QFrame):
    """One cited file under an answer: its icon, name and the cited places as numbered chips. It springs open to show
    each cited passage with the supporting sentence highlighted, and Open / Show in folder."""

    def __init__(self, turn, items):
        super().__init__()
        self.turn, self.items, self.win = turn, items, turn.win
        self.path = items[0].get('path', '')
        self.setObjectName('sourcerow')
        self.setAttribute(Qt.WA_Hover)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        art = QLabel()
        art.setPixmap(FileIcons.get(self.path, 16))
        top.addWidget(art)
        name = label(items[0].get('filename') or Path(self.path).name, 'name')
        name.setToolTip(self.path)
        top.addWidget(name)
        pal = self.win.pal
        places = '&nbsp;&nbsp;'.join(
            f"<span style='color:{pal['accent'].name()}; background-color:{rgba(pal['accent'], .16)};'>&nbsp;{html.escape(str(i.get('marker', '')))}&nbsp;</span>"
            f"&nbsp;<span style='color:{rgba(pal['secondary'])};'>{html.escape(i.get('location', ''))}</span>" for i in items)
        self.places = QLabel(places)
        self.places.setTextFormat(Qt.RichText)
        self.places.setStyleSheet('font-size: 9pt;')
        top.addWidget(self.places, 1)
        self.chevron = Chevron(self.win)
        top.addWidget(self.chevron)
        layout.addLayout(top)
        self.box = QWidget()
        box = QVBoxLayout(self.box)
        box.setContentsMargins(24, 4, 0, 2)
        box.setSpacing(6)
        self.passages = {}
        for item in items:
            caption = label(f"{item.get('marker', '')} · {item.get('label') or item.get('location', '')}", 'caption')
            box.addWidget(caption)
            text = QLabel(passage_html(item.get('text') or item.get('snippet_full') or item.get('snippet') or '', item.get('highlight'), pal))
            text.setTextFormat(Qt.RichText)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            text.setObjectName('secondary')
            box.addWidget(text)
            self.passages[item.get('marker')] = caption
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(button('Open', self.open_file, 'link'))
        row.addWidget(button('Show in folder', self.reveal_file, 'link'))
        row.addStretch()
        box.addLayout(row)
        self.drawer = Drawer(self.box)
        layout.addWidget(self.drawer)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip('Show the cited passages')

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() < 34:
            self.toggle()

    def toggle(self, opening=None, focus=None):
        opening = not self.drawer.opened if opening is None else opening
        self.chevron.set_open(opening)
        self.drawer.set_open(opening)
        if opening:
            self.reveal(self.passages.get(focus))

    def reveal(self, passage=None):
        """Glide the chat so the opening passages (or the one a citation chip points at) end up in view."""
        view = self.turn.view
        bar = view.scroll.verticalScrollBar()
        top = self.mapTo(view.scroll.widget(), QPoint(0, 0)).y()
        if passage is not None:
            start = top + self.drawer.y() + passage.mapTo(self.box, QPoint(0, 0)).y()
            end = start + passage.height() + 80  # the caption and the start of its passage
        else:
            start, end = top, top + self.drawer.y() + self.drawer.full_height() + 8
        visible = view.scroll.viewport().height()
        if start >= bar.value() and end <= bar.value() + visible:
            return
        view.scroll_spring.snap(bar.value())
        view.scroll_spring.to(max(0, min(start - 12, end - visible)) if passage is None else max(0, start - 60))

    def target(self):
        return self.win.index.resolve(self.path)

    def open_file(self):
        try:
            open_path(self.target(), (self.items[0].get('loc') or {}) if self.items else None)
            self.win.hide_panel()
        except (paths.PathError, OSError) as error:
            self.win.toast(f'Cannot open: {error}', 'red')

    def reveal_file(self):
        try:
            reveal_in_explorer(self.target())
        except paths.PathError as error:
            self.win.toast(str(error), 'red')


def when(timestamp):
    """'16:52' today, 'Yesterday', 'Mon' this week, otherwise 'Oct 21'."""
    moment = datetime.fromtimestamp(timestamp or 0)
    days = (datetime.now().date() - moment.date()).days
    return moment.strftime('%H:%M') if days == 0 else 'Yesterday' if days == 1 else moment.strftime('%a') if days < 7 else moment.strftime('%b %d')


class ChatRow(QWidget):
    """A saved chat in the history panel; rename and delete appear on hover."""
    HEIGHT = 46

    def __init__(self, panel, chat):
        super().__init__()
        self.panel, self.win, self.chat = panel, panel.win, chat
        self.hover = self.selected = False
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 4, 4, 4)
        row.setSpacing(2)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.title = label('', None)
        self.title.setToolTip(chat['title'])
        self.edit = QLineEdit(chat['title'])
        self.edit.hide()
        self.edit.returnPressed.connect(self.commit)
        self.edit.editingFinished.connect(self.commit)
        texts.addWidget(self.title)
        texts.addWidget(self.edit)
        self.meta = label(when(chat['updated']), 'caption')
        texts.addWidget(self.meta)
        row.addLayout(texts, 1)
        self.rename_button = button('', self.start_rename, 'ghost', 'Rename', icon('pencil', 12, self.win.pal['secondary']))
        self.delete_button = button('', lambda: self.panel.delete(self), 'ghost', 'Delete this chat', icon('close', 11, self.win.pal['secondary']))
        for widget in (self.rename_button, self.delete_button):
            widget.setFixedSize(24, 24)
            widget.hide()
            row.addWidget(widget)
        self.elide()

    def elide(self):
        width = self.panel.WIDTH - 24 - (52 if self.hover else 0)
        self.title.setText(self.title.fontMetrics().elidedText(self.chat['title'], Qt.ElideRight, width))

    def enterEvent(self, event):
        self.hover = True
        for widget in (self.rename_button, self.delete_button):
            widget.show()
        self.elide()
        self.update()

    def leaveEvent(self, event):
        self.hover = False
        for widget in (self.rename_button, self.delete_button):
            widget.hide()
        self.elide()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.edit.isVisible():
            self.panel.open(self.chat['id'])

    def mouseDoubleClickEvent(self, event):
        self.start_rename()

    def start_rename(self):
        self.title.hide()
        self.edit.setText(self.chat['title'])
        self.edit.show()
        self.edit.setFocus()
        self.edit.selectAll()

    def commit(self):
        if not self.edit.isVisible():
            return
        self.edit.hide()
        self.title.show()
        title = ' '.join(self.edit.text().split())
        if title and title != self.chat['title']:
            self.panel.rename(self, title)

    def paintEvent(self, event):
        if not (self.hover or self.selected):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.win.pal['fill_strong' if self.selected else 'hover'])
        p.drawRoundedRect(QRectF(self.rect()).adjusted(2, 1, -2, -1), 9, 9)


class HistoryPanel(QWidget):
    """Saved chats grouped by day, with a filter and New chat; springs open and closed from the left."""
    WIDTH = 230

    def __init__(self, view):
        super().__init__()
        self.view, self.win = view, view.win
        self.rows, self.current = [], None
        self.setFixedWidth(0)
        self.inner = QWidget(self)
        self.inner.setFixedWidth(self.WIDTH)
        layout = QVBoxLayout(self.inner)
        layout.setContentsMargins(14, 10, 8, 10)
        layout.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText('Search chats')
        self.filter.textChanged.connect(lambda _: self.refresh())
        top.addWidget(self.filter, 1)
        top.addWidget(button('', self.view.new_chat, 'ghost', 'New chat  (Ctrl+N)', icon('plus', 14, self.win.pal['text'])))
        layout.addLayout(top)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName('clear')
        self.list = QVBoxLayout(holder)
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(1)
        self.list.addStretch()
        self.scroll.setWidget(holder)
        layout.addWidget(self.scroll, 1)
        self.empty = label('Your chats will appear here.', 'caption', wrap=True)
        layout.addWidget(self.empty)
        self.width_spring = Spring(0, 300, 0.88, self._width, precision=0.1)

    def _width(self, value):
        value = max(0, int(value))
        self.setFixedWidth(value)
        self.inner.move(value - self.WIDTH, 0)  # slides in from the left edge
        self.setVisible(value > 0)
        if self.width_spring.value == self.width_spring.target and not self.width_spring.velocity:
            self._unfreeze()

    def _freeze(self, target):
        """Hold the chat at the width it will have once the sidebar arrives. Re-wrapping every answer at a new width on
        each frame of the slide is what made it stutter; this way the chat is laid out once and simply moves."""
        holder = self.view.scroll.widget()
        width = self.view.scroll.viewport().width() + self.width() - target
        if width > 0:
            holder.setFixedWidth(width)

    def _unfreeze(self):
        holder = self.view.scroll.widget()
        if holder.minimumWidth() or holder.maximumWidth() < 16777215:
            holder.setMinimumWidth(0)
            holder.setMaximumWidth(16777215)

    def resizeEvent(self, event):
        self.inner.setFixedHeight(self.height())

    @property
    def is_open(self):
        return self.width_spring.target > 0

    def set_open(self, on, animate=True):
        if on:
            self.refresh()
        target = self.WIDTH if on else 0
        if animate and not Motion.reduced and self.view.isVisible() and target != self.width_spring.value:
            self._freeze(target)
        (self.width_spring.to if animate else self.width_spring.snap)(target)

    def refresh(self):
        for row in self.rows:
            row.setParent(None)
            row.deleteLater()
        while self.list.count() > 1:
            item = self.list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.rows = []
        found = self.view.store.list(self.filter.text())
        position = 0
        for name, items in chats.group(found):
            header = label(name, 'section')
            header.setContentsMargins(10, 8 if position else 0, 0, 2)
            self.list.insertWidget(position, header)
            position += 1
            for chat in items:
                row = ChatRow(self, chat)
                row.selected = chat['id'] == self.current
                self.list.insertWidget(position, row)
                self.rows.append(row)
                position += 1
        self.empty.setText('No chats match.' if self.filter.text() else 'Your chats will appear here.')
        self.empty.setVisible(not found)

    def select(self, chat_id):
        self.current = chat_id
        for row in self.rows:
            row.selected = row.chat['id'] == chat_id
            row.update()

    def open(self, chat_id):
        self.view.open_chat(chat_id)

    def rename(self, row, title):
        chat = self.view.store.rename(row.chat['id'], title)
        if chat is None:
            return
        row.chat['title'] = chat['title']
        row.title.setToolTip(chat['title'])
        row.elide()
        if self.view.chat and self.view.chat['id'] == chat['id']:
            self.view.chat['title'] = chat['title']
            self.view.chat['_path'] = chat['_path']
            self.view.show_title()

    def delete(self, row):
        removed = self.view.store.delete(row.chat['id'])
        if removed is None:
            return
        if self.view.chat and self.view.chat['id'] == removed['id']:
            self.view.new_chat(quiet=True)
        fade_out_and_remove = lambda: (self.refresh(), self.win.settings_view.show_chats_info())
        if Motion.reduced:
            fade_out_and_remove()
        else:
            effect = QGraphicsOpacityEffect(row)
            row.setGraphicsEffect(effect)
            spring = Spring(1, 420, 1.0)

            def change(value):
                try:
                    effect.setOpacity(max(0.0, value))
                except RuntimeError:
                    return
                if value <= 0.02:
                    spring.on_change = None
                    fade_out_and_remove()
            spring.on_change = change
            spring.to(0)
            row._fade = spring
        removed.pop('_path', None)
        self.win.toast(f'Deleted “{removed["title"]}”', 'green', ('Undo', lambda: self.restore(removed)), 6)

    def restore(self, chat):
        self.view.store.save(chat, touch=False)
        self.refresh()
        self.win.settings_view.show_chats_info()
        self.win.toast('Chat restored', 'green', seconds=2)


class AskView(QWidget):
    SUGGESTIONS = ['Summarize the most important points in my files', 'Which documents mention costs or budgets?',
                   'What did I save about travel?']

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.history, self.cancel, self.turns, self.chat, self.models = [], None, [], None, []
        self.models_known = False  # True once the installed models have been listed
        self.store = chats.ChatStore(win.settings.chats_folder)
        self.scope = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(16, 8, 20, 6)
        bar.setSpacing(8)
        self.history_button = button('', self.toggle_history, 'ghost', 'Chat history  (Ctrl+H)', icon('sidebar', 16, win.pal['secondary']))
        self.history_button.setFixedSize(30, 30)
        bar.addWidget(self.history_button)
        self.title = label('', 'secondary')
        bar.addWidget(self.title, 1)
        self.scope_chip = button('', self.clear_scope, 'source', 'Ask about all your files again')
        self.scope_chip.hide()
        bar.addWidget(self.scope_chip)
        self.model_pill = picker.ModelButton(win)
        self.model_pill.clicked.connect(lambda: self.show_model_menu(self.model_pill))
        self.popover = None
        bar.addWidget(self.model_pill)
        layout.addLayout(bar)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.panel = HistoryPanel(self)
        body.addWidget(self.panel)
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        layout.addLayout(body, 1)

        intro = QWidget()
        box = QVBoxLayout(intro)
        box.setContentsMargins(40, 0, 40, 0)
        box.setSpacing(10)
        box.addStretch(3)
        art = QLabel()
        art.setPixmap(sparkle_pixmap(44))
        art.setAlignment(Qt.AlignCenter)
        box.addWidget(art)
        self.intro_title = label('', 'hero')
        self.intro_title.setAlignment(Qt.AlignCenter)
        box.addWidget(self.intro_title)
        self.intro_text = label('', 'secondary', wrap=True)
        self.intro_text.setAlignment(Qt.AlignCenter)
        self.intro_text.setMinimumHeight(40)
        box.addWidget(self.intro_text)
        self.suggestions = QWidget()
        chips = QVBoxLayout(self.suggestions)
        chips.setContentsMargins(0, 6, 0, 0)
        chips.setSpacing(7)
        for text in self.SUGGESTIONS:
            chips.addWidget(button(text, lambda _=False, t=text: (self.win.search.setText(t), self.win.search.setFocus()),
                                   'chip', 'Puts this question in the box — edit it, then press Enter'), 0, Qt.AlignCenter)
        box.addWidget(self.suggestions)
        self.intro_button = button('', self.intro_action, 'primary')
        box.addWidget(self.intro_button, 0, Qt.AlignCenter)
        box.addStretch(4)
        self.stack.addWidget(intro)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName('clear')
        self.thread = QVBoxLayout(holder)
        self.thread.setContentsMargins(26, 10, 26, 14)
        self.thread.setSpacing(4)
        self.thread.addStretch()
        self.scroll.setWidget(holder)
        self.stack.addWidget(self.scroll)
        self.scroll_spring = Spring(0, 260, 1.0, lambda v: self.scroll.verticalScrollBar().setValue(int(v)), precision=0.1)
        self.show_title()

    # ---------------------------------------------------------------- state
    def activated(self):
        self.stack.show()
        self.refresh_intro()
        self.history_button.setVisible(self.saving)
        if self.saving and not self.panel.is_open and not self.win.settings.get('sidebar_collapsed') and self.store.list():
            self.panel.set_open(True, animate=False)
        elif not self.saving and self.panel.is_open:
            self.panel.set_open(False, animate=False)
        elif self.panel.is_open:
            self.panel.refresh()
        if not self.turns:
            self.stack.setCurrentIndex(0)
        if self.scope:
            self.win.search.setPlaceholderText(f'Ask about {Path(self.scope).name}…')
        settings = dict(self.win.settings)
        run_job(lambda s: providers.catalog(settings), self.models_loaded)

    def deactivated(self):
        """Leaving Ask AI: the chat is hidden at once (it was photographed for the transition already), so no frame of it
        can show while another view slides in, and it is laid out afresh when it comes back."""
        self.stack.hide()

    @property
    def saving(self):
        return bool(self.win.settings.get('save_chat_history', True))

    def models_loaded(self, entries):
        self.models, self.models_known = entries, True
        self.refresh_intro()

    def chat_model(self):
        """(provider, its description) for the model Ask Juno will use."""
        provider = providers.for_settings(self.win.settings)
        info = provider.describe()
        if provider.kind == 'claude_code':
            info['name'] = dict(providers.CLAUDE_PLAN_MODELS).get(provider.model, provider.model)
        elif provider.kind == 'anthropic':
            info['name'] = dict(providers.CLAUDE_MODELS).get(provider.model, provider.model)
        return provider, info

    def refresh_intro(self):
        provider, info = self.chat_model()
        chosen = info['configured']
        name = info.get('name') or ''
        self.model_pill.set_model(provider.kind if chosen else None, name if chosen else 'Choose a model', animate=self.isVisible())
        where = 'runs on this PC' if not provider.cloud else f'answers {provider.via}'
        folders = self.win.settings.folders
        if not chosen:
            local = next((e for e in self.models if e['kind'] == 'local'), None)
            self.intro_title.setText('Choose a model to chat with your files')
            lead = ('Use a model on this PC, or Claude or ChatGPT with your subscription or an API key. '
                    'Juno answers from the folders you track.')
            missing = self.models_known and local is not None and not local['models']
            self.intro_text.setText(f'{lead} (The model for this PC, gemma3:4b, is not downloaded yet.)' if missing else lead)
            self.intro_button.setText('Choose a model')
            self.intro_button.show()
            self.suggestions.hide()
        elif not folders:
            self.intro_title.setText('Track a folder to chat with it')
            self.intro_text.setText(f'{name} answers from the folders you track. Track one, like Downloads or Documents, to begin.')
            self.intro_button.setText('Track folder…')
            self.intro_button.show()
            self.suggestions.hide()
        else:
            names = ', '.join(f.name for f in folders[:3]) + (f' and {len(folders) - 3} more' if len(folders) > 3 else '')
            self.intro_title.setText('Ask anything about your files')
            privacy = '' if not provider.cloud else ' Your files stay on this PC; only your questions and the passages found for them are sent.'
            self.intro_text.setText(f'Answers come from {names}, with the sources they used. {name} {where}.{privacy}')
            self.intro_button.hide()
            self.suggestions.show()

    def intro_action(self):
        if not self.chat_model()[1]['configured']:
            self.show_model_menu()
        else:
            self.win.choose_folder()

    def show_title(self):
        title = self.chat['title'] if self.chat else 'New chat'
        self.title.setText(self.title.fontMetrics().elidedText(title, Qt.ElideRight, 300))
        self.title.setToolTip(title if self.chat else '')

    # ---------------------------------------------------------------- model
    def show_model_menu(self, anchor=None):
        """Open the model picker under ``anchor`` (the model button here, or the one in Settings)."""
        anchor = anchor or self.model_pill
        if self.popover is not None:
            return
        settings = dict(self.win.settings)

        def opened(value):
            entries, (efforts, _) = value
            self.models, self.models_known = entries, True
            current = (settings.get('provider') or 'local', settings.get('model') or '')
            items = picker.model_items(entries, current, efforts, settings.get('effort'))
            self.popover = popover = picker.Popover(self.win, items)
            popover.chosen.connect(self.picked)
            popover.closed.connect(lambda: (anchor.set_open(False), setattr(self, 'popover', None)))
            anchor.set_open(True)
            popover.open_at(anchor)
        run_job(lambda s: (picker.catalog_with_details(settings), providers.for_settings(settings).effort_options()), opened)

    def picked(self, item):
        if item.get('action') == 'settings':
            self.win.open_settings('Ask Juno')
        elif item.get('action') == 'effort':
            self.choose_effort(item['value'], item['title'])
        else:
            self.choose_model(item['kind'], item['model'], item.get('cloud', False))
        if self.win.mode == SETTINGS:
            self.win.settings_view.load_models()
            self.win.settings_view.load_effort()

    def choose_model(self, kind, model, cloud=False):
        settings = self.win.settings
        if cloud and kind not in (settings.get('cloud_ok') or []):
            group = providers.GROUPS[kind]
            answer = ask_box(self, f'Use {group}?',
                                          f'Answers will come from {group}, so your questions and the few passages Juno finds '
                                          'in your files for each question are sent to that service. '
                                          'Your files themselves stay on this PC, and indexing and search stay local.',
                                          QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
            if answer != QMessageBox.Ok:
                return
            settings['cloud_ok'] = list(settings.get('cloud_ok') or []) + [kind]
        settings.update(provider=kind, model=model)
        settings.save()
        self.refresh_intro()
        self.win.refresh_status()
        provider, info = self.chat_model()
        self.win.toast(f"Chatting with {info.get('name')} {provider.via}", 'green', seconds=3)

    def choose_effort(self, value, text):
        self.win.settings['effort'] = value
        self.win.settings.save()
        self.win.toast(f'Thinking: {text}', 'green', seconds=2)

    # ---------------------------------------------------------------- scope
    def set_scope(self, path):
        """Answer the next questions only from this file (from Search → Ask about this)."""
        self.scope = path
        name = Path(path).name
        self.scope_chip.setText(f'Only from: {name}   ✕')
        self.scope_chip.setIcon(QIcon(FileIcons.get(path, 16)))
        self.scope_chip.show()
        fade_in(self.scope_chip)
        self.win.search.setPlaceholderText(f'Ask about {name}…')
        self.win.search.setFocus()

    def clear_scope(self):
        self.scope = None
        self.scope_chip.hide()
        if self.win.mode == ASK:
            self.win.search.setPlaceholderText(PLACEHOLDERS[ASK])
            self.win.search.setFocus()

    # -------------------------------------------------------------- history
    def toggle_history(self):
        if not self.saving:
            self.win.toast('Chat history is off', 'orange', ('Turn on in Settings', lambda: self.win.open_settings('Ask Juno')), 6)
            return
        opening = not self.panel.is_open
        self.panel.set_open(opening)
        self.win.settings['sidebar_collapsed'] = not opening
        self.win.settings.save()
        if opening:
            self.panel.filter.setFocus()

    def chats_changed(self, current_deleted=False):
        """Settings changed where or whether chats are saved."""
        self.store = chats.ChatStore(self.win.settings.chats_folder)
        if current_deleted and self.chat:
            self.new_chat(quiet=True)
        if self.win.mode == ASK:
            self.activated()

    def clear_thread(self):
        if self.cancel:
            self.cancel.set()
        self.history = []
        for turn in self.turns:
            turn.deleteLater()
        self.turns = []

    def new_chat(self, quiet=False):
        def change():
            self.clear_scope()
            self.clear_thread()
            self.chat = None
            self.panel.select(None)
            self.show_title()
            self.stack.setCurrentIndex(0)
        if self.win.mode == ASK and self.turns:
            self.win.transition(change)
        else:
            change()
        if self.win.mode == ASK:
            self.win.search.setFocus()
        if not quiet:
            self.win.toast('New chat', 'green', seconds=2)

    def open_chat(self, chat_id):
        if self.cancel is not None:
            self.win.toast('Wait for the answer to finish, or press Enter to stop it', 'orange')
            return
        chat = self.store.load(chat_id)
        if chat is None:
            self.win.toast('That chat is gone — it may have been deleted', 'orange')
            self.panel.refresh()
            return
        def change():
            self.clear_scope()
            self.clear_thread()
            self.chat = chat
            messages = chat['messages']
            for number, message in enumerate(messages):
                if message.get('role') != 'user':
                    continue
                turn = Turn(self, message.get('content', ''), message.get('scope'))
                reply = messages[number + 1] if number + 1 < len(messages) and messages[number + 1].get('role') == 'assistant' else None
                if reply:
                    turn.restore(reply)
                    if reply.get('content') and not reply.get('error'):
                        self.history += [{'role': 'user', 'content': message['content']}, {'role': 'assistant', 'content': reply['content']}]
                else:
                    turn.status.set('No answer was saved', False)
                self.turns.append(turn)
                self.thread.insertWidget(self.thread.count() - 1, turn)
            self.panel.select(chat_id)
            self.show_title()
            self.stack.setCurrentIndex(1 if self.turns else 0)
            # Lay the chat out now and open it at its end, so not even one frame shows it scrolled to the top.
            QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
            QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
        self.win.transition(change)
        self.win.search.setFocus()

    def record(self, question, scope, reply):
        """Add one exchange to the current chat and save it (when chat history is on)."""
        provider, info = self.chat_model()
        model = info.get('name') or provider.model
        if self.chat is None:
            self.chat = chats.ChatStore.new_chat(question, model)
        self.chat.update(model=model, provider=provider.kind)
        self.chat['messages'] += [{'role': 'user', 'content': question, 'time': time.time(), 'scope': scope},
                                  dict(reply, role='assistant', time=time.time(), model=model, provider=provider.kind)]
        self.show_title()
        if not self.saving:
            return
        try:
            self.store.save(self.chat)
        except OSError as error:
            self.win.toast(f'Couldn’t save this chat: {error}', 'red', seconds=6)
            return
        self.panel.select(self.chat['id'])
        if self.panel.is_open:
            self.panel.refresh()
        elif not self.win.settings.get('sidebar_collapsed'):
            self.panel.set_open(True)

    # ----------------------------------------------------------------- chat
    def follow(self, soft=False):
        bar = self.scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: (self.scroll_spring.snap(bar.value()), self.scroll_spring.to(bar.maximum())) if not soft else None)

    def send(self):
        if self.cancel is not None:
            self.cancel.set()
            return
        question = self.win.search.text().strip()
        if not question:
            return
        self.win.search.clear()
        self.stack.setCurrentIndex(1)
        scope = self.scope
        turn = Turn(self, question, scope)
        self.turns.append(turn)
        self.thread.insertWidget(self.thread.count() - 1, turn)
        fade_in(turn)
        self.follow()
        self.cancel = cancel = threading.Event()
        self.win.thinking(True)
        history, settings, index = list(self.history), self.win.settings, self.win.index

        def work(signals):
            return rag.ask(index, settings, question, history, on_token=signals.token.emit, cancel=cancel,
                           on_evidence=signals.progress.emit, only_path=scope,
                           on_status=lambda text: signals.progress.emit(('status', text)))

        def done(result):
            turn.finish(result)
            self.history += [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': result['answer']}]
            sources = [{k: item.get(k) for k in ('marker', 'path', 'filename', 'folder', 'kind', 'location', 'label', 'segment_id', 'text',
                                                 'highlight')}
                       for item in result.get('citations') or []]
            self.record(question, scope, {'content': result['answer'], 'sources': sources, 'error': None,
                                          'beyond': bool(result.get('beyond')), 'small_talk': bool(result.get('small_talk')),
                                          'query': result.get('query')})
            self.end()

        def failed(error):
            turn.fail(error)
            self.record(question, scope, {'content': '', 'sources': [], 'error': str(error), 'setup': isinstance(error, ai.NotConfigured)})
            self.end()
        run_job(work, done, failed, progress=turn.evidence, token=turn.add_token)

    def end(self):
        self.cancel = None
        self.win.thinking(False)


class Group(QFrame):
    """macOS-Settings-style inset group: rounded fill with separated rows."""

    def __init__(self, win, title=None, note=None):
        super().__init__()
        self.win = win
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(0, 0, 0, 0)
        self.outer.setSpacing(5)
        if title:
            self.outer.addWidget(label(title, 'section'))
        self.card = QFrame()
        self.card.setObjectName('group')
        self.rows = QVBoxLayout(self.card)
        self.rows.setContentsMargins(14, 4, 14, 4)
        self.rows.setSpacing(0)
        self.outer.addWidget(self.card)
        if note:
            self.outer.addWidget(label(note, 'caption', wrap=True))
        self.count = 0

    def add(self, widget):
        """Add a custom row (with the hairline above it, like row())."""
        if self.count:
            line = QFrame()
            line.setFixedHeight(1)
            line.setObjectName('hairline')
            self.rows.addWidget(line)
        self.count += 1
        self.rows.addWidget(widget)
        return widget

    def row(self, title, widget=None, caption=None, tip=None):
        """A row: its name, at most one line of help (``caption``; '' makes an empty line to fill later, kept as
        ``holder.caption``) and one control on the right. ``tip``: the longer explanation, as the name's tooltip."""
        line = None
        if self.count:
            line = QFrame()
            line.setFixedHeight(1)
            line.setObjectName('hairline')
            self.rows.addWidget(line)
        self.count += 1
        holder = QWidget()
        holder.hairline = line  # hide it together with the row
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 7, 0, 7)
        row.setSpacing(10)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        holder.title = label(title)
        if tip:
            holder.title.setToolTip(tip)
        texts.addWidget(holder.title)
        holder.caption = None
        if caption is not None:
            holder.caption = label(caption, 'caption', wrap=True)
            texts.addWidget(holder.caption)
        row.addLayout(texts, 1)
        if widget is not None:
            if isinstance(widget, QWidget):
                row.addWidget(widget, 0, Qt.AlignRight | Qt.AlignVCenter)
            else:
                row.addLayout(widget)
        self.rows.addWidget(holder)
        return holder


# Settings' words for text that comes from elsewhere (the runtime, the reranker, diagnostics): design 3.2 bans the jargon.
PLAIN_WORDS = [(r'\s*\(llama\.cpp\)', ''), (r'llama\.cpp', 'the engine'), (r'nvidia-smi', 'the graphics driver'),
               (r'[Tt]he picture part of the search model', 'The picture search model'), (r'(?i)picture part', 'picture search model'),
               (r'\bGPUs\b', 'graphics cards'), (r'\bGPU\b', 'graphics card'), (r'(?i)\bembeddings\b', 'meaning vectors'),
               (r'\bpassages\b', 'pieces of text'), (r'\bpassage\b', 'piece of text'), (r'\binference\b', 'test'),
               (r'JUNO_MODELS_DIR|JUNO_LLAMA_DIR', 'a folder set up outside Juno')]


def plain(text):
    """``text`` in Settings' words (see PLAIN_WORDS)."""
    for pattern, words in PLAIN_WORDS:
        text = re.sub(pattern, words, text or '')
    return text


class SectionHeader(QWidget):
    """The heading of a Section: its name and a chevron; a click, Space or Enter opens or closes it."""

    def __init__(self, section, title):
        super().__init__()
        self.section = section
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 12, 10)
        row.addWidget(label(title, 'name'), 1)
        self.chevron = Chevron(section.win)
        row.addWidget(self.chevron)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.section.set_open(not self.section.open)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.section.set_open(not self.section.open)
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        if not self.hasFocus():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(self.section.win.pal['accent'], 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5), 10, 10)


class Section(QFrame):
    """A closed section that springs open on click (Settings → Advanced): a card with its heading, and its rows below a
    hairline. ``row`` and ``add`` are its Group's."""

    def __init__(self, win, title):
        super().__init__()
        self.setObjectName('group')
        self.win, self.title, self.open = win, title, False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = SectionHeader(self, title)
        outer.addWidget(self.header)
        self.body = QWidget()
        self.body.setMaximumHeight(0)
        inner = QVBoxLayout(self.body)
        inner.setContentsMargins(14, 0, 14, 0)
        inner.setSpacing(0)
        line = QFrame()
        line.setFixedHeight(1)
        line.setObjectName('hairline')
        inner.addWidget(line)
        self.group = Group(win)
        self.group.card.setObjectName('plain')
        self.group.rows.setContentsMargins(0, 2, 0, 6)
        inner.addWidget(self.group)
        outer.addWidget(self.body)
        self.row, self.add = self.group.row, self.group.add
        self.height_spring = Spring(0, 300, 0.86, self._height, precision=0.1)

    def set_open(self, on, animate=True):
        self.open = on
        shown = self.body.height() if self.body.maximumHeight() >= QWIDGETSIZE else self.body.maximumHeight()
        self.height_spring.snap(shown)
        target = self.body.sizeHint().height() if on else 0
        if animate:
            self.header.chevron.set_open(on)
            self.height_spring.to(target)
        else:
            self.header.chevron.angle.snap(90 if on else 0)
            self.height_spring.snap(target)

    def _height(self, value):
        try:
            settled = value == self.height_spring.target and not self.height_spring.velocity
            # Open and at rest: no cap, so rows whose text changes later (a download's progress) are never cut.
            self.body.setMaximumHeight(QWIDGETSIZE if self.open and settled else max(0, int(value)))
        except RuntimeError:
            pass


QWIDGETSIZE = 16777215  # Qt's "no maximum"


class PageList(QWidget):
    """Settings' list of pages on the left: one painted row per page; the selected one has the selected-row fill of the
    chat history and glides to a new choice. Click a row, or Up/Down while it has focus (or from the search box)."""
    ROW, TOP, WIDTH = 34, 4, 150
    changed = Signal(int)

    def __init__(self, win, names, icons):
        super().__init__()
        self.win, self.names, self.icons = win, list(names), list(icons)
        self.current, self.hover = 0, -1
        self.setFixedWidth(self.WIDTH)
        self.setFixedHeight(2 * self.TOP + self.ROW * len(self.names))
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.glide = Spring(self.TOP, 420, 0.86, lambda v: self.update(), precision=0.1)

    def set_current(self, index):
        index = max(0, min(len(self.names) - 1, index))
        if index == self.current:
            return
        self.current = index
        self.glide.to(self.TOP + index * self.ROW)
        self.update()
        self.changed.emit(index)

    def move(self, delta):
        self.set_current(self.current + delta)

    def row_at(self, y):
        index = int((y - self.TOP) // self.ROW)
        return index if 0 <= index < len(self.names) else -1

    def mouseMoveEvent(self, event):
        index = self.row_at(event.position().y())
        if index != self.hover:
            self.hover = index
            self.update()

    def leaveEvent(self, event):
        self.hover = -1
        self.update()

    def mousePressEvent(self, event):
        index = self.row_at(event.position().y())
        if event.button() == Qt.LeftButton and index >= 0:
            self.setFocus()
            self.set_current(index)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Up, Qt.Key_Down):
            self.move(-1 if key == Qt.Key_Up else 1)
        elif key in (Qt.Key_Home, Qt.Key_End):
            self.set_current(0 if key == Qt.Key_Home else len(self.names) - 1)
        else:
            super().keyPressEvent(event)  # Tab goes on into the page; Esc reaches SettingsView

    def focusInEvent(self, event):
        self.update()

    def focusOutEvent(self, event):
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        pal = self.win.pal
        width = self.width()
        p.setPen(Qt.NoPen)
        if 0 <= self.hover < len(self.names) and self.hover != self.current:
            p.setBrush(pal['hover'])
            p.drawRoundedRect(QRectF(12, self.TOP + self.hover * self.ROW + 1, width - 18, self.ROW - 2), 9, 9)
        selected = QRectF(12, self.glide.value + 1, width - 18, self.ROW - 2)
        p.setBrush(pal['fill_strong'])
        p.drawRoundedRect(selected, 9, 9)
        if self.hasFocus():
            p.setPen(QPen(pal['accent'], 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(selected.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
        p.setFont(self.font())
        for index, (name, art) in enumerate(zip(self.names, self.icons)):
            top = self.TOP + index * self.ROW
            color = pal['text'] if index == self.current else pal['secondary']
            p.drawPixmap(QPointF(24, top + (self.ROW - 16) / 2), icon_pixmap(art, 16, color))
            p.setPen(color)
            p.drawText(QRectF(50, top, width - 56, self.ROW), Qt.AlignLeft | Qt.AlignVCenter, name)


class PageBody(QWidget):
    """A page's rows. Its minimum height is what the rows need at the page's width (QScrollArea asks heightForWidth),
    not at their narrowest, where every wrapped help line is tall: so a page that fits does not scroll."""

    def minimumSizeHint(self):
        return QSize(0, 0)


class Page(QScrollArea):
    """One page of Settings. It scrolls only when its rows do not fit, and then they fade out over the last FADE pixels
    at the top and bottom edge instead of being cut mid-line: an opacity mask on the viewport (real transparency, so it
    works on the see-through panel), there only while something is out of sight."""
    FADE = 16

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        holder = PageBody()
        holder.setObjectName('clear')
        self.body = QVBoxLayout(holder)
        self.body.setSizeConstraint(QVBoxLayout.SetNoConstraint)
        self.body.setContentsMargins(8, 10, 22, 14)
        self.body.setSpacing(12)
        self.setWidget(holder)
        self.mask = None
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self.fade_edges)
        bar.rangeChanged.connect(self.fade_edges)

    def add(self, widget):
        self.body.addWidget(widget)
        return widget

    def scrolls(self):
        return self.verticalScrollBar().maximum() > 0

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fade_edges()

    def fade_edges(self, *_):
        bar, viewport = self.verticalScrollBar(), self.viewport()
        top, bottom = bar.value() > bar.minimum(), bar.value() < bar.maximum()
        if not (top or bottom):
            if self.mask is not None:
                viewport.setGraphicsEffect(None)  # deletes it
                self.mask = None
            return
        if self.mask is None:
            self.mask = QGraphicsOpacityEffect(viewport)
            self.mask.setOpacity(1.0)  # 0.7 by default
            viewport.setGraphicsEffect(self.mask)
        height = max(1, viewport.height())
        edge = min(0.45, self.FADE / height)
        solid, clear = QColor(0, 0, 0, 255), QColor(0, 0, 0, 0)
        gradient = QLinearGradient(0, 0, 0, height)
        gradient.setColorAt(0, clear if top else solid)
        gradient.setColorAt(edge, solid)
        gradient.setColorAt(1 - edge, solid)
        gradient.setColorAt(1, clear if bottom else solid)
        self.mask.setOpacityMask(QBrush(gradient))


def folder_icon(path, size=28):
    """The folder's own Windows shell icon (Downloads arrow, Pictures, …)."""
    from PySide6.QtCore import QFileInfo
    FileIcons.provider = FileIcons.provider or QFileIconProvider()
    return FileIcons.provider.icon(QFileInfo(str(path))).pixmap(QSize(size, size), 2.0)


def suggested_folders():
    """Common Windows folders that exist on this PC, as (name, path)."""
    from PySide6.QtCore import QStandardPaths
    places = [('Downloads', QStandardPaths.DownloadLocation), ('Pictures', QStandardPaths.PicturesLocation),
              ('Documents', QStandardPaths.DocumentsLocation), ('Desktop', QStandardPaths.DesktopLocation),
              ('Videos', QStandardPaths.MoviesLocation)]
    found = []
    for name, place in places:
        location = QStandardPaths.writableLocation(place)
        if location and Path(location).is_dir() and Path(location) != Path.home():
            found.append((name, Path(os.path.realpath(location))))
    return found


class Chevron(QWidget):
    """A small chevron that springs between pointing right (closed) and down (open)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setFixedSize(16, 16)
        self.angle = Spring(0, 320, 0.82, lambda v: self.update())

    def set_open(self, on):
        self.angle.to(90 if on else 0)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(8, 8)
        p.rotate(self.angle.value)
        p.drawPixmap(-7, -7, icon_pixmap('chevron', 14, self.win.pal['secondary']))


class FolderRow(QWidget):
    """One tracked folder: shell icon, name, short path, live status and an untrack button."""

    def __init__(self, panel, folder):
        super().__init__()
        self.panel, self.win, self.folder = panel, panel.win, Path(folder)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 7, 0, 7)
        row.setSpacing(11)
        art = QLabel()
        art.setFixedSize(30, 30)
        art.setPixmap(folder_icon(self.folder, 28))
        row.addWidget(art)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        texts.addWidget(label(self.folder.name or str(self.folder), 'name'))
        place = label(paths.shorten(self.folder, 46), 'caption')
        place.setToolTip(str(self.folder))
        texts.addWidget(place)
        includes = QHBoxLayout()
        includes.setContentsMargins(0, 0, 0, 0)
        includes.setSpacing(2)
        self.includes = label('', 'caption')
        includes.addWidget(self.includes)
        self.include_older = button('Include older files too', self.include, 'link', 'Read the files that were already there in full')
        includes.addWidget(self.include_older)
        includes.addStretch()
        texts.addLayout(includes)
        self.show_includes()
        row.addLayout(texts, 1)
        self.status = label('', 'caption')
        self.status.setTextFormat(Qt.RichText)
        row.addWidget(self.status)
        remove = button('', self.untrack, 'ghost', f'Stop tracking {self.folder.name} (its files leave the index; nothing is deleted)',
                        icon('close', 12, self.win.pal['secondary']))
        remove.setFixedSize(26, 26)
        row.addWidget(remove)
        self.opacity = None

    def set_status(self, text, color='secondary'):
        dot = f"<span style='color:{self.win.pal[color].name()}'>●</span>&nbsp; " if color != 'secondary' else ''
        self.status.setText(dot + html.escape(text))

    def show_includes(self):
        """"Everything", or "New files since 15 Nov" with "Include older files too" (a folder tracked as only new)."""
        since = self.win.settings.since_for(self.folder)
        when = datetime.fromtimestamp(since) if since is not None else None
        self.includes.setText(f'New files since {when.day} {when:%b}' + (f' {when.year}' if when.year != datetime.now().year else '') + ' ·'
                              if when else 'Everything')
        self.include_older.setVisible(when is not None)

    def include(self):
        self.win.include_older(self.folder)
        self.show_includes()

    def untrack(self):
        self.panel.untrack(self)

    def fade_out(self, then):
        if Motion.reduced:
            then()
            return
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        self.opacity = Spring(1, 420, 1.0)

        def change(value):
            try:
                effect.setOpacity(max(0.0, min(1.0, value)))
            except RuntimeError:
                return
            if value <= 0.02:
                self.opacity.on_change = None
                then()
        self.opacity.on_change = change
        self.opacity.to(0)


class TrackedFolders(QWidget):
    """Settings → Folders: the tracked folders as an open list (each with its live status), Add folder… with quick-add
    chips for common folders, then how the index keeps up: automatically, on battery, Rescan all and Rebuild index."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        self.note = label('Juno reads the files in these folders on this PC and keeps up as they change.', 'caption', wrap=True)
        outer.addWidget(self.note)
        card = QFrame()
        card.setObjectName('group')
        self.card = QVBoxLayout(card)
        self.card.setContentsMargins(14, 4, 14, 4)
        self.card.setSpacing(0)
        outer.addWidget(card)

        self.list = QVBoxLayout()
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(0)
        self.card.addLayout(self.list)
        self.empty = label('No folders yet. Add one to start searching.', 'caption')
        self.empty.setContentsMargins(0, 10, 0, 10)
        self.card.addWidget(self.empty)

        self.add_line = self._hairline()
        self.card.addWidget(self.add_line)
        track = QWidget()
        row = QHBoxLayout(track)
        row.setContentsMargins(0, 8, 0, 8)
        row.addWidget(button('Add folder…', self.win.choose_folder, 'primary', 'Choose any folder to search', icon('plus', 13, self.win.pal['accent_text'])))
        row.addStretch()
        self.summary = label('', 'caption')
        self.summary.setTextFormat(Qt.RichText)
        row.addWidget(self.summary)
        self.card.addWidget(track)

        self.chips_holder = QWidget()
        self.chips = QHBoxLayout(self.chips_holder)
        self.chips.setContentsMargins(0, 0, 0, 9)
        self.chips.setSpacing(7)
        self.card.addWidget(self.chips_holder)

        keep = Group(win)
        pal = lambda: self.win.pal
        self.auto_index = Toggle(pal)
        keep.row('Keep the index up to date automatically', self.auto_index, 'Juno watches these folders while it runs')
        self.index_on_battery = Toggle(pal)
        keep.row('Also index on battery', self.index_on_battery, 'Off: on battery, automatic runs read names and text only')
        keep.row('Rescan all', button('Rescan now', lambda: self.win.start_index(), None, 'Check every tracked folder for changes'))
        keep.row('Rebuild index', button('Rebuild', lambda: self.win.start_index(force=True), None, 'Read every file again from scratch'))
        outer.addWidget(keep)

    def _hairline(self):
        line = QFrame()
        line.setFixedHeight(1)
        line.setObjectName('hairline')
        return line

    # ------------------------------------------------------------- content
    def load(self):
        """Rebuild rows and chips from settings (when Settings opens)."""
        while self.list.count():  # the rows and the hairlines between them
            item = self.list.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        self.rows = []
        for folder in self.win.settings.folders:
            self._add_row(folder, delay=None)
        self.fill_chips()
        self.refresh()

    def _add_row(self, folder, delay=0):
        row = FolderRow(self, folder)
        if self.rows:
            row.hairline = self._hairline()
            self.list.addWidget(row.hairline)
        self.list.addWidget(row)
        self.rows.append(row)
        if delay is not None:
            row.hide()
            QTimer.singleShot(0 if Motion.reduced else delay, lambda r=row: (r.show(), fade_in(r)))
        return row

    def fill_chips(self):
        while self.chips.count():
            item = self.chips.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        tracked = [paths.canonical(f) for f in self.win.settings.folders]
        free = [(name, path) for name, path in suggested_folders()
                if not any(paths.is_within(t, path) for t in tracked) and not any(paths.is_within(path, t) for t in tracked)]
        if free:
            self.chips.addWidget(label('Suggested', 'caption'))
        for name, path in free:
            chip = button(name, lambda _=False, p=path: self.win.offer_folder(p), 'chip', f'Track {paths.shorten(path)}')
            chip.setIcon(QIcon(folder_icon(path, 16)))
            chip.setIconSize(QSize(16, 16))
            self.chips.addWidget(chip)
        self.chips.addStretch()
        self.chips_holder.setVisible(bool(free))

    def refresh(self, state=None):
        """Update the summary and each folder's live status."""
        folders = self.win.settings.folders
        stats = getattr(self.win, 'folder_stats', {}) or {}
        self.empty.setVisible(not self.rows)
        self.add_line.setVisible(bool(self.rows))
        total = sum(s.get('files', 0) for s in stats.values())
        busy_root = state.get('root') if state else None
        for row in self.rows:
            key = str(Path(os.path.realpath(row.folder)))
            info = stats.get(key, {'files': 0, 'skipped': 0})
            row.show_includes()
            if not row.folder.is_dir():
                row.set_status('Folder missing', 'red')
            elif self.win.indexing and busy_root and paths.canonical(busy_root) == paths.canonical(row.folder):
                phase = state.get('phase')
                if phase == 'scanning':
                    row.set_status('Looking for files…', 'accent')
                elif phase == 'reading video':
                    row.set_status(f"Reading video {state.get('frame', 0)}/{state.get('frames', 0)}", 'accent')
                elif phase == 'reading tables':
                    row.set_status(f"Reading tables in PDFs {min(state.get('done', 0) + 1, state.get('total', 0))} of {state.get('total', 0)}", 'accent')
                elif phase in ('names', 'reading pictures', 'embedding pictures', 'adding context', 'reading code', 'learning code'):
                    row.set_status(f"{phase_label(phase)} {min(state.get('done', 0) + 1, state.get('total', 0))} of {state.get('total', 0)}", 'accent')
                else:
                    row.set_status(f"Indexing {min(state.get('done', 0) + 1, state.get('total', 0))} of {state.get('total', 0)}", 'accent')
            elif self.win.indexing and key not in stats:
                row.set_status('Waiting…')
            else:
                text = f"{info['files']:,} file{'s' * (info['files'] != 1)}"
                if info.get('older'):  # a folder tracked as "only new": how many are searchable by their content
                    text += f" · {info.get('full', 0):,} fully searchable"
                row.set_status(text + ' · up to date' if self.win.status.get('initialized') else text, 'green' if info['files'] else 'secondary')
        count = f'{len(folders)} folder{"s" * (len(folders) != 1)}' if folders else ''
        if self.win.indexing:
            phase = (state or {}).get('phase')
            text = (f"{phase_label(phase)} {state.get('done', 0)}/{state.get('total', 0)}"
                    if phase in ('embedding', 'names', 'reading pictures', 'embedding pictures', 'adding context', 'reading code',
                                 'learning code') else 'Indexing…')
            layers = layers_text(self.win.layers, unfinished_only=True)
            self.summary.setText(f"<span style='color:{self.win.pal['accent'].name()}'>●</span>&nbsp; {text}" + (f' · {layers}' if layers else ''))
        else:
            text = f'{count} · {total:,} searchable file{"s" * (total != 1)}' if folders else ''
            if folders and self.win.paused == 'battery':
                text += ' · paused on battery'
            elif folders and layers_incomplete(self.win.layers):
                text += ' · ' + layers_text(self.win.layers, unfinished_only=True)
            self.summary.setText(text)

    # ------------------------------------------------------------- actions
    def added(self, folder):
        """A folder was just tracked: slide its row in."""
        self.fill_chips()
        self._add_row(folder, delay=60)
        self.refresh()

    def untrack(self, row):
        def gone():
            if row in self.rows:
                index = self.rows.index(row)
                self.rows.remove(row)
                line = getattr(row, 'hairline', None)
                if line is None and self.rows and index == 0:  # the first row went: the next one loses its line
                    line = getattr(self.rows[0], 'hairline', None)
                    self.rows[0].hairline = None
                if line is not None:
                    line.setParent(None)
                    line.deleteLater()
            row.setParent(None)
            row.deleteLater()
            self.fill_chips()
            self.refresh()
        self.win.untrack_folder(row.folder)
        row.fade_out(gone)


# Settings rows for the files Juno runs locally: key -> (row title, name in a sentence, caption).
DOWNLOAD_ROWS = {'embed-text': ('Search model', 'the search model', 'EmbeddingGemma 2: search by meaning'),
                 'embed-pictures': ('Picture search model', 'the picture search model', 'EmbeddingGemma 2: find pictures by what they show'),
                 models.ENGINE: ('Engine', 'the engine', 'Runs the models on this PC'),
                 'answer': ('Answer model', 'the answer model', 'gemma3:4b: answers on this PC')}


class ModelDownloads(QObject):
    """Downloads of the model files and the engine, each on a worker thread (run_job); one at a time per file."""
    changed = Signal(str)           # a download of this key started, moved on or ended
    finished = Signal(str, object)  # key, the error (None when the file is in place)

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.jobs, self.progress = {}, {}  # key -> cancel Event; key -> (bytes done, total)

    def running(self, key):
        return key in self.jobs

    def start(self, key):
        if key in self.jobs:
            return
        cancel = threading.Event()
        self.jobs[key] = cancel
        self.progress[key] = (0, models.size_of(key))
        home = self.win.settings.home
        self.changed.emit(key)

        def work(signals):
            report = lambda done, total: signals.progress.emit((done, total))
            if key == models.ENGINE:
                return models.download_engine(home, report, cancel)
            return models.download(key, home, report, cancel)

        def moved(value):
            if key in self.progress:
                self.progress[key] = value
                self.changed.emit(key)

        def ended(error=None):
            self.jobs.pop(key, None)
            self.progress.pop(key, None)
            self.changed.emit(key)
            self.finished.emit(key, error)
        run_job(work, lambda _: ended(), ended, progress=moved)

    def cancel(self, key):
        if key in self.jobs:
            self.jobs[key].set()

    def status_text(self):
        """One short line for the status bar (about 60 characters fit), or None when nothing downloads."""
        if not self.progress:
            return None
        done = sum(value[0] for value in self.progress.values())
        total = sum(value[1] for value in self.progress.values())
        what = DOWNLOAD_ROWS[next(iter(self.progress))][1] if len(self.progress) == 1 else f'{len(self.progress)} files'
        return f'Downloading {what}… {done / 1e6:,.0f} of {total / 1e6:,.0f} MB'


SETTINGS_PAGES = ('General', 'Folders', 'Search', 'Ask Juno', 'AI tools', 'Advanced')
PAGE_ICONS = ('gear', 'folder', 'search', 'sparkle', 'server', 'graph')
MEANING_FILES = (models.ENGINE, 'embed-text')  # what search by meaning needs (the Search page's Download button)
CALLS_SHOWN = 6  # recent AI-tool requests shown before "Show more"


def call_text(call):
    """One logged AI-tool request in plain words: '2026-01-01 10:00 · Claude Code searched for “tides” · 2 files'."""
    client, outcome = call.get('client') or 'A tool', call.get('outcome')
    if call.get('tool') == 'search_files':
        what = f"searched for “{call.get('query') or ''}”"
    elif call.get('tool') == 'read_passage':
        what = f"read {Path(call.get('path') or '').name or 'a file'}"
    else:
        what = f"asked for {call.get('tool') or 'something'}"
    count = call.get('count', 0)
    result = {'denied': 'not allowed', 'error': 'failed'}.get(outcome, f"{count} file{'s' * (count != 1)}")
    return f"{str(call.get('time', ''))[:16]} · {client} {what} · {result}"


class SettingsView(QWidget):
    """Settings: a list of six pages on the left and one page of rows on the right (design 3.1). It opens on General and
    keeps the last page while Juno runs (show_page). Every change saves itself after a short pause (save)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.pages_list = PageList(win, SETTINGS_PAGES, PAGE_ICONS)
        column = QVBoxLayout()
        column.setContentsMargins(0, 8, 0, 0)
        column.addWidget(self.pages_list)
        column.addStretch()
        layout.addLayout(column)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.pages, self.sections = {}, {}
        self.pages_list.changed.connect(self.stack.setCurrentIndex)
        self.file_status, self.file_button, self.file_states = {}, {}, {}
        self.account_status, self.account_dot, self.account_button = {}, {}, {}
        self.rerank_job, self.calls_limit = None, CALLS_SHOWN
        pal = lambda: self.win.pal

        # ---------------------------------------------------------- General
        page = self.page('General')
        g = page.add(Group(win))
        self.theme = QComboBox()
        self.theme.addItems(['Automatic', 'Dark', 'Light'])
        self.theme.setMinimumWidth(150)
        g.row('Theme', self.theme, 'Automatic follows what is behind the panel, or Windows')
        self.see_through = Toggle(pal)
        g.row('See-through panel', self.see_through, 'Off: an opaque panel that needs no screen capture')
        self.reduce_motion = Toggle(pal)
        g.row('Reduce motion', self.reduce_motion, 'Also follows the Windows animation setting')
        self.start_login = Toggle(pal)
        self.start_login.toggled.connect(self.change_start_login)
        g.row('Start Juno when you sign in', self.start_login, 'Starts hidden in the tray, so the shortcut works at once')
        self.hotkey_edit = QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        self.hotkey_edit.setFixedWidth(150)
        self.hotkey_edit.setToolTip('Click the box and press a combination with Ctrl, Alt, Shift or Win')
        self.hotkey_edit.editingFinished.connect(self.change_hotkey)
        hotkey_row = QHBoxLayout()
        hotkey_row.addWidget(self.hotkey_edit)
        hotkey_row.addWidget(button('Reset', lambda: self.change_hotkey(DEFAULT_HOTKEY), None, f'Back to {DEFAULT_HOTKEY}'))
        self.hotkey_label = g.row('Shortcut', hotkey_row, '', 'Shows and hides Juno from anywhere').caption
        g.row('Run setup again', button('Start', lambda: self.win.run_setup(), None, 'Choose folders, search by meaning and the shortcut'),
              'Folders, search by meaning and the shortcut, step by step')
        about = page.add(label(f'{APP_NAME} {__version__} · settings in {paths.shorten(self.win.settings.path, 60)}', 'caption', selectable=True))
        about.setToolTip(str(self.win.settings.path))

        # ---------------------------------------------------------- Folders
        page = self.page('Folders')
        self.tracked = page.add(TrackedFolders(win))
        self.auto_index, self.index_on_battery = self.tracked.auto_index, self.tracked.index_on_battery

        # ----------------------------------------------------------- Search
        page = self.page('Search')
        page.add(label('Search by name and by the text inside your files always works, on this PC.', 'caption', wrap=True))
        g = page.add(Group(win))
        self.embed_enabled = Toggle(pal)
        self.meaning_button = button('Download…', self.meaning_action)
        self.meaning_button.hide()
        line = QHBoxLayout()
        line.setSpacing(10)
        line.addWidget(self.meaning_button)
        line.addWidget(self.embed_enabled)
        self.meaning_row = g.row('Search by meaning', line, 'Checking…', 'Finds files by what they are about, not only the words in them')
        self.ocr_enabled = Toggle(pal)
        g.row('Read text in pictures and videos', self.ocr_enabled, 'Photos, screenshots, scanned pages and video frames')
        self.picture_search = Toggle(pal)
        self.picture_row = g.row('Find pictures by what they show', self.picture_search, '')
        self.code_search = Toggle(pal)
        g.row('Search code', self.code_search, 'For AI tools such as Claude Code and Codex: code in your folders, found by what it '
              'does, by name and by what calls it')
        self.embed_enabled.toggled.connect(lambda _: self.show_pictures())

        # ------------------------------------------------------- Ask Juno
        page = self.page('Ask Juno')
        g = page.add(Group(win))
        self.model = picker.ModelButton(win, compact=False)
        self.model.setMinimumWidth(240)
        self.model.clicked.connect(lambda: self.win.ask_view.show_model_menu(self.model))
        chat_row = QHBoxLayout()
        chat_row.addWidget(self.model)
        chat_row.addWidget(button('', self.refresh_models, 'ghost', 'Check the accounts and refresh the list of models',
                                  icon('refresh', 14, QColor(140, 140, 150))))
        self.location = g.row('Answer model', chat_row, '', 'Also in the model button at the top of Ask Juno').caption
        self.effort = QComboBox()
        self.effort.setMinimumWidth(240)
        self.effort_row = g.row('Thinking', self.effort, 'How long the model thinks before it answers')
        g = page.add(Group(win))
        for kind, title, plan, action in (('claude_code', 'Claude', 'Your Pro or Max plan', 'Sign in…'),
                                          ('codex', 'ChatGPT', 'Your Plus or Pro plan', 'Sign in…')):
            g.add(self.account_row(kind, title, plan, action))
        other = QHBoxLayout()
        other.setContentsMargins(30, 0, 0, 4)
        other.addWidget(button('Use an API key or another server instead…', lambda: self.show_page('Advanced', 'API keys'), 'link'))
        other.addStretch()
        g.rows.addLayout(other)
        g = page.add(Group(win))
        self.beyond = Toggle(pal)
        g.row('Allow answers beyond your files', self.beyond, 'General knowledge is marked as such and never cited')
        self.save_chats = Toggle(pal)
        g.row('Save chat history', self.save_chats, 'Off: new chats are not saved; saved ones stay', 'Reopen saved chats from the history panel (Ctrl+H)')
        place = QHBoxLayout()
        place.addWidget(button('Open folder', self.open_chats_folder))
        place.addWidget(button('Change…', self.change_chats_folder, None, 'The chats folder is never added to search'))
        self.chats_place = g.row('Saved in', place, '').caption
        self.delete_chats = button('Delete all chats…', self.delete_all_chats, None, 'Permanently deletes every saved chat (asks first)')
        self.delete_chats_note = g.row('Delete saved chats', self.delete_chats, 'Permanently, after asking first').caption

        # --------------------------------------------------------- AI tools
        page = self.page('AI tools')
        page.add(label('Claude Code, Codex and similar tools can search your tracked folders through Juno: read-only, on this PC. '
                       'Each tool asks once before it may.', 'caption', wrap=True))
        page.add(label('Your AI tools', 'section'))
        self.mcp_tools = page.add(Group(win))  # one row per AI tool found on this PC, filled by show_other_tools
        self.mcp_tool_rows = {}
        self.mcp_busy = set()
        for tool in connect.TOOLS:
            action = button('Connect', lambda _=False, t=tool.id: self.connect_tool(t))
            holder = self.mcp_tools.row(tool.name, action, '')
            holder.action = action
            self.mcp_tool_rows[tool.id] = holder
        self.mcp_other = self.mcp_tools.row('Any other tool', button('Copy', self.copy_mcp_command, None, 'Copy the server entry as JSON'), '',
                                            'Most MCP tools take this {"mcpServers": …} entry in their settings')
        self.mcp_command = self.mcp_other.caption
        self.mcp_command.setTextInteractionFlags(Qt.TextSelectableByMouse)
        page.add(label('Allowed tools', 'section'))
        g = page.add(Group(win))
        self.mcp_clients = QVBoxLayout()
        self.mcp_clients.setContentsMargins(0, 4, 0, 4)
        holder = QWidget()
        holder.setLayout(self.mcp_clients)
        g.add(holder)
        page.add(label('Recent requests', 'section'))
        g = page.add(Group(win))
        calls = QWidget()
        lines = QVBoxLayout(calls)
        lines.setContentsMargins(0, 8, 0, 8)
        lines.setSpacing(4)
        self.mcp_calls = label('', 'caption', wrap=True, selectable=True)
        lines.addWidget(self.mcp_calls)
        self.more_calls = button('Show more', self.toggle_calls, 'link')
        lines.addWidget(self.more_calls, 0, Qt.AlignLeft)
        g.add(calls)

        # --------------------------------------------------------- Advanced
        page = self.page('Advanced')
        s = self.section(page, 'Performance')
        self.device = QComboBox()
        self.device.addItems(['Auto (graphics card when available)', 'CPU only'])
        self.device.setMinimumWidth(250)
        s.row('Answer model device', self.device)
        self.embed_device = QComboBox()
        self.embed_device.addItems(['Auto (graphics card when available)', 'CPU only'])
        self.embed_device.setMinimumWidth(250)
        s.row('Indexing device', self.embed_device, None, 'Where search by meaning reads your files while indexing')
        self.search_on_gpu = Toggle(pal)
        s.row('Search on the graphics card', self.search_on_gpu, 'Off: searches run on the CPU and need no graphics memory')
        self.keep_minutes = QComboBox()
        for minutes in KEEP_LOADED:
            self.keep_minutes.addItem(duration_text(minutes), minutes)
        s.row('Keep models loaded', self.keep_minutes, 'After the last use; then their memory is freed')
        self.gpu_check = s.row('Graphics card check', button('Check again', self.check_gpu_again, None,
                                                             'Forget the last result; the next indexing run on the graphics card checks it again'),
                               'Not checked yet.', 'Once per driver: the graphics card must compute the same results as the CPU').caption

        s = self.section(page, 'Model files')
        for key, (title, _, caption) in DOWNLOAD_ROWS.items():
            self.file_button[key] = button('Download…', lambda _=False, k=key: self.file_action(k))
            self.file_button[key].hide()
            self.file_status[key] = s.row(title, self.file_button[key], 'Checking…', caption).caption
        win.downloads.changed.connect(self.show_file)
        win.downloads.finished.connect(self.file_finished)

        s = self.section(page, 'Answer quality')
        self.rerank_model = QComboBox()
        for name, info in rerank.MODELS.items():
            self.rerank_model.addItem(f"{info['label']} · {info['languages']}", name)
            self.rerank_model.setItemData(self.rerank_model.count() - 1, f"{info['size_mb']} MB", Qt.ToolTipRole)
        self.rerank_model.setMinimumWidth(200)
        self.rerank_model.setMaximumWidth(250)
        self.rerank_button = button('Download…', self.rerank_action)
        self.rerank_gpu_button = button('Faster copy…', self.rerank_gpu_action)
        self.rerank_gpu_button.hide()
        rr = QHBoxLayout()
        rr.addWidget(self.rerank_model)
        rr.addWidget(self.rerank_button)
        rr.addWidget(self.rerank_gpu_button)
        self.rerank_status = s.row('Reranker', rr, '', 'Reads your question together with each result and keeps the most relevant. '
                                                       'Runs on this PC.').caption
        self.rerank_enabled = Toggle(pal)
        s.row('Re-order results with the reranker', self.rerank_enabled, 'Off or not downloaded: keyword and meaning order')
        self.video_interval = NumberBox()
        self.video_interval.setRange(2, 120)
        self.video_interval.setSuffix(' s')
        s.row('Video: one frame every', self.video_interval, 'Frames that look the same are read once; at most 120 per video')

        s = self.section(page, 'API keys')
        for kind, title, plan, action in (('anthropic', 'Claude API', 'Pay as you go with an API key', 'Add key…'),
                                          ('openai', 'OpenAI API', 'Pay as you go with an API key', 'Add key…'),
                                          ('compatible', 'Custom server', 'Any OpenAI-compatible server: OpenRouter, Groq, LM Studio…', 'Key…')):
            s.add(self.account_row(kind, title, plan, action))
        self.compatible_url = QLineEdit()
        self.compatible_url.setPlaceholderText('Server address, e.g. https://openrouter.ai/api/v1 or http://localhost:1234/v1')
        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(36, 0, 0, 6)
        line.addWidget(self.compatible_url)
        s.group.rows.addWidget(holder)
        s.header.setToolTip('Keys are kept in Windows Credential Manager, never in Juno’s files')

        s = self.section(page, 'Diagnostics')
        row = QHBoxLayout()
        row.addWidget(button('Check status', lambda: self.diagnose(False)))
        row.addWidget(button('Run test', lambda: self.diagnose(True), 'primary', 'Loads the models and answers one test question'))
        self.diag = s.row('Graphics card and models', row, 'Not checked yet.').caption
        self.diag.setTextFormat(Qt.MarkdownText)
        self.diag.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.index_info = label('', 'caption', wrap=True, selectable=True)
        self.index_info.setContentsMargins(0, 6, 0, 6)
        s.add(self.index_info)
        for page in self.pages.values():
            page.body.addStretch()

        self.save_timer = QTimer(self, singleShot=True, timeout=self.save)
        self.compatible_url.textEdited.connect(lambda _: self.save_timer.start(600))
        for widget in (self.device, self.embed_device, self.theme, self.effort):
            widget.currentIndexChanged.connect(lambda _: self.save_timer.start(250))
        self.video_interval.valueChanged.connect(lambda _: self.save_timer.start(800))
        self.keep_minutes.currentIndexChanged.connect(lambda _: self.save_timer.start(250))
        for toggle in (self.embed_enabled, self.ocr_enabled, self.reduce_motion, self.auto_index, self.search_on_gpu, self.save_chats,
                       self.beyond, self.rerank_enabled, self.see_through, self.index_on_battery, self.picture_search,
                       self.code_search):
            toggle.toggled.connect(lambda _: self.save_timer.start(250))
        self.rerank_model.currentIndexChanged.connect(lambda _: (self.save_timer.start(250), self.show_rerank()))
        self.loading = False
        guard_wheel(self)

    # ------------------------------------------------------------ pages
    def page(self, name):
        page = Page(name)
        self.pages[name] = page
        self.stack.addWidget(page)
        return page

    def section(self, page, title):
        section = page.add(Section(self.win, title))
        self.sections[title] = section
        return section

    def current_page(self):
        return SETTINGS_PAGES[self.pages_list.current]

    def show_page(self, name, section=None):
        """Show page ``name`` (one of SETTINGS_PAGES); ``section``: also open that section of it and scroll to it."""
        self.pages_list.set_current(SETTINGS_PAGES.index(name))
        if section:
            target = self.sections[section]
            target.set_open(True, animate=False)
            page = self.pages[name]
            QTimer.singleShot(0, lambda: page.ensureWidgetVisible(target, 0, 0))

    def enter_page(self):
        """Tab from the search box: the first control of the page that is showing gets the focus."""
        page = self.stack.currentWidget()
        widget = self.pages_list.nextInFocusChain()
        while widget is not None and widget is not self.pages_list:
            if (page.isAncestorOf(widget) and widget.isVisibleTo(page) and widget.isEnabled() and widget.focusPolicy() & Qt.TabFocus):
                widget.setFocus(Qt.TabFocusReason)
                page.ensureWidgetVisible(widget)
                return True
            widget = widget.nextInFocusChain()
        self.pages_list.setFocus(Qt.TabFocusReason)
        return False

    def keyPressEvent(self, event):
        """Keys that reach Settings from the page list or a control on a page (the search box has its own)."""
        key, ctrl = event.key(), bool(event.modifiers() & Qt.ControlModifier)
        if key == Qt.Key_Escape:
            self.win.set_mode(SEARCH)
        elif ctrl and Qt.Key_1 <= key <= Qt.Key_3:
            self.win.set_mode(key - Qt.Key_1)
        else:
            super().keyPressEvent(event)

    def activated(self):
        self.loading = True
        s = self.win.settings
        for widget in self.findChildren(QWidget):
            widget.blockSignals(True)
        self.compatible_url.setText(s.get('compatible_base_url') or '')
        self.device.setCurrentIndex(1 if s.get('device') == 'cpu' else 0)
        self.embed_enabled.setChecked(bool(s.get('embed_enabled')))
        self.embed_device.setCurrentIndex(1 if s.get('embed_device') == 'cpu' else 0)
        self.search_on_gpu.setChecked(bool(s.get('search_on_gpu')))
        self.ocr_enabled.setChecked(bool(s.get('ocr_enabled', True)))
        self.picture_search.setChecked(bool(s.get('picture_search', True)))
        self.code_search.setChecked(bool(s.get('code_search', True)))
        self.video_interval.setValue(int(s.get('video_interval') or 10))
        self.save_chats.setChecked(bool(s.get('save_chat_history', True)))
        self.theme.setCurrentIndex({'system': 0, 'dark': 1, 'light': 2}.get(s.get('theme'), 0))
        self.reduce_motion.setChecked(bool(s.get('reduce_motion')))
        self.see_through.setChecked(bool(s.get('see_through', True)))
        self.auto_index.setChecked(bool(s.get('auto_index', True)))
        self.index_on_battery.setChecked(bool(s.get('index_on_battery', False)))
        self.show_keep_loaded(ai.keep_alive_minutes(s))
        self.beyond.setChecked(bool(s.get('answer_beyond_files', False)))
        self.rerank_enabled.setChecked(bool(s.get('rerank_enabled', False)))
        self.rerank_model.setCurrentIndex(max(0, self.rerank_model.findData(s.get('rerank_model') or rerank.DEFAULT)))
        for widget in self.findChildren(QWidget):
            widget.blockSignals(False)
        self.show_rerank()
        self.show_pictures()
        self.load_files()
        self.load_models()
        self.load_effort()
        self.show_chats_info()
        self.show_hotkey()
        self.show_other_tools()
        run_job(lambda s: startup.enabled(), self.show_start_login)
        self.tracked.load()
        self.show_index_info()
        self.show_gpu_check()
        self.loading = False

    def show_meaning(self):
        """The Search page's "Search by meaning" row: what it needs and its download, in the row itself."""
        downloads, caption = self.win.downloads, self.meaning_row.caption
        running = [key for key in MEANING_FILES if downloads.running(key)]
        if running:
            done = sum(downloads.progress.get(key, (0, 0))[0] for key in running)
            total = sum(downloads.progress.get(key, (0, models.size_of(key)))[1] for key in running)
            caption.setText(f'Downloading the search model… {done / 1e6:,.0f} of {total / 1e6:,.0f} MB')
            self.meaning_button.setText('Cancel')
            self.meaning_button.show()
            return
        states = [self.file_states.get(key) for key in MEANING_FILES]
        if any(state is None for state in states):
            caption.setText('Checking…')
            self.meaning_button.hide()
            return
        missing = [state for state in states if state['where'] == 'missing']
        if not missing:
            caption.setText('Finds files by what they are about · ready on this PC')
            self.meaning_button.hide()
            return
        left = sum(state['size'] - state['part'] for state in missing)
        offered = all(state['downloadable'] for state in missing)
        caption.setText(f'Needs the search model ({left / 1e6:,.0f} MB, once) · not downloaded yet' if offered else
                        'Needs the search model, which this copy of Juno cannot download')
        self.meaning_button.setText('Download…')
        self.meaning_button.setVisible(offered)

    def meaning_action(self):
        downloads = self.win.downloads
        running = [key for key in MEANING_FILES if downloads.running(key)]
        if running:
            for key in running:
                downloads.cancel(key)
            return
        missing = [key for key in MEANING_FILES if (self.file_states.get(key) or {}).get('where') == 'missing'
                   and self.file_states[key]['downloadable']]
        if not missing:
            return
        left = sum(self.file_states[key]['size'] - self.file_states[key]['part'] for key in missing)
        home = self.win.settings.home
        answer = ask_box(self, 'Download the search model?',
                         f'Download what search by meaning needs: {left / 1e6:,.0f} MB, once. Free space on this drive: '
                         f'{models.free_space(home) / 1e9:,.1f} GB. It comes from github.com and is checked against its SHA-256 '
                         'before Juno uses it.', QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        if answer == QMessageBox.Ok:
            for key in missing:
                downloads.start(key)

    def show_pictures(self):
        """"Find pictures by what they show" needs search by meaning (and the picture search model): its help line says so."""
        on = self.embed_enabled.isChecked()
        self.picture_search.setEnabled(on)
        state = self.file_states.get('embed-pictures')
        if not on:
            text = 'Turn on search by meaning first'
        elif state is not None and state['where'] == 'missing':
            text = f"Needs the picture search model ({state['size'] / 1e6:,.0f} MB) · Advanced → Model files"
        else:
            text = 'Find a photo by describing it, like “dog on a beach”'
        self.picture_row.caption.setText(text)

    def show_gpu_check(self):
        """The graphics card check's verdict or reason (asking llama-server for devices takes a moment: in the background)."""
        settings = self.win.settings
        run_job(lambda s: ai.runtime.gpu_summary(settings), lambda text: self.gpu_check.setText(plain(text)),
                lambda error: self.gpu_check.setText(plain(f'Unknown ({error})')))

    def check_gpu_again(self):
        ai.runtime.clear_gpu_verdict(self.win.settings.home)
        self.win.toast('The graphics card is checked again at the next indexing run', 'green', seconds=4)
        self.show_gpu_check()

    def mcp_command_line(self):
        """What any other MCP tool runs, in one line: program, arguments and environment."""
        spec = connect.server_spec()
        env = ''.join(f'{key}={value}  ·  ' for key, value in spec['env'].items())
        return f'{env}"{spec["command"]}" {" ".join(spec["args"])}'

    def copy_mcp_command(self):
        QApplication.clipboard().setText(connect.other_tool_json())
        self.win.toast('Copied the server entry for your AI tool’s MCP settings', 'green', seconds=3)

    def show_tool_rows(self):
        """Each AI tool found on this PC, with Connect / Update / Remove; tools that aren't installed stay hidden."""
        shown = 0
        for tool in connect.TOOLS:
            holder = self.mcp_tool_rows[tool.id]
            try:
                found = tool.found()
            except OSError:
                found = False
            holder.setVisible(found)
            if holder.hairline is not None:
                holder.hairline.setVisible(found and shown > 0)  # no line above the first row shown
            if not found:
                continue
            shown += 1
            if tool.id in self.mcp_busy:
                holder.caption.setText('Working…')
                holder.action.setEnabled(False)
                continue
            try:
                status = tool.status()
            except Exception as error:  # a tool's settings we can't read must not break the page
                status = {'state': 'outdated', 'detail': f'Couldn’t read its settings: {error}'}
            holder.caption.setText(status['detail'])
            holder.action.setEnabled(True)
            holder.action.setText({'connected': 'Remove', 'outdated': 'Update'}.get(status['state'], 'Connect'))
            holder.state = status['state']
        self.mcp_other.hairline.setVisible(shown > 0)

    def connect_tool(self, tool_id):
        """Connect (or update, or remove) Juno in one AI tool, in the background: the CLIs take a few seconds."""
        tool = connect.tool(tool_id)
        remove = getattr(self.mcp_tool_rows[tool_id], 'state', '') == 'connected'
        self.mcp_busy.add(tool_id)
        self.show_tool_rows()

        def finish(_=None, error=None):
            self.mcp_busy.discard(tool_id)
            self.show_tool_rows()
            if error:
                self.win.toast(f'{tool.name}: {error}', 'red', seconds=8)
            elif remove:
                self.win.toast(f'Removed Juno from {tool.name}', 'green', seconds=4)
            else:
                self.win.toast(f'Connected to {tool.name} and checked that it can start Juno. If it is open, restart it', 'green', seconds=7)

        run_job(lambda *_: tool.disconnect() if remove else tool.connect(), finish, lambda error: finish(error=error))

    def show_other_tools(self):
        """The AI tools on this PC, the tools that asked (Allowed / Remove) and the last 50 calls, from the data folder."""
        home = self.win.settings.home
        self.show_tool_rows()
        self.mcp_command.setText(self.mcp_command_line())
        while self.mcp_clients.count():
            item = self.mcp_clients.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        clients = mcp_server.list_clients(home)
        if not clients:
            self.mcp_clients.addWidget(label('No AI tool has asked yet.', 'caption'))
        for name, info in clients:
            line = QWidget()
            row = QHBoxLayout(line)
            row.setContentsMargins(0, 4, 0, 4)
            row.addWidget(label(name), 1)
            allowed = Toggle(lambda: self.win.pal)
            allowed.setChecked(info.get('allowed') is True)
            allowed.toggled.connect(lambda on, n=name: mcp_server.set_allowed(home, n, on))
            row.addWidget(label('Allowed', 'caption'))
            row.addWidget(allowed)
            row.addWidget(button('Remove', lambda _=False, n=name: (mcp_server.remove_client(home, n), self.show_other_tools()),
                                 None, 'Forget this tool; it is asked again next time'))
            self.mcp_clients.addWidget(line)
        lines = [call_text(call) for call in mcp_server.recent_calls(home, 50)]
        self.mcp_calls.setText('\n'.join(lines[:self.calls_limit]) if lines else 'No requests yet.')
        self.more_calls.setVisible(len(lines) > CALLS_SHOWN)
        self.more_calls.setText('Show fewer' if self.calls_limit > CALLS_SHOWN else f'Show more ({len(lines) - CALLS_SHOWN})')

    def toggle_calls(self):
        self.calls_limit = CALLS_SHOWN if self.calls_limit > CALLS_SHOWN else 50
        self.show_other_tools()

    def show_keep_loaded(self, minutes):
        """Select ``minutes`` in the Keep models loaded list (a value saved by hand that isn't listed is added to it)."""
        index = self.keep_minutes.findData(minutes)
        if index < 0:
            index = sum(1 for m in KEEP_LOADED if m < minutes)
            self.keep_minutes.insertItem(index, duration_text(minutes), minutes)
        self.keep_minutes.setCurrentIndex(index)

    def show_rerank(self):
        if self.rerank_job is not None:
            return
        state = rerank.state(dict(self.win.settings, rerank_model=self.rerank_model.currentData()))
        self.rerank_button.setText('Remove' if state['installed'] else 'Download…')
        self.rerank_gpu_button.setVisible(bool(state['installed'] and state['gpu'] and state['gpu_size_mb']))
        self.rerank_gpu_button.setText('Remove faster copy' if state['gpu_model'] else 'Faster copy…')
        self.rerank_gpu_button.setToolTip(f"A half-precision copy ({state['gpu_size_mb'] or 0:,} MB) that your graphics card runs several times faster")
        if state['ready']:
            text = 'Ready · runs on the graphics card' + (' (half precision)' if state['gpu_model'] else ' (8-bit)') if state['gpu'] else 'Ready · runs on this PC'
        else:
            text = plain(state['detail']) if state['installed'] else f"Not downloaded · {state['size_mb']} MB"
        self.rerank_status.setText(text)
        self.rerank_status.setToolTip('' if state['installed'] else
                                      f"{state['label']} is {state['size_mb']} MB from Hugging Face, checked against its published checksum")

    def rerank_action(self):
        name = self.rerank_model.currentData()
        info = rerank.spec(name)
        if self.rerank_job is not None:
            self.rerank_job.set()
            return
        if rerank.installed(name):
            answer = ask_box(self, 'Remove the reranker?', f"Delete {info['label']} ({info['size_mb']} MB) from this PC? "
                                          'Answers will use the keyword + meaning order until you download it again.',
                                          QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer == QMessageBox.Yes:
                rerank.remove(name)
                self.show_rerank()
            return
        answer = ask_box(self, 'Download the reranker?',
                                      f"Download {info['label']} ({info['size_mb']} MB, {info['languages']}) from huggingface.co/{info['repo']}? "
                                      'It runs on this PC; nothing about your files is sent anywhere.', QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        if answer != QMessageBox.Ok:
            return
        self.rerank_job = cancel = threading.Event()
        self.rerank_button.setText('Stop')

        def progress(value):
            done, total = value
            self.rerank_status.setText(f'Downloading {info["label"]}… {done / 2 ** 20:,.0f} of {total / 2 ** 20:,.0f} MB')

        def finished(result=None, error=None):
            self.rerank_job = None
            self.show_rerank()
            if error is not None:
                self.win.toast(str(error), 'orange' if cancel.is_set() else 'red', seconds=6)
            else:
                self.win.toast(f"{info['label']} is ready — answers now use it", 'green', seconds=4)
                run_job(lambda s: rerank.for_settings(dict(self.win.settings)) and None)  # load it now, not on the next question
        run_job(lambda s: rerank.download(name, lambda d, t: s.progress.emit((d, t)), cancel), lambda r: finished(r),
                lambda e: finished(error=e), progress=progress)

    def rerank_gpu_action(self):
        name = self.rerank_model.currentData()
        info = rerank.spec(name)
        if self.rerank_job is not None:
            self.rerank_job.set()
            return
        if rerank.gpu_model_installed(name):
            answer = ask_box(self, 'Remove the faster copy?', f"Delete the graphics card copy of {info['label']} ({info['gpu_size_mb']:,} MB)? "
                             'The reranker keeps working with the smaller copy.', QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer == QMessageBox.Yes:
                for local in info.get('gpu_files', {}):
                    (rerank.folder(name) / local).unlink(missing_ok=True)
                rerank.forget()
                self.show_rerank()
            return
        answer = ask_box(self, 'Download the faster copy?',
                         f"Download the half-precision copy of {info['label']} ({info['gpu_size_mb']:,} MB) from huggingface.co/{info['repo']}? "
                         'Your graphics card runs it several times faster, and it scores closer to the original model.',
                         QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        if answer != QMessageBox.Ok:
            return
        self.rerank_job = cancel = threading.Event()
        self.rerank_gpu_button.setText('Stop')

        def progress(value):
            done, total = value
            self.rerank_status.setText(f'Downloading the faster copy… {done / 2 ** 20:,.0f} of {total / 2 ** 20:,.0f} MB')

        def finished(result=None, error=None):
            self.rerank_job = None
            self.show_rerank()
            if error is not None:
                self.win.toast(str(error), 'orange' if cancel.is_set() else 'red', seconds=6)
            else:
                self.win.toast('The reranker now runs on your graphics card', 'green', seconds=4)
                run_job(lambda s: rerank.for_settings(dict(self.win.settings)) and None)
        run_job(lambda s: rerank.download(name, lambda d, t: s.progress.emit((d, t)), cancel, gpu=True), lambda r: finished(r),
                lambda e: finished(error=e), progress=progress)

    def load_files(self):
        """Where each model file and the engine are (finding Ollama's copy may hash it once: in the background)."""
        home = self.win.settings.home

        def shown(states):
            self.file_states.update(states)
            for key in DOWNLOAD_ROWS:
                self.show_file(key)
            self.show_meaning()
            self.show_pictures()
        run_job(lambda s: {key: models.state(key, home) for key in DOWNLOAD_ROWS}, shown,
                lambda error: logging.getLogger('juno').warning('Model files: %s', error))

    def show_file(self, key):
        status, action = self.file_status[key], self.file_button[key]
        downloads = self.win.downloads
        if key in MEANING_FILES:
            self.show_meaning()
        if downloads.running(key):
            done, total = downloads.progress.get(key, (0, models.size_of(key)))
            status.setText(f'Downloading… {done / 1e6:,.0f} of {total / 1e6:,.0f} MB')
            action.setText('Cancel')
            action.show()
            return
        state = self.file_states.get(key)
        if state is None:
            status.setText('Checking…')
            action.hide()
            return
        size = f"{state['size'] / 1e6:,.0f} MB"
        where = state['where']
        if where == 'juno':
            status.setText(f'Juno’s copy · {size}')
            action.setText('Remove…')
        elif where == 'ollama':
            status.setText(f'Using Ollama’s copy · {size}')
        elif where == 'external':
            status.setText('Using a copy set up outside Juno' + ('' if key == models.ENGINE else f' · {size}'))
        elif where == 'included':
            status.setText('Came with Juno')
        elif not state['downloadable']:
            status.setText('Not downloaded · no download offered')
        else:
            part = f" · {state['part'] / 1e6:,.0f} MB so far" if state['part'] else ''
            status.setText(f'Not downloaded · {size}{part}')
            action.setText('Download…')
        action.setVisible(where == 'juno' or (where == 'missing' and state['downloadable']))

    def file_action(self, key):
        downloads = self.win.downloads
        if downloads.running(key):
            downloads.cancel(key)
            return
        state = self.file_states.get(key)
        if state is None:
            return
        name = DOWNLOAD_ROWS[key][1]
        home = self.win.settings.home
        if state['where'] == 'juno':
            answer = ask_box(self, f'Remove {name}?', f"Delete Juno’s copy of {state['label']} ({state['size'] / 1e6:,.0f} MB) from this PC? "
                             'What needs it stops until you download it again.', QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer == QMessageBox.Yes:
                remove = (lambda: models.remove_engine(home)) if key == models.ENGINE else (lambda: models.remove(key, home))
                run_job(lambda s: remove(), lambda _: self.load_files(),
                        lambda error: (self.load_files(), self.win.toast(str(error), 'red', seconds=8)))
            return
        if state['where'] != 'missing' or not state['downloadable']:
            return
        left = state['size'] - state['part']
        so_far = f" ({state['part'] / 1e6:,.0f} MB already downloaded)" if state['part'] else ''
        answer = ask_box(self, f'Download {name}?',
                         f"Download {state['label']}: {state['size'] / 1e6:,.0f} MB{so_far}, {left / 1e6:,.0f} MB to go. "
                         f"Free space on this drive: {models.free_space(home) / 1e9:,.1f} GB. It comes from github.com and is checked "
                         'against its SHA-256 before Juno uses it.', QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        if answer == QMessageBox.Ok:
            downloads.start(key)

    def file_finished(self, key, error):
        self.load_files()

    def show_index_info(self):
        st = self.win.status
        self.index_info.setText(f"Index: {st['files']:,} files · {st['segments']:,} pieces of text · {st['vectors']:,} searchable by meaning · "
                                f"{st['errors']} failed · {st['unsupported']} unsupported · last indexed {st['last_indexed'] or 'never'}"
                                + ''.join(f"\n{p['status']}: {p['path']} — {plain(p['error'])}" for p in self.win.index.problems(6)))

    def load_models(self, announce=False):
        """Check every account and show the current chat model."""
        settings = dict(self.win.settings)
        if announce:
            providers.forget_status()

        def done(entries):
            provider, info = self.win.ask_view.chat_model()
            chosen = info['configured']
            self.model.set_model(provider.kind if chosen else None,
                                 f"{info.get('name')} · {picker.SUBTITLES.get(provider.kind, 'On this PC')}" if chosen else 'Choose a model',
                                 animate=self.isVisible())
            self.show_accounts(entries)
            self.show_location()
            if announce:
                ready = sum(len(e['models']) for e in entries)
                self.win.toast(f'{ready} chat model{"s" * (ready != 1)} ready', 'green' if ready else 'orange', seconds=4)
        run_job(lambda s: providers.catalog(settings), done)

    def account_row(self, kind, title, plan, action):
        """Logo, name (with an Experimental badge for subscriptions), a drawn status dot and text, and the action."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 9, 0, 9)
        layout.setSpacing(12)
        art = QLabel()
        art.setFixedSize(24, 24)
        art.setPixmap(brand_pixmap(picker.LOGOS[kind], 24, self.win.pal['text']))
        art.kind = kind
        self.account_logos = getattr(self, 'account_logos', []) + [art]
        layout.addWidget(art)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(7)
        top.addWidget(label(title, 'name'))
        if kind in picker.EXPERIMENTAL:
            badge = label('Experimental', 'badge')
            badge.setToolTip('Uses the provider’s official command-line tool with your subscription; it can change when they update it')
            top.addWidget(badge)
        top.addStretch()
        texts.addLayout(top)
        status = QHBoxLayout()
        status.setSpacing(6)
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.hide()
        status.addWidget(dot)
        text = label(plan, 'caption')
        text.plan = plan
        status.addWidget(text)
        status.addStretch()
        texts.addLayout(status)
        layout.addLayout(texts, 1)
        act = button(action, lambda _=False, k=kind: self.account_action(k))
        layout.addWidget(act, 0, Qt.AlignVCenter)
        self.account_status[kind], self.account_dot[kind], self.account_button[kind] = text, dot, act
        return row

    def show_accounts(self, entries):
        for art in getattr(self, 'account_logos', []):  # re-tint for the current theme
            art.setPixmap(brand_pixmap(picker.LOGOS[art.kind], 24, self.win.pal['text']))
        for entry in entries:
            kind, state = entry['kind'], entry['status']['state']
            if kind == 'local':
                continue
            color = {'ready': 'green', 'signed_out': 'orange', 'unreachable': 'red'}.get(state)
            dot = self.account_dot[kind]
            dot.setVisible(color is not None)
            if color:
                dot.setPixmap(dot_pixmap(self.win.pal[color], 8))
            detail = entry['status']['detail'] or ''
            if state in ('no_key', 'missing'):
                note = {'missing': 'not installed', 'no_key': 'no address yet' if kind == 'compatible' else 'no key yet'}[state]
                detail = f"{self.account_status[kind].plan} · {note}"
            if kind in ('claude_code', 'codex'):  # a subscription: "Signed in" or not; what the tool said is the tooltip
                self.account_status[kind].setToolTip(plain(entry['status']['detail'] or ''))
                detail = {'ready': 'Signed in', 'signed_out': 'Not signed in', 'missing': 'Its command-line tool is not installed'}.get(state, detail)
            self.account_status[kind].setText(plain(detail))
            if kind in ('claude_code', 'codex'):
                self.account_button[kind].setText({'ready': 'Re-check', 'missing': 'How to install'}.get(state, 'Sign in…'))
            elif kind in ('anthropic', 'openai'):
                self.account_button[kind].setText('Remove key' if state == 'ready' else 'Add key…')
            else:
                self.account_button[kind].setText('Remove key' if keys.has('compatible') else 'Key…')

    def show_location(self):
        provider = providers.for_settings(self.win.settings)
        if not provider.describe()['configured']:
            self.location.setText('Choose an answer model to use Ask Juno')
        elif provider.cloud:
            self.location.setText(f'Answers come {provider.via}: your questions and the text found for them go there')
        else:
            self.location.setText('Answers are written on this PC; nothing leaves it')

    def account_action(self, kind):
        provider = providers.for_settings(self.win.settings, kind, '')
        names = {'anthropic': ('Claude API key', 'Paste your Anthropic API key (from console.anthropic.com).'),
                 'openai': ('OpenAI API key', 'Paste your OpenAI API key (from platform.openai.com).'),
                 'compatible': ('Custom server key', 'Paste the API key for your custom server (leave empty if it needs none).')}
        if kind in ('claude_code', 'codex'):
            state = provider.status()['state']
            if state == 'missing':
                self.win.toast(provider.install_hint, 'orange', seconds=10)
            elif state == 'ready':
                self.load_models(announce=True)
            else:
                try:
                    provider.sign_in()
                    self.win.toast('Finish signing in in the window that opened, then press Re-check', 'green', seconds=10)
                except ai.ProviderError as error:
                    self.win.toast(str(error), 'red', seconds=8)
            return
        if keys.has(kind) and self.account_button[kind].text() == 'Remove key':
            keys.delete(kind)
            self.win.toast('Key removed from Windows Credential Manager', 'green')
            self.load_models()
            return
        title, prompt = names[kind]
        value, ok = ask_text(self, title, prompt + '\nIt’s stored in Windows Credential Manager.', QLineEdit.Password)
        if not ok or not value.strip():
            return
        try:
            keys.put(kind, value)
        except RuntimeError as error:
            self.win.toast(str(error), 'red', seconds=8)
            return
        settings = dict(self.win.settings)

        def checked(models):
            self.win.toast(f'Key saved · {len(models)} models available' if models else 'Key saved, but no models came back — check the key',
                           'green' if models else 'orange', seconds=6)
            self.load_models()
        run_job(lambda s: providers.for_settings(settings, kind, '').models(), checked)

    def load_effort(self):
        settings = dict(self.win.settings)

        def show(value):
            fill_effort(self.effort, None, value[0], value[1], settings.get('effort'))
            self.effort.setToolTip(plain(value[1]))
            for widget in (self.effort_row, self.effort_row.hairline):
                widget.setVisible(bool(value[0]))  # only for models that can think
        run_job(lambda s: providers.for_settings(settings).effort_options(), show)

    def show_chats_info(self):
        store = self.win.ask_view.store
        count = len(store.list())
        self.chats_place.setText(paths.shorten(store.folder, 44))
        self.chats_place.setToolTip(str(store.folder))
        self.delete_chats.setText(f'Delete all chats ({count})…' if count else 'Delete all chats…')
        self.delete_chats.setEnabled(bool(count))
        self.delete_chats_note.setText('Permanently, after asking first' if count else 'No saved chats yet')

    def open_chats_folder(self):
        folder = self.win.settings.chats_folder
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

    def change_chats_folder(self):
        old = self.win.settings.chats_folder
        chosen = QFileDialog.getExistingDirectory(self, 'Choose where Juno saves your chats', str(old))
        if not chosen:
            return
        folder = Path(os.path.realpath(chosen))
        if folder.parent == folder:
            self.win.toast('Choose a folder, not a whole drive', 'orange')
            return
        if any(paths.is_within(t, folder) or paths.is_within(folder, t) for t in self.win.settings.folders):
            self.win.toast('Keep chats outside your tracked folders, so they don’t show up in search', 'orange', seconds=6)
            return
        moved = 0
        if old.is_dir() and paths.canonical(old) != paths.canonical(folder):
            for file in old.glob('*.json'):
                target = folder / file.name
                if not target.exists():
                    shutil.move(str(file), str(target))
                    moved += 1
        default = self.win.settings.home / 'Chats'
        self.win.settings['chats_folder'] = '' if paths.canonical(folder) == paths.canonical(default) else str(folder)
        self.win.settings.save()
        self.win.ask_view.chats_changed()
        self.show_chats_info()
        self.win.toast(f'Chats are now saved in {folder.name}' + (f' · moved {moved} chat{"s" * (moved != 1)}' if moved else ''), 'green', seconds=5)

    def delete_all_chats(self):
        store = self.win.ask_view.store
        count = len(store.list())
        if not count:
            return
        answer = ask_box(self, 'Delete all chats?', f'This permanently deletes {count} saved chat{"s" * (count != 1)} from '
                                      f'{store.folder}. It can’t be undone.', QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return
        deleted = store.delete_all()
        self.win.ask_view.chats_changed(current_deleted=True)
        self.show_chats_info()
        self.win.toast(f'Deleted {deleted} chat{"s" * (deleted != 1)}', 'green')

    def show_start_login(self, on):
        self.start_login.blockSignals(True)
        self.start_login.setChecked(bool(on))
        self.start_login.blockSignals(False)

    def change_start_login(self, on):
        def work(signals):
            startup.enable() if on else startup.disable()
            return startup.enabled()

        def done(now):
            self.show_start_login(now)
            self.win.toast('Juno will start hidden in the tray when you sign in' if now else
                           'Juno won’t start automatically any more', 'green' if now == on else 'red', seconds=4)
        run_job(work, done, lambda error: (self.show_start_login(not on), self.win.toast(f'Couldn’t change start-up: {error}', 'red', seconds=8)))

    def show_hotkey(self):
        active = self.win.active_hotkey
        self.hotkey_edit.blockSignals(True)
        self.hotkey_edit.setKeySequence(QKeySequence(active.replace('Win+', 'Meta+')) if active else QKeySequence())
        self.hotkey_edit.blockSignals(False)
        self.hotkey_label.setText(f'Active: {active}' if active else 'None is active: open Juno from its icon near the clock')

    def change_hotkey(self, text=None):
        if text is None:
            sequence = self.hotkey_edit.keySequence().toString(QKeySequence.PortableText)
            if not sequence:
                self.show_hotkey()
                return
            text = sequence.replace('Meta+', 'Win+')
        if text == self.win.active_hotkey:
            self.show_hotkey()
            return
        try:
            self.win.rebind_hotkey(text)
            self.win.toast(f'Shortcut changed to {text}', 'green')
        except (OSError, ValueError) as error:
            self.win.toast(f'{text}: {error}', 'red', seconds=6)
        self.show_hotkey()

    def save(self):
        if self.loading:
            return
        s = self.win.settings
        before = (s.get('embed_enabled'), s.get('ocr_enabled'), s.get('picture_search', True), s.get('video_interval'), s.get('contextual_chunks'),
                  s.get('code_search', True))
        before_model = (s.get('provider'), s.get('model'))
        before_search = s.get('search_on_gpu')
        before_glass = bool(s.get('see_through', True))
        s.update(compatible_base_url=self.compatible_url.text().strip())
        if self.effort.isEnabled() and self.effort.currentData() is not None:
            s['effort'] = self.effort.currentData()
        s.update({'device': 'cpu' if self.device.currentIndex() else 'auto',
                  'embed_enabled': self.embed_enabled.isChecked(), 'search_on_gpu': self.search_on_gpu.isChecked(),
                  'embed_device': 'cpu' if self.embed_device.currentIndex() else 'auto',
                  'ocr_enabled': self.ocr_enabled.isChecked(), 'picture_search': self.picture_search.isChecked(),
                  'code_search': self.code_search.isChecked(),
                  'index_on_battery': self.index_on_battery.isChecked(),
                  'video_interval': self.video_interval.value(), 'theme': ['system', 'dark', 'light'][self.theme.currentIndex()],
                  'reduce_motion': self.reduce_motion.isChecked(), 'auto_index': self.auto_index.isChecked(),
                  'keep_alive_minutes': self.keep_minutes.currentData(),
                  'save_chat_history': self.save_chats.isChecked(), 'answer_beyond_files': self.beyond.isChecked(),
                  'rerank_enabled': self.rerank_enabled.isChecked(), 'rerank_model': self.rerank_model.currentData() or rerank.DEFAULT,
                  'see_through': self.see_through.isChecked()})
        s.save()
        self.win.watch_folders()
        Motion.detect(s)
        if s['see_through'] != before_glass:
            self.win.glass_setting_changed()
        self.win.apply_theme()
        self.win.refresh_status()
        self.win.ask_view.chats_changed()
        if (s.get('provider'), s.get('model')) != before_model:
            self.load_effort()
            self.show_location()
        if s.get('search_on_gpu') != before_search:
            ai.runtime.stop('search', s)  # this process's search server restarts on its new device at the next search
        after = (s.get('embed_enabled'), s.get('ocr_enabled'), s.get('picture_search', True), s.get('video_interval'), s.get('contextual_chunks'),
                 s.get('code_search', True))
        if after != before:
            self.win.toast('Saved · updating your index', 'green', seconds=4)
            self.win.start_index(quiet=True)
        else:
            self.win.toast('Settings saved', 'green', seconds=2)

    def refresh_models(self):
        self.load_models(announce=True)

    def diagnose(self, probe):
        settings = dict(self.win.settings)
        self.diag.setText('Checking…' + (' loading the models for a test' if probe else ''))
        if probe:
            self.win.thinking(True)

        def done(r):
            self.win.thinking(False)
            lines = []
            for g in r['gpu']['gpus']:
                lines.append(f"**Graphics card:** {g['name']} · {g['vram_used_mib']:,} / {g['vram_total_mib']:,} MiB in use · driver {g['driver']}")
            if not r['gpu']['available']:
                lines.append(f"**Graphics card:** none — {plain(r['gpu'].get('note', ''))}")
            for key, name in (('chat', 'Answer model'), ('embeddings', 'Search by meaning')):
                info = r.get(key, {})
                if (key == 'chat' and not info.get('configured')) or (key != 'chat' and not info.get('enabled')):
                    lines.append(f'**{name}:** off')
                    continue
                state = ({True: 'on the graphics card', False: 'on the CPU', None: 'device not confirmed'}[info.get('gpu_active')]
                         if info.get('location') == 'local' else 'in the cloud')
                lines.append(f"**{name}:** {info.get('model')} · **{state}** — {plain(str(info.get('execution', '')))}")
            lines += [f'**Error:** {plain(str(e))}' for e in r['errors']]
            self.diag.setText('  \n'.join(lines))
        run_job(lambda s: ai.diagnostics(settings, probe), done, lambda e: (self.win.thinking(False), self.diag.setText(plain(f'Failed: {e}'))))


# ------------------------------------------------------------------ window
def snapshot(widget):
    """A picture of ``widget`` and its children at the screen's pixel ratio on a transparent background.
    QWidget.grab() fills the picture with the palette's opaque window colour first, which shows as a dark slab over
    the see-through panel."""
    ratio = widget.devicePixelRatioF()
    picture = QPixmap(widget.size() * ratio)
    picture.setDevicePixelRatio(ratio)
    picture.fill(Qt.transparent)
    widget.render(picture, QPoint(), QRegion(widget.rect()), QWidget.RenderFlag.DrawChildren)
    return picture


class Veil(QWidget):
    """Transitions the panel's content: a snapshot of what was showing glides out while a snapshot of what comes next
    glides in, both on one spring, and then the real widgets take over pixel for pixel.

    The real widgets are swapped behind it (moved out of sight but still shown, so their layouts and scroll positions
    settle) and photographed only once that has happened, so a transition never shows a half-updated view, such as the
    last chat for a frame before the new one is laid out. Each frame is two pixmap blits: nothing is re-laid out or
    re-rendered (a QGraphicsOpacityEffect re-renders its whole widget tree into a buffer on every frame)."""
    SHIFT = 34  # how far content slides between modes

    def __init__(self, parent, finished):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.finished = finished
        self.old = self.new = None
        self.direction, self.active, self.collapsing = 0, False, False
        self.spring = Spring(0, 250, 0.84, self._step)  # slightly under-damped: settles with a soft liquid overshoot
        self.hide()

    def begin(self, old, direction, collapsing=False):
        self.old, self.new, self.direction, self.active, self.collapsing = old, None, direction, True, collapsing
        self.spring.snap(0)
        self.raise_()
        self.show()
        self.update()

    def arrive(self, new):
        """The next view, laid out and photographed: start the glide."""
        if not self.active:
            return
        self.new = new
        self.spring.to(1)

    def frame(self):
        """What is on screen right now (to start the next transition from mid-flight)."""
        return snapshot(self)

    def _step(self, value):
        if not self.active:
            return
        if self.new is not None and abs(1 - value) < 0.004 and abs(self.spring.velocity) < 0.06:
            self.spring.value = self.spring.target = 1.0  # the tail is invisible: hand over now
            self.spring.velocity = 0.0
            self.stop()
            return
        self.update()

    def stop(self):
        if self.active:
            self.active = False
            self.old = self.new = None
            self.hide()
            self.finished()

    def _draw(self, p, pixmap, opacity, dx, dy):
        """A snapshot blitted 1:1: it was rendered at the screen's pixel ratio and its offset is rounded to whole device
        pixels, so it is never resampled (no scaling, no smoothing) and looks exactly like the live widgets."""
        if pixmap is None or opacity <= 0.003:
            return
        p.setOpacity(min(1.0, opacity))
        # On the window's device-pixel grid (at 125 % the veil itself starts half a device pixel off it).
        p.drawPixmap(snapped(QRectF(dx, dy, 1, 1), self).topLeft(), pixmap)

    def paintEvent(self, event):
        p = QPainter(self)
        t = self.spring.value if self.new is not None else 0.0
        side = self.direction
        ease = max(0.0, min(1.0, t))
        if side:  # between modes: slide sideways, the old view leaving faster than the new one arrives
            # Into a collapsing panel the old view is gone within the first third, so it never shrinks away visibly.
            out = (1 - min(1.0, ease * 3)) ** 1.4 if self.collapsing else (1 - ease) ** 1.6
            self._draw(p, self.old, out, -side * self.SHIFT * 0.6 * t, 0)
            self._draw(p, self.new, min(1.0, ease * 1.5), side * self.SHIFT * (1 - t), 0)
        else:  # new content in the same place (another chat): crossfade with a small lift
            self._draw(p, self.old, (1 - ease) ** 1.4, 0, -6 * ease)
            self._draw(p, self.new, min(1.0, ease * 1.4), 0, 10 * (1 - t))


class EdgeFade:
    """Fades the resting shape's rim and lens band in over EDGE_FADE seconds on the shared frame clock, repainting only
    the edge ring."""

    def __init__(self, win):
        self.win = win

    def advance(self, elapsed):
        win = self.win
        win.edge = min(1.0, win.edge + elapsed / EDGE_FADE)
        win.update(win.edge_region())
        if win.edge >= 1.0:
            DRIVER.loops.discard(self)
            win.sync_glass_busy()


# ------------------------------------------------------------------ setup
SETUP_FOLDERS = ('Documents', 'Desktop', 'Downloads', 'Pictures')  # setup step 1's rows, in this order
SETUP_TICKED = ('Documents', 'Desktop')
SETUP_STEPS = ('What should Juno search?', 'Search by meaning', 'Open Juno from anywhere')
FILES_PER_MINUTE = 1250  # a rough indexing rate for the chooser's "About N minutes" (labelled "about")
ESTIMATE_ABOVE = 2000    # the time estimate shows only above this many files
LARGE_FOLDER = 20000     # above this, the chooser suggests starting with new files
ONLY_NEW_NOTE = 'Files already there stay findable by name. Anything you add or change from now on is fully searchable.'
LARGE_HINT = 'Large folder: you can start with new files and include the rest later.'


def count_files(folder, cancel=None):
    """How many files Juno would look at in ``folder`` (the index's own walk, nothing is read); None when cancelled."""
    count = 0
    for count, _ in enumerate(discover(folder), 1):
        if cancel is not None and count % 256 == 0 and cancel.is_set():
            return None
    return count


def estimate_text(count):
    """'About 10 minutes' for indexing ``count`` files in full (a rough constant rate)."""
    minutes = max(1, round(count / FILES_PER_MINUTE))
    if minutes >= 90:
        hours = round(minutes / 60)
        return f'About {hours} hours'
    return f'About {minutes} minute{"s" * (minutes != 1)}'


def files_text(count):
    return f'{count:,} file{"s" * (count != 1)}'


class FolderChooser(QWidget):
    """"Everything in it" (the default) or "Only new files from now on" for folders being tracked (design 4.1). One
    control for setup step 1 (all ticked folders: ``plural``) and the add-folder sheet."""
    changed = Signal()

    def __init__(self, win, plural=False):
        super().__init__()
        self.win, self.plural, self.count = win, plural, None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self.everything = QRadioButton()
        self.only_new = QRadioButton('Only new files from now on')
        self.group = QButtonGroup(self)
        for choice in (self.everything, self.only_new):
            self.group.addButton(choice)
            choice.setCursor(Qt.PointingHandCursor)
            choice.toggled.connect(lambda _: self.changed.emit())
        self.everything.setChecked(True)
        self.everything_note = label('', 'caption', wrap=True)
        self.only_new_note = label(ONLY_NEW_NOTE, 'caption', wrap=True)
        for note in (self.everything_note, self.only_new_note):
            note.setContentsMargins(26, 0, 0, 0)
        self.hint = label(LARGE_HINT, 'caption', wrap=True)
        self.hint.setContentsMargins(0, 6, 0, 0)
        layout.addWidget(self.everything)
        layout.addWidget(self.everything_note)
        layout.addSpacing(6)
        layout.addWidget(self.only_new)
        layout.addWidget(self.only_new_note)
        layout.addWidget(self.hint)
        self.show_texts()

    def reset(self, count=None):
        self.everything.setChecked(True)
        self.set_count(count)

    def set_count(self, count):
        """The number of files the folder(s) hold, or None while it is being counted."""
        self.count = count
        self.show_texts()

    def show_texts(self):
        self.everything.setText('Everything in them' if self.plural else 'Everything in it')
        count = self.count
        if count is None:
            text = 'All files become searchable.'
        elif count == 1:
            text = 'Its one file becomes searchable.'
        else:
            text = f'All {count:,} files become searchable.'
        if count and count > ESTIMATE_ABOVE:
            text += f' {estimate_text(count)}.'
        self.everything_note.setText(text)
        self.hint.setVisible(bool(count and count > LARGE_FOLDER))

    def only_new_files(self):
        return self.only_new.isChecked()


class StepDots(QWidget):
    """The setup's step dots: the current one in the accent colour."""

    def __init__(self, win, steps=3):
        super().__init__()
        self.win, self.steps, self.current = win, steps, 0
        self.setFixedSize(steps * 14, 14)

    def set(self, current):
        self.current = current
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        for number in range(self.steps):
            p.setBrush(self.win.pal['accent'] if number == self.current else self.win.pal['tertiary'])
            p.drawEllipse(QPointF(7 + 14 * number, 7), 3.5, 3.5)


class Keycaps(QWidget):
    """The shortcut drawn as keycaps (Ctrl + Alt + L); green once it was pressed."""

    def __init__(self, win):
        super().__init__()
        self.win, self.keys, self.done = win, [], False
        self.key_font = text_font(13, QFont.DemiBold)
        self.key_font.setFamilies(['Segoe UI Variable Text', 'Segoe UI'])  # as the style sheet's text
        self.setFixedHeight(54)

    def set_keys(self, text):
        self.keys = [part for part in (text or '').split('+') if part]
        self.done = False
        metrics = QFontMetrics(self.key_font)
        self.widths = [max(44, metrics.horizontalAdvance(key) + 30) for key in self.keys]
        self.setFixedWidth(max(1, sum(self.widths) + 26 * max(0, len(self.keys) - 1) + 4))
        self.update()

    def set_done(self, done=True):
        self.done = done
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.win.pal
        p.setFont(self.key_font)
        x = 2.0
        for number, (key, width) in enumerate(zip(self.keys, self.widths)):
            rect = QRectF(x, 3, width, 46)
            p.setPen(QPen(pal['green'] if self.done else pal['outline'], 1.4 if self.done else 1))
            p.setBrush(pal['fill_strong'])
            p.drawRoundedRect(rect, 9, 9)
            p.setPen(pal['green'] if self.done else pal['text'])
            p.drawText(rect, Qt.AlignCenter, key)
            x += width
            if number < len(self.keys) - 1:
                p.setPen(pal['secondary'])
                p.drawText(QRectF(x, 3, 26, 46), Qt.AlignCenter, '+')
                x += 26


def _card():
    """A rounded group card (the Settings look) and its row layout."""
    card = QFrame()
    card.setObjectName('group')
    rows = QVBoxLayout(card)
    rows.setContentsMargins(14, 4, 14, 4)
    rows.setSpacing(0)
    return card, rows


def _hairline():
    line = QFrame()
    line.setFixedHeight(1)
    line.setObjectName('hairline')
    return line


def _switch_row(title, caption, toggle):
    row = QWidget()
    line = QHBoxLayout(row)
    line.setContentsMargins(0, 9, 0, 9)
    texts = QVBoxLayout()
    texts.setSpacing(1)
    row.title = label(title)
    texts.addWidget(row.title)
    texts.addWidget(label(caption, 'caption', wrap=True))
    line.addLayout(texts, 1)
    line.addWidget(toggle, 0, Qt.AlignRight | Qt.AlignVCenter)
    return row


class SetupView(QWidget):
    """The first-run setup (design 2): three steps in the panel, one decision each. The bar shows the step title
    (JunoWindow.setup_title); the footer has step dots, Back, one primary button (Enter) and "Skip setup"."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.step = 0
        self.rows = []  # [(path, checkbox, count label)]
        self.counts = {}  # path -> files (None while counting)
        self.cancel = threading.Event()  # set when setup ends: background counts stop
        self.needs = None  # what search by meaning still needs (models.first_run_needs); None while checked
        self.download_wanted = False
        self.start_login_initial = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 14, 30, 10)
        outer.setSpacing(0)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._folders_page())
        self.pages.addWidget(self._meaning_page())
        self.pages.addWidget(self._shortcut_page())
        outer.addWidget(self.pages, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 8, 0, 0)
        self.skip_button = button('Skip setup', self.skip, 'link', 'Start with an empty Juno; folders can be added in Settings')
        footer.addWidget(self.skip_button)
        footer.addStretch(1)
        self.dots = StepDots(win, len(SETUP_STEPS))
        footer.addWidget(self.dots)
        footer.addStretch(1)
        self.back_button = button('Back', self.back)
        self.primary = button('Continue', self.forward, 'primary')
        self.primary.setMinimumWidth(110)
        footer.addWidget(self.back_button)
        footer.addSpacing(6)
        footer.addWidget(self.primary)
        outer.addLayout(footer)
        self.setTabOrder(self.primary, self.back_button)
        self.setTabOrder(self.back_button, self.skip_button)

    # ------------------------------------------------------------- pages
    def _page(self, sentence):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        page.sentence = label(sentence, 'secondary', wrap=True)
        layout.addWidget(page.sentence)
        return page, layout

    def _folders_page(self):
        page, layout = self._page('Choose the folders to start with. You can add or remove folders later in Settings.')
        card, self.folder_rows = _card()
        layout.addWidget(card)
        self.add_button = button('Add another folder…', self.add_folder, 'link', 'Choose any folder on this PC')
        add = QHBoxLayout()
        add.setContentsMargins(0, 0, 0, 0)
        add.addWidget(self.add_button)
        add.addStretch()
        layout.addLayout(add)
        self.chooser = FolderChooser(self.win, plural=True)
        layout.addWidget(self.chooser)
        layout.addStretch(1)
        lock = QHBoxLayout()
        lock.setSpacing(7)
        self.lock_icon = QLabel()
        self.lock_icon.setFixedSize(14, 14)
        lock.addWidget(self.lock_icon)
        lock.addWidget(label('Your files are read on this PC and never leave it.', 'caption'))
        lock.addStretch()
        layout.addLayout(lock)
        return page

    def _meaning_page(self):
        page, layout = self._page('Find files by what they are about, not only the words in them.')
        card, rows = _card()
        self.download = Toggle(lambda: self.win.pal)
        self.download.setChecked(True)
        self.download_row = _switch_row('Download the search model (once)',
                                        'Search by name and text already works. You can add this later in Settings.', self.download)
        rows.addWidget(self.download_row)
        self.download_card = card
        layout.addWidget(card)
        self.ready = label('Search by meaning is ready', 'name')
        self.ready_note = label('The search model is already on this PC.', 'caption')
        layout.addWidget(self.ready)
        layout.addWidget(self.ready_note)
        layout.addStretch(1)
        return page

    def _shortcut_page(self):
        page, layout = self._page('Press the shortcut in any app to open Juno, and again to hide it.')
        layout.addSpacing(8)
        caps = QHBoxLayout()
        caps.addStretch()
        self.keycaps = Keycaps(self.win)
        caps.addWidget(self.keycaps)
        caps.addStretch()
        layout.addLayout(caps)
        self.try_note = label('Try it now', 'caption')
        self.try_note.setAlignment(Qt.AlignCenter)
        self.try_note.setTextFormat(Qt.RichText)
        layout.addWidget(self.try_note)
        change = QHBoxLayout()
        change.addStretch()
        self.change_button = button('Change shortcut', self.show_recorder, 'link', 'Choose another combination')
        change.addWidget(self.change_button)
        self.recorder = QKeySequenceEdit()
        self.recorder.setMaximumSequenceLength(1)
        self.recorder.setFixedWidth(170)
        self.recorder.editingFinished.connect(self.record_finished)
        self.recorder.hide()
        change.addWidget(self.recorder)
        change.addStretch()
        layout.addLayout(change)
        layout.addSpacing(10)
        card, rows = _card()
        self.start_login = Toggle(lambda: self.win.pal)
        self.start_login.setChecked(True)
        rows.addWidget(_switch_row('Start Juno when I sign in', 'Juno waits near the clock, so the shortcut works right away',
                                   self.start_login))
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------- state
    def prepare(self):
        """Fill the steps for this showing of setup (at the start, or "Run setup again")."""
        self.cancel.set()
        self.cancel = threading.Event()
        while self.folder_rows.count():
            item = self.folder_rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.rows, self.counts = [], {}
        tracked = self.win.settings.folders
        found = dict(suggested_folders())
        for name in SETUP_FOLDERS:
            if name in found:
                done = any(paths.is_within(t, found[name]) for t in tracked)
                self._add_row(found[name], name, ticked=name in SETUP_TICKED or done, tracked=done)
        for folder in tracked:
            if not any(paths.canonical(path) == paths.canonical(folder) for path, _, _ in self.rows):
                self._add_row(folder, folder.name, ticked=True, tracked=True)
        self.chooser.reset()
        self.lock_icon.setPixmap(icon_pixmap('lock', 14, self.win.pal['secondary']))
        self.download.setChecked(True)
        self.download_wanted = False
        self.needs = None
        home = self.win.settings.home
        run_job(lambda s: models.first_run_needs(home), self.needs_checked,
                lambda error: logging.getLogger('juno').warning('Setup could not check the search model: %s', error))
        configured = bool(self.win.settings.get('start_with_windows_configured'))
        self.start_login_initial = True
        self.start_login.setChecked(True)
        if configured:  # setup run again: the switch shows how it is now
            run_job(lambda s: startup.enabled(), self.start_login_checked)
        self.show_step(0)

    def start_login_checked(self, on):
        self.start_login_initial = bool(on)
        self.start_login.setChecked(bool(on))

    def _add_row(self, folder, name, ticked=True, tracked=False):
        folder = Path(folder)
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 7, 0, 7)
        line.setSpacing(10)
        box = QCheckBox(name)
        box.setIcon(QIcon(folder_icon(folder, 20)))
        box.setIconSize(QSize(20, 20))
        box.setChecked(ticked)
        box.setEnabled(not tracked)
        box.setCursor(Qt.PointingHandCursor)
        box.toggled.connect(lambda _: self.update_choice())
        line.addWidget(box)
        place = label(paths.shorten(folder, 46), 'caption')
        place.setToolTip(str(folder))
        line.addWidget(place, 1)
        count = label('Tracked' if tracked else 'counting…', 'caption')
        line.addWidget(count)
        if self.rows:
            self.folder_rows.addWidget(_hairline())
        self.folder_rows.addWidget(row)
        self.rows.append((folder, box, count))
        if not tracked:
            self.counts[folder] = None
            cancel = self.cancel
            run_job(lambda s: count_files(folder, cancel), lambda n: self.counted(folder, count, n))
        self.update_choice()
        return box

    def counted(self, folder, widget, count):
        if count is None or folder not in self.counts:
            return
        self.counts[folder] = count
        try:
            widget.setText(files_text(count))
        except RuntimeError:  # the row went (setup prepared again)
            return
        self.update_choice()

    def chosen(self):
        """Ticked folders not tracked yet."""
        return [folder for folder, box, _ in self.rows if box.isChecked() and box.isEnabled()]

    def update_choice(self):
        chosen = self.chosen()
        counts = [self.counts.get(folder) for folder in chosen]
        self.chooser.set_count(sum(counts) if chosen and None not in counts else None)
        self.chooser.setEnabled(bool(chosen))
        if self.step == 0:
            self.primary.setText('Continue' if chosen or any(not box.isEnabled() for _, box, _ in self.rows) else 'Continue without folders')

    def needs_checked(self, needs):
        self.needs = list(needs)
        total = sum(models.size_of(key) for key in self.needs)
        self.download_row.title.setText(f'Download the search model ({total / 1e6:,.0f} MB, once)')
        self.download_card.setVisible(bool(self.needs))
        self.ready.setVisible(not self.needs)
        self.ready_note.setVisible(not self.needs)
        if self.download_wanted:
            self.download_wanted = False
            self.start_downloads()

    def start_downloads(self):
        for key in self.needs or []:
            self.win.downloads.start(key)

    # ------------------------------------------------------------- steps
    def title(self):
        return SETUP_STEPS[self.step]

    def show_step(self, step):
        self.step = step
        self.pages.setCurrentIndex(step)
        self.dots.set(step)
        self.back_button.setVisible(step > 0)
        if step == 0:
            self.update_choice()
        elif step == 1:
            self.primary.setText('Continue')
            ready = self.needs is not None and not self.needs
            self.download_card.setVisible(not ready)
            self.ready.setVisible(ready)
            self.ready_note.setVisible(ready)
        else:
            self.primary.setText('Start searching')
            self.show_shortcut()
        self.win.setup_title.setText(self.title())
        self.primary.setFocus()

    def go(self, step, direction):
        if self.win.mode == SETUP:
            self.win.transition(lambda: self.show_step(step), direction)
        else:
            self.show_step(step)
        self.primary.setFocus()

    def forward(self):
        if self.step == 0:
            self.track_chosen()
            self.go(1, 1)
        elif self.step == 1:
            if self.download.isChecked() and self.needs is None:
                self.download_wanted = True  # started once the check has answered
            elif self.download.isChecked():
                self.start_downloads()
            self.win.settings['model_download_asked'] = True
            self.win.settings.save()
            self.go(2, 1)
        else:
            self.finish()

    def back(self):
        if self.step > 0:
            self.go(self.step - 1, -1)

    def track_chosen(self):
        """Step 1's Continue: track the ticked folders with the chosen mode; one indexing run for all of them."""
        only_new = self.chooser.only_new_files()
        tracked = []
        for folder, box, count in self.rows:
            if box.isChecked() and box.isEnabled() and self.win.track_folder(folder, only_new=only_new, announce=False):
                tracked.append(folder)
                box.setEnabled(False)
                count.setText('Tracked')
        if tracked:
            self.win.toast(f'Tracking {len(tracked)} folder{"s" * (len(tracked) != 1)} · indexing now', 'green', seconds=5)
            self.win.start_index(quiet=True, roots=tracked)
        return tracked

    def finish(self):
        on = self.start_login.isChecked()
        if not self.win.settings.get('start_with_windows_configured') or on != self.start_login_initial:
            self.win.choose_start_login(on)
        self.win.finish_setup()

    def skip(self):
        self.win.choose_start_login(True, keep_existing=True)
        self.win.finish_setup()

    def add_folder(self):
        start = str(self.rows[0][0].parent) if self.rows else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, 'Choose a folder for Juno to search', start)
        if folder:
            self.add_chosen(folder)

    def add_chosen(self, folder):
        folder = Path(os.path.realpath(folder))
        reason = self.win.folder_problem(folder)
        if reason:
            self.win.toast(reason, 'orange', seconds=8)
            return None
        for path, box, _ in self.rows:
            if paths.canonical(path) == paths.canonical(folder):
                box.setChecked(True)
                return box
        return self._add_row(folder, folder.name or str(folder), ticked=True)

    # ------------------------------------------------------------- shortcut
    def show_shortcut(self):
        active = self.win.active_hotkey
        self.keycaps.set_keys(active)
        self.keycaps.setVisible(bool(active))
        self.change_button.setVisible(bool(active))
        self.recorder.setVisible(not active)
        self.try_note.setText('Try it now' if active else 'Open Juno from its icon near the clock, or choose a shortcut:')

    def hotkey_pressed(self):
        """The global shortcut while step 3 shows: tick the keycaps instead of hiding the panel."""
        self.keycaps.set_done(True)
        self.try_note.setText(f"<span style='color:{self.win.pal['green'].name()}'>That’s it</span>")

    def show_recorder(self):
        self.change_button.hide()
        self.recorder.show()
        self.recorder.clear()
        self.recorder.setFocus()

    def record_finished(self):
        sequence = self.recorder.keySequence().toString(QKeySequence.PortableText)
        if sequence:
            text = sequence.replace('Meta+', 'Win+')
            if text != self.win.active_hotkey:
                try:
                    self.win.rebind_hotkey(text)
                except (OSError, ValueError) as error:
                    self.win.toast(f'{text}: {error}', 'red', seconds=6)
            self.win.settings_view.show_hotkey()
        self.show_shortcut()
        self.primary.setFocus()

    # ------------------------------------------------------------- keys
    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            focused = QApplication.focusWidget()
            target = focused if isinstance(focused, QPushButton) and self.isAncestorOf(focused) else self.primary
            target.click()
            return
        if key == Qt.Key_Escape:
            self.win.hide_panel()
            return
        super().keyPressEvent(event)


class AddFolderSheet(QWidget):
    """Adding a folder (Settings, the empty state's suggestions): the folder, its count, the chooser and "Track folder".
    Shown in the panel in place of the current view; Enter tracks, Esc cancels."""

    def __init__(self, win):
        super().__init__()
        self.win, self.folder, self.inner, self.token = win, None, [], 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 22, 30, 16)
        outer.setSpacing(12)
        outer.addWidget(label('Track a folder', 'title'))
        card, rows = _card()
        rows.setContentsMargins(16, 12, 16, 14)
        rows.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(11)
        self.art = QLabel()
        self.art.setFixedSize(30, 30)
        top.addWidget(self.art)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.name = label('', 'name')
        self.place = label('', 'caption')
        texts.addWidget(self.name)
        texts.addWidget(self.place)
        top.addLayout(texts, 1)
        self.count = label('', 'caption')
        top.addWidget(self.count)
        rows.addLayout(top)
        self.merge_note = label('', 'caption', wrap=True)
        rows.addWidget(self.merge_note)
        rows.addWidget(_hairline())
        self.chooser = FolderChooser(win)
        rows.addWidget(self.chooser)
        outer.addWidget(card)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = button('Cancel', self.cancel)
        self.track_button = button('Track folder', self.accept, 'primary')
        self.track_button.setMinimumWidth(110)
        buttons.addWidget(self.cancel_button)
        buttons.addSpacing(6)
        buttons.addWidget(self.track_button)
        outer.addLayout(buttons)
        outer.addStretch(1)

    def open(self, folder, inner=()):
        self.folder, self.inner = Path(folder), list(inner)
        self.token += 1
        token = self.token
        self.art.setPixmap(folder_icon(self.folder, 28))
        self.name.setText(self.folder.name or str(self.folder))
        self.place.setText(paths.shorten(self.folder, 58))
        self.place.setToolTip(str(self.folder))
        self.count.setText('counting…')
        names = ', '.join(Path(f).name for f in self.inner)
        self.merge_note.setText(f'It includes {names}, already tracked: those files stay searchable as they are.' if self.inner else '')
        self.merge_note.setVisible(bool(self.inner))
        self.chooser.reset()
        folder = self.folder
        run_job(lambda s: count_files(folder), lambda n: self.counted(token, n))

    def counted(self, token, count):
        if token != self.token or count is None:
            return
        self.count.setText(files_text(count))
        self.chooser.set_count(count)

    def accept(self):
        folder, only_new = self.folder, self.chooser.only_new_files()
        self.win.close_sheet()
        if folder is not None:
            self.win.track_folder(folder, only_new=only_new)

    def cancel(self):
        self.win.close_sheet()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            focused = QApplication.focusWidget()
            (focused if isinstance(focused, QPushButton) and self.isAncestorOf(focused) else self.track_button).click()
            return
        if event.key() == Qt.Key_Escape:
            self.cancel()
            return
        super().keyPressEvent(event)


class JunoWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.settings = config.load()
        Motion.detect(self.settings)
        self.pal = palette_for(self.settings.get('theme'))
        self.index = Index(self.settings)
        self.status, self.folder_stats = self.index.status(), {}
        self.indexing, self.progress, self.mode, self.hiding = False, None, SEARCH, False
        self.active_hotkey = ''
        self.tray, self.reindex_pending = None, False
        self.layers, self.layers_loading = None, False  # {'names': [done, total], ...}: live while indexing, else from the database
        self.index_background, self.priority_low, self.paused = False, False, None
        self.battery_timer = QTimer(self, interval=60000, timeout=self.battery_tick)
        self.hotkey, self.open_action = None, None
        self.warming = False
        self.engine_signals = Signals()  # the runtime reports from worker threads ("Preparing the graphics card (once)…")
        self.engine_signals.progress.connect(self.engine_status)
        ai.runtime.status_hook = self.engine_signals.progress.emit
        self.downloads = ModelDownloads(self)  # model files and the engine (Settings rows, the first-run question)
        self.downloads.changed.connect(lambda _: self.engine_status(self.downloads.status_text()))
        self.downloads.finished.connect(self.download_finished)
        self.watcher = FolderWatcher(parent=self)
        self.watcher.changed.connect(self.auto_index)
        # Live glass (juno.glass): the engine exists on a real Windows display only; plain material everywhere else.
        live_platform = glass.WINDOWS and QGuiApplication.platformName() not in ('offscreen', 'minimal')
        self.glass = glass.Backdrop(on_change=self.glass_changed, on_theme=lambda theme: self.apply_theme(),
                                    margin=M, width=W, radius=R, full_shape=MAXH) if live_platform else None
        self.glass_live, self.glass_reason = False, 'not shown yet'
        self.edge, self.edge_fade = 1.0, EdgeFade(self)  # the resting shape's rim (0 while the height animates)
        self.setFixedSize(W + 2 * M, MAXH + 2 * M)
        if self.glass is not None:
            QTimer.singleShot(1500, self.warm_glass)

        self.height_spring = Spring(BAR, 300, 0.86, self._height, precision=0.1)
        self.appear = Spring(0, 420, 0.82, self._appear)
        # The glow only touches the panel's edge: repaint that ring, not the whole window and every widget in it.
        self.glow_strength = Spring(0, 140, 1.0, lambda v: self.update(self.glow_region()))
        self.glow = Loop(3.0, self)
        self.glow.tick.connect(lambda _: self.update(self.glow_region()))
        self.shimmer = Loop(1.4, self)
        self.shimmer.tick.connect(lambda _: self.update(int(M), int(M + BAR - 2), W, 4))

        self.search = QLineEdit(self)
        self.search.setObjectName('search')
        self.search.setGeometry(M + 56, M + (BAR - 40) // 2, W - 56 - ModeSwitch.SIZE * len(ModeSwitch.ITEMS) - 34, 40)
        self.search.textChanged.connect(self.text_changed)
        self.search.installEventFilter(self)
        self.modes = ModeSwitch(self)
        self.modes.move(M + W - self.modes.width() - 14, M + (BAR - self.modes.height()) // 2)
        self.modes.changed.connect(lambda i: self.set_mode(i))
        self.setup_title = label('', 'hero')  # in setup the bar shows the step's title instead of the search box
        self.setup_title.setParent(self)
        self.setup_title.setGeometry(M + 56, M + (BAR - 40) // 2, W - 56 - 34, 40)
        self.setup_title.hide()

        self.clip = QWidget(self)
        self.clip.setObjectName('clear')
        self.inner = QWidget(self.clip)
        self.inner.setFixedSize(W, MAXH - BAR)
        inner = QVBoxLayout(self.inner)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)
        self.views = QStackedWidget()
        self.search_view, self.ask_view = SearchView(self), AskView(self)
        self.settings_view = SettingsView(self)
        self.setup_view = SetupView(self)
        for view in (self.search_view, self.ask_view, self.settings_view, self.setup_view):
            self.views.addWidget(view)
        inner.addWidget(self.views, 1)
        self.sheet, self.sheet_open = AddFolderSheet(self), False  # adding a folder: shown in place of the views
        self.sheet.hide()
        inner.addWidget(self.sheet, 1)
        footer = QWidget()
        footer.setFixedHeight(FOOT)
        row = QHBoxLayout(footer)
        row.setContentsMargins(20, 0, 16, 4)
        self.status_label = label('', 'caption')
        self.status_label.setTextFormat(Qt.RichText)
        self.hints = label('', 'caption')
        self.toast_action = button('', None, 'link')
        self.toast_action.hide()
        row.addWidget(self.status_label, 1)
        row.addWidget(self.toast_action)
        row.addWidget(self.hints)
        inner.addWidget(footer)
        self.veil = Veil(self.clip, self._transition_done)
        self.veil.setGeometry(self.inner.geometry())
        self.veil_token = 0
        self.toast_timer = QTimer(self, singleShot=True, timeout=self.end_toast)
        self.toasting = False
        self._height(BAR)
        self.watch_folders()
        self.apply_theme()
        self.refresh_status()
        self.set_mode(SEARCH, animate=False)

    # ---------------------------------------------------------------- theme
    def current_theme(self):
        """'dark' or 'light'. A fixed Theme setting wins; Automatic follows what is behind the panel with live glass
        (the engine's appearance) and Windows otherwise."""
        setting = self.settings.get('theme')
        if setting in ('dark', 'light'):
            return setting
        if self.glass_live and self.glass is not None and self.glass.theme:
            return self.glass.theme
        return 'dark' if system_dark() else 'light'

    def apply_theme(self):
        self.pal = PALETTES[self.current_theme()]
        pal = self.pal
        sep = pal['separator']
        group = pal['fill']
        sheet = stylesheet(pal) + (f"QFrame#group {{ background: rgba({group.red()},{group.green()},{group.blue()},{group.alphaF():.3f}); border-radius: 11px; }}"
                                   f"QFrame#hairline {{ background: rgba({sep.red()},{sep.green()},{sep.blue()},{sep.alphaF():.3f}); }}")
        QApplication.instance().setStyleSheet(sheet)
        placeholder = self.search.palette()
        placeholder.setColor(placeholder.ColorRole.PlaceholderText, pal['tertiary'])
        placeholder.setColor(placeholder.ColorRole.Text, pal['text'])
        self.search.setPalette(placeholder)
        self.search_view.themed()
        if self.glass_live:
            self.glass.set_appearance(self.settings.get('theme'))
        self.update()

    # ------------------------------------------------------------ geometry
    def panel_rect(self):
        return QRectF(M, M, W, self.height_spring.value)

    def _height(self, value):
        self.clip.setGeometry(M, M + BAR, W, max(0, int(value - BAR)))
        previous, self.painted_height = getattr(self, 'painted_height', None), value
        resting = value == self.height_spring.target
        self.sync_glass_busy()
        if not resting and self.edge > 0:
            # The resting shape's rim and lens band go; a simple hairline anchored to the top-left follows the edge.
            self.edge = 0.0
            DRIVER.loops.discard(self.edge_fade)
            self.update()
            return
        if resting and self.edge < 1 and self.edge_fade not in DRIVER.loops:
            if Motion.reduced:
                self.edge = 1.0
                self.update(self.edge_region())
            else:
                DRIVER.add_loop(self.edge_fade)
        if previous is None or min(previous, value) < BAR + SEPARATOR_FADE:
            self.update()  # near the bar the separator line fades too
            return
        # Only the band around the moving bottom edge (its rounded corners, lens band and shadow) changes: repaint just
        # that, not the whole translucent window, which on Windows means re-sending every pixel of it to the compositor.
        top = M + min(previous, value) - R - 2 - glass.BAND
        bottom = M + max(previous, value) + PANEL_SHADOW['drop'] + PANEL_SHADOW['spread'] + 2
        self.update(0, int(top), self.width(), int(math.ceil(bottom - top)))

    def edge_region(self):
        """The ring the resting shape's rim and lens band occupy: what the edge fade repaints."""
        rect = QRectF(M, M, W, self.height_spring.target).toAlignedRect()
        inset = glass.BAND + 4
        return QRegion(rect.adjusted(-3, -3, 3, 3)).subtracted(QRegion(rect.adjusted(inset, inset, -inset, -inset)))

    def sync_glass_busy(self):
        """The still-background check waits while the panel itself moves (height, drop-in, edge fade)."""
        if self.glass_live:
            self.glass.set_animating(self.height_spring in DRIVER.springs or self.appear in DRIVER.springs
                                     or self.edge_fade in DRIVER.loops)

    def glow_region(self):
        rect = self.panel_rect().toAlignedRect()
        return QRegion(rect.adjusted(-3, -3, 3, 3)).subtracted(QRegion(rect.adjusted(18, 18, -18, -18)))

    # ---------------------------------------------------------- transitions
    def transition(self, change, direction=0):
        """Run ``change`` (it swaps what the content area shows) behind a glide between snapshots of before and after."""
        if Motion.reduced or not self.isVisible() or self.hiding:
            self.veil.stop()
            change()
            return
        # Snapshots are taken at the screen's pixel ratio (QWidget.grab renders at devicePixelRatio), never scaled.
        old = self.veil.frame() if self.veil.active else snapshot(self.inner)
        Motion.instant = True  # anything the change animates (fades, reveals) is photographed finished
        try:
            change()
        finally:
            Motion.instant = False
        self.inner.move(0, -self.inner.height() - 40)  # out of sight, still shown: layouts and scrolling settle
        self.veil.begin(old, direction, collapsing=bool(direction) and not self.wants_expanded())
        self.veil_token += 1
        token = self.veil_token
        # Photograph the new state on the next turn of the event loop, after the deferred layout and scroll updates.
        QTimer.singleShot(0, lambda: token == self.veil_token and self._capture())

    def _capture(self):
        for spring in [s for s in DRIVER.springs if getattr(s, 'fade', False)]:
            spring.snap(spring.target)  # a fade still running inside the content would be photographed half-done
        QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        self.veil.arrive(snapshot(self.inner))

    def _transition_done(self):
        self.inner.move(0, 0)

    def wants_expanded(self):
        return self.mode != SEARCH or self.sheet_open or self.search_view.wants_expanded()

    def update_height(self):
        target = MAXH if self.wants_expanded() else BAR
        if self.glass_live:
            self.glass.set_shape(target)  # the lens band is made for the shape the panel comes to rest in
        self.height_spring.to(target)

    DROP = 14  # the panel drops in from this far above

    def _appear(self, value):
        self.setWindowOpacity(max(0.0, min(1.0, value)))
        self.sync_glass_busy()
        if hasattr(self, 'base_pos'):
            self._place((1 - value) * self.DROP)
        if self.hiding and value < 0.03:
            self.hiding = False
            self.hide()

    def _place(self, lift):
        """Put the panel ``lift`` logical pixels above its resting place. On Windows the window is moved in whole device
        pixels, so at 125 % or 150 % scaling the drop glides one device pixel at a time instead of stepping by 1.25 or
        1.5 (Qt positions windows in whole logical pixels)."""
        native = getattr(self, 'native_base', None)
        if native is not None:
            dpr = self.devicePixelRatioF()
            import ctypes
            # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOREDRAW: just move the composited surface.
            if ctypes.windll.user32.SetWindowPos(int(self.winId()), 0, native[0], native[1] - round(lift * dpr), 0, 0, 0x1 | 0x4 | 0x10 | 0x8):
                return
        self.move(self.base_pos.x(), round(self.base_pos.y() - lift))

    def _native_base(self):
        """The resting place in device pixels (read from Windows once the window sits there), or None elsewhere."""
        if os.name != 'nt':
            return None
        try:
            import ctypes
            from ctypes import wintypes
            box = wintypes.RECT()
            if ctypes.windll.user32.GetWindowRect(int(self.winId()), ctypes.byref(box)):
                return box.left, box.top
        except Exception:
            pass
        return None

    def _start_appear(self):
        if getattr(self, 'appear_pending', False):
            self.appear_pending = False
            self.appear.to(1)

    def screen_changed(self, screen):
        """Animations follow the refresh rate of whichever display the panel is on, and text is rendered for its scale."""
        DRIVER.follow(screen, self)
        if tune_text(screen.devicePixelRatio()):
            self.apply_theme()  # re-polish: style-sheet fonts pick up the new rendering
        if self.glass_live and self.isVisible() and not self.hiding and abs(screen.devicePixelRatio() - self.glass.scale) > 0.01:
            self.open_glass(screen, visible=True)  # another screen: close and reopen the engine for it

    def show_panel(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.geometry()
        self.base_pos = QPoint(area.center().x() - self.width() // 2, area.top() + int(area.height() * 0.17) - M)
        self.open_glass(screen)
        self.hiding = False
        self.appear.snap(0 if not Motion.reduced else 1)
        self.native_base = None
        self.move(self.base_pos)
        handle = self.windowHandle()
        if handle is not None and not getattr(self, 'screen_hooked', False):
            handle.screenChanged.connect(self.screen_changed)
            self.screen_hooked = True
        if tune_text(screen.devicePixelRatio()):  # before the first frame on this display
            self.apply_theme()  # re-polish: style-sheet fonts pick up the new rendering
        self.show()
        DRIVER.follow(self.screen(), self)
        self.native_base = self._native_base()
        resting = glass.window_rect(int(self.winId())) if self.glass_live else None
        if resting is not None:
            # The capture rectangle is the window's resting rectangle as Windows has it (one more capture if it differs).
            self.glass.move(resting)
        self._place(0 if Motion.reduced else self.DROP)
        self.raise_()
        self.activateWindow()
        if os.name == 'nt':
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except Exception:
                pass
        # The drop-in starts once the first frame is on screen (see paintEvent): that first paint and hand-over to the
        # compositor is the slowest of all, and inside the animation it would swallow its opening frames.
        self.appear_pending = True
        self.focus_input()
        self.search.selectAll()
        self.refresh_status()
        self.warm_up()

    def hide_panel(self):
        if not self.isVisible():
            return
        self.hiding, self.appear_pending = True, False
        if self.glass is not None:
            self.glass.close(release=False)  # no hook, timer or job from now; the pictures stay for the fade-out
        if Motion.reduced:
            self.hide()
            self.hiding = False
        else:
            self.appear.to(0)

    def toggle_panel(self):
        if self.isVisible() and not self.hiding:
            self.hide_panel()
        else:
            self.show_panel()

    def hotkey_pressed(self):
        """The global shortcut: shows or hides the panel; while setup's shortcut step shows, it ticks the keycaps."""
        if self.mode == SETUP and self.setup_view.step == 2 and self.isVisible() and not self.hiding:
            self.setup_view.hotkey_pressed()
        else:
            self.toggle_panel()

    # ---------------------------------------------------------------- glass
    def open_glass(self, screen, visible=False):
        """Choose live glass or the plain material for this showing (section 3 of the design) and, for live glass, open
        the engine on ``screen``: it captures what is behind the resting rectangle and waits up to 80 ms for the first
        picture. ``visible``: reopen while shown (the rectangle is the window's own)."""
        reason = glass.plain_reason(QGuiApplication.platformName(), bool(self.settings.get('see_through', True)))
        if reason is None and self.glass is None:
            reason = 'platform'
        hwnd = int(self.winId()) if reason is None else None
        if reason is None and not glass.affinity_supported(hwnd):
            reason = 'capture exclusion refused'
        self.glass_live = False
        if reason is None:
            rect = glass.window_rect(hwnd) if visible else None
            if rect is None:
                rect = glass.native_rect(screen, self.base_pos.x(), self.base_pos.y(), self.width(), self.height())
            self.glass_live = True  # the engine's first picture may switch the appearance (on_theme reads this)
            self.glass.open(hwnd, rect, screen.devicePixelRatio(), int(self.height_spring.target), self.settings.get('theme'),
                            'dark' if system_dark() else 'light', hidden=not self.isVisible(), others=self.other_windows)
            if self.glass.failed:
                reason, self.glass_live = 'first capture failed', False
                self.glass.close()
        elif self.glass is not None:
            self.glass.close()
        self.glass_reason = reason
        if self.current_theme() != self.pal['name']:
            self.apply_theme()

    def other_windows(self):
        """Juno's other visible top-level windows (popover, menu, tooltip, combo list): excluded from captures too."""
        return [int(w.winId()) for w in QApplication.topLevelWidgets()
                if w is not self and w.isVisible() and w.testAttribute(Qt.WA_WState_Created)]

    def warm_glass(self):
        """Lens maps and rim layers for the primary screen, built on the worker ahead of the first showing."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if self.glass is not None and screen is not None:
            dpr = screen.devicePixelRatio()
            self.glass.warm(glass.device_size(self.width(), self.height(), dpr), dpr)

    def glass_changed(self):
        """The engine has new pictures (or gave up): repaint the panel."""
        if self.glass_live and self.glass.failed:
            self.glass_live, self.glass_reason = False, 'capture failed'
            self.glass.close()
            if self.current_theme() != self.pal['name']:
                self.apply_theme()
        self.update(self.panel_rect().toAlignedRect().adjusted(-2, -2, 2, 2))

    def glass_setting_changed(self):
        """Settings → See-through panel changed: switch at once."""
        if self.isVisible() and not self.hiding:
            self.open_glass(self.screen() or QGuiApplication.primaryScreen(), visible=True)
            self.update()

    def wallpaper_changed(self):
        if self.glass_live:
            self.glass.event('wallpaper')

    def paint_body(self, p, rect):
        shape = int(round(rect.height()))
        layer = None
        if self.edge > 0 and shape in (BAR, MAXH):
            dpr = self.devicePixelRatioF()
            layer = glass.edge_layer(glass.device_size(self.width(), self.height(), dpr), dpr, shape, self.pal['name'], M, W, R)
        if self.glass_live:
            try:
                paint_glass(p, rect, R, self.pal, self.glass.picture(self.edge), self.glass.body_alpha, layer, self.edge)
                return
            except Exception as error:  # never an empty panel: the plain material, and plain for the rest of this showing
                logging.getLogger('juno').warning('Glass painting failed, plain material for this showing: %r', error)
                self.glass_live, self.glass_reason = False, 'paint failed'
                self.glass.close()
        paint_glass(p, rect, R, self.pal, None, 1.0, layer, self.edge)

    # ---------------------------------------------------------------- paint
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        rect = self.panel_rect()
        paint_shadow(p, rect, R, self.pal['shadow'], **PANEL_SHADOW)
        self.paint_body(p, rect)
        if self.glass_live:
            area = event.rect()
            self.glass.mark_dirty(area.x(), area.y(), area.width(), area.height())
        icon_rect = snapped(QRectF(M + 22, M + (BAR - 22) / 2, 22, 22), self)
        p.drawPixmap(icon_rect, sparkle_pixmap(22) if self.mode in (ASK, SETUP) else icon_pixmap(
            {SEARCH: 'search', SETTINGS: 'gear'}.get(self.mode, 'search'), 22, self.pal['secondary']), QRectF())
        expansion = max(0.0, min(1.0, (self.height_spring.value - BAR) / SEPARATOR_FADE))
        if expansion > 0:
            sep = QColor(self.pal['separator'])
            sep.setAlphaF(sep.alphaF() * expansion)
            p.setPen(QPen(sep, 1))
            p.drawLine(QPointF(M + 16, M + BAR - 0.5), QPointF(M + W - 16, M + BAR - 0.5))
        if self.indexing:
            self.paint_progress(p)
        paint_glow(p, rect, R, self.glow.phase, self.glow_strength.value)
        if getattr(self, 'appear_pending', False):
            QTimer.singleShot(0, self._start_appear)  # after this frame has been handed to the compositor

    def paint_progress(self, p):
        y = M + BAR - 1.5
        track = QRectF(M + 18, y, W - 36, 2.5)
        p.setPen(Qt.NoPen)
        if self.progress is not None:
            fill = QRectF(track.left(), y, track.width() * self.progress, 2.5)
            gradient = QLinearGradient(fill.topLeft(), fill.topRight())
            gradient.setColorAt(0, self.pal['accent'].lighter(130))
            gradient.setColorAt(1, self.pal['accent'])
            p.setBrush(gradient)
            p.drawRoundedRect(fill, 1.2, 1.2)
        else:
            x = track.left() + self.shimmer.phase * (track.width() + 160) - 160
            gradient = QLinearGradient(x, 0, x + 160, 0)
            transparent = QColor(self.pal['accent'])
            transparent.setAlpha(0)
            gradient.setColorAt(0, transparent)
            gradient.setColorAt(0.5, self.pal['accent'])
            gradient.setColorAt(1, transparent)
            p.setBrush(gradient)
            p.drawRoundedRect(track, 1.2, 1.2)

    def thinking(self, on):
        if on:
            self.glow.start()
            self.glow_strength.to(1)
        else:
            self.glow_strength.to(0)
            QTimer.singleShot(900, lambda: self.glow_strength.value < 0.05 and self.glow.stop())

    # ---------------------------------------------------------------- modes
    def set_mode(self, mode, animate=True):
        previous = self.mode
        self.mode = mode
        if self.sheet_open:
            self.sheet_open = False
            self.sheet.hide()
            self.views.show()
        setup = mode == SETUP
        self.search.setVisible(not setup)
        self.modes.setVisible(not setup)
        self.setup_title.setVisible(setup)
        if setup:
            self.setup_title.setText(self.setup_view.title())
        else:
            self.modes.set(mode, animate and previous != SETUP)
        self.search.setPlaceholderText(PLACEHOLDERS[mode])
        self.search.setReadOnly(mode == SETTINGS)
        if mode == SETTINGS:
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)

        def change():
            leaving = self.views.currentWidget()
            if previous != mode and hasattr(leaving, 'deactivated'):
                leaving.deactivated()
            self.views.setCurrentIndex(mode)
            view = self.views.currentWidget()
            if hasattr(view, 'activated'):
                view.activated()
            if mode == SEARCH:
                if self.search_view.shown not in (None, self.search.text().strip()):
                    self.search_view.blank()  # results for another query: never glide them in
                self.search_view.set_query(self.search.text())
            self.update_hints()
        if animate and previous != mode:
            self.transition(change, 1 if mode > previous else -1)
        else:
            change()
        self.update_height()
        self.update()
        self.focus_input()

    def open_settings(self, page=None, section=None):
        """Settings, on ``page`` (one of SETTINGS_PAGES; the last page shown when None) and, in Advanced, ``section`` open."""
        if page:
            self.settings_view.show_page(page, section)
        self.set_mode(SETTINGS)

    def focus_input(self):
        """The search box, or in setup its primary button."""
        if self.mode == SETUP:
            self.setup_view.primary.setFocus()
        else:
            self.search.setFocus()

    def update_hints(self):
        hints = {SEARCH: '↑↓ Select   ↵ Open   Ctrl+↵ Show in folder   ⇥ Ask Juno   Esc Close',
                 ASK: '↵ Ask   Ctrl+N New chat   Ctrl+H History   Esc Close',
                 SETTINGS: '↑↓ Pages   ⇥ Into the page   Changes save automatically   Esc Back',
                 SETUP: '↵ Continue   Esc Close (setup resumes next time)'}
        if not self.toasting:
            self.hints.setText(hints[self.mode])

    def text_changed(self, text):
        if self.mode == SEARCH:
            self.search_view.set_query(text)

    # ------------------------------------------------------------- keyboard
    def eventFilter(self, obj, event):
        if obj is self.search and event.type() == QEvent.KeyPress:
            key, mods = event.key(), event.modifiers()
            ctrl = bool(mods & Qt.ControlModifier)
            if self.sheet_open and key in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
                self.sheet.keyPressEvent(event)
                return True
            if key == Qt.Key_Escape:
                if self.search.text() and self.mode in (SEARCH, ASK):
                    self.search.clear()
                elif self.mode != SEARCH:
                    self.set_mode(SEARCH)
                else:
                    self.hide_panel()
                return True
            if key == Qt.Key_Tab and self.mode == SETTINGS:
                self.settings_view.enter_page()  # Tab goes into the page that is showing
                return True
            if key == Qt.Key_Tab or key == Qt.Key_Backtab:
                self.set_mode(SEARCH if self.mode == ASK else ASK)
                return True
            if ctrl and Qt.Key_1 <= key <= Qt.Key_4:
                self.set_mode(min(key - Qt.Key_1, SETTINGS))
                return True
            if ctrl and key == Qt.Key_R:
                self.start_index()
                return True
            if ctrl and key == Qt.Key_N:
                self.set_mode(ASK)
                self.ask_view.new_chat()
                return True
            if ctrl and key == Qt.Key_H:
                if self.mode != ASK:
                    self.set_mode(ASK)
                self.ask_view.toggle_history()
                return True
            if key in (Qt.Key_Up, Qt.Key_Down):
                delta = -1 if key == Qt.Key_Up else 1
                if self.mode == SEARCH:
                    self.search_view.list.move(delta)
                elif self.mode == SETTINGS:
                    self.settings_view.pages_list.move(delta)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if self.mode == SEARCH:
                    self.search_view.reveal_current() if ctrl else self.search_view.activate()
                elif self.mode == ASK:
                    self.ask_view.send()
                return True
        return super().eventFilter(obj, event)

    def event(self, event):
        if event.type() == QEvent.WindowDeactivate and self.isVisible():
            QTimer.singleShot(160, self.maybe_hide)
        return super().event(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_priority()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self.glass is not None and not event.spontaneous():
            self.glass.close()  # hidden: no hook, no timer, no job, pictures freed (lens maps and rim layers stay cached)
            self.glass_live = False
        self.sync_priority()

    def sync_priority(self):
        """The one rule for process priority: below normal only while an automatic run goes on with the window hidden."""
        low = bool(self.indexing and self.index_background and not self.isVisible())
        if low != self.priority_low:
            self.priority_low = low
            power.background_priority(low)

    def battery_tick(self):
        """Once a minute while a run is paused on battery: when mains power returns, carry on in the background."""
        if power.on_battery():
            return
        self.battery_timer.stop()
        self.paused = None
        self.start_index(quiet=True, auto=True)

    def maybe_hide(self):
        if self.mode == SETUP:
            return  # setup stays while the user looks elsewhere (the folder dialog, the shortcut); Esc closes it
        if QApplication.activeWindow() is None and QApplication.activeModalWidget() is None and not self.hiding:
            self.hide_panel()

    def mousePressEvent(self, event):
        if not self.panel_rect().contains(event.position()) and self.mode != SETUP:
            self.hide_panel()
        else:
            super().mousePressEvent(event)

    # ---------------------------------------------------------------- state
    def problem_state(self):
        st = self.status
        if not st['folders']:
            suggestions = [(f'Track {name}', lambda _=False, p=path: self.offer_folder(p)) for name, path in suggested_folders()[:1]]
            return ('Track a folder to start searching',
                    'Juno searches the folders you track — like Downloads or Pictures — including the text inside documents, '
                    'images and videos, and keeps up with new files automatically.',
                    [('Track folder…', self.choose_folder)] + suggestions, 'folder')
        if not st['root_exists']:
            return ('Your tracked folders are missing', 'They may have been moved, renamed or be on a drive that isn’t connected.',
                    [('Open Settings', lambda: self.open_settings('Folders')), ('Track folder…', self.choose_folder)], 'folder')
        if not st['files']:
            if self.indexing:
                return ('Reading your files…', 'Results appear here as soon as the first files are indexed.', [], 'refresh')
            if st['initialized']:
                return ('Nothing searchable yet', 'Juno reads documents, PDFs, slides, spreadsheets, text files, images and videos. '
                        'Add some to a tracked folder, or track another folder.', [('Track folder…', self.choose_folder)], 'folder')
            return ('Your folders haven’t been indexed yet', 'Index them now to start searching.', [('Index now', self.start_index)], 'refresh')
        return None

    def warm_up(self, on_ready=None):
        """Wake the search server (starting it if needed) when the panel opens, so its 1.3-1.6 s wake overlaps with
        typing. Keyword results never wait for it."""
        if on_ready is not None:
            self.ready_callbacks = getattr(self, 'ready_callbacks', []) + [on_ready]
        if self.warming or not self.settings.get('embed_enabled'):
            return
        settings = self.settings
        if ai.runtime.missing(settings):  # the engine or the search model is not on this PC: keyword search only
            return
        self.warming = True

        def done(report):
            self.warming = False
            logging.getLogger('juno').info('Search model awake (started=%s)', report['started'])
            callbacks, self.ready_callbacks = getattr(self, 'ready_callbacks', []), []
            for callback in callbacks:
                callback()

        def failed(error):
            self.warming = False
            self.ready_callbacks = []
            logging.getLogger('juno').warning('The search model did not start: %s', error)
        run_job(lambda s: ai.warm_up(settings), done, failed)

    def engine_status(self, text):
        """runtime's status line (for example "Preparing the graphics card (once)…"); None when it is over."""
        if text:
            if not self.toasting:
                self.status_label.setText(f"<span style='color:{self.pal['accent'].name()}'>●</span>&nbsp; {html.escape(text)}")
            self.shimmer.start()
        else:
            if not self.indexing:
                self.shimmer.stop()
            if not self.toasting:
                self.status_label.setText(self.status_text())

    def download_finished(self, key, error):
        """A model file or the engine arrived (or did not). When search by meaning can run now, it turns on without a
        restart: the search model wakes, and an indexing run embeds what is missing."""
        name = DOWNLOAD_ROWS[key][1]
        if error is not None:
            cancelled = isinstance(error, models.DownloadCancelled)
            self.toast(f'Download of {name} stopped; it continues next time' if cancelled else str(error),
                       'orange' if cancelled else 'red', seconds=8)
            return
        if key in models.FIRST_RUN and self.downloads.running(models.ENGINE if key != models.ENGINE else 'embed-text'):
            return  # the other half of search by meaning is still coming
        if key in models.FIRST_RUN and not ai.runtime.missing(self.settings):
            self.toast('Search by meaning is on · adding it to your files', 'green', seconds=6)
            self.warm_up()
            self.start_index(quiet=True, background=True)
        else:
            self.toast(f'{name[0].upper()}{name[1:]} is downloaded', 'green', seconds=4)

    def rebind_hotkey(self, text):
        """Switch the global shortcut live; GlobalHotkey restores the old one if the new one is taken."""
        if self.hotkey is None:
            raise OSError('Global shortcuts are unavailable on this system')
        self.hotkey.register(text)
        self.active_hotkey = text
        self.settings['hotkey'] = text
        self.settings.save()
        if self.tray is not None:
            self.tray.setToolTip(f'Juno — {text}')
        if self.open_action is not None:
            self.open_action.setText(f'Open Juno\t{text}')

    def watch_folders(self):
        """(Re)start watching the tracked folders when auto-indexing is on."""
        if self.settings.get('auto_index', True) and self.settings.folders:
            self.watcher.set_roots(self.settings.folders)
        else:
            self.watcher.stop()

    def auto_index(self):
        if not self.settings.get('auto_index', True):
            return
        if self.indexing:
            self.reindex_pending = True  # changes during a run are picked up right after it
        else:
            self.start_index(quiet=True, auto=True)

    def notify(self, text, color='green'):
        """Footer toast when the panel is open, a tray notification when it is hidden."""
        if self.isVisible() and not self.hiding:
            self.toast(text, color, seconds=6)
        elif self.tray is not None:
            self.tray.showMessage('Juno', text, app_icon(), 5000)

    def load_layers(self):
        """Read the layer progress from the database off the UI thread (never while a run reports it live)."""
        if self.indexing or self.layers_loading:
            return
        self.layers_loading = True
        index = self.index

        def done(layers):
            self.layers_loading = False
            if not self.indexing:
                self.set_layers(layers)

        run_job(lambda s: index.layer_status(), done, lambda error: setattr(self, 'layers_loading', False))

    def set_layers(self, layers):
        self.layers = layers
        self.search_view.update_notice()

    def refresh_status(self):
        self.load_layers()
        self.status = self.index.status()
        self.folder_stats = self.index.folder_stats()
        if not self.toasting:
            self.status_label.setText(self.status_text())
        if self.mode == SEARCH:
            self.search_view.show_problem_or_collapse()
        if self.mode == SETTINGS:
            self.settings_view.tracked.refresh()
            self.settings_view.show_index_info()

    def status_text(self):
        st, pal = self.status, self.pal
        dot = lambda color: f"<span style='color:{pal[color].name()}'>●</span>&nbsp; "
        if self.indexing:
            return dot('accent') + 'Indexing…'
        if self.paused == 'battery' and st['files']:
            return dot('orange') + BATTERY_TEXT
        if not st['folders']:
            return dot('orange') + 'No folders tracked yet'
        if not st['root_exists']:
            return dot('red') + 'Tracked folders missing'
        if not st['files']:
            return dot('orange') + 'Nothing indexed yet'
        meaning = ' · search by meaning on' if st['vectors'] else ' · keyword search'
        live = ' · watching for changes' if self.watcher.active else ''
        folders = f"{st['folders']} folder{'s' * (st['folders'] != 1)}"
        return dot('green') + f"{st['files']:,} files in {folders}{meaning}{live}"

    def toast(self, text, color='green', action=None, seconds=4):
        self.toasting = True
        self.status_label.setText(f"<span style='color:{self.pal[color].name()}'>●</span>&nbsp; {html.escape(text)}")
        self.hints.setText('')
        if getattr(self, '_toast_slot', None) is not None:
            self.toast_action.clicked.disconnect(self._toast_slot)
            self._toast_slot = None
        if action:
            self.toast_action.setText(action[0])
            self._toast_slot = action[1]
            self.toast_action.clicked.connect(self._toast_slot)
            self.toast_action.show()
        else:
            self.toast_action.hide()
        fade_in(self.status_label)
        self.toast_timer.start(seconds * 1000)

    def end_toast(self):
        self.toasting = False
        self.toast_action.hide()
        self.status_label.setText(self.status_text())
        self.update_hints()
        fade_in(self.status_label)

    def start_index(self, force=False, quiet=False, auto=False, roots=None, background=None):
        """``background``: an automatic run (smaller thread budget, names and text only on battery, low priority while
        hidden); by default the same as ``auto``. Index now / Rescan / adding a folder run in the foreground."""
        if self.indexing:
            self.reindex_pending = True  # picked up right after the current run
            return
        if not self.settings.folders:
            if not quiet:
                self.toast('Track a folder first', 'orange', ('Track folder…', self.choose_folder))
            return
        background = auto if background is None else bool(background)
        self.battery_timer.stop()
        self.paused = None
        self.indexing, self.progress, self.live_refresh = True, None, time.monotonic()
        self.index_background = background
        self.sync_priority()
        self.shimmer.start()
        self.status_label.setText(self.status_text())
        index, settings = self.index, self.settings
        tracked = self.settings_view.tracked

        def progress(state):
            total, done = state.get('total', 0), state.get('done', 0)
            self.progress = done / total if total else None
            phase = phase_label(state.get('phase'))
            if state.get('layers'):
                before = layers_notice(self.layers)
                self.layers = state['layers']
                if layers_notice(self.layers) != before:
                    self.search_view.update_notice()
            if not self.toasting:
                where = f" · {html.escape(Path(state['current']).name)}" if state.get('current') else ''
                count = f' {done}/{total}' if total else ''
                layers = layers_text(self.layers, unfinished_only=True)  # the line is narrow: unfinished layers, or the file name
                self.status_label.setText(f"<span style='color:{self.pal['accent'].name()}'>●</span>&nbsp; {phase}{count}"
                                          + (f' · {layers}' if layers else where))
            now = time.monotonic()
            if now - self.live_refresh > 2:  # files become searchable as they are read: keep counts live
                self.live_refresh = now
                self.status, self.folder_stats = index.status(), index.folder_stats()
                if self.mode == SEARCH and not self.search.text().strip():
                    self.search_view.show_problem_or_collapse()
            if self.mode == SETTINGS:
                tracked.refresh(state)
            self.update()

        def done(summary):
            self.indexing = False
            self.sync_priority()
            self.paused = summary.get('paused')
            if self.paused == 'battery':
                self.battery_timer.start()
            self.shimmer.stop()
            logging.getLogger('juno').info('Indexed: %s', {k: summary.get(k) for k in ('found', 'indexed', 'removed', 'errors', 'unsupported',
                                                                                        'embedded', 'seconds', 'warnings')})
            self.refresh_status()
            changed = summary['indexed'] + summary['removed'] + summary['errors']
            text = (f"Indexed {summary['indexed']} · {summary['unchanged']} unchanged · {summary['removed']} removed"
                    + (f" · {summary['errors']} couldn’t be read" if summary['errors'] else ''))
            if summary['warnings']:
                text += ' · ' + summary['warnings'][0]
            color = 'orange' if summary['warnings'] or summary['errors'] else 'green'
            if auto and changed:
                parts = [f"{summary['indexed']} new or changed file{'s' * (summary['indexed'] != 1)} indexed"] if summary['indexed'] else []
                parts += [f"{summary['removed']} removed"] if summary['removed'] else []
                parts += [f"{summary['errors']} could not be read"] if summary['errors'] else []
                self.notify('Folders updated · ' + ' · '.join(parts), color)
            elif not quiet or changed:
                self.toast(text, color, seconds=6)
            if self.mode == SEARCH and self.search.text().strip():
                self.search_view.run()
            self.update()
            self.after_index()

        def failed(error):
            self.indexing = False
            self.sync_priority()
            self.shimmer.stop()
            self.refresh_status()
            if not isinstance(error, IndexBusy):
                (self.notify if auto else lambda text, color: self.toast(text, color, seconds=8))(f'Indexing failed: {error}', 'red')
            self.after_index()

        def work(signals):
            embedder = ai.Embedder.from_settings(settings)
            if embedder is not None and not embedder.available():  # the llama.cpp engine or the search model is missing
                logging.getLogger('juno').warning('Indexing without search by meaning: %s', ai.runtime.missing(settings))
                embedder = None  # keyword search still works; meaning catches up on the next run
            return index.update(signals.progress.emit, embedder, force=force, roots=roots, background=background)
        run_job(work, done, failed, progress=progress)

    def after_index(self):
        if self.reindex_pending:
            self.reindex_pending = False
            QTimer.singleShot(400, lambda: self.start_index(quiet=True, auto=True))

    def choose_folder(self):
        start = next((str(path) for _, path in suggested_folders()), str(Path.home()))
        folder = QFileDialog.getExistingDirectory(self, 'Choose a folder for Juno to track', start)
        if folder:
            self.offer_folder(folder)

    def folder_problem(self, folder):
        """Why ``folder`` can't be tracked (a message), or None."""
        folder = Path(os.path.realpath(folder))
        reason = paths.forbidden_reason(folder, self.settings.home)
        if reason:
            return reason
        parent = next((t for t in self.settings.folders if paths.is_within(t, folder)), None)
        if parent is not None:
            return f'{folder.name} is already tracked' + (f' as part of {parent.name}' if paths.canonical(parent) != paths.canonical(folder) else '')
        saved_chats = self.settings.chats_folder
        if paths.is_within(saved_chats, folder) or paths.is_within(folder, saved_chats):
            return 'That folder holds your saved chats, so Juno won’t search it'
        return None

    def offer_folder(self, folder):
        """Adding a folder: the sheet with its count and the "Everything / Only new files" chooser (Enter tracks it)."""
        folder = Path(os.path.realpath(folder))
        reason = self.folder_problem(folder)
        if reason:
            self.toast(reason, 'orange', seconds=8)
            return False
        inner = [t for t in self.settings.folders if paths.is_within(folder, t)]
        self.sheet.open(folder, inner)
        if not self.sheet_open:
            self.sheet_open = True
            self.transition(lambda: (self.views.hide(), self.sheet.show()), 0)
        self.update_height()
        self.sheet.track_button.setFocus()
        return True

    def close_sheet(self):
        if not self.sheet_open:
            return
        self.sheet_open = False
        self.transition(lambda: (self.sheet.hide(), self.views.show()), 0)
        self.update_height()
        self.focus_input()

    def track_folder(self, folder, only_new=False, announce=True):
        """Track a folder: index it right away, then watch it for changes. ``only_new``: "only new files from now on"
        (the files already there are found by name only; see Index.track_only_new). Tracked folders inside it keep their
        indexed files (Index.adopt). ``announce`` False (setup tracks several): no toast and no indexing run; the
        caller starts one."""
        folder = Path(os.path.realpath(folder))
        reason = self.folder_problem(folder)
        if reason:
            self.toast(reason, 'orange', seconds=8)
            return False
        tracked = self.settings.folders
        inside = [t for t in tracked if paths.is_within(folder, t)]
        self.settings['tracked_folders'] = [str(t) for t in tracked if t not in inside] + [str(folder)]
        for merged_folder in inside:
            self.settings.clear_since(merged_folder)
        if only_new:
            self.index.track_only_new(folder)
        else:
            self.settings.clear_since(folder)
        self.settings.save()
        if inside:  # before any indexing run could drop the inner folders' files as untracked
            if self.indexing:
                index = self.index
                run_job(lambda s: index.adopt(folder, inside, include_older=not only_new),
                        lambda _: announce and self.start_index(quiet=True, roots=[folder]),
                        lambda error: self.toast(f'Couldn’t move the files of {inside[0].name}: {error}', 'red', seconds=8))
                announce_run = False
            else:
                self.index.adopt(folder, inside, include_older=not only_new)
                announce_run = announce
        else:
            announce_run = announce
        self.watch_folders()
        if self.mode == SETTINGS:
            if inside:
                self.settings_view.tracked.load()
            else:
                self.settings_view.tracked.added(folder)
        self.refresh_status()
        if announce:
            merged = f" (it includes {', '.join(t.name for t in inside)})" if inside else ''
            self.toast(f'Tracking {folder.name}{merged} · indexing now', 'green', seconds=5)
        if announce_run:
            self.start_index(quiet=True, roots=[folder])
        return True

    def include_older(self, folder):
        """"Include older files too" for a folder tracked as only new: they are read in full in the background."""
        index, name = self.index, Path(folder).name

        def done(count):
            self.refresh_status()
            self.toast(f'Reading {count:,} older file{"s" * (count != 1)} in {name}', 'green', seconds=5)
            self.start_index(quiet=True, roots=[folder])
        run_job(lambda s: index.include_older(folder), done,
                lambda error: self.toast(f'Couldn’t include the older files of {name}: {error}', 'red', seconds=8))

    # ---------------------------------------------------------------- setup
    def start_setup_if_needed(self):
        """At start: a first run (setup not done, no folder tracked) opens in setup. A Juno that already tracks
        folders is marked as set up, silently (and its start-at-sign-in is settled as it always was). True when setup shows."""
        if self.settings.get('setup_done'):
            return False
        if self.settings.folders:
            self.settings['setup_done'] = True
            self.settings.save()
            self.choose_start_login(True, keep_existing=True)
            return False
        self.run_setup()
        return True

    def run_setup(self):
        """Show setup from its first step (at the first run, or Settings → Run setup again)."""
        self.setup_view.prepare()
        self.set_mode(SETUP, animate=self.isVisible())

    def finish_setup(self):
        """Setup finished or skipped: never shown by itself again; Search with the cursor in the box."""
        self.setup_view.cancel.set()
        self.settings['setup_done'] = True
        self.settings.save()
        self.set_mode(SEARCH)
        self.refresh_status()

    def choose_start_login(self, on, keep_existing=False):
        """Start Juno at sign-in (on) or not, chosen in setup; ``keep_existing``: only when it was never settled.
        The Startup shortcut is written off the UI thread."""
        if keep_existing and self.settings.get('start_with_windows_configured'):
            return
        self.settings['start_with_windows_configured'] = True
        self.settings.save()

        def work(signals):
            startup.enable() if on else startup.disable()
        run_job(work, None, lambda error: logging.getLogger('juno').warning('Could not change start with Windows: %s', error))

    def untrack_folder(self, folder):
        """Stop tracking a folder; its files leave the index (nothing on disk is touched)."""
        self.settings.untrack(folder)  # and its folder_since entry
        self.settings.save()
        self.watch_folders()
        index = self.index
        run_job(lambda s: index.remove_root(folder), lambda _: self.refresh_status(),
                lambda error: self.toast(f'Couldn’t remove {Path(folder).name} from the index: {error}', 'red'))
        self.toast(f'Stopped tracking {Path(folder).name}', 'green', seconds=4)

# ------------------------------------------------------------------ app
INSTANCE = 'juno-desktop-' + os.environ.get('USERNAME', 'user')
NEWLINE = b'\n'  # ends each command and reply on the instance socket


def send_command(message, reply=True, timeout_ms=60000):
    """Send one command to the running Juno over its local socket. None when Juno isn't running;
    otherwise its JSON reply (or {} when no reply is wanted)."""
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE)
    if not socket.waitForConnected(300):
        return None
    socket.write(json.dumps(message).encode('utf-8') + NEWLINE)
    socket.flush()
    socket.waitForBytesWritten(1000)
    if not reply:
        return {}
    data = b''
    while not data.endswith(NEWLINE) and socket.waitForReadyRead(timeout_ms):
        data += bytes(socket.readAll())
    return json.loads(data or b'{}')


def serve_command(socket, window):
    """Handle one command from send_command: show, track <folder>, search <query>, ask <question>, status,
    rerank_download [model]."""
    if socket is None:
        return
    buffer = bytearray()

    def answer(value):
        try:
            socket.write(json.dumps(value, default=str).encode('utf-8') + NEWLINE)
            socket.flush()
            socket.disconnectFromServer()
        except RuntimeError:
            pass

    def received():
        buffer.extend(bytes(socket.readAll()))
        if not buffer.endswith(NEWLINE) and buffer.strip() != b'show':
            return
        try:
            message = json.loads(bytes(buffer)) if buffer.strip() != b'show' else {'cmd': 'show'}
        except ValueError:
            message = {'cmd': 'show'}
        cmd, arg = message.get('cmd'), message.get('arg')
        if cmd == 'track':
            answer({'ok': bool(window.track_folder(arg)), 'tracked': window.settings.get('tracked_folders')})
        elif cmd == 'search':
            settings, index = window.settings, window.index
            run_job(lambda s: index.search(arg, limit=int(message.get('limit') or 10), embedder=ai.Embedder.from_settings(settings)),
                    answer, lambda error: answer({'error': str(error)}))
        elif cmd == 'status':
            answer({'status': window.index.status(), 'folders': window.index.folder_stats(), 'indexing': window.indexing,
                    'hotkey': window.active_hotkey, 'settings_file': str(window.settings.path),
                    'reranker': rerank.state(window.settings)})
        elif cmd == 'ask':
            settings, index = window.settings, window.index
            run_job(lambda s: rag.ask(index, settings, arg, message.get('history') or [], only_path=message.get('path')),
                    answer, lambda error: answer({'error': str(error)}))
        elif cmd == 'rerank_download':
            settings = window.settings
            name = arg or settings.get('rerank_model') or rerank.DEFAULT
            gpu = bool(message.get('gpu'))
            run_job(lambda s: (rerank.download(name, gpu=gpu), rerank.state(settings))[1], answer, lambda error: answer({'error': str(error)}))
        else:
            window.show_panel()
            answer({})
    socket.readyRead.connect(received)
    if socket.bytesAvailable():
        received()


def move_from_alfred():
    """Juno was called Alfred until November 2026 (the data folder moves over in config.data_home, API keys in keys.get):
    swap Alfred's shortcuts and AI-tool entries for Juno's. Runs at every start, in the background; it changes nothing
    once nothing of Alfred is left."""
    try:
        startup.move_from_alfred()
        connect.move_from_alfred()
    except Exception:
        logging.getLogger('juno').exception('Moving from Alfred to Juno failed')


def run():
    if startup.sandboxed() and startup.relaunch_outside():
        return 0  # Windows Explorer starts the real Juno; this redirected copy just exits
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(startup.APP_ID)
        except Exception:
            pass
    handler = RotatingFileHandler(config.data_home() / 'assistant.log', maxBytes=512 * 1024, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger('juno').addHandler(handler)
    logging.getLogger('juno').setLevel(logging.INFO)
    logging.getLogger('juno').info('Juno %s starting', __version__)
    app = QApplication(sys.argv)
    tune_text(app.primaryScreen().devicePixelRatio())
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon())
    startup.register_app()
    app.setQuitOnLastWindowClosed(False)
    app.setStyle('Fusion')
    # Single instance: a second launch just shows the running Juno.
    if send_command({'cmd': 'show'}, reply=False) is not None:
        return 0
    QLocalServer.removeServer(INSTANCE)
    server = QLocalServer()
    server.listen(INSTANCE)
    threading.Thread(target=move_from_alfred, daemon=True).start()
    window = JunoWindow()
    window.start_setup_if_needed()  # a first run opens in setup; existing users are marked as set up
    # On quit, stop indexing at the next file and let background work finish before Python shuts down.
    app.aboutToQuit.connect(lambda: (window.index.cancel.set(), QThreadPool.globalInstance().waitForDone(5000)))
    server.newConnection.connect(lambda: serve_command(server.nextPendingConnection(), window))
    run_job(lambda s: rerank.for_settings(dict(window.settings)) and None)  # load the reranker now, not on the first question

    hotkey_error = ''
    try:
        from .hotkey import GlobalHotkey
        hotkey = GlobalHotkey()
        for combo in dict.fromkeys([window.settings.get('hotkey') or DEFAULT_HOTKEY] + FALLBACK_HOTKEYS):
            try:
                hotkey.register(combo)
                window.active_hotkey = combo
                break
            except (OSError, ValueError) as error:
                hotkey_error = str(error)
        if window.active_hotkey:
            window.hotkey = hotkey
            app.aboutToQuit.connect(hotkey.unregister)
    except Exception as error:
        hotkey = None
        hotkey_error = str(error)
    # The native event filter: WM_HOTKEY (when a shortcut is registered) and wallpaper changes for the live glass.
    native = HotkeyFilter(window.hotkey_pressed if window.active_hotkey else None, on_wallpaper=window.wallpaper_changed)
    app.installNativeEventFilter(native)
    app._hotkey = (hotkey, native)
    window.settings_view.show_hotkey()
    shortcut = window.active_hotkey or 'the tray icon'

    tray = QSystemTrayIcon(app_icon(), app)
    tray.setToolTip(f'Juno — {shortcut}')
    menu = QMenu()
    window.open_action = menu.addAction(f'Open Juno	{window.active_hotkey}', window.show_panel)
    menu.addAction('Rescan files', lambda: (window.show_panel(), window.start_index()))
    menu.addAction('Settings', lambda: (window.show_panel(), window.open_settings()))
    menu.addSeparator()
    menu.addAction('Quit Juno', app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: window.toggle_panel() if reason == QSystemTrayIcon.Trigger else None)
    tray.show()
    window.tray = tray
    at_sign_in = '--startup' in sys.argv
    if at_sign_in:
        # Started with Windows: stay hidden in the tray. Give Windows and the GPU driver a moment,
        # then run the sign-in tasks (catch up the index; no model starts until it is needed).
        logging.getLogger('juno').info('Started at sign-in (hidden in the tray)')
        QTimer.singleShot(startup.TASK_DELAY_MS, lambda: startup.run_tasks(window))
    else:
        window.show_panel()
        QTimer.singleShot(700, lambda: window.start_index(quiet=True, background=True))
    if window.active_hotkey:
        tray.showMessage('Juno is ready' if at_sign_in else 'Juno is running',
                         f'Press {window.active_hotkey} anywhere to open Juno. Esc hides it.', app_icon(), 4000)
        if window.active_hotkey != (window.settings.get('hotkey') or DEFAULT_HOTKEY):
            window.toast(f"{window.settings.get('hotkey')} is used by another app — using {window.active_hotkey}", 'orange', seconds=8)
    else:
        window.toast(f'Global shortcut unavailable ({hotkey_error}); use the tray icon.', 'orange', seconds=8)
    return app.exec()
