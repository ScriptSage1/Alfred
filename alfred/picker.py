"""The model picker: a frosted popover listing every provider's models with its logo, and the
button that opens it. Both are painted by hand so they match the panel exactly and animate
with the same springs (open/close, the gliding highlight, the chevron, the label cross-fade).
"""
from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import providers
from .ui_kit import Motion, Spring, brand_pixmap, icon_pixmap, paint_shadow

LOGOS = {'ollama': 'ollama', 'claude_code': 'claude', 'anthropic': 'anthropic', 'codex': 'openai', 'openai': 'openai', 'compatible': 'server'}
EXPERIMENTAL = {'claude_code', 'codex'}  # subscription routes through CLIs
SUBTITLES = {'claude_code': 'Your Claude plan', 'codex': 'Your ChatGPT plan', 'anthropic': 'Claude API', 'openai': 'OpenAI API',
             'compatible': 'Custom server'}
NOTES = {'missing': 'Not installed', 'signed_out': 'Sign in under Settings → Accounts', 'no_key': 'Add a key under Settings → Accounts',
         'unreachable': 'Unavailable right now'}
ROW, HEADER, NOTE, GAP, PAD, RADIUS, SHADOW = 44, 28, 42, 9, 6, 14, 22


def model_items(entries, current, efforts=(), effort=None):
    """Picker rows from providers.catalog(): headers, selectable models, quiet notes for providers
    that aren't ready, then the Thinking choices and a link to Settings."""
    items, unavailable = [], []
    for entry in entries:
        kind, state = entry['kind'], entry['status']['state']
        if state != 'ready' or not entry['models']:
            note = NOTES.get(state, 'No models found') if state != 'ready' else (
                'No chat models yet — ollama pull gemma3:4b' if kind == 'ollama' else 'No models found')
            if kind == 'compatible' and state == 'no_key':
                note = 'Add its address under Settings → Accounts'
            unavailable.append({'type': 'note', 'kind': kind, 'title': entry['group'], 'subtitle': note})
            continue
        items.append({'type': 'header', 'title': entry['group'], 'cloud': entry['cloud']})
        sizes = {m['model']: m for m in entry.get('details', [])}
        for model, label in entry['models']:
            detail = sizes.get(model)
            subtitle = (f"{detail['size_gb']} GB" + (' · reads images' if detail.get('vision') else '')) if detail else SUBTITLES.get(kind, '')
            items.append({'type': 'row', 'kind': kind, 'model': model, 'title': label, 'subtitle': subtitle, 'cloud': entry['cloud'],
                          'badge': 'Experimental' if kind in EXPERIMENTAL else '', 'selected': (kind, model) == current})
    if unavailable:
        items.append({'type': 'header', 'title': 'Not set up'})
        items += unavailable
    if efforts:
        items.append({'type': 'header', 'title': 'Thinking'})
        items += [{'type': 'row', 'action': 'effort', 'value': value, 'title': text, 'selected': value == effort} for value, text in efforts]
    items.append({'type': 'gap'})
    items.append({'type': 'row', 'action': 'settings', 'title': 'Accounts and models…', 'icon': 'gear'})
    return items


def catalog_with_details(settings):
    """providers.catalog plus Ollama's size / vision details for the subtitles."""
    from . import ai
    entries = providers.catalog(settings)
    local = {m['model']: m for m in ai.available_models(settings)}
    for entry in entries:
        if entry['kind'] == 'ollama':
            entry['details'] = [local[m] for m, _ in entry['models'] if m in local]
    return entries


def place(right, bottom, top, width, height, bounds=None, gap=6):
    """Top-left of the popover window (which includes a SHADOW margin on every side): right-aligned under
    the anchor, flipped above it when there's no room below, and kept inside ``bounds`` (left, top, right,
    bottom) of the anchor's screen — which may sit at negative coordinates left of or above the primary one."""
    x = right - width + SHADOW
    y = bottom + gap - SHADOW
    if bounds is None:
        return x, y
    left_edge, top_edge, right_edge, bottom_edge = bounds
    if y + height - SHADOW > bottom_edge and top - gap - (height - SHADOW) >= top_edge:
        y = top - gap - height + SHADOW  # open upward
    x = max(left_edge - SHADOW, min(x, right_edge + 1 - width + SHADOW))
    y = max(top_edge - SHADOW, min(y, bottom_edge + 1 - height + SHADOW))
    return x, y


