#!/usr/bin/env python
"""The "Zoom" tab: shows an entry's image with outlines where its zoom-ins
(see zoomIndex.py) are, and animates into a zoom when one is clicked.

Coordinates: each image's "scene" has its height as 1 unit and its width as
its aspect ratio, y pointing down. The camera maps scene to widget pixels as
a complex similarity w = a*z + b (scale + rotation + translation in one), so
cameras can be composed, inverted and interpolated with plain complex maths.
Only the current image and its immediate parent/children are ever drawn, in
the current image's own coordinates, which is what keeps extremely deep
zooms (magn ~1e27) numerically fine.

Zoom animation interpolates the camera along K^t, where K is the similarity
taking the start camera to the end one: a constant-speed zoom (in log scale)
about K's fixed point - i.e. it looks like zooming into that spot.
"""

import cmath
import os
from collections import OrderedDict

from PyQt6.QtWidgets import QWidget
from PyQt6.QtWidgets import QVBoxLayout
from PyQt6.QtWidgets import QHBoxLayout
from PyQt6.QtWidgets import QLabel
from PyQt6.QtWidgets import QComboBox
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtWidgets import QListWidget
from PyQt6.QtWidgets import QListWidgetItem
from PyQt6.QtWidgets import QSplitter
from PyQt6.QtWidgets import QSizePolicy
from PyQt6.QtWidgets import QToolTip

from PyQt6.QtGui import QPainter
from PyQt6.QtGui import QPixmap
from PyQt6.QtGui import QImage
from PyQt6.QtGui import QImageReader
from PyQt6.QtGui import QTransform
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QPen
from PyQt6.QtGui import QBrush
from PyQt6.QtGui import QPolygonF
from PyQt6.QtGui import QFont
from PyQt6.QtGui import QIcon

from PyQt6.QtCore import Qt
from PyQt6.QtCore import QObject
from PyQt6.QtCore import QRunnable
from PyQt6.QtCore import QThreadPool
from PyQt6.QtCore import QVariantAnimation
from PyQt6.QtCore import QTimer
from PyQt6.QtCore import QEasingCurve
from PyQt6.QtCore import QPointF
from PyQt6.QtCore import QRectF
from PyQt6.QtCore import QSize
from PyQt6.QtCore import pyqtSignal

from zoomIndex import TIER_EXACT, TIER_RECOLOR, TIER_VARIANT, TIER_NAMES

TIER_COLORS = {
    TIER_EXACT: QColor(102, 187, 106),
    TIER_RECOLOR: QColor(255, 202, 40),
    TIER_VARIANT: QColor(239, 83, 80),
}
TIER_CHOICES = [
    (TIER_EXACT, "Exact zooms only"),
    (TIER_RECOLOR, "Exact + recolored (same fractal)"),
    # Checked against the renders, variant-tier "zooms" matched no better
    # than chance - the formula parameters differ enough to change the
    # picture - so they're opt-in and labelled as such.
    (TIER_VARIANT, "+ variants (same formula; often not a true zoom)"),
]
DEFAULT_MAX_TIER = TIER_RECOLOR

# Images are only ever shown at roughly screen size, so load them no bigger
# than this, and keep this many around.
MAX_IMAGE_SIDE = 2048
PIXMAP_CACHE_SIZE = 24
# Biggest render to fall back on when an entry has no top-level (2k) image.
MAX_FALLBACK_RENDER_SIDE = 4096

CONTEXT_OPACITY = 0.35

# Zooming waits (at most this long) for the target image to load, so the
# animation doesn't arrive at a grey placeholder.
LOAD_WAIT_MS = 4000
# Thread pool priority for an image that's being waited on, so it jumps
# ahead of queued preloads.
URGENT_LOAD_PRIORITY = 10


