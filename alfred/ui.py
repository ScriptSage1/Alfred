"""Alfred desktop UI: a Spotlight-style floating glass panel.

Search · Ask Alfred · Knowledge · Settings live in one panel that springs open
from a single search bar. A global shortcut (default Ctrl+Alt+L) shows/hides
it; Esc hides it.
"""
import html
import json
import logging
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
from PySide6.QtGui import (QColor, QKeySequence, QCursor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QIcon, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFileIconProvider, QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QKeySequenceEdit, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget,
                               QSystemTrayIcon, QTextBrowser, QVBoxLayout, QWidget)

from . import APP_NAME, __version__, ai, chats, config, keys, paths, picker, providers, rag, startup
from .index import IMAGE_SUFFIXES, VIDEO_SUFFIXES, Index, IndexBusy, video_frame_png
from .watcher import FolderWatcher
from .ui_kit import (DRIVER, Loop, Motion, Spring, Toggle, blurred_backdrop, brand_pixmap, dot_pixmap, fallback_backdrop, icon, icon_pixmap,
                     logo_pixmap,
                     paint_glass, paint_glow, paint_shadow, palette_for, sparkle_pixmap, stylesheet, HotkeyFilter)

W, BAR, MAXH, M, R, FOOT = 780, 62, 600, 46, 26, 34
DEFAULT_HOTKEY, FALLBACK_HOTKEYS = 'Ctrl+Alt+L', ['Ctrl+Alt+L', 'Ctrl+Alt+K', 'Ctrl+Alt+A']
SEARCH, ASK, SETTINGS = range(3)
PLACEHOLDERS = {SEARCH: 'Search your files', ASK: 'Ask anything about your files', SETTINGS: 'Settings'}
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


def open_path(path):
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