class Popover(QWidget):
    """Frosted popover that scales and fades in from its anchor; keyboard and mouse driven."""
    chosen = Signal(dict)
    closed = Signal()
    WIDTH = 340

    def __init__(self, win, items):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.win, self.items = win, items
        self.rects, y = [], PAD
        for item in items:
            height = {'row': ROW if item.get('subtitle') else 36, 'header': HEADER, 'note': NOTE, 'gap': GAP}[item['type']]
            self.rects.append(QRectF(PAD, y, self.WIDTH - 2 * PAD, height))
            y += height
        self.content_height = y + PAD
        self.setFixedSize(self.WIDTH + 2 * SHADOW, int(self.content_height) + 2 * SHADOW)
        self.index = next((i for i, it in enumerate(items) if it.get('selected') and not it.get('action')), self._step(-1, 1))
        rect = self.rects[self.index] if self.index is not None else QRectF()
        self.hy, self.hh = Spring(rect.top(), 520, 0.84, lambda v: self.update()), Spring(rect.height(), 520, 0.84, lambda v: self.update())
        self.appear = Spring(0, 420, 0.82, self._appear)
        self.leaving = False

    # ---------------------------------------------------------------- show
    def open_at(self, anchor):
        """Show under ``anchor`` (a widget), on the screen that widget is on — never just the primary one."""
        bottom_right = anchor.mapToGlobal(QPoint(anchor.width(), anchor.height()))
        top = anchor.mapToGlobal(QPoint(0, 0)).y()
        target = QGuiApplication.screenAt(bottom_right - QPoint(4, 4)) or anchor.screen() or self.win.screen()
        if target is not None:
            self.setScreen(target)
        area = target.availableGeometry() if target is not None else None
        bounds = (area.left(), area.top(), area.right(), area.bottom()) if area is not None else None
        x, y = place(bottom_right.x(), bottom_right.y(), top, self.width(), self.height(), bounds)
        self.move(int(x), int(y))
        self.setWindowOpacity(0.0 if not Motion.reduced else 1.0)
        self.show()
        self.setFocus()
        self.appear.snap(0)
        self.appear.to(1)

    def _appear(self, value):
        try:
            self.setWindowOpacity(max(0.0, min(1.0, value)))
            self.update()
            if self.leaving and value < 0.03:
                self.leaving = False
                self.close()
        except RuntimeError:
            pass

    def dismiss(self):
        if self.leaving:
            return
        self.leaving = True
        if Motion.reduced:
            self.close()
        else:
            self.appear.to(0)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    # ----------------------------------------------------------- interact
    def _selectable(self, i):
        item = self.items[i]
        return item['type'] == 'row'

    def _step(self, start, delta):
        i = start + delta
        while 0 <= i < len(self.items):
            if self._selectable(i):
                return i
            i += delta
        return None

    def highlight(self, i, animate=True):
        if i is None or i == self.index:
            return
        self.index = i
        rect = self.rects[i]
        (self.hy.to if animate else self.hy.snap)(rect.top())
        (self.hh.to if animate else self.hh.snap)(rect.height())

    def _at(self, pos):
        point = QPointF(pos) - QPointF(SHADOW, SHADOW)
        return next((i for i, r in enumerate(self.rects) if r.contains(point) and self._selectable(i)), None)

    def mouseMoveEvent(self, event):
        hit = self._at(event.position())
        if hit is not None:
            self.highlight(hit)

    def mousePressEvent(self, event):
        if not QRectF(SHADOW, SHADOW, self.WIDTH, self.content_height).contains(event.position()):
            self.dismiss()
            return
        hit = self._at(event.position())
        if hit is not None:
            self.pick(hit)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Down, Qt.Key_Tab):
            self.highlight(self._step(self.index if self.index is not None else -1, 1))
        elif key in (Qt.Key_Up, Qt.Key_Backtab):
            self.highlight(self._step(self.index if self.index is not None else len(self.items), -1))
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space) and self.index is not None:
            self.pick(self.index)
        elif key == Qt.Key_Escape:
            self.dismiss()
        else:
            super().keyPressEvent(event)

    def pick(self, i):
        item = self.items[i]
        for other in self.items:
            if other['type'] == 'row' and other.get('action') == item.get('action'):
                other['selected'] = other is item
        self.update()
        self.chosen.emit(item)
        self.dismiss()

    # --------------------------------------------------------------- paint
    def paintEvent(self, event):
        pal = self.win.pal
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        v = max(0.0, min(1.0, self.appear.value))
        # Grow from the top-right corner (where the button is) and settle with the spring.
        scale = 0.94 + 0.06 * v
        p.translate(SHADOW + self.WIDTH, SHADOW)
        p.scale(scale, scale)
        p.translate(-(SHADOW + self.WIDTH), -SHADOW + (1 - v) * -6)
        panel = QRectF(SHADOW, SHADOW, self.WIDTH, self.content_height)
        paint_shadow(p, panel, RADIUS, pal['shadow'], spread=18)
        path = QPainterPath()
        path.addRoundedRect(panel, RADIUS, RADIUS)
        fill = QColor(pal['tint'])
        fill.setAlpha(250)
        p.fillPath(path, fill)
        p.setPen(QPen(pal['rim_top'], 0.8))
        p.drawRoundedRect(panel.adjusted(0.4, 0.4, -0.4, -0.4), RADIUS, RADIUS)
        p.translate(SHADOW, SHADOW)
        if self.index is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(pal['accent'] if self.hasFocus() or self.underMouse() else pal['fill_strong'])
            p.drawRoundedRect(QRectF(PAD, self.hy.value, self.WIDTH - 2 * PAD, self.hh.value), 9, 9)
        title_font, small = QFont(self.font()), QFont(self.font())
        title_font.setPointSizeF(10)
        small.setPointSizeF(8.5)
        header_font = QFont(small)
        header_font.setWeight(QFont.DemiBold)
        header_font.setCapitalization(QFont.AllUppercase)
        header_font.setLetterSpacing(QFont.PercentageSpacing, 104)
        for number, (item, rect) in enumerate(zip(self.items, self.rects)):
            # Rows fade in one after another as the popover opens.
            p.setOpacity(max(0.0, min(1.0, (v - number * 0.025) / 0.55)) if not Motion.reduced else 1.0)
            lit = number == self.index and abs(self.hy.value - rect.top()) < rect.height() / 2
            text = pal['accent_text'] if lit and (self.hasFocus() or self.underMouse()) else pal['text']
            sub = QColor(text) if text is pal['accent_text'] else pal['secondary']
            if text is pal['accent_text']:
                sub.setAlphaF(0.78)
            kind = item['type']
            if kind == 'header':
                p.setFont(header_font)
                p.setPen(pal['secondary'])
                p.drawText(rect.adjusted(10, 6, -10, 0), Qt.AlignLeft | Qt.AlignVCenter, item['title'])
                if 'cloud' in item and item['title'] != 'On this PC':
                    p.drawText(rect.adjusted(10, 6, -10, 0), Qt.AlignRight | Qt.AlignVCenter, 'Cloud' if item['cloud'] else 'On this PC')
                continue
            if kind == 'gap':
                p.setPen(QPen(pal['separator'], 1))
                p.drawLine(QPointF(rect.left() + 8, rect.center().y()), QPointF(rect.right() - 8, rect.center().y()))
                continue
            left = rect.left() + 10
            glyph = item.get('icon')
            logo = LOGOS.get(item.get('kind'))
            if glyph or logo:
                pix = icon_pixmap(glyph, 16, text) if glyph else brand_pixmap(logo, 20, text)
                size = 16 if glyph else 20
                faded = kind == 'note'
                if faded:
                    p.setOpacity(p.opacity() * 0.45)
                p.drawPixmap(QRectF(left, rect.center().y() - size / 2, size, size), pix, QRectF())
                if faded:
                    p.setOpacity(p.opacity() / 0.45)
                left += size + 10
            elif item.get('action') == 'effort':
                left += 30
            right = rect.right() - 10
            if item.get('selected'):
                p.drawPixmap(QRectF(right - 14, rect.center().y() - 7, 14, 14), icon_pixmap('check', 14, text), QRectF())
                right -= 22
            p.setFont(title_font)
            metrics = QFontMetrics(title_font)
            title = item['title']
            badge = item.get('badge')
            badge_width = QFontMetrics(small).horizontalAdvance(badge) + 14 if badge else 0
            title_width = min(metrics.horizontalAdvance(title) + 3, right - left - badge_width - 8)
            p.setPen(text if kind != 'note' else pal['secondary'])
            two_lines = bool(item.get('subtitle'))
            title_rect = QRectF(left, rect.top() + (6 if two_lines else 0), title_width, 20 if two_lines else rect.height())
            p.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, metrics.elidedText(title, Qt.ElideRight, int(title_width) + 1))
            if badge:
                bx = left + title_width + 7
                badge_rect = QRectF(bx, title_rect.center().y() - 8, badge_width, 16)
                p.setPen(Qt.NoPen)
                tag = QColor(sub)
                tag.setAlphaF(0.16)
                p.setBrush(tag)
                p.drawRoundedRect(badge_rect, 8, 8)
                p.setFont(small)
                p.setPen(sub)
                p.drawText(badge_rect, Qt.AlignCenter, badge)
            if two_lines:
                p.setFont(small)
                p.setPen(sub if kind != 'note' else pal['tertiary'])
                p.drawText(QRectF(left, rect.top() + 24, right - left, 16), Qt.AlignLeft | Qt.AlignVCenter,
                           QFontMetrics(small).elidedText(item['subtitle'], Qt.ElideRight, int(right - left)))
        p.end()


