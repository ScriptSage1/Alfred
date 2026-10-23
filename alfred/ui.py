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
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QKeySequence, QCursor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QIcon, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFileIconProvider, QFrame, QGraphicsOpacityEffect,
                               QGridLayout, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QSystemTrayIcon, QTextBrowser, QVBoxLayout,
                               QWidget)

from . import APP_NAME, REPO, __version__, ai, config, knowledge, paths, rag, startup
from .index import IMAGE_SUFFIXES, VIDEO_SUFFIXES, Index, video_frame_png
from .watcher import FolderWatcher
from .ui_kit import (DRIVER, Loop, Motion, Spring, Toggle, blurred_backdrop, fallback_backdrop, icon, icon_pixmap, logo_pixmap,
                     paint_glass, paint_glow, paint_shadow, palette_for, sparkle_pixmap, stylesheet, HotkeyFilter)

W, BAR, MAXH, M, R, FOOT = 780, 62, 600, 46, 26, 34
DEFAULT_HOTKEY, FALLBACK_HOTKEYS = 'Ctrl+Alt+L', ['Ctrl+Alt+L', 'Ctrl+Alt+K', 'Ctrl+Alt+A']
SEARCH, ASK, KNOW, SETTINGS, NOTE = range(5)
PLACEHOLDERS = {SEARCH: 'Search your files', ASK: 'Ask Alfred anything about your files', KNOW: 'Filter knowledge notes',
                SETTINGS: 'Settings', NOTE: 'Review knowledge note'}
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
    """Four glass icon buttons; a liquid pill glides (and stretches) to the active one."""
    changed = Signal(int)
    ITEMS = [('search', 'Search  (Ctrl+1)'), ('sparkle', 'Ask Alfred  (Ctrl+2 · Tab)'),
             ('graph', 'Knowledge  (Ctrl+3)'), ('gear', 'Settings  (Ctrl+4)')]
    SIZE = 34

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.index, self.hover = 0, -1
        self.setFixedSize(self.SIZE * 4 + 8, self.SIZE + 6)
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
        hover = hover if 0 <= hover < 4 else -1
        if hover != self.hover:
            self.hover = hover
            self.setToolTip(self.ITEMS[hover][1] if hover >= 0 else '')
            self.update()

    def leaveEvent(self, event):
        self.hover = -1
        self.update()

    def mousePressEvent(self, event):
        index = int((event.position().x() - 4) // self.SIZE)
        if 0 <= index < 4:
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
        root = self.win.settings.root
        items, seen = [], set()
        results = [r for r in results if not (r['path'] in seen or seen.add(r['path']))]

        def row(result):
            absolute = root / result['path']
            folder = str(Path(result['path']).parent).replace('\\', '/')
            return ('row', dict(result, kind_row='file', title=result['filename'], pixmap=FileIcons.get(absolute),
                                rounded=Path(result['path']).suffix.lower() in IMAGE_SUFFIXES,
                                subtitle=f"{result['location']} — {result['snippet']}",
                                right='' if folder == '.' else folder))
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
            self.p_title.setText('Ask Alfred')
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
        root = self.win.settings.root
        absolute = root / item['path']
        self.big_icon.setPixmap(FileIcons.get(absolute, 52))
        self.p_title.setText(item['filename'])
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
        query, index = self.query, self.win.index
        run_job(lambda s: index.read_excerpt(item['path'], segment_id=item.get('segment_id'), max_chars=2400),
                lambda ex: self.list.item() is item and self.excerpt.setHtml(_highlight(ex['excerpt'], query, self.win.pal)),
                lambda error: self.excerpt.setPlainText(f'Preview unavailable: {error}'))
        fade_in(self.pane)

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
        return paths.resolve_within(self.win.settings.root, item['path'])

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
    """One question and Alfred's streamed, cited answer."""

    def __init__(self, view, question, scope=None):
        super().__init__()
        self.view, self.win, self.question = view, view.win, question
        self.text, self.result = '', None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 10)
        layout.setSpacing(8)
        q = label(question, 'title', wrap=True, selectable=True)
        layout.addWidget(q)
        if scope:
            layout.addWidget(label(f'Only from {Path(scope).name}', 'caption'))
        head = QHBoxLayout()
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
        self.body.setOpenExternalLinks(True)  # web sources open in the browser
        self.body.setStyleSheet('font-size: 10.5pt;')
        self.body.hide()
        layout.addWidget(self.body)
        self.sources = QVBoxLayout()
        self.sources.setSpacing(5)
        layout.addLayout(self.sources)
        self.actions = QHBoxLayout()
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

    def finish(self, result):
        self.flush.stop()
        self.result = result
        self.text = result['answer'] or self.text
        self.render()
        self.body.show()
        provider = result['provider']
        where = 'on this PC' if provider.get('location') == 'local' else f"via {provider.get('host', 'remote provider')}"
        if provider.get('provider') == 'claude_code':
            where = 'via your Claude plan'
        effort = f" · {provider['effort']} effort" if provider.get('provider') in ('anthropic', 'claude_code') else ''
        who = f"{provider.get('name', provider.get('model', ''))} {where}{effort}"
        searches = len(result.get('web_searches') or [])
        web = f" + web search ({searches} search{'es' * (searches != 1)})" if result.get('web') else ''
        if result['grounded']:
            status = f"Grounded in {len(result['used'])} source{'s' * (len(result['used']) != 1)}{web} · {who}"
        elif result.get('web'):
            status = f"From the web — not in your files · {who}"
        elif not result['citations']:
            status = 'Not found in your files'
        else:
            status = f"No sources cited — the files may not cover this · {provider.get('model', '')}"
        if result.get('cancelled'):
            status += ' · stopped'
        self.status.set(status, False)
        for number, item in enumerate(result['citations']):
            source = SourceRow(self, item, item['marker'] in result['used'] or not result['used'])
            self.sources.addWidget(source)
            QTimer.singleShot(0 if Motion.reduced else 45 * number, lambda s=source: (s.show(), fade_in(s)))
            source.hide()
        if result['citations']:
            self.actions.addWidget(button('Save to Knowledge', lambda: self.win.open_note_sheet(self), 'primary',
                                          'Ctrl+S', icon('bookmark', 14, QColor('white'))))
        self.actions.addWidget(button('Copy', lambda: (QApplication.clipboard().setText(self.text), self.win.toast('Answer copied', 'green')),
                                      None, None, icon('copy', 14, self.win.pal['text'])))
        self.actions.addStretch()
        self.view.follow()

    def fail(self, error):
        self.flush.stop()
        setup = isinstance(error, ai.NotConfigured)
        self.status.set('Setup needed' if setup else 'Provider error', False)
        self.body.setTextFormat(Qt.PlainText)
        self.body.setText(str(error))
        self.body.setStyleSheet(f"color: {self.win.pal['orange' if setup else 'red'].name()}; font-size: 10pt;")
        self.body.show()
        self.actions.addWidget(button('Open Settings', lambda: self.win.set_mode(SETTINGS), 'primary'))
        self.actions.addStretch()