def fade_in(widget, duration=0.22, dy=0):
    """Opacity spring for content changes (removed afterwards to keep text crisp)."""
    if Motion.reduced:
        return
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    effect.setOpacity(0)
    spring = Spring(0, 160 / duration, 1.0)

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
        if suffix in IMAGE_SUFFIXES and path.is_file():
            key = ('thumb', str(path), size)
            if key not in cls.cache:
                pixmap = QPixmap(str(path))
                if not pixmap.isNull():
                    pixmap = pixmap.scaled(size * 2, size * 2, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    pixmap.setDevicePixelRatio(2)
                cls.cache[key] = pixmap
            return cls.cache[key]
        key = ('type', suffix, size)
        if key not in cls.cache:
            from PySide6.QtCore import QFileInfo
            cls.provider = cls.provider or QFileIconProvider()
            cls.cache[key] = cls.provider.icon(QFileInfo(str(path))).pixmap(QSize(size, size), 2.0)
        return cls.cache[key]


# ------------------------------------------------------------------ widgets
class ModeSwitch(QWidget):
    """Glass icon buttons, one per mode; a liquid pill glides (and stretches) to the active one."""
    changed = Signal(int)
    ITEMS = [('search', 'Search  (Ctrl+1)'), ('sparkle', 'Ask Alfred  (Ctrl+2 · Tab)'), ('gear', 'Settings  (Ctrl+3)')]
    SIZE = 34

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.index, self.hover = 0, -1
        self.setFixedSize(self.SIZE * len(self.ITEMS) + 8, self.SIZE + 6)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.left = Spring(4, 380, 0.8, lambda v: self.update())
        self.right = Spring(4 + self.SIZE, 380, 0.8, lambda v: self.update())

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
            rect = QRectF(4 + i * self.SIZE + 9, 12, 16, 16)
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
        font = QFont('Segoe UI', 10)
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
        self.top = Spring(0, 500, 0.82, lambda v: self.update())
        self.bottom = Spring(0, 500, 0.82, lambda v: self.update())
        self.reveal = Spring(1, 90, 1.0, lambda v: self.update())
        self.scroll_spring = Spring(0, 300, 1.0, lambda v: self.scroll.verticalScrollBar().setValue(int(v)))

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
        title_font = QFont('Segoe UI', 10)
        title_font.setWeight(QFont.Medium)
        sub_font = QFont('Segoe UI', 8.5)
        head_font = QFont('Segoe UI', 8.5)
        head_font.setWeight(QFont.DemiBold)
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
            secondary = _mix(pal['secondary'], QColor(255, 255, 255, 200), cover)
            pixmap = value.get('pixmap')
            if pixmap is not None and not pixmap.isNull():
                target = QRectF(16, y + offset + (h - 30) / 2, 30, 30)
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

    def show_state(self, title, text, actions=(), art='search'):
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
class DescriptionCard(QFrame):
    """A picture's AI description under its preview: caption, description, tags and the model,
    with any text OCR found in a separate block. Shows a shimmer while the description is pending."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName('group')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 11)
        layout.setSpacing(5)
        self.pending = ShimmerLabel(win, 'Describing this picture…')
        self.caption = label('', 'name', wrap=True, selectable=True)
        self.text = label('', 'secondary', wrap=True, selectable=True)
        self.tags = QWidget()
        self.tag_rows = QVBoxLayout(self.tags)
        self.tag_rows.setContentsMargins(0, 3, 0, 1)
        self.tag_rows.setSpacing(5)
        self.source = label('', 'caption')
        self.ocr_title = label('TEXT IN IMAGE', 'section')
        self.ocr = label('', 'secondary', wrap=True, selectable=True)
        self.description_parts = (self.caption, self.text, self.tags, self.source)
        for widget in (self.pending,) + self.description_parts + (self.ocr_title, self.ocr):
            layout.addWidget(widget)

    def _tags(self, tags, width):
        while self.tag_rows.count():
            row = self.tag_rows.takeAt(0).layout()
            while row is not None and row.count():
                item = row.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        row, used = None, width
        for tag in tags:
            chip = label(tag, 'tag')
            needed = chip.sizeHint().width() + 5
            if row is None or used + needed > width:
                row, used = QHBoxLayout(), 0
                row.setSpacing(5)
                row.addStretch()
                self.tag_rows.addLayout(row)
            row.insertWidget(row.count() - 1, chip)
            used += needed

    def show_state(self, vision=None, ocr_text='', pending=False, width=300):
        """vision: {'caption', 'description', 'tags', 'model'} or None. Hides itself when there's nothing to show."""
        has_ocr = bool(ocr_text.strip())
        self.pending.setVisible(pending and not vision)
        self.pending.set('Describing this picture…', pending and not vision)
        for widget in self.description_parts:
            widget.setVisible(bool(vision))
        if vision:
            self.caption.setText(vision.get('caption') or '')
            self.text.setText(vision.get('description') or '')
            self.text.setVisible(bool(vision.get('description')))
            self._tags(vision.get('tags') or [], width - 28)
            self.tags.setVisible(bool(vision.get('tags')))
            self.source.setText(f"AI description · {vision.get('model') or 'local vision model'} · on this PC")
        self.ocr_title.setVisible(has_ocr)
        self.ocr.setVisible(has_ocr)
        self.ocr.setText(ocr_text.strip()[:900])
        self.setVisible(bool(vision) or has_ocr or pending)
        if vision or has_ocr:
            fade_in(self)


class SearchView(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.generation = 0
        self.query = ''
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
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
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.hide()
        pane.addWidget(self.image)
        self.card = DescriptionCard(win)
        self.card.hide()
        holder = QWidget()
        holder.setObjectName('clear')
        stack = QVBoxLayout(holder)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.addWidget(self.card)
        stack.addStretch()
        self.card_area = QScrollArea()  # a long description scrolls instead of being clipped
        self.card_area.setWidgetResizable(True)
        self.card_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.card_area.setWidget(holder)
        self.card_area.hide()
        pane.addWidget(self.card_area, 1)
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

    def wants_expanded(self):
        return len(self.query) >= 2 or self.win.problem_state() is not None

    def set_query(self, text):
        self.query = text.strip()
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
        self.win.update_height()

    def run(self):
        self.generation += 1
        generation, query = self.generation, self.query
        settings, index = self.win.settings, self.win.index
        def work(signals):
            embedder = ai.Embedder.from_settings(settings)
            down = bool(embedder and embedder.kind == 'ollama' and embedder.local and not ai.ollama_running(embedder.base, 0.6))
            results = index.search(query, limit=24, mode='hybrid', embedder=embedder)
            return results, (index.pending_changes() if not results else None), down
        run_job(work, lambda value: (self.show(value[0], generation, query, value[1]), value[2] and self.heal(generation)),
                lambda error: generation == self.generation and self.show_error(error))

    def show_error(self, error):
        self.empty.show_state('Search unavailable', str(error), [('Rescan', self.win.start_index)], 'warning')
        self.stack.setCurrentIndex(0)
        self.win.update_height()

    def heal(self, generation):
        """Ollama isn't running, so this search was keyword-only: start it and search again once it's ready."""
        if not self.win.settings.get('ollama_autostart', True):
            self.win.toast('Ollama isn’t running — searching by keyword only (Settings → Start Ollama automatically)', 'orange', seconds=8)
            return
        if not self.win.warming:  # one notice, even if several searches notice at once
            self.win.toast('Ollama wasn’t running — starting it; this search will refresh with matches by meaning', 'orange', seconds=8)
        self.win.warm_up(on_ready=lambda: generation == self.generation and self.query and self.run())

    def show(self, results, generation, query, pending=None):
        if generation != self.generation:
            return
        if not results and pending and any(pending.values()):
            count = sum(pending.values())
            self.empty.show_state('Your folder has changed', f'{count} file{"s" * (count != 1)} added, changed or removed since the last '
                                  'index, so results may be missing.', [('Index now', self.win.start_index)], 'refresh')
            self.stack.setCurrentIndex(0)
            self.win.update_height()
            return
        if not results:
            self.empty.show_state(f'No results for “{query}”', 'Nothing in your indexed files matches. Ask Alfred to reason over your documents instead.',
                                  [('Ask Alfred  ⇥', lambda: self.win.set_mode(ASK))], 'search')
            self.stack.setCurrentIndex(0)
            self.win.update_height()
            return
        items, seen = [], set()
        results = [r for r in results if not (r['path'] in seen or seen.add(r['path']))]

        def row(result):
            return ('row', dict(result, kind_row='file', title=result['filename'], pixmap=FileIcons.get(result['path']),
                                rounded=Path(result['path']).suffix.lower() in IMAGE_SUFFIXES,
                                subtitle=f"{result['location']} — {result['snippet']}", right=result.get('folder', '')))
        items.append(('header', 'Top Hit'))
        items.append(row(results[0]))
        grouped = {}
        for result in results[1:]:
            group = next((name for name, types in GROUPS if result['file_type'] in types), 'Documents')
            grouped.setdefault(group, []).append(result)
        for name in ['Documents', 'PDF Documents', 'Presentations', 'Spreadsheets', 'Images', 'Videos']:
            if grouped.get(name):
                items.append(('header', name))
                items.extend(row(result) for result in grouped[name])
        items.append(('header', 'Ask Alfred'))
        items.append(('row', dict(kind_row='ask', title=f'Ask Alfred: “{query}”', pixmap=sparkle_pixmap(30),
                                  subtitle='Get an answer grounded in your files, with citations', right='Tab')))
        self.list.set_items(items)
        self.list.scroll_spring.snap(0)
        self.stack.setCurrentIndex(1)
        self.win.update_height()

    def preview(self, item):
        if not item:
            return
        if item.get('kind_row') == 'ask':
            self.big_icon.setPixmap(sparkle_pixmap(52))
            self.set_title('Ask Alfred')
            self.card.hide()
            self.card_area.hide()
            self.excerpt.show()
            self.p_meta.setText(f"{ai.ChatProvider(self.win.settings).describe()['summary']}")
            self.image.hide()
            self.excerpt.setHtml(f"<p style='font-size:11pt'>Alfred searches your indexed files for “{html.escape(self.query)}”, "
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
        self.p_meta.setText(f"{item['file_type'].upper()} · {nice} · {item['location']} · modified {item['modified']}")
        if Path(item['path']).suffix.lower() in IMAGE_SUFFIXES and absolute.is_file():
            pixmap = QPixmap(str(absolute))
            if not pixmap.isNull():
                scaled = pixmap.scaled(330 * 2, 150 * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                scaled.setDevicePixelRatio(2)
                self.image.setPixmap(_rounded(scaled, 10))
                self.image.show()
        elif Path(item['path']).suffix.lower() in VIDEO_SUFFIXES and absolute.is_file():
            self.image.hide()

            def show_frame(data):
                if self.list.item() is not item or not data:
                    return
                pixmap = QPixmap()
                if pixmap.loadFromData(data, 'PNG'):
                    scaled = pixmap.scaled(330 * 2, 170 * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    scaled.setDevicePixelRatio(2)
                    self.image.setPixmap(_rounded(scaled, 10))
                    self.image.show()
                    fade_in(self.image)
            moment = item.get('seconds') or 0
            run_job(lambda s: video_frame_png(absolute, moment), show_frame)
        else:
            self.image.hide()
        self.excerpt.setHtml('')
        media = Path(item['path']).suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES
        picture = Path(item['path']).suffix.lower() in IMAGE_SUFFIXES
        self.card.setVisible(False)
        self.excerpt.setVisible(not picture)  # a picture's text goes in the card
        self.card_area.setVisible(media)
        self.card_area.verticalScrollBar().setValue(0)
        self.load_details(item, media, picture)
        fade_in(self.pane)

    def load_details(self, item, media, picture, attempt=0):
        query, index = self.query, self.win.index

        def show(ex):
            if self.list.item() is not item:
                return
            if not picture:
                self.excerpt.setHtml(_highlight(ex['excerpt'], query, self.win.pal) if ex['excerpt'] else '')
            if media:
                self.card.show_state(ex.get('vision'), ex.get('ocr_text', '') if picture else '', ex.get('description_pending'),
                                     self.pane.width() - 46)
                if ex.get('description_pending') and attempt < 60:  # check back until the description arrives
                    QTimer.singleShot(3000, lambda: self.list.item() is item and self.load_details(item, media, picture, attempt + 1))
        run_job(lambda s: index.read_excerpt(item['path'], segment_id=item.get('segment_id'), max_chars=2400), show,
                lambda error: self.list.item() is item and self.excerpt.setPlainText(f'Preview unavailable: {error}'))

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
                open_path(target)
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


class Turn(QWidget):
    """One exchange: your question in a bubble on the right, Alfred's answer on the left with its sources."""

    def __init__(self, view, question, scope=None):
        super().__init__()
        self.view, self.win, self.question = view, view.win, question
        self.text, self.result = '', None
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
        self.body.setTextFormat(Qt.MarkdownText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.body.setStyleSheet('font-size: 10.5pt;')
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
        self.status.set(f'Reading {len(items)} source{"s" * (len(items) != 1)}…' if items else 'No matching passages', True)

    def add_token(self, token):
        if not self.text:
            self.status.set('Writing…', True)
            self.body.show()
            fade_in(self.body)
            self.flush.start()
        self.text += token

    def render(self):
        self.body.setText(self.text)
        self.view.follow()

    def _show_sources(self, citations, used):
        for number, item in enumerate(citations):
            source = SourceRow(self, item, item['marker'] in used or not used)
            self.sources.addWidget(source)
            source.hide()
            QTimer.singleShot(0 if Motion.reduced else 45 * number, lambda s=source: (s.show(), fade_in(s)))

    def _add_actions(self):
        self.actions.addWidget(button('Copy', lambda: (QApplication.clipboard().setText(self.text), self.win.toast('Answer copied', 'green')),
                                      'link', 'Copy the answer'))
        self.actions.addStretch()

    def finish(self, result):
        self.flush.stop()
        self.result = result
        self.text = result['answer'] or self.text
        self.render()
        self.body.show()
        info = result.get('provider', {})
        model = info.get('name') or info.get('model') or ''
        where = f"via {info['group']}" if info.get('cloud') else 'on this PC'
        used = result.get('used') or []
        if result.get('grounded'):
            status = f"From {len(used)} source{'s' * (len(used) != 1)} in your files · {model} {where}"
        elif not result.get('citations'):
            status = f'Not found in your files · {model}'
        else:
            status = f'No sources cited — your files may not cover this · {model}'
        if result.get('cancelled'):
            status += ' · stopped'
        self.status.set(status, False)
        self._show_sources(result.get('citations') or [], used)
        self._add_actions()
        self.view.follow()

    def restore(self, message):
        """Show a saved answer (reopened from history) without animating it in."""
        if message.get('error'):
            self.fail(message['error'], setup=message.get('setup', False))
            return
        self.text = message.get('content') or ''
        self.body.setText(self.text)
        self.body.show()
        sources = message.get('sources') or []
        self.status.set(f"From {len(sources)} source{'s' * (len(sources) != 1)} · {message.get('model') or ''}" if sources
                        else (message.get('model') or ''), False)
        for item in sources:
            self.sources.addWidget(SourceRow(self, item, True))
        self._add_actions()

    def fail(self, error, setup=None):
        self.flush.stop()
        setup = isinstance(error, ai.NotConfigured) if setup is None else setup
        self.status.set('Not available yet' if setup else 'Something went wrong', False)
        self.body.setTextFormat(Qt.PlainText)
        self.body.setText(str(error))
        self.body.setStyleSheet(f"color: {self.win.pal['secondary' if setup else 'red'].name()}; font-size: 10pt;")
        self.body.show()


class SourceRow(QWidget):
    """A source chip under an answer; it springs open to show the passage, with Open / Show in folder."""

    def __init__(self, turn, item, used):
        super().__init__()
        self.turn, self.item, self.win = turn, item, turn.win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        text = f"{item.get('marker', '')}   {item['filename']}   ·   {item.get('location', '')}".strip()
        if not used:
            text += '   (retrieved, not cited)'
        self.chip = button(text, self.toggle, 'source', 'Show the passage')
        self.chip.setProperty('used', 'true' if used else 'false')
        self.chip.setIcon(QIcon(FileIcons.get(item['path'], 16)))
        layout.addWidget(self.chip)
        self.box = QFrame()
        box = QVBoxLayout(self.box)
        box.setContentsMargins(14, 8, 14, 10)
        box.addWidget(label((item.get('snippet_full') or item.get('snippet') or '')[:1600], 'secondary', wrap=True, selectable=True))
        row = QHBoxLayout()
        row.addWidget(button('Open', self.open_file, 'link'))
        row.addWidget(button('Show in folder', self.reveal_file, 'link'))
        row.addStretch()
        box.addLayout(row)
        self.box.setMaximumHeight(0)
        layout.addWidget(self.box)
        self.height_spring = Spring(0, 300, 0.85, self._height)

    def _height(self, value):
        try:
            self.box.setMaximumHeight(max(0, int(value)))
            self.turn.view.follow(soft=True)
        except RuntimeError:
            pass

    def toggle(self):
        opening = self.box.maximumHeight() < 5
        self.height_spring.snap(self.box.maximumHeight())
        self.height_spring.to(self.box.sizeHint().height() if opening else 0)

    def target(self):
        return self.win.index.resolve(self.item['path'])

    def open_file(self):
        try:
            open_path(self.target())
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
        self.width_spring = Spring(0, 300, 0.88, self._width)

    def _width(self, value):
        value = max(0, int(value))
        self.setFixedWidth(value)
        self.inner.move(value - self.WIDTH, 0)  # slides in from the left edge
        self.setVisible(value > 0)

    def resizeEvent(self, event):
        self.inner.setFixedHeight(self.height())

    @property
    def is_open(self):
        return self.width_spring.target > 0

    def set_open(self, on, animate=True):
        if on:
            self.refresh()
        (self.width_spring.to if animate else self.width_spring.snap)(self.WIDTH if on else 0)

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
        self.scroll_spring = Spring(0, 260, 1.0, lambda v: self.scroll.verticalScrollBar().setValue(int(v)))
        self.show_title()

    # ---------------------------------------------------------------- state
    def activated(self):
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

    @property
    def saving(self):
        return bool(self.win.settings.get('save_chat_history', True))

    def models_loaded(self, entries):
        self.models, self.models_known = entries, True
        self.refresh_intro()

    def chat_model(self):
        """(provider, its description) for the model Ask Alfred will use."""
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
            local = next((e for e in self.models if e['kind'] == 'ollama'), None)
            self.intro_title.setText('Choose a model to chat with your files')
            lead = ('Use a model on this PC through Ollama, or Claude or ChatGPT with your subscription or an API key. '
                    'Alfred answers from the folders you track.')
            missing = self.models_known and local is not None and not local['models']
            self.intro_text.setText(f'{lead} (No local chat models yet — e.g. ollama pull gemma3:4b)' if missing else lead)
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
            current = (settings.get('provider') or 'ollama', settings.get('model') or '')
            items = picker.model_items(entries, current, efforts, settings.get('effort'))
            self.popover = popover = picker.Popover(self.win, items)
            popover.chosen.connect(self.picked)
            popover.closed.connect(lambda: (anchor.set_open(False), setattr(self, 'popover', None)))
            anchor.set_open(True)
            popover.open_at(anchor)
        run_job(lambda s: (picker.catalog_with_details(settings), providers.for_settings(settings).effort_options()), opened)

    def picked(self, item):
        if item.get('action') == 'settings':
            self.win.set_mode(SETTINGS)
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
            answer = QMessageBox.question(self, f'Use {group}?',
                                          f'Answers will come from {group}, so your questions — and, once Alfred answers from your documents, '
                                          'the few passages it finds for each question — are sent to that service. '
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
            self.win.toast('Chat history is off', 'orange', ('Turn on in Settings', lambda: self.win.set_mode(SETTINGS)), 6)
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
        self.clear_scope()
        self.clear_thread()
        self.chat = None
        self.panel.select(None)
        self.show_title()
        self.stack.setCurrentIndex(0)
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
        fade_in(self.scroll)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))
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
            if settings.get('ollama_autostart', True):
                try:
                    ai.ensure_ollama(settings.get('base_url'))
                except ai.ProviderError as error:
                    logging.getLogger('alfred').warning('Ollama unavailable before Ask: %s', error)
            return rag.ask(index, settings, question, history, on_token=signals.token.emit, cancel=cancel,
                           on_evidence=signals.progress.emit, only_path=scope,
                           on_status=lambda text: signals.progress.emit(('status', text)))

        def done(result):
            turn.finish(result)
            self.history += [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': result['answer']}]
            sources = [{k: item.get(k) for k in ('marker', 'path', 'filename', 'location', 'segment_id', 'snippet_full')}
                       for item in result.get('citations') or []]
            self.record(question, scope, {'content': result['answer'], 'sources': sources, 'error': None})
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
        self.rows.setContentsMargins(14, 6, 14, 6)
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

    def row(self, title, widget=None, caption=None):
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
        texts = QVBoxLayout()
        texts.setSpacing(0)
        texts.addWidget(label(title))
        if caption:
            texts.addWidget(label(caption, 'caption', wrap=True))
        row.addLayout(texts, 1)
        if widget is not None:
            if isinstance(widget, QWidget):
                row.addWidget(widget, 0, Qt.AlignRight | Qt.AlignVCenter)
            else:
                row.addLayout(widget)
        self.rows.addWidget(holder)
        return holder


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
        place = label(paths.shorten(self.folder, 58), 'caption')
        place.setToolTip(str(self.folder))
        texts.addWidget(place)
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
    """Settings → Tracked folders: track a folder (indexed at once, then watched), quick-add chips
    for common folders, and a disclosure that springs open to list what is tracked."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows, self.open = [], False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(5)
        outer.addWidget(label('TRACKED FOLDERS', 'section'))
        card = QFrame()
        card.setObjectName('group')
        self.card = QVBoxLayout(card)
        self.card.setContentsMargins(14, 6, 14, 6)
        self.card.setSpacing(0)
        outer.addWidget(card)
        self.note = label('Alfred reads documents, PDFs, slides, spreadsheets, and the text in images and videos — all on this PC — '
                          'and keeps up as files are added, changed or deleted.', 'caption', wrap=True)
        outer.addWidget(self.note)

        track = QWidget()
        row = QHBoxLayout(track)
        row.setContentsMargins(0, 7, 0, 7)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        texts.addWidget(label('Track a folder'))
        texts.addWidget(label('Indexed right away, then watched for new files', 'caption'))
        row.addLayout(texts, 1)
        row.addWidget(button('Track folder…', self.win.choose_folder, 'primary', 'Choose any folder to search',
                             icon('plus', 13, QColor('white'))))
        self.card.addWidget(track)

        self.chips_line, self.chips_holder = self._hairline(), QWidget()
        self.chips = QHBoxLayout(self.chips_holder)
        self.chips.setContentsMargins(0, 8, 0, 8)
        self.chips.setSpacing(7)
        self.card.addWidget(self.chips_line)
        self.card.addWidget(self.chips_holder)

        self.disclosure_line, self.disclosure = self._hairline(), QWidget()
        self.disclosure.setCursor(Qt.PointingHandCursor)
        self.disclosure.mousePressEvent = lambda event: self.set_open(not self.open)
        row = QHBoxLayout(self.disclosure)
        row.setContentsMargins(0, 9, 0, 9)
        self.count_label = label('')
        row.addWidget(self.count_label, 1)
        self.summary = label('', 'caption')
        self.summary.setTextFormat(Qt.RichText)
        row.addWidget(self.summary)
        self.chevron = Chevron(win)
        row.addWidget(self.chevron)
        self.card.addWidget(self.disclosure_line)
        self.card.addWidget(self.disclosure)

        self.body = QWidget()
        self.body.setMaximumHeight(0)
        self.list = QVBoxLayout(self.body)
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(0)
        self.footer = QWidget()
        foot = QHBoxLayout(self.footer)
        foot.setContentsMargins(40, 2, 0, 8)
        foot.setSpacing(2)
        foot.addWidget(button('Rescan now', lambda: self.win.start_index(), 'link', 'Check every tracked folder for changes'))
        foot.addWidget(label('·', 'caption'))
        foot.addWidget(button('Rebuild index', lambda: self.win.start_index(force=True), 'link', 'Read every file again from scratch'))
        foot.addStretch()
        self.list.addWidget(self.footer)
        self.card.addWidget(self.body)
        self.height = Spring(0, 300, 0.86, self._height)

        self.auto_line = self._hairline()
        self.card.addWidget(self.auto_line)
        auto = QWidget()
        row = QHBoxLayout(auto)
        row.setContentsMargins(0, 7, 0, 7)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        texts.addWidget(label('Index new files automatically'))
        texts.addWidget(label('Alfred watches tracked folders while it runs (also from the tray)', 'caption'))
        row.addLayout(texts, 1)
        self.auto_index = Toggle(lambda: self.win.pal)
        row.addWidget(self.auto_index, 0, Qt.AlignRight | Qt.AlignVCenter)
        self.card.addWidget(auto)

    def _hairline(self):
        line = QFrame()
        line.setFixedHeight(1)
        line.setObjectName('hairline')
        return line

    # ------------------------------------------------------------- content
    def load(self):
        """Rebuild rows and chips from settings (when Settings opens)."""
        for row in self.rows:
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        for number, folder in enumerate(self.win.settings.folders):
            self._add_row(folder, delay=None)
        self.fill_chips()
        self.refresh()
        self._snap_height()

    def _add_row(self, folder, delay=0):
        row = FolderRow(self, folder)
        self.list.insertWidget(len(self.rows), row)
        self.rows.append(row)
        if delay is not None:
            row.hide()
            QTimer.singleShot(0 if Motion.reduced else delay, lambda r=row: (r.show(), fade_in(r), self._retarget()))
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
            chip = button(name, lambda _=False, p=path: self.win.track_folder(p), 'chip', f'Track {paths.shorten(path)}')
            chip.setIcon(QIcon(folder_icon(path, 16)))
            chip.setIconSize(QSize(16, 16))
            self.chips.addWidget(chip)
        self.chips.addStretch()
        self.chips_holder.setVisible(bool(free))
        self.chips_line.setVisible(bool(free))

    def refresh(self, state=None):
        """Update the count, the summary and each folder's live status."""
        folders = self.win.settings.folders
        stats = getattr(self.win, 'folder_stats', {}) or {}
        self.disclosure.setVisible(bool(folders))
        self.disclosure_line.setVisible(bool(folders))
        self.count_label.setText(f'{len(folders)} folder{"s" * (len(folders) != 1)} tracked')
        total = sum(s.get('files', 0) for s in stats.values())
        busy_root = state.get('root') if state else None
        for row in self.rows:
            key = str(Path(os.path.realpath(row.folder)))
            info = stats.get(key, {'files': 0, 'skipped': 0})
            if not row.folder.is_dir():
                row.set_status('Folder missing', 'red')
            elif self.win.indexing and busy_root and paths.canonical(busy_root) == paths.canonical(row.folder):
                phase = state.get('phase')
                if phase == 'scanning':
                    row.set_status('Looking for files…', 'accent')
                elif phase == 'describing':
                    row.set_status(f"Describing pictures {min(state.get('done', 0) + 1, state.get('total', 0))} of {state.get('total', 0)}", 'accent')
                elif phase == 'reading video':
                    row.set_status(f"Reading video {state.get('frame', 0)}/{state.get('frames', 0)}", 'accent')
                else:
                    row.set_status(f"Indexing {min(state.get('done', 0) + 1, state.get('total', 0))} of {state.get('total', 0)}", 'accent')
            elif self.win.indexing and key not in stats:
                row.set_status('Waiting…')
            else:
                text = f"{info['files']:,} file{'s' * (info['files'] != 1)}"
                row.set_status(text + ' · up to date' if self.win.status.get('initialized') else text, 'green' if info['files'] else 'secondary')
        if self.win.indexing:
            phase = (state or {}).get('phase')
            text = f"Learning meaning {state.get('done', 0)}/{state.get('total', 0)}" if phase == 'embedding' else 'Indexing…'
            self.summary.setText(f"<span style='color:{self.win.pal['accent'].name()}'>●</span>&nbsp; {text}")
        else:
            self.summary.setText(f'{total:,} searchable file{"s" * (total != 1)}' if folders else '')

    # ------------------------------------------------------------- motion
    def set_open(self, on):
        self.open = on
        self.chevron.set_open(on)
        self.height.snap(self.body.maximumHeight())
        self.height.to(self.body.sizeHint().height() if on else 0)
        if on:
            for number, row in enumerate(self.rows):
                row.hide()
                QTimer.singleShot(0 if Motion.reduced else 40 + 45 * number, lambda r=row: (r.show(), fade_in(r), self._retarget()))

    def _retarget(self):
        if self.open:
            self.height.to(self.body.sizeHint().height())

    def _snap_height(self):
        self.chevron.angle.snap(90 if self.open else 0)
        self.height.snap(self.body.sizeHint().height() if self.open else 0)

    def _height(self, value):
        try:
            self.body.setMaximumHeight(max(0, int(value)))
        except RuntimeError:
            pass

    # ------------------------------------------------------------- actions
    def added(self, folder):
        """A folder was just tracked: open the list and slide its row in."""
        self.fill_chips()
        if not self.open:
            self.open = True
            self.chevron.set_open(True)
        self._add_row(folder, delay=60)
        self.refresh()
        self.height.snap(self.body.maximumHeight())
        self._retarget()

    def untrack(self, row):
        def gone():
            if row in self.rows:
                self.rows.remove(row)
            row.setParent(None)
            row.deleteLater()
            self.fill_chips()
            self.refresh()
            self._retarget()
            if not self.rows:
                self.set_open(False)
        self.win.untrack_folder(row.folder)
        row.fade_out(gone)


class SettingsView(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName('clear')
        body = QVBoxLayout(holder)
        body.setContentsMargins(26, 14, 26, 18)
        body.setSpacing(16)
        scroll.setWidget(holder)
        layout.addWidget(scroll)
        pal = lambda: self.win.pal

        self.tracked = TrackedFolders(win)
        self.auto_index = self.tracked.auto_index
        body.addWidget(self.tracked)
        self.index_info = label('', 'caption', wrap=True, selectable=True)
        body.addWidget(self.index_info)

        g = Group(win, 'AI MODEL', 'Indexing, OCR, picture descriptions and search always run on this PC. Only the chat model can be in the cloud.')
        self.model = picker.ModelButton(win, compact=False)
        self.model.setMinimumWidth(300)
        self.model.clicked.connect(lambda: self.win.ask_view.show_model_menu(self.model))
        models = QHBoxLayout()
        models.addWidget(self.model)
        models.addWidget(button('', self.refresh_models, None, 'Check accounts and refresh the model list', icon('refresh', 14, QColor(140, 140, 150))))
        g.row('Chat model', models, 'Also in the model button at the top of Ask Alfred')
        self.effort = QComboBox()
        self.effort.setMinimumWidth(300)
        self.effort_note = label('', 'caption', wrap=True)
        self.effort_row = g.row('Thinking', self.effort)
        self.location = label('', 'caption', wrap=True)
        g.outer.addWidget(self.location)
        g.outer.addWidget(self.effort_note)
        body.addWidget(g)

        g = Group(win, 'ACCOUNTS', 'Use Claude or ChatGPT with your subscription (through their official command-line tools, which you sign in to '
                                   'yourself) or with an API key. Keys are kept in Windows Credential Manager, never in Alfred’s files.')
        self.account_status, self.account_dot, self.account_button = {}, {}, {}
        rows = [('claude_code', 'Claude', 'Your Pro or Max plan', 'Sign in…'), ('codex', 'ChatGPT', 'Your Plus or Pro plan', 'Sign in…'),
                ('anthropic', 'Claude API', 'Pay as you go with an API key', 'Add key…'),
                ('openai', 'OpenAI API', 'Pay as you go with an API key', 'Add key…')]
        for kind, title, plan, action in rows:
            g.add(self.account_row(kind, title, plan, action))
        g.add(self.account_row('compatible', 'Custom server', 'Any OpenAI-compatible API: OpenRouter, Groq, LM Studio, llama.cpp…', 'Key…'))
        self.compatible_url = QLineEdit()
        self.compatible_url.setPlaceholderText('Server address, e.g. https://openrouter.ai/api/v1 or http://localhost:1234/v1')
        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(36, 0, 0, 9)
        line.addWidget(self.compatible_url)
        g.rows.addWidget(holder)
        body.addWidget(g)

        g = Group(win, 'OLLAMA', 'Runs local models: chat models you pick, plus the search and picture models.')
        self.device = QComboBox()
        self.device.addItems(['Auto (GPU when available)', 'CPU only'])
        self.device.setMinimumWidth(260)
        g.row('Device', self.device)
        self.base_url = QLineEdit()
        self.base_url.setMinimumWidth(260)
        g.row('Ollama server', self.base_url)
        self.autostart = Toggle(pal)
        g.row('Start Ollama automatically', self.autostart, 'When Alfred starts, start Ollama in the background if it isn’t running')
        self.keep_minutes = QSpinBox()
        self.keep_minutes.setRange(5, 240)
        self.keep_minutes.setSuffix(' min')
        g.row('Keep models loaded', self.keep_minutes, 'After the last use; then Ollama unloads them and frees GPU memory')
        body.addWidget(g)

        g = Group(win, 'CHATS', 'Chats are saved as files on this PC, so you can reopen them from the history panel (Ctrl+H) in Ask Alfred. '
                               'The chats folder is never added to search.')
        self.save_chats = Toggle(pal)
        g.row('Save chat history', self.save_chats, 'When off, new chats aren’t saved; chats already saved stay until you delete them')
        place = QHBoxLayout()
        self.chats_place = label('', 'caption')
        place.addWidget(self.chats_place)
        place.addWidget(button('Open folder', self.open_chats_folder))
        place.addWidget(button('Change…', self.change_chats_folder))
        g.row('Saved in', place)
        self.delete_chats = button('Delete all chats…', self.delete_all_chats, None, 'Permanently deletes every saved chat (asks first)')
        g.row('Saved chats', self.delete_chats)
        body.addWidget(g)

        g = Group(win, 'SEARCH BY MEANING', 'EmbeddingGemma 2 runs in Ollama on this PC and finds passages by meaning, not just matching words. '
                                         'Keyword search always works, even when Ollama is off.')
        self.embed_enabled = Toggle(pal)
        self.embed_model, self.embed_url = QLineEdit(), QLineEdit()
        self.embed_device = QComboBox()
        self.embed_device.addItems(['Auto (GPU when available)', 'CPU only'])
        self.embed_batch = QSpinBox()
        self.embed_batch.setRange(1, 128)
        for widget in (self.embed_model, self.embed_url, self.embed_device):
            widget.setMinimumWidth(300)
        g.row('Use embeddings', self.embed_enabled)
        g.row('Model', self.embed_model)
        g.row('Ollama server', self.embed_url)
        g.row('Device', self.embed_device)
        g.row('Batch size', self.embed_batch, 'Lower it if the GPU runs out of memory')
        body.addWidget(g)

        g = Group(win, 'IMAGES & VIDEOS', 'A local OCR model (RapidOCR) reads the text in photos, screenshots, scanned PDF pages and video frames. '
                                          'A local vision model in Ollama describes what pictures show, so “kid on a bench in autumn” finds the photo. '
                                          'Both run on this PC; descriptions are labelled as AI-written.')
        self.ocr_enabled = Toggle(pal)
        g.row('Read text in images and videos', self.ocr_enabled)
        self.vision_enabled = Toggle(pal)
        g.row('Describe images with AI', self.vision_enabled, 'A caption, description and tags for each picture and one frame of each video')
        self.vision_model = QLineEdit()
        self.vision_model.setMinimumWidth(220)
        g.row('Vision model', self.vision_model, 'An Ollama vision model, e.g. gemma3:4b  ·  ollama pull gemma3:4b')
        self.video_interval = QSpinBox()
        self.video_interval.setRange(2, 120)
        self.video_interval.setSuffix(' s')
        g.row('Video: one frame every', self.video_interval,
              'Frames that look the same (a still slide) are read once. Long videos are capped at 120 frames.')
        body.addWidget(g)

        g = Group(win, 'DIAGNOSTICS', 'A detected GPU is not the same as an accelerated model. Device facts come from nvidia-smi and from Ollama’s own report — nothing is inferred.')
        row = QHBoxLayout()
        row.addWidget(button('Check status', lambda: self.diagnose(False)))
        row.addWidget(button('Run test inference', lambda: self.diagnose(True), 'primary'))
        g.row('GPU & models', row)
        self.diag = label('Not checked yet.', 'caption', wrap=True, selectable=True)
        self.diag.setTextFormat(Qt.MarkdownText)
        g.outer.addWidget(self.diag)
        body.addWidget(g)

        g = Group(win, 'APPEARANCE')
        self.theme = QComboBox()
        self.theme.addItems(['Match Windows', 'Dark', 'Light'])
        g.row('Theme', self.theme)
        self.reduce_motion = Toggle(pal)
        g.row('Reduce motion', self.reduce_motion, 'Also follows Windows “Animation effects”')
        self.start_login = Toggle(pal)
        self.start_login.toggled.connect(self.change_start_login)
        g.row('Start Alfred when you sign in', self.start_login,
              'Starts hidden in the tray, so the shortcut works right away; it also starts Ollama for search by meaning')
        self.hotkey_edit = QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        self.hotkey_edit.setFixedWidth(170)
        self.hotkey_edit.editingFinished.connect(self.change_hotkey)
        self.hotkey_label = label('', 'caption')
        hotkey_row = QHBoxLayout()
        hotkey_row.addWidget(self.hotkey_edit)
        hotkey_row.addWidget(button('Reset', lambda: self.change_hotkey(DEFAULT_HOTKEY)))
        g.row('Show / hide Alfred', hotkey_row, 'Click the box and press a combination with Ctrl, Alt, Shift or Win')
        g.outer.addWidget(self.hotkey_label)
        body.addWidget(g)
        body.addWidget(label(f'{APP_NAME} {__version__} ·{self.win.settings.path}', 'caption', wrap=True))
        body.addStretch()

        self.save_timer = QTimer(self, singleShot=True, timeout=self.save)
        for widget in (self.base_url, self.embed_model, self.embed_url, self.vision_model, self.compatible_url):
            widget.textEdited.connect(lambda _: self.save_timer.start(600))
        for widget in (self.device, self.embed_device, self.theme, self.effort):
            widget.currentIndexChanged.connect(lambda _: self.save_timer.start(250))
        self.embed_batch.valueChanged.connect(lambda _: self.save_timer.start(600))
        self.video_interval.valueChanged.connect(lambda _: self.save_timer.start(800))
        self.keep_minutes.valueChanged.connect(lambda _: self.save_timer.start(600))
        for toggle in (self.embed_enabled, self.ocr_enabled, self.vision_enabled, self.reduce_motion, self.auto_index, self.autostart, self.save_chats):
            toggle.toggled.connect(lambda _: self.save_timer.start(250))
        self.loading = False
        guard_wheel(self)

    def activated(self):
        self.loading = True
        s = self.win.settings
        for widget in self.findChildren(QWidget):
            widget.blockSignals(True)
        self.base_url.setText(s.get('base_url') or ai.OLLAMA_DEFAULT)
        self.compatible_url.setText(s.get('compatible_base_url') or '')
        self.device.setCurrentIndex(1 if s.get('device') == 'cpu' else 0)
        self.embed_enabled.setChecked(bool(s.get('embed_enabled')))
        self.embed_model.setText(s.get('embed_model') or '')
        self.embed_url.setText(s.get('embed_base_url') or '')
        self.embed_device.setCurrentIndex(1 if s.get('embed_device') == 'cpu' else 0)
        self.embed_batch.setValue(int(s.get('embed_batch') or 16))
        self.ocr_enabled.setChecked(bool(s.get('ocr_enabled', True)))
        self.vision_enabled.setChecked(bool(s.get('vision_enabled', True)))
        self.vision_model.setText(s.get('vision_model') or 'gemma3:4b')
        self.video_interval.setValue(int(s.get('video_interval') or 10))
        self.save_chats.setChecked(bool(s.get('save_chat_history', True)))
        self.theme.setCurrentIndex({'system': 0, 'dark': 1, 'light': 2}.get(s.get('theme'), 0))
        self.reduce_motion.setChecked(bool(s.get('reduce_motion')))
        self.auto_index.setChecked(bool(s.get('auto_index', True)))
        self.autostart.setChecked(bool(s.get('ollama_autostart', True)))
        self.keep_minutes.setValue(max(5, int(s.get('keep_alive_minutes') or 5)))
        for widget in self.findChildren(QWidget):
            widget.blockSignals(False)
        self.load_models()
        self.load_effort()
        self.show_chats_info()
        self.show_hotkey()
        run_job(lambda s: startup.enabled(), self.show_start_login)
        self.tracked.load()
        self.show_index_info()
        self.loading = False

    def show_index_info(self):
        st = self.win.status
        self.index_info.setText(f"{st['files']} files indexed · {st['segments']} passages · {st['vectors']} embeddings · "
                                f"{st['errors']} failed · {st['unsupported']} unsupported · last indexed {st['last_indexed'] or 'never'}"
                                + ''.join(f"\n{p['status']}: {p['path']} — {p['error']}" for p in self.win.index.problems(6)))

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
            if kind == 'ollama':
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
            self.account_status[kind].setText(detail)
            if kind in ('claude_code', 'codex'):
                self.account_button[kind].setText({'ready': 'Re-check', 'missing': 'How to install'}.get(state, 'Sign in…'))
            elif kind in ('anthropic', 'openai'):
                self.account_button[kind].setText('Remove key' if state == 'ready' else 'Add key…')
            else:
                self.account_button[kind].setText('Remove key' if keys.has('compatible') else 'Key…')

    def show_location(self):
        provider = providers.for_settings(self.win.settings)
        if not provider.describe()['configured']:
            self.location.setText('Choose a chat model to use Ask Alfred.')
        elif provider.cloud:
            self.location.setText(f'Answers come {provider.via}: your questions (and the passages Alfred finds for them) are sent there. '
                                  'Your files stay on this PC.')
        else:
            self.location.setText('Answers are written on this PC — nothing leaves it.')

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
        from PySide6.QtWidgets import QInputDialog
        value, ok = QInputDialog.getText(self, title, prompt + '\nIt’s stored in Windows Credential Manager.', QLineEdit.Password)
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
            fill_effort(self.effort, self.effort_note, value[0], value[1], settings.get('effort'))
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

    def open_chats_folder(self):
        folder = self.win.settings.chats_folder
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

    def change_chats_folder(self):
        old = self.win.settings.chats_folder
        chosen = QFileDialog.getExistingDirectory(self, 'Choose where Alfred saves your chats', str(old))
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
        answer = QMessageBox.question(self, 'Delete all chats?', f'This permanently deletes {count} saved chat{"s" * (count != 1)} from '
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
            self.win.toast('Alfred will start hidden in the tray when you sign in' if now else
                           'Alfred won’t start automatically any more', 'green' if now == on else 'red', seconds=4)
        run_job(work, done, lambda error: (self.show_start_login(not on), self.win.toast(f'Couldn’t change start-up: {error}', 'red', seconds=8)))

    def show_hotkey(self):
        active = self.win.active_hotkey
        self.hotkey_edit.blockSignals(True)
        self.hotkey_edit.setKeySequence(QKeySequence(active.replace('Win+', 'Meta+')) if active else QKeySequence())
        self.hotkey_edit.blockSignals(False)
        self.hotkey_label.setText(f'Active: {active}' if active else 'No global shortcut is active — use the tray icon.')

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
        before = (s.get('embed_enabled'), s.get('embed_model'), s.get('ocr_enabled'), s.get('video_interval'), s.get('vision_enabled'), s.get('vision_model'))
        before_model = (s.get('provider'), s.get('model'))
        s.update(base_url=self.base_url.text().strip() or ai.OLLAMA_DEFAULT, compatible_base_url=self.compatible_url.text().strip())
        if self.effort.isEnabled() and self.effort.currentData() is not None:
            s['effort'] = self.effort.currentData()
        s.update({'device': 'cpu' if self.device.currentIndex() else 'auto',
                  'embed_enabled': self.embed_enabled.isChecked(), 'embed_model': self.embed_model.text().strip(),
                  'embed_base_url': self.embed_url.text().strip(), 'embed_device': 'cpu' if self.embed_device.currentIndex() else 'auto',
                  'embed_batch': self.embed_batch.value(), 'ocr_enabled': self.ocr_enabled.isChecked(),
                  'vision_enabled': self.vision_enabled.isChecked(), 'vision_model': self.vision_model.text().strip() or 'gemma3:4b',
                  'video_interval': self.video_interval.value(), 'theme': ['system', 'dark', 'light'][self.theme.currentIndex()],
                  'reduce_motion': self.reduce_motion.isChecked(), 'auto_index': self.auto_index.isChecked(),
                  'ollama_autostart': self.autostart.isChecked(), 'keep_alive_minutes': self.keep_minutes.value(),
                  'save_chat_history': self.save_chats.isChecked()})
        s.save()
        self.win.watch_folders()
        Motion.detect(s)
        self.win.apply_theme()
        self.win.refresh_status()
        self.win.ask_view.chats_changed()
        if (s.get('provider'), s.get('model')) != before_model:
            self.load_effort()
            self.show_location()
        after = (s.get('embed_enabled'), s.get('embed_model'), s.get('ocr_enabled'), s.get('video_interval'), s.get('vision_enabled'), s.get('vision_model'))
        if after != before:
            self.win.toast('Saved · updating your index', 'green', seconds=4)
            self.win.start_index(quiet=True)
        else:
            self.win.toast('Settings saved', 'green', seconds=2)

    def refresh_models(self):
        self.load_models(announce=True)

    def diagnose(self, probe):
        settings = dict(self.win.settings)
        self.diag.setText('Checking…' + (' loading models for a test inference' if probe else ''))
        if probe:
            self.win.thinking(True)

        def done(r):
            self.win.thinking(False)
            lines = []
            for g in r['gpu']['gpus']:
                lines.append(f"**GPU detected:** {g['name']} · {g['vram_used_mib']:,} / {g['vram_total_mib']:,} MiB in use · driver {g['driver']}")
            if not r['gpu']['available']:
                lines.append(f"**GPU detected:** none — {r['gpu'].get('note', '')}")
            if r.get('ollama'):
                o = r['ollama']
                lines.append(f"**Ollama:** {'v' + str(o.get('version')) if o.get('running') else 'not running'}" +
                             (' · ' + ', '.join(f"{m['model']} → {m['device']}" for m in o['loaded']) if o.get('loaded') else ' · nothing loaded'))
            for key, name in (('chat', 'Chat'), ('embeddings', 'Search by meaning'), ('vision', 'Picture descriptions')):
                info = r.get(key, {})
                if (key == 'chat' and not info.get('configured')) or (key != 'chat' and not info.get('enabled')):
                    lines.append(f'**{name}:** off')
                    continue
                state = {True: 'GPU ACTIVE', False: 'CPU', None: 'device not confirmed'}[info.get('gpu_active')] if info.get('location') == 'local' else 'remote'
                lines.append(f"**{name}:** {info.get('model')} · **{state}** — {info.get('execution', '')}")
            lines += [f'**Error:** {e}' for e in r['errors']]
            self.diag.setText('  \n'.join(lines))
        run_job(lambda s: ai.diagnostics(settings, probe), done, lambda e: (self.win.thinking(False), self.diag.setText(f'Failed: {e}')))


# ------------------------------------------------------------------ window
class AlfredWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(logo_pixmap(64)))
        self.settings = config.load()
        Motion.detect(self.settings)
        self.pal = palette_for(self.settings.get('theme'))
        self.index = Index(self.settings)
        self.status, self.folder_stats = self.index.status(), {}
        self.indexing, self.progress, self.mode, self.hiding = False, None, SEARCH, False
        self.active_hotkey = ''
        self.tray, self.reindex_pending = None, False
        self.hotkey, self.open_action = None, None
        self.warming = False
        self.watcher = FolderWatcher(parent=self)
        self.watcher.changed.connect(self.auto_index)
        self.backdrop, self.grab_origin = None, QPoint()
        self.setFixedSize(W + 2 * M, MAXH + 2 * M)

        self.height_spring = Spring(BAR, 300, 0.86, self._height)
        self.appear = Spring(0, 420, 0.82, self._appear)
        self.glow_strength = Spring(0, 140, 1.0, lambda v: self.update())
        self.glow = Loop(3.0, self)
        self.glow.tick.connect(lambda _: self.update())
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
        for view in (self.search_view, self.ask_view, self.settings_view):
            self.views.addWidget(view)
        inner.addWidget(self.views, 1)
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
        self.toast_timer = QTimer(self, singleShot=True, timeout=self.end_toast)
        self.toasting = False
        self._height(BAR)
        self.watch_folders()
        self.apply_theme()
        self.refresh_status()
        self.set_mode(SEARCH, animate=False)

    # ---------------------------------------------------------------- theme
    def apply_theme(self):
        self.pal = palette_for(self.settings.get('theme'))
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
        if self.backdrop is not None and self.grab_origin == QPoint(-1, -1):
            self.backdrop = fallback_backdrop(self.size(), pal['name'] == 'dark')
        self.update()

    # ------------------------------------------------------------ geometry
    def panel_rect(self):
        return QRectF(M, M, W, self.height_spring.value)

    def _height(self, value):
        self.clip.setGeometry(M, M + BAR, W, max(0, int(value - BAR)))
        self.update()

    def update_height(self):
        expanded = self.mode != SEARCH or self.search_view.wants_expanded()
        self.height_spring.to(MAXH if expanded else BAR)

    def _appear(self, value):
        self.setWindowOpacity(max(0.0, min(1.0, value)))
        if hasattr(self, 'base_pos'):
            self.move(self.base_pos.x(), int(self.base_pos.y() - (1 - value) * 14))
        if self.hiding and value < 0.03:
            self.hiding = False
            self.hide()

    def show_panel(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.geometry()
        self.base_pos = QPoint(area.center().x() - self.width() // 2, area.top() + int(area.height() * 0.17) - M)
        self.capture_backdrop(screen)
        self.hiding = False
        self.appear.snap(0 if not Motion.reduced else 1)
        self.move(self.base_pos.x(), self.base_pos.y() - 14)
        self.show()
        self.raise_()
        self.activateWindow()
        if os.name == 'nt':
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except Exception:
                pass
        self.appear.to(1)
        self.search.setFocus()
        self.search.selectAll()
        self.refresh_status()
        self.warm_up()

    def hide_panel(self):
        if not self.isVisible():
            return
        self.hiding = True
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

    def capture_backdrop(self, screen):
        if QGuiApplication.platformName() == 'offscreen':
            self.backdrop, self.grab_origin = fallback_backdrop(self.size(), self.pal['name'] == 'dark'), QPoint(-1, -1)
            return
        area = screen.geometry()
        x, y = self.base_pos.x(), self.base_pos.y() - 20
        shot = screen.grabWindow(0, x - area.x(), y - area.y(), self.width(), self.height() + 20)
        self.backdrop = blurred_backdrop(shot) if not shot.isNull() else None
        self.grab_origin = QPoint(x, y)
        if self.backdrop is None:
            self.backdrop, self.grab_origin = fallback_backdrop(self.size(), self.pal['name'] == 'dark'), QPoint(-1, -1)

    # ---------------------------------------------------------------- paint
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        rect = self.panel_rect()
        paint_shadow(p, rect, R, self.pal['shadow'])
        origin = QPointF(0, 0) if self.grab_origin == QPoint(-1, -1) else QPointF(self.grab_origin - self.pos())
        paint_glass(p, rect, R, self.pal, self.backdrop, origin)
        icon_rect = QRectF(M + 22, M + (BAR - 22) / 2, 22, 22)
        p.drawPixmap(icon_rect, sparkle_pixmap(22) if self.mode == ASK else icon_pixmap(
            {SEARCH: 'search', SETTINGS: 'gear'}.get(self.mode, 'search'), 22, self.pal['secondary']), QRectF())
        expansion = max(0.0, min(1.0, (self.height_spring.value - BAR) / 60))
        if expansion > 0:
            sep = QColor(self.pal['separator'])
            sep.setAlphaF(sep.alphaF() * expansion)
            p.setPen(QPen(sep, 1))
            p.drawLine(QPointF(M + 16, M + BAR - 0.5), QPointF(M + W - 16, M + BAR - 0.5))
        if self.indexing:
            self.paint_progress(p)
        paint_glow(p, rect, R, self.glow.phase, self.glow_strength.value)

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
        self.modes.set(mode, animate)
        self.search.setPlaceholderText(PLACEHOLDERS[mode])
        self.search.setReadOnly(mode == SETTINGS)
        if mode == SETTINGS:
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self.views.setCurrentIndex(mode)
        view = self.views.currentWidget()
        if hasattr(view, 'activated'):
            view.activated()
        if animate and previous != mode:
            fade_in(view)
        if mode == SEARCH:
            self.search_view.set_query(self.search.text())
        self.update_hints()
        self.update_height()
        self.update()
        self.search.setFocus()

    def update_hints(self):
        hints = {SEARCH: '↑↓ Select   ↵ Open   Ctrl+↵ Show in folder   ⇥ Ask Alfred   Esc Close',
                 ASK: '↵ Ask   Ctrl+N New chat   Ctrl+H History   Esc Close',
                 SETTINGS: 'Changes save automatically   Esc Back'}
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
            if key == Qt.Key_Escape:
                if self.search.text() and self.mode in (SEARCH, ASK):
                    self.search.clear()
                elif self.mode != SEARCH:
                    self.set_mode(SEARCH)
                else:
                    self.hide_panel()
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

    def maybe_hide(self):
        if QApplication.activeWindow() is None and QApplication.activeModalWidget() is None and not self.hiding:
            self.hide_panel()

    def mousePressEvent(self, event):
        if not self.panel_rect().contains(event.position()):
            self.hide_panel()
        else:
            super().mousePressEvent(event)

    # ---------------------------------------------------------------- state
    def problem_state(self):
        st = self.status
        if not st['folders']:
            suggestions = [(f'Track {name}', lambda _=False, p=path: self.track_folder(p)) for name, path in suggested_folders()[:1]]
            return ('Track a folder to start searching',
                    'Alfred searches the folders you track — like Downloads or Pictures — including the text inside documents, '
                    'images and videos, and keeps up with new files automatically.',
                    [('Track folder…', self.choose_folder)] + suggestions, 'folder')
        if not st['root_exists']:
            return ('Your tracked folders are missing', 'They may have been moved, renamed or be on a drive that isn’t connected.',
                    [('Open Settings', lambda: self.set_mode(SETTINGS)), ('Track folder…', self.choose_folder)], 'folder')
        if not st['files']:
            if self.indexing:
                return ('Reading your files…', 'Results appear here as soon as the first files are indexed.', [], 'refresh')
            if st['initialized']:
                return ('Nothing searchable yet', 'Alfred reads documents, PDFs, slides, spreadsheets, text files, images and videos. '
                        'Add some to a tracked folder, or track another folder.', [('Track folder…', self.choose_folder)], 'folder')
            return ('Your folders haven’t been indexed yet', 'Index them now to start searching.', [('Index now', self.start_index)], 'refresh')
        return None

    def warm_up(self, on_ready=None, retry=True):
        """Start Ollama if needed and pre-load the models; each open restarts their keep-alive timer."""
        if on_ready is not None:
            self.ready_callbacks = getattr(self, 'ready_callbacks', []) + [on_ready]
        if self.warming or not self.settings.get('ollama_autostart', True):
            return
        uses_ollama = bool(self.settings.get('model')) or bool(self.settings.get('embed_enabled'))
        if not uses_ollama:
            return
        self.warming = True
        settings = dict(self.settings)
        minutes = ai.keep_alive(settings)[:-1]

        def progress(text):
            if not self.toasting:
                self.status_label.setText(f"<span style='color:{self.pal['accent'].name()}'>●</span>&nbsp; {html.escape(text)}")
            self.shimmer.start()

        def done(report):
            self.warming = False
            logging.getLogger('alfred').info('Ollama ready (started=%s, loaded=%s)', report['started'], report['loaded'])
            callbacks, self.ready_callbacks = getattr(self, 'ready_callbacks', []), []
            for callback in callbacks:
                callback()
            if not self.indexing:
                self.shimmer.stop()
            if report['started']:
                self.toast(f"Ollama started · models ready and kept loaded {minutes} min after last use", 'green', seconds=5)
            elif not self.toasting:
                self.status_label.setText(self.status_text())
            self.update()

        def failed(error):
            self.warming = False
            if not self.indexing:
                self.shimmer.stop()
            logging.getLogger('alfred').warning('Ollama start failed: %s', error)
            if retry:  # e.g. right after boot the GPU driver may not be ready yet: try once more shortly
                self.toast(f'Ollama didn’t start ({error}) — retrying in 30 s; searching by keyword meanwhile', 'orange', seconds=8)
                QTimer.singleShot(30000, lambda: self.warm_up(retry=False))
            else:
                self.ready_callbacks = []
                self.toast(f'Ollama unavailable: {error} — searching by keyword only', 'orange', seconds=10)
        run_job(lambda s: ai.warm_up(settings, s.progress.emit), done, failed, progress=progress)

    def rebind_hotkey(self, text):
        """Switch the global shortcut live; GlobalHotkey restores the old one if the new one is taken."""
        if self.hotkey is None:
            raise OSError('Global shortcuts are unavailable on this system')
        self.hotkey.register(text)
        self.active_hotkey = text
        self.settings['hotkey'] = text
        self.settings.save()
        if self.tray is not None:
            self.tray.setToolTip(f'Alfred — {text}')
        if self.open_action is not None:
            self.open_action.setText(f'Open Alfred\t{text}')

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
            self.tray.showMessage('Alfred', text, QIcon(logo_pixmap(32)), 5000)

    def refresh_status(self):
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

    def start_index(self, force=False, quiet=False, auto=False, roots=None):
        if self.indexing:
            self.reindex_pending = True  # picked up right after the current run
            return
        if not self.settings.folders:
            if not quiet:
                self.toast('Track a folder first', 'orange', ('Track folder…', self.choose_folder))
            return
        self.indexing, self.progress, self.live_refresh = True, None, time.monotonic()
        self.shimmer.start()
        self.status_label.setText(self.status_text())
        index, settings = self.index, self.settings
        tracked = self.settings_view.tracked

        def progress(state):
            total, done = state.get('total', 0), state.get('done', 0)
            self.progress = done / total if total else None
            phase = {'scanning': 'Looking for files', 'indexing': 'Reading', 'embedding': 'Learning meaning', 'reading video': 'Reading video',
                     'describing': 'Describing pictures',
                     'done': 'Finishing'}.get(state.get('phase'), state.get('phase'))
            if not self.toasting:
                where = f" · {html.escape(Path(state['current']).name)}" if state.get('current') else ''
                count = f' {done}/{total}' if total else ''
                self.status_label.setText(f"<span style='color:{self.pal['accent'].name()}'>●</span>&nbsp; {phase}{count}{where}")
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
            self.shimmer.stop()
            logging.getLogger('alfred').info('Indexed: %s', {k: summary.get(k) for k in ('found', 'indexed', 'removed', 'errors', 'unsupported',
                                                                                        'embedded', 'described', 'seconds', 'warnings')})
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
            self.shimmer.stop()
            self.refresh_status()
            if not isinstance(error, IndexBusy):
                (self.notify if auto else lambda text, color: self.toast(text, color, seconds=8))(f'Indexing failed: {error}', 'red')
            self.after_index()

        def work(signals):
            embedder = ai.Embedder.from_settings(settings)
            if embedder is not None and embedder.local and settings.get('ollama_autostart', True):
                try:
                    ai.ensure_ollama(embedder.base)
                except ai.ProviderError as error:
                    logging.getLogger('alfred').warning('Indexing without search by meaning: %s', error)
                    embedder = None  # keyword search still works; meaning catches up on the next run
            if embedder is not None and not ai.ollama_running(embedder.base, 1.5):
                embedder = None
            vision = ai.Vision.from_settings(settings)
            if vision is not None and not ai.ollama_running(vision.base, 1.5):
                vision = None  # described on a later run, once Ollama is up
            return index.update(signals.progress.emit, embedder, force=force, roots=roots, vision=vision)
        run_job(work, done, failed, progress=progress)

    def after_index(self):
        if self.reindex_pending:
            self.reindex_pending = False
            QTimer.singleShot(400, lambda: self.start_index(quiet=True, auto=True))

    def choose_folder(self):
        start = next((str(path) for _, path in suggested_folders()), str(Path.home()))
        folder = QFileDialog.getExistingDirectory(self, 'Choose a folder for Alfred to track', start)
        if folder:
            self.track_folder(folder)

    def track_folder(self, folder):
        """Track a folder: index it right away, then watch it for changes."""
        folder = Path(os.path.realpath(folder))
        reason = paths.forbidden_reason(folder, self.settings.home)
        if reason:
            self.toast(reason, 'orange', seconds=8)
            return False
        tracked = self.settings.folders
        parent = next((t for t in tracked if paths.is_within(t, folder)), None)
        if parent is not None:
            self.toast(f'{folder.name} is already tracked as part of {parent.name}', 'orange', seconds=6)
            return False
        saved_chats = self.settings.chats_folder
        if paths.is_within(saved_chats, folder) or paths.is_within(folder, saved_chats):
            self.toast('That folder holds your saved chats, so Alfred won’t search it', 'orange', seconds=6)
            return False
        inside = [t for t in tracked if paths.is_within(folder, t)]
        self.settings['tracked_folders'] = [str(t) for t in tracked if t not in inside] + [str(folder)]
        self.settings.save()
        self.watch_folders()
        if self.mode == SETTINGS:
            if inside:
                self.settings_view.tracked.load()
            else:
                self.settings_view.tracked.added(folder)
        merged = f" (it includes {', '.join(t.name for t in inside)})" if inside else ''
        self.toast(f'Tracking {folder.name}{merged} · indexing now', 'green', seconds=5)
        self.refresh_status()
        self.start_index(quiet=True, roots=[folder])
        return True

    def untrack_folder(self, folder):
        """Stop tracking a folder; its files leave the index (nothing on disk is touched)."""
        key = paths.canonical(folder)
        self.settings['tracked_folders'] = [str(t) for t in self.settings.folders if paths.canonical(t) != key]
        self.settings.save()
        self.watch_folders()
        index = self.index
        run_job(lambda s: index.remove_root(folder), lambda _: self.refresh_status(),
                lambda error: self.toast(f'Couldn’t remove {Path(folder).name} from the index: {error}', 'red'))
        self.toast(f'Stopped tracking {Path(folder).name}', 'green', seconds=4)

# ------------------------------------------------------------------ app
INSTANCE = 'alfred-desktop-' + os.environ.get('USERNAME', 'user')
NEWLINE = b'\n'  # ends each command and reply on the instance socket


def send_command(message, reply=True, timeout_ms=60000):
    """Send one command to the running Alfred over its local socket. None when Alfred isn't running;
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
    """Handle one command from send_command: show, track <folder>, search <query>, status."""
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
                    'hotkey': window.active_hotkey, 'settings_file': str(window.settings.path)})
        else:
            window.show_panel()
            answer({})
    socket.readyRead.connect(received)
    if socket.bytesAvailable():
        received()


def run():
    if startup.sandboxed() and startup.relaunch_outside():
        return 0  # Windows Explorer starts the real Alfred; this redirected copy just exits
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Alfred.KnowledgeAssistant')
        except Exception:
            pass
    handler = RotatingFileHandler(config.data_home() / 'assistant.log', maxBytes=512 * 1024, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger('alfred').addHandler(handler)
    logging.getLogger('alfred').setLevel(logging.INFO)
    logging.getLogger('alfred').info('Alfred %s starting', __version__)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle('Fusion')
    # Single instance: a second launch just shows the running Alfred.
    if send_command({'cmd': 'show'}, reply=False) is not None:
        return 0
    QLocalServer.removeServer(INSTANCE)
    server = QLocalServer()
    server.listen(INSTANCE)
    window = AlfredWindow()
    startup.first_run(window.settings)
    # On quit, stop indexing at the next file and let background work finish before Python shuts down.
    app.aboutToQuit.connect(lambda: (window.index.cancel.set(), QThreadPool.globalInstance().waitForDone(5000)))
    server.newConnection.connect(lambda: serve_command(server.nextPendingConnection(), window))

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
            native = HotkeyFilter(window.toggle_panel)
            app.installNativeEventFilter(native)
            app._hotkey = (hotkey, native)
            window.hotkey = hotkey
            app.aboutToQuit.connect(hotkey.unregister)
    except Exception as error:
        hotkey_error = str(error)
    window.settings_view.show_hotkey()
    shortcut = window.active_hotkey or 'the tray icon'

    tray = QSystemTrayIcon(QIcon(logo_pixmap(32)), app)
    tray.setToolTip(f'Alfred — {shortcut}')
    menu = QMenu()
    window.open_action = menu.addAction(f'Open Alfred	{window.active_hotkey}', window.show_panel)
    menu.addAction('Rescan files', lambda: (window.show_panel(), window.start_index()))
    menu.addAction('Settings', lambda: (window.show_panel(), window.set_mode(SETTINGS)))
    menu.addSeparator()
    menu.addAction('Quit Alfred', app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: window.toggle_panel() if reason == QSystemTrayIcon.Trigger else None)
    tray.show()
    window.tray = tray
    at_sign_in = '--startup' in sys.argv
    if at_sign_in:
        # Started with Windows: stay hidden in the tray. Give Windows and the GPU driver a moment,
        # then run the sign-in tasks (start Ollama, catch up the index).
        logging.getLogger('alfred').info('Started at sign-in (hidden in the tray)')
        QTimer.singleShot(startup.TASK_DELAY_MS, lambda: startup.run_tasks(window))
    else:
        window.show_panel()
        QTimer.singleShot(700, lambda: window.start_index(quiet=True))
    if window.active_hotkey:
        tray.showMessage('Alfred is ready' if at_sign_in else 'Alfred is running',
                         f'Press {window.active_hotkey} anywhere to open Alfred. Esc hides it.', QIcon(logo_pixmap(32)), 4000)
        if window.active_hotkey != (window.settings.get('hotkey') or DEFAULT_HOTKEY):
            window.toast(f"{window.settings.get('hotkey')} is used by another app — using {window.active_hotkey}", 'orange', seconds=8)
    else:
        window.toast(f'Global shortcut unavailable ({hotkey_error}); use the tray icon.', 'orange', seconds=8)
    return app.exec()