def imagePathForEntry(entry):
    """A displayable-size image for an entry: its matched top-level image,
    else its biggest render that isn't huge (never an 8k/16k)."""
    if entry is None:
        return None
    if entry.get('image_path'):
        return entry['image_path']
    for render in entry.get('renders', []):
        if max(render['width'] or 0, render['height'] or 0) <= MAX_FALLBACK_RENDER_SIDE:
            return render['path']
    return None


def toQTransform(a, b):
    """QTransform for the complex similarity w = a*z + b."""
    return QTransform(a.real, a.imag, -a.imag, a.real, b.real, b.imag)


def smoothstep(edge0, edge1, x):
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3 - 2 * t)


class _LoaderSignals(QObject):
    loaded = pyqtSignal(object, QImage)


class _ImageLoadTask(QRunnable):
    def __init__(self, key, path, signals):
        super().__init__()
        self.key = key
        self.path = path
        self.signals = signals

    def run(self):
        # QImage (unlike QPixmap) is safe to create off the GUI thread.
        reader = QImageReader(self.path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and max(size.width(), size.height()) > MAX_IMAGE_SIDE:
            reader.setScaledSize(size.scaled(MAX_IMAGE_SIDE, MAX_IMAGE_SIDE, Qt.AspectRatioMode.KeepAspectRatio))
        self.signals.loaded.emit(self.key, reader.read())


class ZoomCanvas(QWidget):
    """Draws the current entry's image, its zoom outlines and the zoom
    animations. Navigation: click an outline to zoom in, right-click or
    Backspace to zoom out, wheel to zoom freely, drag to pan, double-click
    to refit."""

    currentChanged = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.zoom_index = None
        self.entries = {}
        self.max_tier = DEFAULT_MAX_TIER
        self.message = "No zoom index yet."

        self.current = None
        self.camera = (1 + 0j, 0j)
        self.user_moved_camera = False
        self.hovered = None

        self.pixmaps = OrderedDict()
        self.pending_loads = {}
        self.loader_signals = _LoaderSignals()
        self.loader_signals.loaded.connect(self._onImageLoaded)
        self.thread_pool = QThreadPool(self)

        self.animation = QVariantAnimation(self)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.animation.valueChanged.connect(self._onAnimationStep)
        self.animation.finished.connect(self._onAnimationFinished)
        self.anim = None

        # (key, action) while a zoom waits for its image to load.
        self.waiting = None
        self.wait_timer = QTimer(self)
        self.wait_timer.setSingleShot(True)
        self.wait_timer.timeout.connect(self._finishWaiting)

        self._press_pos = None
        self._dragged = False

    # ---------- data ----------

    def setData(self, zoom_index, entries):
        """entries: { (upr_file, upr_name): entry_dict } for the whole library."""
        self.zoom_index = zoom_index
        self.entries = entries
        if self.current is not None and zoom_index is not None and self.current not in zoom_index.views:
            self.current = None
        self.update()

    def setMessage(self, message):
        self.message = message
        self.update()

    def setMaxTier(self, max_tier):
        self.max_tier = max_tier
        self.hovered = None
        self._preload()
        self.update()
        self.currentChanged.emit(self.current)

    def hasView(self, key):
        return self.zoom_index is not None and key in self.zoom_index.views

    def aspect(self, key):
        return self.zoom_index.views[key].aspect

    def children(self):
        if self.current is None:
            return []
        return self.zoom_index.childrenOf(self.current, self.max_tier)

    def parentLink(self, key=None):
        key = self.current if key is None else key
        if key is None or self.zoom_index is None:
            return None
        return self.zoom_index.parentOf(key, self.max_tier)

    # ---------- geometry ----------

    def placement(self, parent_key, child_key):
        """(p, q): the child's scene -> the parent's scene, z -> p*z + q."""
        corners = self.zoom_index.placement(parent_key, child_key)
        parent_aspect = self.aspect(parent_key)
        origin = complex(corners[0][0] * parent_aspect, corners[0][1])
        down = complex(corners[2][0] * parent_aspect, corners[2][1])
        # The child's (0,1) corner is 1j in its own scene.
        return (down - origin) / 1j, origin

    def childPolygon(self, child_key, camera):
        a, b = camera
        parent_aspect = self.aspect(self.current)
        corners = self.zoom_index.placement(self.current, child_key)
        points = []
        for u, v in (corners[0], corners[1], corners[3], corners[2]):
            w = a * complex(u * parent_aspect, v) + b
            points.append(QPointF(w.real, w.imag))
        return QPolygonF(points)

    def fitCamera(self, key):
        aspect = self.aspect(key)
        scale = min(self.width() / aspect, self.height()) * 0.97
        return (complex(scale, 0), complex((self.width() - scale * aspect) / 2, (self.height() - scale) / 2))

    # ---------- navigation ----------

    def showEntry(self, key):
        """Jumps (without animating) to an entry."""
        if not self.hasView(key):
            return False
        self.animation.stop()
        self.anim = None
        self._cancelWaiting()
        self.current = key
        self.camera = self.fitCamera(key)
        self.user_moved_camera = False
        self.hovered = None
        self._preload()
        self.update()
        self.currentChanged.emit(key)
        return True

    def busy(self):
        return self.anim is not None or self.waiting is not None

    def zoomInto(self, child_key):
        if self.current is None or self.busy():
            return
        self._waitForImage(child_key, lambda: self._zoomIntoNow(child_key))

    def _zoomIntoNow(self, child_key):
        p, q = self.placement(self.current, child_key)
        a0, b0 = self.camera
        f, g = self.fitCamera(child_key)
        # Target: the child's fit camera, expressed in the current scene.
        a1, b1 = f / p, g - f * q / p
        self._startAnimation('in', child_key, (p, q), (a0, b0), (a1, b1))

    def zoomOut(self):
        if self.current is None or self.busy():
            return
        link = self.parentLink()
        if link is None:
            self._zoomOutNow()
        else:
            self._waitForImage(link[0], self._zoomOutNow)

    def _zoomOutNow(self):
        link = self.parentLink()
        if link is None:
            # Nothing further out - just animate back to fitting the image.
            self._startAnimation('fit', None, None, self.camera, self.fitCamera(self.current))
            return
        parent_key = link[0]
        child_key = self.current
        p, q = self.placement(parent_key, child_key)
        a, b = self.camera
        # Re-root on the parent straight away, with the same on-screen view
        # expressed in the parent's scene, then animate out to fitting it.
        self.current = parent_key
        self.currentChanged.emit(parent_key)
        self._startAnimation('out', child_key, (p, q), (a / p, b - a * q / p), self.fitCamera(parent_key))
        self._preload()

    def _startAnimation(self, kind, child_key, placement, start, end):
        (a0, b0), (a1, b1) = start, end
        k = a1 / a0
        self.anim = { 'kind': kind, 'child': child_key, 'placement': placement,
                      'a0': a0, 'b0': b0, 'k': k, 'log_k': cmath.log(k), 'm': b1 - k * b0, 't': 0.0 }
        # Longer for bigger zooms, but never sluggish.
        doublings = abs(cmath.log(k).real) / 0.693
        self.animation.setDuration(int(min(2200, 450 + 220 * doublings)))
        self.hovered = None
        self.animation.start()

    @staticmethod
    def _animationCamera(anim, t):
        k_t = cmath.exp(t * anim['log_k'])
        if abs(anim['k'] - 1) > 1e-12:
            offset = anim['m'] * (k_t - 1) / (anim['k'] - 1)
        else:
            offset = anim['m'] * t
        return (k_t * anim['a0'], k_t * anim['b0'] + offset)

    def _onAnimationStep(self, value):
        if self.anim is None:
            return
        self.anim['t'] = value
        self.camera = self._animationCamera(self.anim, value)
        self.update()

    def _onAnimationFinished(self):
        anim = self.anim
        self.anim = None
        if anim is None:
            return
        if anim['kind'] == 'in':
            # Re-root on the child; its fit camera is exactly where we ended.
            self.current = anim['child']
            self.camera = self.fitCamera(self.current)
            self.currentChanged.emit(self.current)
            self._preload()
        else:
            self.camera = self._animationCamera(anim, 1.0)
        self.user_moved_camera = False
        self.update()

    def _waitForImage(self, key, action):
        """Runs action once key's image is loaded (or can't be, or after
        LOAD_WAIT_MS), straight away if it already is."""
        self._requestImage(key, urgent=True)
        if key not in self.pending_loads:
            action()
            return
        self.waiting = (key, action)
        # Keep the clicked outline highlighted while it loads.
        self.hovered = key
        self.wait_timer.start(LOAD_WAIT_MS)
        self.setCursor(Qt.CursorShape.BusyCursor)
        self.update()

    def _finishWaiting(self):
        if self.waiting is None:
            return
        _key, action = self.waiting
        self._cancelWaiting()
        action()

    def _cancelWaiting(self):
        self.waiting = None
        self.wait_timer.stop()
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # ---------- images ----------

    def _requestImage(self, key, urgent=False):
        if key in self.pixmaps:
            self.pixmaps.move_to_end(key)
            return
        if key in self.pending_loads:
            # Queued behind preloads? Move it to the front.
            task = self.pending_loads[key]
            if urgent and self.thread_pool.tryTake(task):
                self.thread_pool.start(task, URGENT_LOAD_PRIORITY)
            return
        path = imagePathForEntry(self.entries.get(key))
        if not path or not os.path.isfile(path):
            return
        task = _ImageLoadTask(key, path, self.loader_signals)
        # Kept alive by pending_loads instead, so tryTake above is safe.
        task.setAutoDelete(False)
        self.pending_loads[key] = task
        self.thread_pool.start(task, URGENT_LOAD_PRIORITY if urgent else 0)

    def _preload(self):
        if self.current is None:
            return
        self._requestImage(self.current)
        link = self.parentLink()
        if link:
            self._requestImage(link[0])
        for child_key, _tier in self.children()[:PIXMAP_CACHE_SIZE // 2]:
            self._requestImage(child_key)

    def _onImageLoaded(self, key, image):
        self.pending_loads.pop(key, None)
        if not image.isNull():
            self.pixmaps[key] = QPixmap.fromImage(image)
            self.pixmaps.move_to_end(key)
            while len(self.pixmaps) > PIXMAP_CACHE_SIZE:
                self.pixmaps.popitem(last=False)
            self.update()
        if self.waiting is not None and self.waiting[0] == key:
            self._finishWaiting()

    def hasImage(self, key):
        return imagePathForEntry(self.entries.get(key)) is not None

    # ---------- painting ----------

    def _drawImage(self, painter, key, a, b, opacity=1.0):
        aspect = self.aspect(key)
        painter.save()
        painter.setTransform(toQTransform(a, b))
        painter.setOpacity(opacity)
        target = QRectF(0, 0, aspect, 1)
        pixmap = self.pixmaps.get(key)
        if pixmap is not None:
            painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        else:
            painter.fillRect(target, QColor(40, 40, 40))
        painter.restore()
        if pixmap is None:
            painter.save()
            painter.setOpacity(opacity)
            corner = a * complex(aspect / 2, 0.5) + b
            painter.setPen(QColor(150, 150, 150))
            text = "loading..." if key in self.pending_loads else ("no render" if not self.hasImage(key) else "")
            painter.drawText(QRectF(corner.real - 100, corner.imag - 10, 200, 20), Qt.AlignmentFlag.AlignCenter, text)
            painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 20, 20))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.zoom_index is None or self.current is None:
            painter.setPen(QColor(170, 170, 170))
            text = self.message if self.zoom_index is None else \
                "Double-click a view on the right, or pick an entry in the Library tab\n" \
                "and choose \"Show In Zoom View\"."
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)
            return

        a, b = self.camera
        anim = self.anim

        # Dimmed parent around the current image, for context.
        link = self.parentLink()
        if link is not None:
            p, q = self.placement(link[0], self.current)
            self._drawImage(painter, link[0], a / p, b - a * q / p, CONTEXT_OPACITY)

        self._drawImage(painter, self.current, a, b)

        outline_opacity = 1.0
        if anim is not None:
            t = anim['t']
            if anim['kind'] in ('in', 'out'):
                p, q = anim['placement']
                if anim['kind'] == 'in':
                    child_opacity = smoothstep(0.35, 0.95, t)
                    outline_opacity = 1.0 - smoothstep(0.0, 0.5, t)
                else:
                    child_opacity = 1.0 - smoothstep(0.05, 0.65, t)
                    outline_opacity = smoothstep(0.5, 1.0, t)
                self._drawImage(painter, anim['child'], a * p, a * q + b, child_opacity)

        if outline_opacity > 0:
            painter.setOpacity(outline_opacity)
            for child_key, tier in self.children():
                if anim is not None and child_key == anim['child']:
                    continue
                self._drawOutline(painter, child_key, tier, child_key == self.hovered)
            painter.setOpacity(1.0)

        if self.hovered is not None and anim is None:
            self._drawLabel(painter, self.hovered)

    def _drawOutline(self, painter, child_key, tier, hovered):
        polygon = self.childPolygon(child_key, self.camera)
        color = TIER_COLORS[tier]
        bounds = polygon.boundingRect()
        pen = QPen(color, 2.5 if hovered else 1.5)
        if tier == TIER_VARIANT:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        if max(bounds.width(), bounds.height()) < 8:
            # Too small to outline usefully - mark the spot instead.
            center = bounds.center()
            painter.setBrush(QBrush(color))
            painter.drawEllipse(center, 3.5 if hovered else 2.5, 3.5 if hovered else 2.5)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            return
        if hovered:
            fill = QColor(color)
            fill.setAlpha(50)
            painter.setBrush(QBrush(fill))
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(polygon)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _drawLabel(self, painter, key):
        polygon = self.childPolygon(key, self.camera)
        bounds = polygon.boundingRect()
        text = key[1].split(":")[0]
        font = QFont(self.font())
        font.setPointSize(9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 10
        height = metrics.height() + 4
        x = min(max(4, bounds.left()), self.width() - width - 4)
        y = bounds.top() - height - 3
        if y < 4:
            y = min(bounds.bottom() + 3, self.height() - height - 4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        painter.drawRoundedRect(QRectF(x, y, width, height), 3, 3)
        painter.setPen(QColor(235, 235, 235))
        painter.drawText(QRectF(x, y, width, height), Qt.AlignmentFlag.AlignCenter, text)

    # ---------- interaction ----------

    def childAt(self, pos):
        """The smallest outlined zoom under a widget position, if any."""
        best = None
        for child_key, _tier in self.children():
            polygon = self.childPolygon(child_key, self.camera)
            bounds = polygon.boundingRect()
            hit = polygon.containsPoint(pos, Qt.FillRule.OddEvenFill)
            if not hit and max(bounds.width(), bounds.height()) < 8:
                # Dot markers get a slightly bigger target.
                hit = (bounds.center() - pos).manhattanLength() <= 7
            if hit:
                area = bounds.width() * bounds.height()
                if best is None or area < best[0]:
                    best = (area, child_key)
        return best[1] if best else None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position()
            self._dragged = False
        elif event.button() == Qt.MouseButton.RightButton:
            self.zoomOut()

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._press_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = pos - self._press_pos
            if self._dragged or delta.manhattanLength() > 4:
                self._dragged = True
                a, b = self.camera
                self.camera = (a, b + complex(delta.x(), delta.y()))
                self._press_pos = pos
                self.user_moved_camera = True
                self.update()
            return
        if not self.busy() and self.current is not None:
            hovered = self.childAt(pos)
            if hovered != self.hovered:
                self.hovered = hovered
                self.setCursor(Qt.CursorShape.PointingHandCursor if hovered else Qt.CursorShape.ArrowCursor)
                self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            if not self._dragged and self.anim is None:
                child_key = self.childAt(event.position())
                if child_key is not None:
                    self.zoomInto(child_key)
            self._press_pos = None

    def mouseDoubleClickEvent(self, event):
        if self.current is not None and self.childAt(event.position()) is None and not self.busy():
            self._startAnimation('fit', None, None, self.camera, self.fitCamera(self.current))

    def wheelEvent(self, event):
        if self.current is None or self.anim is not None:
            return
        factor = 1.0015 ** event.angleDelta().y()
        pos = event.position()
        w = complex(pos.x(), pos.y())
        a, b = self.camera
        # Zoom about the point under the cursor.
        self.camera = (a * factor, w - factor * (w - b))
        self.user_moved_camera = True
        self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Escape):
            self.zoomOut()
        elif event.key() == Qt.Key.Key_Home and self.current is not None:
            self.camera = self.fitCamera(self.current)
            self.user_moved_camera = False
            self.update()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        if self.current is not None and self.anim is None and not self.user_moved_camera:
            self.camera = self.fitCamera(self.current)
        super().resizeEvent(event)


def formatSeconds(seconds):
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f}m"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f} days"