class SourceRow(QWidget):
    def __init__(self, turn, item, used):
        super().__init__()
        self.turn, self.item, self.win = turn, item, turn.win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        text = f"{item['marker']}   {item['filename']}   ·   {item['location']}"
        if not used:
            text += '   (retrieved, not cited)'
        if item.get('flagged'):
            text += '   ⚠ instructions withheld'
        self.chip = button(text, self.toggle, 'source', 'Show the supporting excerpt')
        self.chip.setProperty('used', 'true' if used else 'false')
        self.chip.setIcon(QIcon(FileIcons.get(self.win.settings.root / item['path'], 16)))
        layout.addWidget(self.chip)
        self.box = QFrame()
        box = QVBoxLayout(self.box)
        box.setContentsMargins(14, 8, 14, 10)
        body = label(item['snippet_full'][:1600], 'secondary', wrap=True, selectable=True)
        box.addWidget(body)
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
        return paths.resolve_within(self.win.settings.root, self.item['path'])

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


class AskView(QWidget):
    SUGGESTIONS = ['Summarize the most important decisions in my files', 'What risks or problems are mentioned?',
                   'Which documents mention costs or budgets?']

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.history, self.cancel, self.turns = [], None, []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        pickers = QHBoxLayout()
        pickers.setContentsMargins(26, 10, 26, 0)
        pickers.setSpacing(8)
        self.model_box, self.effort_box = QComboBox(), QComboBox()
        self.model_box.setMinimumWidth(250)
        self.effort_box.setMinimumWidth(120)
        self.model_box.setToolTip('Which model writes Ask Alfred answers')
        self.model_box.activated.connect(self.choose_model)
        self.effort_box.activated.connect(self.choose_effort)
        pickers.addWidget(label('Model', 'caption'))
        pickers.addWidget(self.model_box)
        pickers.addSpacing(6)
        pickers.addWidget(label('Thinking', 'caption'))
        pickers.addWidget(self.effort_box)
        pickers.addSpacing(6)
        self.web_box = QCheckBox('Web search')
        self.web_box.setToolTip('When your files don’t cover a question, Claude searches the web and labels what came from it')
        self.web_box.toggled.connect(self.choose_web)
        pickers.addWidget(self.web_box)
        pickers.addStretch()
        self.scope = None
        self.scope_chip = button('', self.clear_scope, 'source', 'Remove to ask about all your files again')
        self.scope_chip.hide()
        pickers.addWidget(self.scope_chip)
        layout.addLayout(pickers)
        guard_wheel(self)
        self.models = []
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)
        intro = QWidget()
        box = QVBoxLayout(intro)
        box.setAlignment(Qt.AlignCenter)
        box.setSpacing(10)
        art = QLabel()
        art.setPixmap(sparkle_pixmap(46))
        art.setAlignment(Qt.AlignCenter)
        box.addWidget(art, 0, Qt.AlignCenter)
        title = label('Ask Alfred', 'hero')
        title.setAlignment(Qt.AlignCenter)
        box.addWidget(title, 0, Qt.AlignCenter)
        self.intro_text = label('', 'secondary', wrap=True)
        self.intro_text.setAlignment(Qt.AlignCenter)
        self.intro_text.setMaximumWidth(500)
        box.addWidget(self.intro_text, 0, Qt.AlignCenter)
        self.suggestions = QVBoxLayout()
        self.suggestions.setAlignment(Qt.AlignCenter)
        for text in self.SUGGESTIONS:
            self.suggestions.addWidget(button(text, lambda _=False, t=text: (self.win.search.setText(t), self.win.search.setFocus()),
                                              'source', 'Puts this question in the box — edit it, then press Enter'), 0, Qt.AlignCenter)
        box.addLayout(self.suggestions)
        self.setup_button = button('Open Settings', lambda: self.win.set_mode(SETTINGS), 'primary')
        box.addWidget(self.setup_button, 0, Qt.AlignCenter)
        self.stack.addWidget(intro)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName('clear')
        self.thread = QVBoxLayout(holder)
        self.thread.setContentsMargins(26, 14, 26, 14)
        self.thread.setSpacing(6)
        self.thread.addStretch()
        self.scroll.setWidget(holder)
        self.stack.addWidget(self.scroll)
        self.scroll_spring = Spring(0, 260, 1.0, lambda v: self.scroll.verticalScrollBar().setValue(int(v)))

    def activated(self):
        info = ai.ChatProvider(self.win.settings).describe()
        if not info['configured']:
            self.intro_text.setText('Connect an AI model to get answers grounded in your files. Search, Knowledge and Claude Code (MCP) work without one.')
            self.setup_button.show()
        else:
            if info['provider'] == 'claude_code':
                where = ('through your Claude plan (Claude Code in the background, no tools) — the passages Alfred retrieves '
                         'for each question are sent to Anthropic under your account; no API billing')
            elif info['provider'] == 'anthropic':
                where = 'via the Claude API — the passages Alfred retrieves for each question are sent to Anthropic'
            elif info['location'] == 'local':
                where = 'running on this PC — your files stay local'
            else:
                where = f"at {info['host']} — retrieved excerpts are sent to this provider"
            web = (info['provider'] in ('claude_code', 'anthropic') and self.win.settings.get('web_search', True))
            lead = ('Answers come from your indexed files first, with citations; when they don’t have the answer, Claude searches the web '
                    'and labels it “From the web”.' if web else 'Answers come only from your indexed files, with citations.')
            self.intro_text.setText(f"{lead}\n{info.get('name', info['model'])} {where}.")
            self.setup_button.hide()
        if not self.turns:
            self.stack.setCurrentIndex(0)
        if self.scope:
            self.win.search.setPlaceholderText(f'Ask about {Path(self.scope).name}…')
        self.load_pickers()

    def load_pickers(self):
        settings = dict(self.win.settings)
        run_job(lambda s: (ai.available_models(settings), ai.effort_options(settings)), self.fill_pickers)

    def fill_pickers(self, value):
        models, (efforts, explanation) = value
        settings = self.win.settings
        provider = settings.get('provider')
        current = (settings.get('claude_model') if provider == 'anthropic' else settings.get('claude_code_model') if provider == 'claude_code'
                   else (settings.get('model') or '').removesuffix(':latest'))
        self.models = list(models)
        index = next((i for i, m in enumerate(self.models) if m['provider'] == provider and m['model'] == current), -1)
        if index < 0 and provider in ('ollama', 'openai', 'anthropic', 'claude_code') and current:
            self.models.insert(0, {'provider': provider, 'model': current, 'label': f'{current}  ·  {provider}'})
            index = 0
        self.model_box.blockSignals(True)
        self.model_box.clear()
        for model in self.models:
            self.model_box.addItem(model['label'])
        if index >= 0:
            self.model_box.setCurrentIndex(index)
        self.model_box.blockSignals(False)
        fill_effort(self.effort_box, None, efforts, explanation, settings.get('effort'))
        claude = settings.get('provider') in ('claude_code', 'anthropic')
        self.web_box.blockSignals(True)
        self.web_box.setChecked(bool(settings.get('web_search', True)) and claude)
        self.web_box.setEnabled(claude)
        self.web_box.setToolTip('When your files don’t cover a question, Claude searches the web and labels what came from it'
                                if claude else 'Web search is available with Claude models; local models answer from your files only')
        self.web_box.blockSignals(False)

    def choose_model(self, index):
        if not 0 <= index < len(self.models):
            return
        choice, settings = self.models[index], self.win.settings
        if choice['provider'] == 'claude_code':
            settings.update(provider='claude_code', claude_code_model=choice['model'])
        elif choice['provider'] == 'anthropic':
            settings.update(provider='anthropic', claude_model=choice['model'])
        else:
            if settings.get('provider') != choice['provider']:
                settings['base_url'] = ai.OLLAMA_DEFAULT if choice['provider'] == 'ollama' else settings.get('base_url')
            settings.update(provider=choice['provider'], model=choice['model'])
        settings.save()
        self.win.refresh_status()
        self.activated()
        name = ai.ChatProvider(settings).describe().get('name', choice['model'])
        if choice['provider'] == 'claude_code':
            snapshot = dict(settings)

            def checked(status):
                if not status['installed']:
                    self.win.toast('Claude Code was not found — install the Claude desktop app', 'orange', seconds=8)
                elif status['logged_in']:
                    self.win.toast(f'Answers now from {name} through your Claude plan · no API billing', 'green', seconds=6)
                else:
                    self.win.toast('Sign in once with your Claude account to use your plan', 'orange',
                                   ('Sign in', lambda: self.win.claude_sign_in()), 12)
            run_job(lambda s: ai.claude_code_status(snapshot, refresh=True), checked)
            return
        if choice['provider'] == 'anthropic':
            has_key = bool(settings.get('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY'))
            if has_key:
                self.win.toast(f'Answers now from {name} · retrieved excerpts are sent to Anthropic', 'green', seconds=6)
            else:
                self.win.toast(f'{name} needs an Anthropic API key', 'orange', ('Add key in Settings', lambda: self.win.set_mode(SETTINGS)), 10)
        else:
            self.win.toast(f'Answers now from {name} on this PC', 'green')
            self.win.warm_up()

    def choose_web(self, on):
        self.win.settings['web_search'] = bool(on)
        self.win.settings.save()
        self.win.toast('Web search on — used when your files don’t have the answer' if on else
                       'Web search off — answers come only from your files', 'green', seconds=3)
        self.activated()

    def choose_effort(self, index):
        value = self.effort_box.itemData(index)
        if value is None:
            return
        self.win.settings['effort'] = value
        self.win.settings.save()
        self.win.toast(f'Thinking effort: {self.effort_box.itemText(index)}', 'green', seconds=2)

    def set_scope(self, rel):
        """Answer the next questions only from this file (from Search → Ask about this)."""
        self.scope = rel
        name = Path(rel).name
        self.scope_chip.setText(f'Only from: {name}   ✕')
        self.scope_chip.setIcon(QIcon(FileIcons.get(self.win.settings.root / rel, 16)))
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

    def reset(self):
        self.clear_scope()
        if self.cancel:
            self.cancel.set()
        self.history.clear()
        for turn in self.turns:
            turn.deleteLater()
        self.turns.clear()
        self.stack.setCurrentIndex(0)
        self.win.toast('New conversation', 'green')

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
                if settings.get('provider') == 'ollama':
                    ai.ensure_ollama(settings.get('base_url'))
                if settings.get('embed_enabled') and settings.get('embed_provider', 'ollama') == 'ollama':
                    try:
                        ai.ensure_ollama(settings.get('embed_base_url'))  # search by meaning needs it, even for Claude answers
                    except ai.ProviderError as error:
                        logging.getLogger('alfred').warning('Ollama unavailable before Ask: %s', error)
            return rag.ask(index, settings, question, history, on_token=signals.token.emit, cancel=cancel,
                           on_evidence=signals.progress.emit, only_path=scope,
                           on_status=lambda text: signals.progress.emit(('status', text)))

        def done(result):
            turn.finish(result)
            self.history += [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': result['answer']}]
            self.end()

        def failed(error):
            turn.fail(error)
            self.end()
        run_job(work, done, failed, progress=turn.evidence, token=turn.add_token)

    def end(self):
        self.cancel = None
        self.win.thinking(False)

    def last_result_turn(self):
        return next((t for t in reversed(self.turns) if t.result and t.result['citations']), None)


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

    def row(self, title, widget=None, caption=None):
        if self.count:
            line = QFrame()
            line.setFixedHeight(1)
            line.setObjectName('hairline')
            self.rows.addWidget(line)
        self.count += 1
        holder = QWidget()
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


class KnowledgeView(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 16, 26, 8)
        layout.setSpacing(12)
        head = QHBoxLayout()
        self.dot = QLabel('●')
        self.status = label('', None, wrap=True)
        head.addWidget(self.dot)
        head.addWidget(self.status, 1)
        head.addWidget(button('Choose vault…', self.win.choose_vault))
        self.open_vault = button('Open in Obsidian', self.open_in_obsidian, 'primary')
        head.addWidget(self.open_vault)
        layout.addLayout(head)
        self.tip = label('Save answers from Ask Alfred as linked notes. In Obsidian, press Ctrl+G for Graph view, '
                         'or run “Open local graph” (Ctrl+P) on a note to see its connections.', 'caption', wrap=True)
        layout.addWidget(self.tip)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list = ResultList(win, self.scroll)
        self.list.activated.connect(self.open_note)
        self.scroll.setWidget(self.list)
        self.body = QStackedWidget()
        self.empty = EmptyState(win)
        self.body.addWidget(self.empty)
        self.body.addWidget(self.scroll)
        layout.addWidget(self.body, 1)
        self.notes = []

    def activated(self):
        status = knowledge.vault_status(self.win.settings)
        ready = status['state'] == 'ready'
        self.dot.setStyleSheet(f"color: {self.win.pal['green' if ready else 'orange'].name()};")
        self.status.setText(status['message'])
        self.open_vault.setEnabled(ready)
        self.notes = []
        if ready:
            try:
                self.notes = knowledge.list_notes(self.win.settings, limit=60)['notes']
            except knowledge.VaultError as error:
                self.status.setText(str(error))
        self.filter(self.win.search.text())

    def filter(self, text):
        words = text.casefold().split()
        notes = [n for n in self.notes if all(w in (n['title'] + ' ' + ' '.join(n['tags'] + n['headings'])).casefold() for w in words)]
        alfred = [n for n in notes if n['by_alfred']]
        others = [n for n in notes if not n['by_alfred']]
        items = []
        note_icon = icon_pixmap('note', 30, self.win.pal['accent'])
        for title, group in (('Saved by Alfred', alfred), ('Your vault', others)):
            if group:
                items.append(('header', title))
                items.extend(('row', dict(n, title=n['title'], pixmap=note_icon, right=n['modified'],
                                          subtitle=(' · '.join('#' + t for t in n['tags'][:4]) + ('   ' if n['tags'] else '') + n['snippet'])))
                             for n in group)
        self.list.set_items(items)
        ready = knowledge.vault_status(self.win.settings)['state'] == 'ready'
        if items:
            self.body.setCurrentIndex(1)
            return
        if not ready:
            self.empty.show_state('Connect your Obsidian vault', 'Alfred works fully without one. Connect a vault to save answers '
                                  'as linked Markdown notes and explore them in Graph view.', [('Choose vault…', self.win.choose_vault)], 'graph')
        elif words:
            self.empty.show_state('No matching notes', 'Try a different filter.', (), 'search')
        else:
            self.empty.show_state('No notes yet', 'Ask Alfred a question, then press “Save to Knowledge” to create your first linked note.',
                                  [('Ask Alfred', lambda: self.win.set_mode(ASK))], 'note')
        self.body.setCurrentIndex(0)

    def open_note(self, note):
        vault = knowledge.require_vault(self.win.settings)
        target = paths.resolve_within(vault, note['path'])
        QDesktopServices.openUrl(QUrl(knowledge.obsidian_uri(target)))
        self.win.hide_panel()

    def activate(self):
        if self.list.item():
            self.open_note(self.list.item())

    def open_in_obsidian(self):
        try:
            QDesktopServices.openUrl(QUrl(knowledge.vault_uri(self.win.settings)))
            self.win.hide_panel()
        except knowledge.VaultError as error:
            self.win.toast(str(error), 'orange')


class NoteSheet(QWidget):
    """Review step: nothing is written until the user presses Save."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 14, 26, 8)
        layout.setSpacing(8)
        top = QHBoxLayout()
        top.addWidget(label('Review knowledge note', 'title'))
        top.addStretch()
        top.addWidget(button('Cancel', lambda: self.win.set_mode(ASK)))
        top.addWidget(button('Save note', self.save, 'primary', 'Ctrl+S'))
        layout.addLayout(top)
        self.title = QLineEdit()
        self.title.textChanged.connect(self.update_destination)
        layout.addWidget(self.title)
        self.destination = label('', 'caption')
        layout.addWidget(self.destination)
        layout.addWidget(label('LINKS TO EXISTING NOTES', 'section'))
        self.links = QVBoxLayout()
        layout.addLayout(self.links)
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont('Cascadia Mono', 9))
        layout.addWidget(self.editor, 1)
        self.checks, self.turn, self.edited = [], None, False
        self.editor.textChanged.connect(lambda: setattr(self, 'edited', True))

    def load(self, turn):
        self.turn = turn
        result = turn.result
        title, _ = knowledge.draft_note(turn.question, result['answer'], result['citations'])
        self.title.blockSignals(True)
        self.title.setText(title)
        self.title.blockSignals(False)
        while self.links.count():
            item = self.links.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.checks = []
        suggestions = knowledge.suggest_links(self.win.settings, title, result['answer'] + ' ' + turn.question)
        if not suggestions:
            self.links.addWidget(label('No existing notes look genuinely related, so no links are proposed.', 'caption'))
        for suggestion in suggestions:
            check = QCheckBox(f"[[{suggestion['title']}]]  —  {suggestion['reason']}")
            check.setChecked(True)
            check.toggled.connect(self.regenerate)
            self.links.addWidget(check)
            self.checks.append((check, suggestion))
        self.edited = False
        self.regenerate()

    def regenerate(self):
        if self.edited and QMessageBox.question(self, 'Regenerate draft?', 'Changing links rebuilds the draft and discards manual edits. Continue?') != QMessageBox.Yes:
            return
        result = self.turn.result
        _, content = knowledge.draft_note(self.turn.question, result['answer'], result['citations'],
                                          [s for c, s in self.checks if c.isChecked()], self.title.text())
        self.editor.blockSignals(True)
        self.editor.setPlainText(content)
        self.editor.blockSignals(False)
        self.edited = False
        self.update_destination()

    def update_destination(self):
        try:
            _, _, rel, target = knowledge.note_path(self.win.settings, self.title.text())
            self.destination.setText(f"Saves to  {Path(self.win.settings['vault_path']).name}/{rel}" + ('   ·   ⚠ already exists' if target.exists() else ''))
        except knowledge.VaultError as error:
            self.destination.setText(str(error))

    def save(self):
        title, content = self.title.text().strip(), self.editor.toPlainText()
        if '\n# ' in content:
            content = content.replace(content.split('\n# ', 1)[1].split('\n', 1)[0], title, 1)
        try:
            saved = knowledge.save_note(self.win.settings, title, content)
        except knowledge.NoteExists as exists:
            box = QMessageBox(QMessageBox.Warning, 'Note already exists', f'{exists.rel} already exists. Replace it, or save as a new note?',
                              QMessageBox.Cancel, self)
            replace = box.addButton('Replace existing', QMessageBox.DestructiveRole)
            copy = box.addButton('Save as new note', QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is replace:
                saved = knowledge.save_note(self.win.settings, title, content, overwrite=True)
            elif box.clickedButton() is copy:
                saved = knowledge.save_note(self.win.settings, knowledge.unique_title(self.win.settings, title), content)
            else:
                return
        except (knowledge.VaultError, OSError) as error:
            QMessageBox.warning(self, 'Could not save note', str(error))
            return
        self.win.set_mode(ASK)
        self.win.toast(f"Saved {saved['path']}", 'green', ('Open in Obsidian', lambda: QDesktopServices.openUrl(QUrl(saved['uri']))), 8)


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

        g = Group(win, 'SEARCH FOLDER', 'Alfred searches exactly one folder. Changing it replaces the index on the next Rescan — folders are never combined.')
        self.root = label('', 'secondary', selectable=True)
        g.row('Folder', self.root)
        row = QHBoxLayout()
        row.addWidget(button('Choose…', self.win.choose_root))
        row.addWidget(button('Use demo_data', self.reset_root))
        row.addWidget(button('Rescan', lambda: self.win.start_index(), 'primary'))
        row.addWidget(button('Rebuild', lambda: self.win.start_index(force=True)))
        g.row('Index', row)
        self.auto_index = Toggle(pal)
        g.row('Index new files automatically', self.auto_index, 'Alfred watches the folder while it runs (also from the tray)')
        self.index_info = label('', 'caption', wrap=True, selectable=True)
        g.outer.addWidget(self.index_info)
        body.addWidget(g)

        g = Group(win, 'AI MODEL', 'Ollama runs models on this PC and uses your GPU when available. Claude runs on Anthropic’s servers with your API key. '
                                   'OpenAI-compatible URLs can be local (LM Studio, llama-server) or a cloud API.')
        self.provider = QComboBox()
        self.provider.addItems(['Not configured', 'Ollama (local)', 'OpenAI-compatible', 'Claude API (API key)', 'Claude (your Claude plan)'])
        self.provider.currentIndexChanged.connect(self.provider_changed)
        self.base_url, self.api_key = QLineEdit(), QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText('optional · stored in app data, never shown again')
        self.model = QComboBox()
        self.model.setEditable(True)
        self.device = QComboBox()
        self.device.addItems(['Auto (GPU when available)', 'CPU only'])
        for widget in (self.provider, self.base_url, self.model, self.api_key, self.device):
            widget.setMinimumWidth(300)
        g.row('Provider', self.provider)
        g.row('Server', self.base_url)
        models = QHBoxLayout()
        models.addWidget(self.model)
        models.addWidget(button('', self.refresh_models, None, 'Refresh models', icon('refresh', 14, QColor(140, 140, 150))))
        g.row('Model', models)
        g.row('API key', self.api_key)
        account = QHBoxLayout()
        self.account_status = label('', 'secondary')
        account.addWidget(self.account_status)
        account.addWidget(button('Sign in…', lambda: self.win.claude_sign_in(), None, 'Opens a window to sign in with your Claude account (subscription)'))
        account.addWidget(button('Check', lambda: self.check_account(refresh=True)))
        self.account_row = g.row('Claude account', account, 'Used by “Claude (your Claude plan)” — answers count toward your Claude plan, no API billing')
        g.row('Device', self.device)
        self.effort = QComboBox()
        self.effort.setMinimumWidth(300)
        self.effort_note = label('', 'caption', wrap=True)
        g.row('Thinking effort', self.effort)
        g.outer.addWidget(self.effort_note)
        self.autostart = Toggle(pal)
        self.web_search = Toggle(pal)
        g.row('Search the web when needed', self.web_search,
              'Claude only: when your files don’t cover a question, Claude searches the web and labels that part “From the web (not in your files)”')
        g.row('Start Ollama automatically', self.autostart,
              'When Alfred opens (shortcut or tray), start Ollama in the background if needed and pre-load the models')
        self.keep_minutes = QSpinBox()
        self.keep_minutes.setRange(5, 240)
        self.keep_minutes.setSuffix(' min')
        g.row('Keep models loaded', self.keep_minutes, 'After the last use; then Ollama unloads them and frees GPU memory')
        self.location = label('', 'caption', wrap=True)
        g.outer.addWidget(self.location)
        body.addWidget(g)

        g = Group(win, 'SEMANTIC SEARCH', 'EmbeddingGemma 2 finds passages by meaning. Keyword search always works.')
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

        g = Group(win, 'IMAGE UNDERSTANDING', 'A local vision model reads text in screenshots and charts and describes diagrams, photos, scanned PDF pages '
                                              'and what’s on screen in videos, so they become searchable. Descriptions are labelled as AI-generated.')
        self.vision_enabled = Toggle(pal)
        self.vision_model = QLineEdit()
        self.vision_model.setMinimumWidth(300)
        self.vision_device = QComboBox()
        self.vision_device.addItems(['Auto (GPU when available)', 'CPU only'])
        self.vision_device.setMinimumWidth(300)
        g.row('Read images', self.vision_enabled)
        g.row('Vision model', self.vision_model, 'e.g. gemma3:4b  ·  ollama pull gemma3:4b')
        g.row('Device', self.vision_device)
        self.video_interval = QSpinBox()
        self.video_interval.setRange(2, 120)
        self.video_interval.setSuffix(' s')
        g.row('Video: one frame every', self.video_interval,
              'Plus a frame at each scene change. Identical frames (a still slide) are described once. Long videos are capped at 120 moments.')
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

        g = Group(win, 'OBSIDIAN', 'Notes are written only inside <vault>/<notes folder>. The vault is never added to search.')
        self.vault = label('', 'secondary', selectable=True)
        vault_row = QHBoxLayout()
        vault_row.addWidget(self.vault)
        vault_row.addWidget(button('Choose…', self.win.choose_vault))
        g.row('Vault', vault_row)
        self.folder = QLineEdit()
        self.folder.setMinimumWidth(200)
        g.row('Notes folder', self.folder)
        body.addWidget(g)

        g = Group(win, 'CLAUDE CODE (MCP)', 'The repository ships .mcp.json — start Claude Code in the Alfred folder and approve “alfred”. It uses these settings and the same index.')
        row = QHBoxLayout()
        row.addWidget(button('Test MCP server', self.test_mcp))
        g.row('alfred_* tools', row)
        self.mcp_result = label('', 'caption', wrap=True)
        g.outer.addWidget(self.mcp_result)
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
              'Starts hidden in the tray, so the shortcut works right away; it also starts Ollama and loads the models')
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
        for widget in (self.base_url, self.api_key, self.embed_model, self.embed_url, self.vision_model, self.folder):
            widget.textEdited.connect(lambda _: self.save_timer.start(600))
        for widget in (self.provider, self.device, self.embed_device, self.vision_device, self.theme, self.effort):
            widget.currentIndexChanged.connect(lambda _: self.save_timer.start(250))
        self.model.currentTextChanged.connect(lambda _: self.save_timer.start(600))
        self.embed_batch.valueChanged.connect(lambda _: self.save_timer.start(600))
        self.video_interval.valueChanged.connect(lambda _: self.save_timer.start(800))
        self.keep_minutes.valueChanged.connect(lambda _: self.save_timer.start(600))
        for toggle in (self.embed_enabled, self.vision_enabled, self.reduce_motion, self.auto_index, self.autostart, self.web_search):
            toggle.toggled.connect(lambda _: self.save_timer.start(250))
        self.loading = False
        guard_wheel(self)

    def activated(self):
        self.loading = True
        s = self.win.settings
        for widget in self.findChildren(QWidget):
            widget.blockSignals(True)
        self.root.setText(str(s.root))
        self.provider.setCurrentIndex({'none': 0, 'ollama': 1, 'openai': 2, 'anthropic': 3, 'claude_code': 4}.get(s.get('provider'), 0))
        self.load_provider_fields()
        self.device.setCurrentIndex(1 if s.get('device') == 'cpu' else 0)
        self.embed_enabled.setChecked(bool(s.get('embed_enabled')))
        self.embed_model.setText(s.get('embed_model') or '')
        self.embed_url.setText(s.get('embed_base_url') or '')
        self.embed_device.setCurrentIndex(1 if s.get('embed_device') == 'cpu' else 0)
        self.embed_batch.setValue(int(s.get('embed_batch') or 16))
        self.vision_enabled.setChecked(bool(s.get('vision_enabled')))
        self.vision_model.setText(s.get('vision_model') or '')
        self.vision_device.setCurrentIndex(1 if s.get('vision_device') == 'cpu' else 0)
        self.video_interval.setValue(int(s.get('video_interval') or 10))
        self.vault.setText(s.get('vault_path') or 'Not set')
        self.folder.setText(s.get('notes_folder') or 'Alfred')
        self.theme.setCurrentIndex({'system': 0, 'dark': 1, 'light': 2}.get(s.get('theme'), 0))
        self.reduce_motion.setChecked(bool(s.get('reduce_motion')))
        self.auto_index.setChecked(bool(s.get('auto_index', True)))
        self.autostart.setChecked(bool(s.get('ollama_autostart', True)))
        self.web_search.setChecked(bool(s.get('web_search', True)))
        self.keep_minutes.setValue(max(5, int(s.get('keep_alive_minutes') or 5)))
        for widget in self.findChildren(QWidget):
            widget.blockSignals(False)
        self.provider_changed(save=False)
        self.load_effort()
        self.show_hotkey()
        run_job(lambda s: startup.enabled(), self.show_start_login)
        st = self.win.status
        self.index_info.setText(f"{st['files']} files indexed · {st['segments']} passages · {st['vectors']} embeddings · "
                                f"{st['errors']} failed · {st['unsupported']} unsupported · last indexed {st['last_indexed'] or 'never'}"
                                + ''.join(f"\n{p['status']}: {p['path']} — {p['error']}" for p in self.win.index.problems(6)))
        self.loading = False

    def load_provider_fields(self):
        """Show the saved server/model/key for the provider selected in the drop-down."""
        s, kind = self.win.settings, self.provider.currentIndex()
        for widget in (self.base_url, self.model, self.api_key):
            widget.blockSignals(True)
        if kind == 4:
            exe = ai.find_claude_code(s)
            self.base_url.setText(f'Claude Code · {exe}' if exe else 'Claude Code not found')
            self.model.clear()
            self.model.addItems([model for model, _ in ai.CLAUDE_PLAN_MODELS])
            self.model.setEditText(s.get('claude_code_model') or 'sonnet')
            self.api_key.setText('')
        elif kind == 3:
            self.base_url.setText(s.get('anthropic_base_url') or ai.ANTHROPIC_DEFAULT)
            self.model.clear()
            self.model.addItems([model for model, _ in ai.CLAUDE_MODELS])
            self.model.setEditText(s.get('claude_model') or 'claude-sonnet-5-5')
            self.api_key.setText(s.get('anthropic_api_key') or '')
        else:
            self.base_url.setText(s.get('base_url') or (ai.OLLAMA_DEFAULT if kind == 1 else ''))
            if self.model.findText('claude-opus-5-5') >= 0 or self.model.findText('sonnet') >= 0:
                self.model.clear()
            self.model.setEditText(s.get('model') or '')
            self.api_key.setText(s.get('api_key') or '')
        for widget in (self.base_url, self.model, self.api_key):
            widget.blockSignals(False)

    def check_account(self, refresh=False):
        settings = dict(self.win.settings)
        self.account_status.setText('Checking…')

        def done(status):
            if not status['installed']:
                self.account_status.setText('Claude Code not found')
            elif status['logged_in']:
                self.account_status.setText('✓ Signed in' + (f" ({status['method']})" if status.get('method') else ''))
            else:
                self.account_status.setText('Not signed in')
        run_job(lambda s: ai.claude_code_status(settings, refresh=refresh), done)

    def load_effort(self):
        settings = dict(self.win.settings)
        run_job(lambda s: ai.effort_options(settings),
                lambda value: fill_effort(self.effort, self.effort_note, value[0], value[1], settings.get('effort')))

    def provider_changed(self, *_, save=True):
        kind = self.provider.currentIndex()
        if save and not self.loading:
            self.load_provider_fields()
        self.base_url.setEnabled(kind in (1, 2))
        self.model.setEnabled(kind != 0)
        self.device.setEnabled(kind == 1)
        self.api_key.setEnabled(kind in (2, 3))
        self.account_row.setVisible(kind == 4)
        if kind == 4:
            self.check_account()
        self.api_key.setPlaceholderText('Anthropic API key from console.anthropic.com (or set ANTHROPIC_API_KEY)' if kind == 3
                                        else 'optional · stored in app data, never shown again')
        url = self.base_url.text()
        if kind == 0:
            self.location.setText('Ask Alfred is off until a model is connected. Search, indexing, Knowledge and MCP still work.')
        elif kind == 4:
            self.location.setText('☁  Claude answers through your Claude plan: Alfred runs Claude Code invisibly with no tools and sends only the passages '
                                  'it retrieved for your question to Anthropic, under your account. Usage counts toward your plan’s limits; no API billing. '
                                  'Search, embeddings and image reading stay on this PC.')
        elif kind == 3:
            has_key = bool(self.api_key.text().strip() or os.environ.get('ANTHROPIC_API_KEY'))
            self.location.setText('☁  Claude runs on Anthropic’s servers: the passages Alfred retrieves for each question are sent to Anthropic. '
                                  'Search, embeddings and image reading stay on this PC.' +
                                  ('' if has_key else '  An API key is needed (separate from a Claude.ai subscription).'))
        elif ai.is_local_url(url):
            self.location.setText('⌂  Local inference — questions and retrieved passages stay on this PC.')
        else:
            self.location.setText('☁  Remote inference — retrieved document excerpts are sent to this provider with each question.')

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

    def reset_root(self):
        self.win.settings['search_root'] = 'demo_data'
        self.win.settings.save()
        self.win.watch_root()
        self.win.refresh_status()
        self.activated()
        self.win.start_index()

    def save(self):
        if self.loading:
            return
        s = self.win.settings
        try:
            knowledge.notes_folder({'notes_folder': self.folder.text()})
        except knowledge.VaultError as error:
            self.win.toast(str(error), 'red')
            return
        before = (s.get('embed_enabled'), s.get('embed_model'), s.get('vision_enabled'), s.get('vision_model'), s.get('video_interval'))
        kind = ['none', 'ollama', 'openai', 'anthropic', 'claude_code'][self.provider.currentIndex()]
        before_model = (s.get('provider'), s.get('model'), s.get('claude_model'), s.get('claude_code_model'))
        if kind == 'claude_code':
            s.update(claude_code_model=self.model.currentText().strip() or 'sonnet')
        elif kind == 'anthropic':
            s.update(claude_model=self.model.currentText().strip() or 'claude-sonnet-5-5', anthropic_api_key=self.api_key.text().strip())
        else:
            s.update(base_url=self.base_url.text().strip(), model=self.model.currentText().strip(), api_key=self.api_key.text().strip())
        if self.effort.isEnabled() and self.effort.currentData() is not None:
            s['effort'] = self.effort.currentData()
        s.update({'provider': kind, 'device': 'cpu' if self.device.currentIndex() else 'auto',
                  'embed_enabled': self.embed_enabled.isChecked(), 'embed_model': self.embed_model.text().strip(),
                  'embed_base_url': self.embed_url.text().strip(), 'embed_device': 'cpu' if self.embed_device.currentIndex() else 'auto',
                  'embed_batch': self.embed_batch.value(), 'vision_enabled': self.vision_enabled.isChecked(),
                  'vision_model': self.vision_model.text().strip(), 'vision_device': 'cpu' if self.vision_device.currentIndex() else 'auto',
                  'video_interval': self.video_interval.value(),
                  'notes_folder': self.folder.text().strip() or 'Alfred', 'theme': ['system', 'dark', 'light'][self.theme.currentIndex()],
                  'reduce_motion': self.reduce_motion.isChecked(), 'auto_index': self.auto_index.isChecked(),
                  'ollama_autostart': self.autostart.isChecked(), 'keep_alive_minutes': self.keep_minutes.value(),
                  'web_search': self.web_search.isChecked()})
        s.save()
        self.win.watch_root()
        Motion.detect(s)
        self.win.apply_theme()
        self.win.refresh_status()
        if (s.get('provider'), s.get('model'), s.get('claude_model'), s.get('claude_code_model')) != before_model:
            self.load_effort()
            self.provider_changed(save=False)
        after = (s.get('embed_enabled'), s.get('embed_model'), s.get('vision_enabled'), s.get('vision_model'), s.get('video_interval'))
        if after != before:
            self.win.toast('Saved. Rescan to apply to your files.', 'green', ('Rescan now', self.win.start_index), 8)
        else:
            self.win.toast('Settings saved · shared with the MCP server', 'green', seconds=2)

    def refresh_models(self):
        settings = dict(self.win.settings)
        settings.update(provider=['none', 'ollama', 'openai', 'anthropic', 'claude_code'][self.provider.currentIndex()], base_url=self.base_url.text().strip(),
                        api_key=self.api_key.text().strip(), anthropic_api_key=self.api_key.text().strip())
        current = self.model.currentText()

        def done(models):
            self.model.blockSignals(True)
            self.model.clear()
            self.model.addItems(models)
            self.model.setEditText(current or (models[0] if models else ''))
            self.model.blockSignals(False)
            self.win.toast(f'{len(models)} models available' if models else 'Reachable, but no models installed (ollama pull llama3.2)',
                           'green' if models else 'orange')
        run_job(lambda s: ai.ChatProvider(settings).list_models(), done, lambda e: self.win.toast(str(e), 'red', seconds=8))

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
            for key, name in (('chat', 'Answers'), ('embeddings', 'Embeddings'), ('vision', 'Vision')):
                info = r.get(key, {})
                if (key == 'chat' and not info.get('configured')) or (key != 'chat' and not info.get('enabled')):
                    lines.append(f'**{name}:** off')
                    continue
                state = {True: 'GPU ACTIVE', False: 'CPU', None: 'device not confirmed'}[info.get('gpu_active')] if info.get('location') == 'local' else 'remote'
                lines.append(f"**{name}:** {info.get('model')} · **{state}** — {info.get('execution', '')}")
            lines += [f'**Error:** {e}' for e in r['errors']]
            self.diag.setText('  \n'.join(lines))
        run_job(lambda s: ai.diagnostics(settings, probe), done, lambda e: (self.win.thinking(False), self.diag.setText(f'Failed: {e}')))

    def test_mcp(self):
        self.mcp_result.setText('Starting the MCP server over stdio…')

        def work(s):
            sys.path.insert(0, str(REPO / 'tests'))
            from mcp_probe import session
            replies, _, code = session([('alfred_status', {})])
            return [t['name'] for t in replies[2]['result']['tools']], json.loads(replies[10]['result']['content'][0]['text'])

        run_job(work, lambda v: self.mcp_result.setText(f"✓ Connected · {len(v[0])} tools: {', '.join(v[0])} · sees {v[1]['index']['indexed_files']} indexed files (same index)"),
                lambda e: self.mcp_result.setText(f'MCP test failed: {e}'))


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
        self.status, self.root_files = self.index.status(), 0
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
        self.search.setGeometry(M + 56, M + (BAR - 40) // 2, W - 56 - ModeSwitch.SIZE * 4 - 34, 40)
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
        self.know_view, self.settings_view, self.note_sheet = KnowledgeView(self), SettingsView(self), NoteSheet(self)
        for view in (self.search_view, self.ask_view, self.know_view, self.settings_view, self.note_sheet):
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
        self.watch_root()
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
            {SEARCH: 'search', KNOW: 'graph', SETTINGS: 'gear', NOTE: 'bookmark'}.get(self.mode, 'search'), 22, self.pal['secondary']), QRectF())
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
        if mode == NOTE and previous != NOTE:
            self.saved_text = self.search.text()
        self.mode = mode
        self.modes.set(min(mode, SETTINGS) if mode != NOTE else ASK, animate)
        self.search.setPlaceholderText(PLACEHOLDERS[mode])
        self.search.setReadOnly(mode in (SETTINGS, NOTE))
        if mode in (SETTINGS, NOTE):
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
                 ASK: '↵ Ask   Ctrl+S Save   Ctrl+N New chat   Esc Close',
                 KNOW: '↑↓ Select   ↵ Open in Obsidian   Esc Back',
                 SETTINGS: 'Changes save automatically   Esc Back', NOTE: 'Ctrl+S Save note   Esc Cancel'}
        if not self.toasting:
            self.hints.setText(hints[self.mode])

    def text_changed(self, text):
        if self.mode == SEARCH:
            self.search_view.set_query(text)
        elif self.mode == KNOW:
            self.know_view.filter(text)

    def open_note_sheet(self, turn=None):
        turn = turn or self.ask_view.last_result_turn()
        if not turn:
            self.toast('Ask a question first, then save the answer.', 'orange')
            return
        status = knowledge.vault_status(self.settings)
        if status['state'] != 'ready':
            self.toast(status['message'], 'orange', ('Choose vault…', lambda: self.choose_vault() and self.open_note_sheet(turn)), 8)
            return
        self.note_sheet.load(turn)
        self.set_mode(NOTE)

    # ------------------------------------------------------------- keyboard
    def eventFilter(self, obj, event):
        if obj is self.search and event.type() == QEvent.KeyPress:
            key, mods = event.key(), event.modifiers()
            ctrl = bool(mods & Qt.ControlModifier)
            if key == Qt.Key_Escape:
                if self.mode == NOTE:
                    self.set_mode(ASK)
                elif self.search.text() and self.mode in (SEARCH, ASK, KNOW):
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
                self.set_mode(key - Qt.Key_1)
                return True
            if ctrl and key == Qt.Key_R:
                self.start_index()
                return True
            if ctrl and key == Qt.Key_N:
                self.set_mode(ASK)
                self.ask_view.reset()
                return True
            if ctrl and key == Qt.Key_S:
                self.note_sheet.save() if self.mode == NOTE else self.open_note_sheet()
                return True
            if key in (Qt.Key_Up, Qt.Key_Down):
                delta = -1 if key == Qt.Key_Up else 1
                if self.mode == SEARCH:
                    self.search_view.list.move(delta)
                elif self.mode == KNOW:
                    self.know_view.list.move(delta)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if self.mode == SEARCH:
                    self.search_view.reveal_current() if ctrl else self.search_view.activate()
                elif self.mode == ASK:
                    self.ask_view.send()
                elif self.mode == KNOW:
                    self.know_view.activate()
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
        if not st['root_exists']:
            return ('Search folder not found', str(self.settings.root),
                    [('Create folder', self.create_root), ('Choose another…', self.choose_root)], 'folder')
        if self.root_files == 0:
            return ('Your search folder is empty', f'Add PDFs, Office documents, text files or images to {self.settings.root.name}/, then Rescan.',
                    [('Open folder', lambda: open_path(self.settings.root)), ('Rescan', self.start_index)], 'folder')
        if st.get('stale_root'):
            return ('The index belongs to another folder', 'Rescan to index the configured folder.', [('Rescan now', self.start_index)], 'refresh')
        if not st['files'] and not self.indexing:
            return (f'{self.root_files} files waiting', 'Press Rescan to index them.', [('Rescan', self.start_index)], 'refresh')
        return None

    def warm_up(self, on_ready=None, retry=True):
        """Start Ollama if needed and pre-load the models; each open restarts their keep-alive timer."""
        if on_ready is not None:
            self.ready_callbacks = getattr(self, 'ready_callbacks', []) + [on_ready]
        if self.warming or not self.settings.get('ollama_autostart', True):
            return
        uses_ollama = self.settings.get('provider') == 'ollama' or (
            self.settings.get('embed_enabled') and self.settings.get('embed_provider', 'ollama') == 'ollama')
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

    def claude_sign_in(self):
        try:
            ai.claude_code_sign_in(self.settings)
            self.toast('A sign-in window opened — finish signing in with your Claude account there', 'green', seconds=10)
        except ai.ProviderError as error:
            self.toast(str(error), 'red', seconds=8)

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

    def watch_root(self):
        """(Re)start watching the search folder when auto-indexing is on."""
        if self.settings.get('auto_index', True) and self.settings.root.is_dir():
            self.watcher.set_root(self.settings.root)
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

    def create_root(self):
        self.settings.root.mkdir(parents=True, exist_ok=True)
        self.watch_root()
        self.refresh_status()
        self.search_view.show_problem_or_collapse()

    def refresh_status(self):
        self.status = self.index.status()
        self.root_files = self.index.root_file_count() if self.status['root_exists'] else 0
        if not self.toasting:
            self.status_label.setText(self.status_text())
        if self.mode == SEARCH:
            self.search_view.show_problem_or_collapse()

    def status_text(self):
        st, pal = self.status, self.pal
        dot = lambda color: f"<span style='color:{pal[color].name()}'>●</span>&nbsp; "
        if self.indexing:
            return dot('accent') + 'Indexing…'
        if not st['root_exists']:
            return dot('red') + 'Search folder missing'
        if not st['files']:
            return dot('orange') + f'Not indexed · {self.root_files} files in folder'
        info = ai.ChatProvider(self.settings).describe()
        model = (f" · {info.get('name', info['model'])} {'on this PC' if info.get('location') == 'local' else '(your Claude plan)' if info['provider'] == 'claude_code' else '(Claude API)' if info['provider'] == 'anthropic' else '(remote)'}"
                 if info['configured'] else ' · AI off')
        live = ' · watching folder' if self.watcher.active else ''
        return dot('green') + f"{st['files']} files · {st['vectors'] or 'no'} embeddings{model}{live}"

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

    def start_index(self, force=False, quiet=False, auto=False):
        if self.indexing:
            self.reindex_pending = self.reindex_pending or auto
            return
        if not self.settings.root.is_dir():
            self.toast('The search folder does not exist.', 'orange')
            return
        self.indexing, self.progress = True, None
        self.shimmer.start()
        self.status_label.setText(self.status_text())
        index, settings = self.index, self.settings

        def progress(state):
            total, done = state.get('total', 0), state.get('done', 0)
            self.progress = done / total if total else None
            phase = {'scanning': 'Scanning', 'indexing': 'Reading', 'embedding': 'Embedding', 'reading images': 'Reading images', 'reading video': 'Reading video',
                     'done': 'Finishing'}.get(state.get('phase'), state.get('phase'))
            if not self.toasting:
                self.status_label.setText(f"<span style='color:{self.pal['accent'].name()}'>●</span>&nbsp; {phase} {done}/{total}" +
                                          (f" · {html.escape(Path(state['current']).name)}" if state.get('current') else ''))
            self.update()

        def done(summary):
            self.indexing = False
            self.shimmer.stop()
            logging.getLogger('alfred').info('Indexed: %s', {k: summary.get(k) for k in ('found', 'indexed', 'removed', 'errors', 'embedded', 'warnings')})
            self.refresh_status()
            changed = summary['indexed'] + summary['removed'] + summary['errors']
            text = (f"Indexed {summary['indexed']} · {summary['unchanged']} unchanged · {summary['removed']} removed"
                    + (f" · {summary['errors']} failed" if summary['errors'] else '') + (f" · {summary['embedded']} embedded" if summary['embedded'] else ''))
            if summary['warnings']:
                text += ' · ' + summary['warnings'][0]
            color = 'orange' if summary['warnings'] or summary['errors'] else 'green'
            if auto and changed:
                parts = [f"{summary['indexed']} new or changed file{'s' * (summary['indexed'] != 1)} indexed"] if summary['indexed'] else []
                parts += [f"{summary['removed']} removed"] if summary['removed'] else []
                parts += [f"{summary['errors']} could not be read"] if summary['errors'] else []
                self.notify('Folder updated · ' + ' · '.join(parts), color)
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
            if auto:
                self.notify(f'Indexing failed: {error}', 'red')
            else:
                self.toast(f'Indexing failed: {error}', 'red', seconds=8)
            self.after_index()
        def work(signals):
            if settings.get('ollama_autostart', True):
                for enabled, url in ((settings.get('embed_enabled'), settings.get('embed_base_url')),
                                     (settings.get('vision_enabled'), settings.get('vision_base_url'))):
                    if enabled:
                        try:
                            ai.ensure_ollama(url)
                        except ai.ProviderError:
                            pass  # indexing still works; the summary reports what was skipped
            return (index.rebuild if force else index.update)(signals.progress.emit, ai.Embedder.from_settings(settings),
                                                                vision=ai.Vision.from_settings(settings))
        run_job(work, done, failed, progress=progress)

    def after_index(self):
        if self.reindex_pending:
            self.reindex_pending = False
            QTimer.singleShot(400, lambda: self.start_index(quiet=True, auto=True))

    def choose_root(self):
        folder = QFileDialog.getExistingDirectory(self, 'Choose the one folder Alfred should search', str(self.settings.root))
        if not folder:
            return
        resolved = Path(folder).resolve()
        home = Path.home().resolve()
        if resolved in [home, home / 'Desktop', home / 'Documents', home / 'Downloads', Path(resolved.anchor)]:
            QMessageBox.warning(self, 'Folder too broad', 'For privacy, Alfred will not index your whole home, Desktop, Documents, '
                                                          'Downloads or a drive root. Choose a dedicated folder.')
            return
        vault = (self.settings.get('vault_path') or '').strip()
        if vault and (paths.is_within(vault, resolved) or paths.is_within(resolved, vault)):
            QMessageBox.warning(self, 'Folder overlaps the vault', 'Keep the search folder and the Obsidian vault separate.')
            return
        try:
            stored = paths.relative_to_root(REPO, resolved)
        except ValueError:
            stored = str(resolved)
        self.settings['search_root'] = stored
        self.settings.save()
        self.watch_root()
        self.refresh_status()
        self.settings_view.activated()
        self.start_index()  # a new folder replaces the index

    def choose_vault(self):
        folder = QFileDialog.getExistingDirectory(self, 'Choose your Obsidian vault folder', self.settings.get('vault_path') or str(Path.home()))
        if not folder:
            return False
        if paths.is_within(self.settings.root, folder) or paths.is_within(folder, self.settings.root):
            QMessageBox.warning(self, 'Vault overlaps the search folder', 'Keep the Obsidian vault separate from the search folder.')
            return False
        self.settings['vault_path'] = str(Path(folder).resolve())
        self.settings.save()
        self.settings_view.activated()
        self.know_view.activated()
        status = knowledge.vault_status(self.settings)
        self.toast(status['message'], 'green' if status['state'] == 'ready' else 'orange')
        return status['state'] == 'ready'


# ------------------------------------------------------------------ app
INSTANCE = 'alfred-desktop-' + os.environ.get('USERNAME', 'user')


def run():
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
    probe = QLocalSocket()
    probe.connectToServer(INSTANCE)
    if probe.waitForConnected(300):
        probe.write(b'show')
        probe.flush()
        probe.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(INSTANCE)
    server = QLocalServer()
    server.listen(INSTANCE)
    window = AlfredWindow()
    startup.first_run(window.settings)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), window.show_panel()))

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
        # Started with Windows: stay hidden in the tray. Give Windows, the GPU driver and Ollama's own
        # start-up a moment, then make sure Ollama runs and the models are loaded, then catch up the index.
        logging.getLogger('alfred').info('Started at sign-in (hidden in the tray)')
        QTimer.singleShot(20000, window.warm_up)
        QTimer.singleShot(25000, lambda: window.start_index(quiet=True))
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