class ModelButton(QWidget):
    """Rounded button showing the current model's logo and name; the chevron turns while the picker is
    open, and a new choice cross-fades in."""
    clicked = Signal()

    def __init__(self, win, compact=True):
        super().__init__()
        self.win, self.compact = win, compact
        self.kind, self.text = None, 'Choose a model'
        self.old_kind, self.old_text = None, ''
        self.hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedHeight(30)
        self.fade = Spring(1, 300, 1.0, lambda v: self.update())
        self.turn = Spring(0, 360, 0.8, lambda v: self.update())
        self._resize()

    def _resize(self):
        metrics = QFontMetrics(self.font())
        width = metrics.horizontalAdvance(self.text) + (22 if self.kind else 0) + 52
        if self.compact:
            self.setFixedWidth(min(260, width))
        else:
            self.setMinimumWidth(min(320, width))

    def sizeHint(self):
        return QSize(self.minimumWidth() or self.width(), 30)

    def set_model(self, kind, text, animate=True):
        if (kind, text) == (self.kind, self.text):
            return
        self.old_kind, self.old_text = self.kind, self.text
        self.kind, self.text = kind, text
        self._resize()
        if animate and not Motion.reduced:
            self.fade.snap(0)
            self.fade.to(1)
        else:
            self.fade.snap(1)
        self.setToolTip(text)

    def set_open(self, on):
        self.turn.to(180 if on else 0)

    def enterEvent(self, event):
        self.hover = True
        self.update()

    def leaveEvent(self, event):
        self.hover = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Down):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)

    def anchor(self):
        return self.mapToGlobal(QPoint(self.width(), self.height()))

    def paintEvent(self, event):
        pal = self.win.pal
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(pal['accent'] if self.hasFocus() else pal['separator'], 1))
        p.setBrush(pal['fill_strong'] if self.hover else pal['fill'])
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        v = max(0.0, min(1.0, self.fade.value))
        for kind, text, alpha in ((self.old_kind, self.old_text, 1 - v), (self.kind, self.text, v)):
            if alpha <= 0.01 or not text:
                continue
            p.setOpacity(alpha)
            x = 12
            if kind:
                p.drawPixmap(QRectF(x, (self.height() - 16) / 2, 16, 16), brand_pixmap(LOGOS[kind], 16, pal['text']), QRectF())
                x += 22
            p.setPen(pal['text'])
            space = self.width() - x - 30
            p.drawText(QRectF(x, 0, space, self.height()), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(self.font()).elidedText(text, Qt.ElideRight, int(space)))
        p.setOpacity(1)
        p.translate(self.width() - 18, self.height() / 2)
        p.rotate(self.turn.value)
        p.drawPixmap(QRectF(-6, -6, 12, 12), icon_pixmap('chevron_down', 12, pal['secondary']), QRectF())
        p.end()