def wrappableName(name):
    """Fractal names are long with no spaces, so a word-wrapping QLabel can't
    break them and grows to fit instead - offer a break after each '_'."""
    return name.replace("_", "_​")


def renderSizeText(render):
    width, height = render['width'], render['height']
    if not width or not height:
        return "?"
    return f"{width}²" if width == height else f"{width}×{height}"


def renderTimeText(entry):
    """Short render time summary across an entry's renders, or ''. '~' marks
    estimates that include a guess for crash gaps."""
    logged = [ render for render in entry.get('renders', []) if render.get('log_path') ]
    if not logged:
        return ""
    total = sum(render['estimated_seconds'] for render in logged)
    estimated = any(render['crash_gaps'] for render in logged)
    return ("~" if estimated else "") + formatSeconds(total)


class ZoomTab(QWidget):
    """The Zoom tab: tier picker and controls, the canvas, and an info panel
    for the current entry. Actions that already exist on the main window
    (copy UPR, open image(s), show in library) are passed in as callbacks."""

    def __init__(self, open_image, open_largest_render, copy_upr, show_in_library, parent=None):
        super().__init__(parent)
        self.open_image = open_image
        self.open_largest_render = open_largest_render
        self.copy_upr = copy_upr
        self.show_in_library = show_in_library

        self.canvas = ZoomCanvas(self)
        self.canvas.currentChanged.connect(self.refreshInfo)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Show:"))
        self.tier_combo = QComboBox(self)
        for max_tier, label in TIER_CHOICES:
            self.tier_combo.addItem(self._swatch(TIER_COLORS[max_tier]), label, max_tier)
        self.tier_combo.setCurrentIndex([ tier for tier, _ in TIER_CHOICES ].index(DEFAULT_MAX_TIER))
        self.tier_combo.currentIndexChanged.connect(lambda _i: self.canvas.setMaxTier(self.tier_combo.currentData()))
        self.tier_combo.setToolTip("Which near-matches count as zooms. Outline colors:\n"
                                   "green = exact (only the view differs)\n"
                                   "yellow = recolored (same fractal and parameters, coloring/layers differ)\n"
                                   "red, dashed = variant (same formula, different parameters)")
        toolbar.addWidget(self.tier_combo)
        self.out_button = QPushButton("Zoom Out", self)
        self.out_button.setToolTip("Back out to the zoom this one is inside (right-click / Backspace).")
        self.out_button.clicked.connect(self.canvas.zoomOut)
        toolbar.addWidget(self.out_button)
        toolbar.addStretch()
        hint = QLabel("click outline: zoom in • right-click: zoom out • wheel/drag: look around")
        hint.setToolTip("Click an outline to zoom into it, right-click (or Backspace) to zoom back out.\n"
                        "Mouse wheel zooms freely, dragging pans, double-click refits the image.")
        hint.setStyleSheet("color: gray;")
        toolbar.addWidget(hint)

        self.info = QWidget(self)
        info_layout = QVBoxLayout(self.info)
        info_layout.setContentsMargins(6, 0, 0, 0)
        self.breadcrumb = QLabel(self.info)
        self.breadcrumb.setWordWrap(True)
        self.breadcrumb.setTextFormat(Qt.TextFormat.RichText)
        self.breadcrumb.linkActivated.connect(self._onBreadcrumb)
        info_layout.addWidget(self.breadcrumb)
        self.title = QLabel(self.info)
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        title_font = QFont(self.title.font())
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 1)
        self.title.setFont(title_font)
        info_layout.addWidget(self.title)
        self.details = QLabel(self.info)
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info_layout.addWidget(self.details)

        for label, callback in (("Copy UPR", lambda: self.copy_upr()),
                                ("Open Image", lambda: self.open_image()),
                                ("Open Largest Render", lambda: self.open_largest_render()),
                                ("Show In Library", lambda: self.show_in_library())):
            button = QPushButton(label, self.info)
            button.clicked.connect(callback)
            info_layout.addWidget(button)
            if label == "Open Largest Render":
                self.largest_render_button = button

        self.zooms_label = QLabel(self.info)
        info_layout.addWidget(self.zooms_label)
        self.zoom_list = QListWidget(self.info)
        self.zoom_list.setIconSize(QSize(10, 10))
        self.zoom_list.setToolTip("Select to highlight its outline, double-click (or Enter) to zoom in.")
        # Selecting highlights the outline, activating (double-click/Enter) zooms.
        self.zoom_list.itemActivated.connect(self._onZoomListItem)
        self.zoom_list.currentItemChanged.connect(self._onZoomListHover)
        info_layout.addWidget(self.zoom_list, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self.canvas)
        splitter.addWidget(self.info)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([1000, 340])

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(splitter, 1)
        self.refreshInfo(None)

    @staticmethod
    def _swatch(color):
        pixmap = QPixmap(10, 10)
        pixmap.fill(color)
        return QIcon(pixmap)

    def setData(self, zoom_index, entries):
        self.canvas.setData(zoom_index, entries)
        self.refreshInfo(self.canvas.current)

    def setMessage(self, message):
        self.canvas.setMessage(message)

    def showEntry(self, key):
        return self.canvas.showEntry(key)

    def currentEntry(self):
        if self.canvas.current is None:
            return None
        return self.canvas.entries.get(self.canvas.current)

    def refreshInfo(self, key):
        canvas = self.canvas
        self.zoom_list.clear()
        if key is None or canvas.zoom_index is None:
            self.breadcrumb.setText("")
            self.title.setText("")
            self.details.setText("")
            self.zooms_label.setText("")
            self.info.setEnabled(False)
            if canvas.zoom_index is not None:
                self._listOutermostViews()
            return
        self.info.setEnabled(True)
        zoom_index = canvas.zoom_index
        entry = canvas.entries.get(key, {})

        ancestors = zoom_index.ancestors(key, canvas.max_tier)
        crumbs = [ f'<a href="{i}" style="color:#7fb7ff">{wrappableName(ancestor[1].split(":")[0])}</a>'
                   for i, ancestor in enumerate(ancestors) ]
        self._breadcrumb_keys = ancestors
        self.breadcrumb.setText(" › ".join(crumbs) if crumbs else "<i>outermost view</i>")
        self.title.setText(wrappableName(key[1].split(":")[0]))

        view = zoom_index.views[key]
        lines = [ f"{os.path.basename(key[0])}, entry {key[1].split(':')[1]}",
                  f"magn {view.magn_float:.4g}, angle {view.angle:g}°" ]
        link = zoom_index.parentOf(key, canvas.max_tier)
        if link is not None:
            parent_view = zoom_index.views[link[0]]
            color = TIER_COLORS[link[1]].name()
            lines.append(f'<span style="color:{color}">{TIER_NAMES[link[1]]}</span> zoom, '
                         f'{view.magn_float / parent_view.magn_float:.3g}× in from its parent')
        if not view.layers_agree:
            lines.append("<i>layers have different views; using the first layer's</i>")

        renders = entry.get('renders', [])
        if renders:
            lines.append("<b>Renders:</b>")
            for render in renders:
                text = renderSizeText(render) + " " + os.path.splitext(render['path'])[1].lstrip(".")
                if render.get('log_path'):
                    text += ", " + formatSeconds(render['active_seconds'])
                    if render['crash_gaps']:
                        text += f" confirmed (~{formatSeconds(render['estimated_seconds'])} with crash gaps)"
                    if render['interruptions']:
                        text += f", {render['interruptions']} interruptions"
                    if not render['finished']:
                        text += ", <i>unfinished</i>"
                lines.append("&nbsp;&nbsp;" + text)
        else:
            lines.append("<i>No renders found.</i>")
        self.details.setText("<br>".join(lines))
        self.largest_render_button.setEnabled(bool(renders))

        children = zoom_index.childrenOf(key, canvas.max_tier)
        self.zooms_label.setText(f"{len(children)} zoom{'s' if len(children) != 1 else ''} in this view:"
                                 if children else "No zooms in this view.")
        for child_key, tier in children:
            child_view = zoom_index.views[child_key]
            nested = len(zoom_index.childrenOf(child_key, canvas.max_tier))
            text = f"{child_key[1].split(':')[0]}  ({child_view.magn_float / view.magn_float:.3g}×"
            text += f", {nested} more inside)" if nested else ")"
            item = QListWidgetItem(self._swatch(TIER_COLORS[tier]), text)
            item.setData(Qt.ItemDataRole.UserRole, child_key)
            if not canvas.hasImage(child_key):
                item.setForeground(QColor(130, 130, 130))
            self.zoom_list.addItem(item)

    def _listOutermostViews(self):
        """With nothing shown yet, offer the outermost views that have zooms,
        most nested zooms first, as starting points."""
        zoom_index = self.canvas.zoom_index
        max_tier = self.canvas.max_tier

        def descendants(key):
            return sum(1 + descendants(child) for child, _tier in zoom_index.childrenOf(key, max_tier))

        roots = [ key for key in zoom_index.children[max_tier] if zoom_index.parentOf(key, max_tier) is None ]
        counted = sorted(( (descendants(key), key) for key in roots ), key=lambda item: -item[0])
        self.info.setEnabled(True)
        self.zooms_label.setText(f"{len(roots)} outermost views with zooms (double-click to open):")
        for count, key in counted:
            item = QListWidgetItem(f"{key[1].split(':')[0]}  ({count} zooms inside)")
            item.setData(Qt.ItemDataRole.UserRole, key)
            if not self.canvas.hasImage(key):
                item.setForeground(QColor(130, 130, 130))
            self.zoom_list.addItem(item)

    def _onBreadcrumb(self, href):
        target = self._breadcrumb_keys[int(href)]
        link = self.canvas.parentLink()
        if link is not None and link[0] == target:
            self.canvas.zoomOut()
        else:
            self.canvas.showEntry(target)

    def _onZoomListItem(self, item):
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if self.canvas.current is None:
            # Listing outermost views - jump straight there.
            self.canvas.showEntry(key)
        else:
            self.canvas.zoomInto(key)

    def _onZoomListHover(self, current, _previous):
        self.canvas.hovered = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self.canvas.update()
