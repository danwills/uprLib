#!/usr/bin/env python
import sys
import os
import copy
import subprocess
import fnmatch
import functools
import time
from collections import deque
from collections import defaultdict
from collections import Counter

from PIL import Image

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

from PyQt6.QtWidgets import QWidget
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QTreeView
from PyQt6.QtWidgets import QVBoxLayout
from PyQt6.QtWidgets import QHBoxLayout
from PyQt6.QtWidgets import QGroupBox
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtWidgets import QCheckBox
from PyQt6.QtWidgets import QLineEdit
from PyQt6.QtWidgets import QAbstractItemView
from PyQt6.QtWidgets import QProgressDialog
from PyQt6.QtWidgets import QMessageBox
from PyQt6.QtWidgets import QSizePolicy
from PyQt6.QtWidgets import QTabWidget
from PyQt6.QtWidgets import QGraphicsView
from PyQt6.QtWidgets import QGraphicsScene
from PyQt6.QtWidgets import QGraphicsItem
from PyQt6.QtWidgets import QRubberBand
from PyQt6.QtWidgets import QSlider
from PyQt6.QtWidgets import QLabel

from PyQt6.QtGui import QKeySequence
from PyQt6.QtGui import QPalette
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QClipboard
from PyQt6.QtGui import QStandardItem
from PyQt6.QtGui import QIcon
from PyQt6.QtGui import QAction
from PyQt6.QtGui import QShortcut
from PyQt6.QtGui import QPainter
from PyQt6.QtGui import QPixmap
from PyQt6.QtGui import QFont
from PyQt6.QtGui import QPainterPath
from PyQt6.QtGui import QBrush
from PyQt6.QtGui import QPen

from PyQt6.QtCore import Qt
from PyQt6.QtCore import QThread
from PyQt6.QtCore import QModelIndex
from PyQt6.QtCore import QItemSelectionModel
from PyQt6.QtCore import QPointF
from PyQt6.QtCore import QRectF
from PyQt6.QtCore import QRect
from PyQt6.QtCore import QSize
from PyQt6.QtCore import QTimer
from PyQt6 import QtGui

from uprLibrary import *
import logspinner

# Custom role (alongside Qt.ItemDataRole.UserRole, which is already used per-column
# for numeric sorting) used to stash the full data dict for a tree row's entry,
# so selections can be resolved back to real data without re-parsing display text.
UPR_ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1


class GraphNode(QGraphicsItem):
    """A single fractal entry shown as a draggable node in the GraphView."""

    def __init__(self, entry):
        super().__init__()
        self.entry = entry
        self.edges = []

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(1)

        self.name = entry.get('upr_name', '').split(":")[0]
        self.setToolTip(self.name)

        self.pixmap = None
        thumb_path = entry.get('thumb_path')
        if thumb_path and os.path.isfile(thumb_path):
            pix = QPixmap(thumb_path)
            if not pix.isNull():
                self.pixmap = pix

        if self.pixmap:
            self.w = max(self.pixmap.width(), 8)
            self.h = max(self.pixmap.height(), 8)
        else:
            self.w = 48
            self.h = 48

    def addEdge(self, edge):
        self.edges.append(edge)

    SHADOW_OFFSET = 4
    SHADOW_COLOR = QColor(0, 0, 0, 150)

    def boundingRect(self):
        # pad covers the drop shadow's offset; extra makes room for the
        # selected-state name label to the right.
        pad = self.SHADOW_OFFSET + 2
        extra = 90 if self.isSelected() else 0
        return QRectF(-self.w/2 - pad, -self.h/2 - pad, self.w + pad*2 + extra, self.h + pad*2)

    def shape(self):
        path = QPainterPath()
        path.addRect(QRectF(-self.w/2, -self.h/2, self.w, self.h))
        return path

    def paint(self, painter, option, widget=None):
        rect = QRectF(-self.w/2, -self.h/2, self.w, self.h)

        # Cheap drop shadow: just an offset dark shape drawn first, no
        # QGraphicsEffect/offscreen compositing needed - scales fine to
        # thousands of nodes.
        shadow_rect = rect.translated(self.SHADOW_OFFSET, self.SHADOW_OFFSET)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self.SHADOW_COLOR))
        if self.pixmap:
            painter.drawRect(shadow_rect)
        else:
            painter.drawRoundedRect(shadow_rect, 6, 6)

        if self.pixmap:
            painter.drawPixmap(rect.topLeft(), self.pixmap)
        else:
            painter.setBrush(QBrush(QColor(90, 90, 90)))
            painter.setPen(QPen(QColor(40, 40, 40)))
            painter.drawRoundedRect(rect, 6, 6)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.isSelected():
            painter.setPen(QPen(QColor(255, 200, 0), 2))
        else:
            painter.setPen(QPen(QColor(20, 20, 20), 1))
        painter.drawRect(rect)

        # Hovering shows the name as a tooltip (see setToolTip above); selected
        # nodes additionally show it directly next to the node.
        if self.isSelected():
            painter.setPen(QPen(QColor(230, 230, 230)))
            painter.setFont(QFont("", 7))
            text_rect = QRectF(rect.right() + 4, rect.top(), 200, rect.height())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.name)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for edge in self.edges:
                edge.adjust()
            scene = self.scene()
            if scene is not None:
                scene.wake()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedChange:
            # boundingRect() grows to fit the name label when selected. This
            # fires before isSelected() actually flips, so calling it here
            # invalidates the old (correctly-sized) rect - without it,
            # deselecting left the label behind as a visual trail, since Qt
            # never knew the larger old rect needed repainting over.
            self.prepareGeometryChange()
        return super().itemChange(change, value)


class GraphEdge(QGraphicsItem):
    """A simple line connecting a parent/child pair of GraphNodes."""

    def __init__(self, source_node, dest_node):
        super().__init__()
        self.source = source_node
        self.dest = dest_node
        self.setZValue(-1)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        source_node.addEdge(self)
        dest_node.addEdge(self)

    def adjust(self):
        self.prepareGeometryChange()

    SHADOW_OFFSET = QPointF(3, 3)
    SHADOW_COLOR = QColor(0, 0, 0, 150)

    def boundingRect(self):
        # Generous margin: covers the shadow offset plus the widest the
        # line-width slider allows, regardless of its current setting.
        extra = 20
        return QRectF(self.source.pos(), self.dest.pos()).normalized().adjusted(-extra, -extra, extra, extra)

    def paint(self, painter, option, widget=None):
        scene = self.scene()
        width = scene.line_width if scene is not None else 2

        # Cheap drop shadow, same trick as GraphNode - an offset, darker,
        # slightly thicker line drawn first instead of a QGraphicsEffect.
        painter.setPen(QPen(self.SHADOW_COLOR, width + 2))
        painter.drawLine(self.source.pos() + self.SHADOW_OFFSET, self.dest.pos() + self.SHADOW_OFFSET)

        painter.setPen(QPen(QColor(170, 170, 170), width))
        painter.drawLine(self.source.pos(), self.dest.pos())


class _QuadTreeNode:
    """One cell of a Barnes-Hut quadtree: either a leaf holding at most one
    point, or an internal node with its center of mass/total count and four
    children. Used to approximate the repulsion force for large graphs in
    O(n log n) instead of all-pairs O(n^2)."""

    __slots__ = ('x0', 'y0', 'x1', 'y1', 'mass', 'com_x', 'com_y', 'children', 'point_idx')

    def __init__(self, x0, y0, x1, y1):
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
        self.mass = 0
        self.com_x = 0.0
        self.com_y = 0.0
        self.children = None
        self.point_idx = None


def _buildQuadTree(indices, xs, ys, x0, y0, x1, y1, depth=0):
    node = _QuadTreeNode(x0, y0, x1, y1)
    n = len(indices)
    node.mass = n
    if n == 0:
        return node
    node.com_x = sum(xs[i] for i in indices) / n
    node.com_y = sum(ys[i] for i in indices) / n
    # depth cap guards against runaway recursion when many points share (or
    # nearly share) the same position, which would otherwise subdivide forever.
    if n <= 1 or depth >= 24:
        node.point_idx = indices[0]
        return node
    mx = (x0 + x1) / 2
    my = (y0 + y1) / 2
    quads = ([], [], [], [])
    for i in indices:
        quads[(1 if xs[i] >= mx else 0) + (2 if ys[i] >= my else 0)].append(i)
    node.children = (
        _buildQuadTree(quads[0], xs, ys, x0, y0, mx, my, depth + 1),
        _buildQuadTree(quads[1], xs, ys, mx, y0, x1, my, depth + 1),
        _buildQuadTree(quads[2], xs, ys, x0, my, mx, y1, depth + 1),
        _buildQuadTree(quads[3], xs, ys, mx, my, x1, y1, depth + 1),
    )
    return node


def _barnesHutForce(node, x, y, self_idx, theta, repulsion):
    if node.mass == 0:
        return 0.0, 0.0

    if node.children is None:
        if node.point_idx == self_idx:
            return 0.0, 0.0
        dx = x - node.com_x
        dy = y - node.com_y
        dist_sq = dx*dx + dy*dy
        if dist_sq < 1.0:
            dist_sq = 1.0
        dist = dist_sq ** 0.5
        force = repulsion * node.mass / dist_sq
        return (dx / dist) * force, (dy / dist) * force

    dx = x - node.com_x
    dy = y - node.com_y
    dist_sq = dx*dx + dy*dy
    dist = dist_sq ** 0.5 if dist_sq > 1e-12 else 1e-6
    size = node.x1 - node.x0
    if size / dist < theta:
        clamped_dist_sq = dist_sq if dist_sq >= 1.0 else 1.0
        force = repulsion * node.mass / clamped_dist_sq
        return (dx / dist) * force, (dy / dist) * force

    fx = 0.0
    fy = 0.0
    for child in node.children:
        cfx, cfy = _barnesHutForce(child, x, y, self_idx, theta, repulsion)
        fx += cfx
        fy += cfy
    return fx, fy


class GraphScene(QGraphicsScene):
    """Holds the node/edge graph and runs a small spring-embedder simulation
    so the layout smoothly detangles itself, waking up again whenever a node
    is dragged."""

    REPULSION = 12000.0
    SPRING_LENGTH = 110.0
    SPRING_K = 0.02
    DAMPING = 0.85
    MAX_STEP = 6.0
    STEPS_PER_TICK = 4
    # tick() runs on the GUI thread, so a slow step freezes the whole window
    # until it returns. Barnes-Hut steps are each already far more expensive
    # than a plain/numpy one at the node counts where Barnes-Hut kicks in, so
    # it only does one per timer fire rather than piling up STEPS_PER_TICK of
    # them - keeps the UI responsive between ticks instead of locking up for
    # several seconds at a time on very large graphs.
    BARNES_HUT_STEPS_PER_TICK = 1
    # A node only counts as "settled" once it has moved less than this (in
    # scene units) continuously for SETTLE_STABLE_SECONDS - a single quiet
    # step isn't enough, since one part of a big graph can look still while
    # another part is still untangling. Wall-clock based rather than a tick
    # count, so it's not thrown off by how long a tick actually takes.
    SETTLE_DISTANCE = 1e-6
    SETTLE_STABLE_SECONDS = 3.0
    # Belt-and-suspenders safety net in case some configuration genuinely
    # never settles (e.g. a perpetually-oscillating disconnected cluster) -
    # high enough to essentially never trigger in normal use.
    MAX_SECONDS_PER_WAKE = 60.0
    # Above this many nodes, approximate repulsion with a Barnes-Hut quadtree
    # (O(n log n)) instead of all-pairs (O(n^2)). The tree itself is built
    # and walked in plain Python (a quadtree doesn't vectorize well), so its
    # per-node overhead is much higher than one numpy array op - profiling
    # found numpy's brute-force approach still winning up to ~4-5k nodes
    # (e.g. 4000: 900ms numpy vs 1.3s Barnes-Hut; 8000: 4.0s vs 3.2s, Barnes-
    # Hut ahead; 16000: 16.3s vs 7.4s). Set well into the range where the
    # crossover is unambiguous rather than where big-O alone suggests.
    BARNES_HUT_MIN_NODES = 5000
    BARNES_HUT_THETA = 0.8

    def __init__(self, parent=None):
        super().__init__(parent)
        # Qt's BSP-tree spatial index speeds up hit-testing a mostly-static
        # scene, but costs more than it saves here since nearly every item
        # moves on every step - a linear scan is cheaper for this item count.
        self.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        # Fixed and generously large rather than fitted to content, so
        # panning feels like an unbounded workspace instead of being
        # clamped to wherever the graph currently sits.
        self.setSceneRect(-50000, -50000, 100000, 100000)
        self.nodes = []
        self.edges = []
        self.line_width = 2
        self._stable_since = []
        self._wake_started_at = 0.0
        self.timer = QTimer()
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)

    def setLineWidth(self, width):
        self.line_width = width
        self.update()

    def addNode(self, node):
        node._idx = len(self.nodes)
        self.nodes.append(node)
        self.addItem(node)

    def addEdge(self, edge):
        self.edges.append(edge)
        self.addItem(edge)

    def clearGraph(self):
        self.timer.stop()
        self.clear()
        self.nodes = []
        self.edges = []

    def wake(self):
        # Every node's own setPos() during a tick calls this too (see
        # GraphNode.itemChange) - only reset state when actually waking from
        # idle (new selection, or a drag after things had settled), not on
        # every single frame while the simulation is already running.
        if not self.timer.isActive():
            now = time.monotonic()
            self._stable_since = [now] * len(self.nodes)
            self._wake_started_at = now
            self.timer.start()

    def _updateStability(self, dxs, dys, now):
        threshold = self.SETTLE_DISTANCE
        stable_since = self._stable_since
        for i in range(len(stable_since)):
            if abs(dxs[i]) >= threshold or abs(dys[i]) >= threshold:
                stable_since[i] = now

    def _isFullySettled(self, now):
        stable_for = self.SETTLE_STABLE_SECONDS
        return all((now - t) >= stable_for for t in self._stable_since)

    def tick(self):
        # Runs several substeps per timer fire (see STEPS_PER_TICK), touching
        # real Qt node objects only twice per node - once to read the starting
        # position, once to write the final one - instead of once per substep.
        # An earlier version both rebuilt a QPointF per pairwise interaction
        # *and* called node.setPos() every substep, and each setPos() cascades
        # through itemChange() into every connected edge's
        # prepareGeometryChange(); that PyQt/sip call overhead, not the
        # underlying arithmetic, was the real bottleneck. When numpy is
        # available the O(n^2) repulsion force is also vectorized (8-12x
        # faster again for realistic sizes), and above BARNES_HUT_MIN_NODES it
        # switches to a Barnes-Hut quadtree approximation (O(n log n)) instead,
        # for graphs with several thousand or more nodes. No GPU needed, or
        # useful, at these scales - dispatch/transfer overhead would
        # dominate well before a GPU's raw throughput advantage caught
        # up for anything short of very large graphs.
        n = len(self.nodes)
        if not n:
            self.timer.stop()
            return

        if HAS_NUMPY and n > self.BARNES_HUT_MIN_NODES:
            settled = self._tickBarnesHut(n)
        elif HAS_NUMPY:
            settled = self._tickNumpy(n)
        else:
            settled = self._tickPython(n)

        timed_out = (time.monotonic() - self._wake_started_at) >= self.MAX_SECONDS_PER_WAKE
        if settled or timed_out:
            self.timer.stop()

    def _tickPython(self, n):
        xs = [0.0] * n
        ys = [0.0] * n
        for i, node in enumerate(self.nodes):
            pos = node.pos()
            xs[i] = pos.x()
            ys[i] = pos.y()

        repulsion = self.REPULSION
        spring_length = self.SPRING_LENGTH
        spring_k = self.SPRING_K
        damping = self.DAMPING
        max_step = self.MAX_STEP

        now = time.monotonic()
        for _ in range(self.STEPS_PER_TICK):
            fxs = [0.0] * n
            fys = [0.0] * n

            for i in range(n):
                xi = xs[i]
                yi = ys[i]
                fxi = fxs[i]
                fyi = fys[i]
                for j in range(i + 1, n):
                    dx = xi - xs[j]
                    dy = yi - ys[j]
                    dist_sq = dx*dx + dy*dy
                    if dist_sq < 1.0:
                        dist_sq = 1.0
                    dist = dist_sq ** 0.5
                    force_mag = repulsion / dist_sq
                    fx = (dx / dist) * force_mag
                    fy = (dy / dist) * force_mag
                    fxi += fx
                    fyi += fy
                    fxs[j] -= fx
                    fys[j] -= fy
                fxs[i] = fxi
                fys[i] = fyi

            for edge in self.edges:
                i = edge.source._idx
                j = edge.dest._idx
                dx = xs[j] - xs[i]
                dy = ys[j] - ys[i]
                dist = (dx*dx + dy*dy) ** 0.5
                if dist < 1.0:
                    dist = 1.0
                stretch = dist - spring_length
                fx = (dx / dist) * stretch * spring_k
                fy = (dy / dist) * stretch * spring_k
                fxs[i] += fx
                fys[i] += fy
                fxs[j] -= fx
                fys[j] -= fy

            dxs = [0.0] * n
            dys = [0.0] * n
            for i in range(n):
                dx = fxs[i] * damping
                dy = fys[i] * damping
                if dx > max_step:
                    dx = max_step
                elif dx < -max_step:
                    dx = -max_step
                if dy > max_step:
                    dy = max_step
                elif dy < -max_step:
                    dy = -max_step
                xs[i] += dx
                ys[i] += dy
                dxs[i] = dx
                dys[i] = dy

            self._updateStability(dxs, dys, now)
            if self._isFullySettled(now):
                break

        for i, node in enumerate(self.nodes):
            node.setPos(xs[i], ys[i])

        return self._isFullySettled(now)

    def _tickNumpy(self, n):
        xs = np.empty(n)
        ys = np.empty(n)
        for i, node in enumerate(self.nodes):
            pos = node.pos()
            xs[i] = pos.x()
            ys[i] = pos.y()

        if self.edges:
            src_idx = np.fromiter((e.source._idx for e in self.edges), dtype=np.intp, count=len(self.edges))
            dst_idx = np.fromiter((e.dest._idx for e in self.edges), dtype=np.intp, count=len(self.edges))
        else:
            src_idx = dst_idx = np.empty(0, dtype=np.intp)

        repulsion = self.REPULSION
        spring_length = self.SPRING_LENGTH
        spring_k = self.SPRING_K
        damping = self.DAMPING
        max_step = self.MAX_STEP

        now = time.monotonic()
        for _ in range(self.STEPS_PER_TICK):
            dx = xs[:, None] - xs[None, :]
            dy = ys[:, None] - ys[None, :]
            dist_sq = dx*dx + dy*dy
            np.fill_diagonal(dist_sq, 1.0)
            np.maximum(dist_sq, 1.0, out=dist_sq)
            dist = np.sqrt(dist_sq)
            force_mag = repulsion / dist_sq
            fx_mat = (dx / dist) * force_mag
            fy_mat = (dy / dist) * force_mag
            np.fill_diagonal(fx_mat, 0.0)
            np.fill_diagonal(fy_mat, 0.0)
            fxs = fx_mat.sum(axis=1)
            fys = fy_mat.sum(axis=1)

            if src_idx.size:
                # Edge endpoints can repeat (a node with several children), so
                # a plain fxs[src_idx] += ... would silently drop all but one
                # of any repeated index - np.add.at accumulates correctly.
                edx = xs[dst_idx] - xs[src_idx]
                edy = ys[dst_idx] - ys[src_idx]
                edist = np.maximum(np.sqrt(edx*edx + edy*edy), 1.0)
                stretch = edist - spring_length
                efx = (edx / edist) * stretch * spring_k
                efy = (edy / edist) * stretch * spring_k
                np.add.at(fxs, src_idx, efx)
                np.add.at(fys, src_idx, efy)
                np.add.at(fxs, dst_idx, -efx)
                np.add.at(fys, dst_idx, -efy)

            dxs = np.clip(fxs * damping, -max_step, max_step)
            dys = np.clip(fys * damping, -max_step, max_step)
            xs += dxs
            ys += dys

            self._updateStability(dxs, dys, now)
            if self._isFullySettled(now):
                break

        for i, node in enumerate(self.nodes):
            node.setPos(float(xs[i]), float(ys[i]))

        return self._isFullySettled(now)

    def _tickBarnesHut(self, n):
        xs = np.empty(n)
        ys = np.empty(n)
        for i, node in enumerate(self.nodes):
            pos = node.pos()
            xs[i] = pos.x()
            ys[i] = pos.y()

        if self.edges:
            src_idx = np.fromiter((e.source._idx for e in self.edges), dtype=np.intp, count=len(self.edges))
            dst_idx = np.fromiter((e.dest._idx for e in self.edges), dtype=np.intp, count=len(self.edges))
        else:
            src_idx = dst_idx = np.empty(0, dtype=np.intp)

        repulsion = self.REPULSION
        spring_length = self.SPRING_LENGTH
        spring_k = self.SPRING_K
        damping = self.DAMPING
        max_step = self.MAX_STEP
        theta = self.BARNES_HUT_THETA
        all_indices = list(range(n))

        now = time.monotonic()
        for _ in range(self.BARNES_HUT_STEPS_PER_TICK):
            # Rebuilt every substep since positions move each time - still
            # O(n log n) overall, versus O(n^2) for all-pairs.
            x0, x1 = float(xs.min()) - 1.0, float(xs.max()) + 1.0
            y0, y1 = float(ys.min()) - 1.0, float(ys.max()) + 1.0
            root = _buildQuadTree(all_indices, xs, ys, x0, y0, x1, y1)

            fxs = np.empty(n)
            fys = np.empty(n)
            for i in range(n):
                fx, fy = _barnesHutForce(root, xs[i], ys[i], i, theta, repulsion)
                fxs[i] = fx
                fys[i] = fy

            if src_idx.size:
                edx = xs[dst_idx] - xs[src_idx]
                edy = ys[dst_idx] - ys[src_idx]
                edist = np.maximum(np.sqrt(edx*edx + edy*edy), 1.0)
                stretch = edist - spring_length
                efx = (edx / edist) * stretch * spring_k
                efy = (edy / edist) * stretch * spring_k
                np.add.at(fxs, src_idx, efx)
                np.add.at(fys, src_idx, efy)
                np.add.at(fxs, dst_idx, -efx)
                np.add.at(fys, dst_idx, -efy)

            dxs = np.clip(fxs * damping, -max_step, max_step)
            dys = np.clip(fys * damping, -max_step, max_step)
            xs += dxs
            ys += dys

            self._updateStability(dxs, dys, now)
            if self._isFullySettled(now):
                break

        for i, node in enumerate(self.nodes):
            node.setPos(float(xs[i]), float(ys[i]))

        return self._isFullySettled(now)


class GraphView(QGraphicsView):
    """Second-tab view: a fluid, draggable node-link graph of selected entries.

    Navigation follows a Houdini-network-editor-ish scheme: no scrollbars, an
    effectively infinite pannable/zoomable canvas, middle-drag to pan,
    mouse-wheel to zoom (centered under the cursor), and Alt+right-drag to
    zoom. Rubber-band and click selection use Shift=add, Ctrl=remove,
    Ctrl+Shift=toggle - Qt's own rubber-band selection only has a combined
    "toggle" for Ctrl, with no way to get a separate "remove", so this view
    implements selection itself rather than using QGraphicsView's built-in
    RubberBandDrag mode.
    """

    ZOOM_WHEEL_FACTOR = 1.0015
    ZOOM_DRAG_FACTOR = 1.004
    MIN_ZOOM = 0.05
    MAX_ZOOM = 20.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.graph_scene = GraphScene(self)
        self.setScene(self.graph_scene)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Minimal/smart update modes compute a dirty region from each item's
        # old vs. new boundingRect(); with items constantly moving (and ours
        # changing shape on selection, plus the drop-shadow below extending
        # past the "plain" rect) that region math kept leaving stale pixels
        # behind as trails. Repainting the whole viewport every frame is more
        # than fast enough here and guarantees no leftovers.
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        # ix -> GraphNode for whatever's currently built - lets UPRLibWindow
        # keep tree/graph selection in sync without caring how the graph
        # itself is laid out.
        self.nodes_by_ix = {}

        self._zoom = 1.0
        self._panning = False
        self._pan_last_pos = None
        self._alt_zooming = False
        self._alt_zoom_last_pos = None

        self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self._rubber_band_origin = None
        self._select_drag_mode = None
        self._select_drag_start_selection = set()

    def buildGraph(self, entries):
        self.graph_scene.clearGraph()

        nodes_by_ix = {}
        for i, entry in enumerate(entries):
            node = GraphNode(entry)
            level = entry.get('level') or 0
            node.setPos(QPointF((i % 6) * 140, level * 160))
            self.graph_scene.addNode(node)
            nodes_by_ix[entry.get('ix')] = node
        self.nodes_by_ix = nodes_by_ix

        for entry in entries:
            parent_ix = entry.get('parent_id')
            if parent_ix is None or parent_ix < 0 or parent_ix not in nodes_by_ix:
                continue
            node = nodes_by_ix[entry.get('ix')]
            parent_node = nodes_by_ix[parent_ix]
            edge = GraphEdge(parent_node, node)
            self.graph_scene.addEdge(edge)

        self.centerOn(self.graph_scene.itemsBoundingRect().center())
        self.graph_scene.wake()

    def selectedEntries(self):
        return [node.entry for node in self.graph_scene.selectedItems() if isinstance(node, GraphNode)]

    # -- Selection (rubber-band and click), Shift=add/Ctrl=remove/Ctrl+Shift=toggle --

    @staticmethod
    def _selectionModeFor(modifiers):
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if ctrl and shift:
            return 'toggle'
        if ctrl:
            return 'remove'
        if shift:
            return 'add'
        return 'replace'

    def _applySelectionOp(self, touched):
        mode = self._select_drag_mode
        start = self._select_drag_start_selection
        if mode == 'add':
            new_selection = start | touched
        elif mode == 'remove':
            new_selection = start - touched
        elif mode == 'toggle':
            new_selection = start ^ touched
        else:
            new_selection = touched
        for node in self.graph_scene.nodes:
            should_select = node in new_selection
            if node.isSelected() != should_select:
                node.setSelected(should_select)

    def _startSelectionDrag(self, event, clicked_node):
        self._select_drag_start_selection = {n for n in self.graph_scene.nodes if n.isSelected()}
        self._select_drag_mode = self._selectionModeFor(event.modifiers())
        if clicked_node is not None:
            self._applySelectionOp({clicked_node})
            return
        self._rubber_band_origin = event.position().toPoint()
        self._rubber_band.setGeometry(QRect(self._rubber_band_origin, QSize()))
        self._rubber_band.show()

    # -- Mouse/wheel navigation --

    def wheelEvent(self, event):
        factor = self.ZOOM_WHEEL_FACTOR ** event.angleDelta().y()
        self._applyZoom(factor)
        event.accept()

    def _applyZoom(self, factor):
        new_zoom = self._zoom * factor
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, new_zoom))
        factor = new_zoom / self._zoom
        if factor == 1.0:
            return
        self._zoom = new_zoom
        self.scale(factor, factor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_last_pos = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton and (event.modifiers() & Qt.KeyboardModifier.AltModifier):
            self._alt_zooming = True
            self._alt_zoom_last_pos = event.position().toPoint()
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            item = self.itemAt(pos)
            clicked_node = item if isinstance(item, GraphNode) else None
            mode = self._selectionModeFor(event.modifiers())
            if mode != 'replace' or clicked_node is None:
                self._startSelectionDrag(event, clicked_node)
                event.accept()
                return
            # Plain click on a node: fall through to Qt's normal
            # click-to-select / drag-to-move handling.

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            pos = event.position().toPoint()
            delta = pos - self._pan_last_pos
            self._pan_last_pos = pos
            h = self.horizontalScrollBar()
            v = self.verticalScrollBar()
            h.setValue(h.value() - delta.x())
            v.setValue(v.value() - delta.y())
            event.accept()
            return

        if self._alt_zooming:
            pos = event.position().toPoint()
            delta_x = pos.x() - self._alt_zoom_last_pos.x()
            self._alt_zoom_last_pos = pos
            self._applyZoom(self.ZOOM_DRAG_FACTOR ** delta_x)
            event.accept()
            return

        if self._rubber_band.isVisible():
            rect = QRect(self._rubber_band_origin, event.position().toPoint()).normalized()
            self._rubber_band.setGeometry(rect)
            touched = {it for it in self.items(rect) if isinstance(it, GraphNode)}
            self._applySelectionOp(touched)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton and self._alt_zooming:
            self._alt_zooming = False
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._rubber_band.isVisible():
            self._rubber_band.hide()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        # Alt+right-drag is reserved for zooming, not the context menu.
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier:
            event.ignore()
            return
        super().contextMenuEvent(event)


class UPRLibWindow(QWidget):
    """Class for the main uprLib window"""

    # noinspection PyUnresolvedReferences
    def __init__(self, upr_lib_val, data_val, app_val, verbose=False):
        """UPRLibWindow constructor.
        
        Args:
            upr_lib_val (UPRLibrary):
                The UPRLibrary object containing the full data that was read from the UPR files.
            data_val (dict):
                A munged form of the data for displaying in the QTreeView
                
            verbose (int):
                When nonzero, more messages will be printed, at increasing amounts with higher values.
        
        Returns:
            Nothing explicitly, implicitly: the constructed object.
        """

        # noinspection PyArgumentList
        super(UPRLibWindow, self).__init__()
        
        self.verbose=verbose
        self.app = app_val
        
        # Add Ctrl+Q hotkey to quit (Alt+F4 still works, but I prefer Ctrl+Q personally)
        quit_shortcut_keyseq = QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Q)
        self.quit_shortcut = QShortcut(quit_shortcut_keyseq, self)
        self.quit_shortcut.activated.connect(self.quitButtonClicked)

        copy_shortcut_keyseq = QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_C)
        self.copy_shortcut = QShortcut(copy_shortcut_keyseq, self)
        self.copy_shortcut.activated.connect(self.copyToClipboard)

        openimage_shortcut_keyseq = QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_R)
        self.openimage_shortcut = QShortcut(openimage_shortcut_keyseq, self)
        self.openimage_shortcut.activated.connect(self.openImages)
        
        self.tree = QTreeView(self)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        # Double-click opens the image viewer instead of the default expand/collapse.
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.doubleClicked.connect(self.treeDoubleClicked)
        
        #self.tree.setContextMenuPolicy(Qt.DefaultContextMenu)
        
        copy_frac_name_acion = QAction("Copy Fractal Name(s)",self)
        copy_frac_name_acion.triggered.connect(self.copySelectedFractalNames)
        self.tree.addAction(copy_frac_name_acion)

        show_in_graph_acion = QAction("Show Selected In Graph View", self)
        show_in_graph_acion.triggered.connect(self.showSelectedInGraphView)
        self.tree.addAction(show_in_graph_acion)

        treefont = self.tree.font()
        treefont.setPointSize(8)
        self.tree.setFont(treefont)
        self.tree.setUniformRowHeights(True)

        main_tab = QWidget()
        layout = QVBoxLayout(main_tab)

        self.top_group = QGroupBox(self)
        self.top_group_layout = QHBoxLayout()
        
        self.top_filter = QLineEdit(self)
        self.top_filter.setEnabled(True)
        self.top_filter.setPlaceholderText("Enter filter string here...")
        #self.top_filter.setText("")
        self.previous_filter_str = ""
        self.top_filter.editingFinished.connect(self.updateList)
        self.top_filter.setToolTip("Filter to use on the list when filtering is enabled.")
        self.top_group_layout.addWidget(self.top_filter)
        
        self.filter_checkbox = QCheckBox(self)
        self.filter_checkbox.setText("Enable Filter")
        self.filter_checkbox.setCheckState(Qt.CheckState.Checked)
        self.filter_checkbox.toggled.connect(self.toggleFilter)
        self.filter_checkbox.setToolTip("Enable filtering of the tree.")
        self.top_group_layout.addWidget(self.filter_checkbox)
        
        self.casesensitive_checkbox = QCheckBox(self)
        self.casesensitive_checkbox.setText("Case Sensitive")
        self.casesensitive_checkbox.setCheckState(Qt.CheckState.Unchecked)
        self.casesensitive_checkbox.toggled.connect(self.updateList)
        self.casesensitive_checkbox.setToolTip("Make the filtering case sensitive.")
        self.top_group_layout.addWidget(self.casesensitive_checkbox)
        
        self.reverse_checkbox = QCheckBox(self)
        self.reverse_checkbox.setText("Reverse")
        self.reverse_checkbox.setCheckState(Qt.CheckState.Unchecked)
        self.reverse_checkbox.toggled.connect(self.updateList)
        self.reverse_checkbox.setToolTip("Reverse the tree.")
        self.top_group_layout.addWidget(self.reverse_checkbox)
        
        self.sort_checkbox = QCheckBox(self)
        self.sort_checkbox.setText("Sort")
        self.sort_checkbox.setCheckState(Qt.CheckState.Checked)
        self.sort_checkbox.toggled.connect(self.updateList)
        self.sort_checkbox.setToolTip("Sort the tree alphabetically.")
        self.top_group_layout.addWidget(self.sort_checkbox)
        
        self.expand_checkbox = QCheckBox(self)
        self.expand_checkbox.setText("Expand")
        self.expand_checkbox.setCheckState(Qt.CheckState.Checked)
        update_list_with_full_refresh = functools.partial( self.updateList, save_and_restore_expanded_state=False )
        self.expand_checkbox.toggled.connect(update_list_with_full_refresh)
        self.expand_checkbox.setToolTip("Pre-expand the tree so that we see all entries.")
        self.top_group_layout.addWidget(self.expand_checkbox)
        
        self.top_group.setLayout(self.top_group_layout)

        layout.addWidget(self.top_group)
        
        layout.addWidget(self.tree)
        
        #button_layout = QVBoxLayout(self)
        
        self.copy_button = QPushButton(self)
        self.copy_button.setText("Copy UPR to Clipboard")
        self.copy_button.clicked.connect(self.copyToClipboard)
        self.copy_button.setToolTip("Copy the currently selected UPR,\n"
                                    "If multiple are selected you will get the\n"
                                    "last one that was selected.")
        layout.addWidget(self.copy_button)
        
        copy_upr_acion = QAction("Copy Upr",self)
        copy_upr_acion.triggered.connect(self.copyToClipboard)
        self.tree.addAction(copy_upr_acion)
        
        self.openimage_button = QPushButton(self)
        self.openimage_button.setText("Open image")
        self.openimage_button.clicked.connect(self.openImages)
        self.openimage_button.setToolTip("Open the pre-rendered images for the selected UPRs if they can be found.")
        layout.addWidget(self.openimage_button)
        
        open_image_acion = QAction("Open Images",self)
        open_image_acion.triggered.connect(self.openImages)
        # Tooltip did not seem to work with these
        # open_image_acion.setToolTip("Open the pre-rendered images for the selected UPRs if they can be found.")
        self.tree.addAction(open_image_acion)
        
        self.quit_button = QPushButton(self)
        self.quit_button.setText("Quit")
        self.quit_button.clicked.connect(self.quitButtonClicked)
        layout.addWidget(self.quit_button)

        # Second tab: a draggable node/thumbnail graph of whatever's selected in the
        # tree (via "Show Selected In Graph View"), sharing the same context-menu
        # actions so selections made there can also be copied/opened.
        self.graph_view = GraphView()
        self.graph_view.addAction(copy_frac_name_acion)
        self.graph_view.addAction(copy_upr_acion)
        self.graph_view.addAction(open_image_acion)

        graph_toolbar = QHBoxLayout()
        graph_toolbar.addWidget(QLabel("Line Width:"))
        self.line_width_slider = QSlider(Qt.Orientation.Horizontal)
        self.line_width_slider.setMinimum(1)
        self.line_width_slider.setMaximum(12)
        self.line_width_slider.setValue(self.graph_view.graph_scene.line_width)
        self.line_width_slider.setFixedWidth(160)
        self.line_width_slider.setToolTip("Width of the wires connecting nodes in the graph view.")
        self.line_width_slider.valueChanged.connect(self.graph_view.graph_scene.setLineWidth)
        graph_toolbar.addWidget(self.line_width_slider)
        graph_toolbar.addStretch()

        self.graph_tab = graph_tab = QWidget()
        graph_tab_layout = QVBoxLayout(graph_tab)
        graph_tab_layout.addLayout(graph_toolbar)
        graph_tab_layout.addWidget(self.graph_view)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(main_tab, "Library")
        self.tabs.addTab(graph_tab, "Graph")

        outer_layout = QVBoxLayout(self)
        outer_layout.addWidget(self.tabs)

        self.model = QtGui.QStandardItemModel()
        self.model.setHorizontalHeaderLabels(["Icon", "Fractal Name","UPR File", "Index In file", "Layers", "Compressed Size"])
        # Sort by the raw value we stuff into Qt.ItemDataRole.UserRole (set per-item in
        # setDataInTreeWidget) rather than the displayed text, so numeric columns sort
        # numerically (1,2,...,10) instead of alphabetically (1,10,2,...).
        self.model.setSortRole(Qt.ItemDataRole.UserRole)
        self.tree.header().setDefaultSectionSize(180)
        self.tree.setModel(self.model)
        self.tree.setSortingEnabled(True)

        # Keep tree and graph selection in sync in both directions. Guarded by
        # _syncing_selection so one view's handler applying the other view's
        # selection doesn't bounce back and forth forever.
        self._syncing_selection = False
        self.tree.selectionModel().selectionChanged.connect(self.onTreeSelectionChanged)
        self.graph_view.graph_scene.selectionChanged.connect(self.onGraphSelectionChanged)

        # Start with no column actively sorted (-1), just "whatever order we built it in".
        self.tree.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        # Method below!
        if self.verbose:
            print("UPRLibWindow init.. setting data..")
            
        self.data=data_val
        self.setDataInTreeWidget(data_val)
        self.upr_lib = upr_lib_val
        self.updateList(save_and_restore_expanded_state=False)

    def displayMessage(self, 
                       dialog_title, 
                       dialog_text, 
                       detailed_text=None, 
                       button_texts=None, 
                       button_roles=None):
                       
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(dialog_title)
        msg_box.setWindowModality(Qt.WindowModality.WindowModal)
        if button_texts :
            for button_text, button_role in zip(button_texts, button_roles):
                _a_btn = msg_box.addButton(button_text, button_role)
            
        #ok_btn = msg_box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
        #cancel_btn = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)

        msg_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        msg_box.setText(dialog_text)
        msg_box.setDetailedText(detailed_text)
        
        # This didn't help to enable resizing:
        # I think I might need to swap back to QDialog!
        #msg_box.setMaximumSize(16777215, 16777215)
        #msg_box.setProperty('resizable', True)
        #msg_box.setSizeGripEnabled(True)
        
        #layout = QVBoxLayout()
        #message = QLabel(dialog_text)
        #layout.addWidget(message)
        
        #QBtn = QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        #self.buttonBox = QDialogButtonBox(QBtn)
        #self.buttonBox.accepted.connect(self.dialogAccept)
        #self.buttonBox.rejected.connect(self.reject)
        #layout.addWidget(self.buttonBox)
        
        #msg_box.setLayout(layout)
        #msg_box.exec()
        msg_box.exec()
        return msg_box
    
    @staticmethod
    def dialogAccept():
        print("Dialog accept!")
    
    def copySelectedFractalNames(self):
        entries = self.getActiveSelectionEntries()
        upr_names = "\n".join( entry['upr_name'].split(":")[0] for entry in entries )
        self.setClipboard( upr_names )

    def getSelectedTreeEntries(self):
        """Resolves the tree's current row selection back to the full data
        dict for each row (stashed on the name item in setDataInTreeWidget)."""
        entries = []
        for index in self.tree.selectionModel().selectedRows():
            name_index = index.siblingAtColumn(1)
            entry = self.model.itemFromIndex(name_index).data(UPR_ENTRY_ROLE)
            if entry is not None:
                entries.append(entry)
        return entries

    def getActiveSelectionEntries(self):
        """Selection, resolved to full data dicts, from whichever of the tree
        or graph tabs is currently active - so shared actions (copy name,
        copy upr, open images) work the same from either view."""
        if self.tabs.currentWidget() is self.graph_tab:
            return self.graph_view.selectedEntries()
        return self.getSelectedTreeEntries()

    def showSelectedInGraphView(self):
        entries = self.getSelectedTreeEntries()
        if not entries:
            return
        self.graph_view.buildGraph(entries)
        self.tabs.setCurrentWidget(self.graph_tab)
        # Newly-built nodes start unselected - bring them into line with
        # whatever's currently selected in the tree.
        self.onTreeSelectionChanged()

    def onTreeSelectionChanged(self, *_args):
        """Mirrors the tree's selection onto whichever graph nodes also
        represent one of its rows - entries the graph doesn't currently have
        as a node are simply left alone there."""
        if self._syncing_selection or not self.graph_view.nodes_by_ix:
            return
        self._syncing_selection = True
        try:
            selected_ix = { entry['ix'] for entry in self.getSelectedTreeEntries() }
            for ix, node in self.graph_view.nodes_by_ix.items():
                should_select = ix in selected_ix
                if node.isSelected() != should_select:
                    node.setSelected(should_select)
        finally:
            self._syncing_selection = False

    def onGraphSelectionChanged(self):
        """Mirrors the graph's selection onto the corresponding tree rows."""
        if self._syncing_selection:
            return
        self._syncing_selection = True
        try:
            selected_ix = { node.entry['ix']
                             for node in self.graph_view.graph_scene.selectedItems()
                             if isinstance(node, GraphNode) }
            selection_model = self.tree.selectionModel()
            for ix in self.graph_view.nodes_by_ix:
                tree_item = self.ix_to_tree_item.get(ix)
                if tree_item is None:
                    continue
                index = tree_item.index()
                should_select = ix in selected_ix
                is_selected = selection_model.isSelected(index)
                if should_select != is_selected:
                    flag = QItemSelectionModel.SelectionFlag.Select if should_select else QItemSelectionModel.SelectionFlag.Deselect
                    selection_model.select(index, flag | QItemSelectionModel.SelectionFlag.Rows)
        finally:
            self._syncing_selection = False


    def toggleFilter(self):
        checkbox_state = self.filter_checkbox.checkState()
        # Looks like checkbox_state is 2 when checked, 0 when unchecked.
        
        if checkbox_state:
            self.top_filter.setEnabled(True)
            if self.previous_filter_str:
                self.top_filter.setText(self.previous_filter_str)
            self.updateList()
        else:
            # Don't really want to have to clear this on disable.. 
            # but can't work out how to grey out the field.
            self.previous_filter_str = self.top_filter.text()
            self.top_filter.setText("")
            self.top_filter.setDisabled(True)
            self.updateList()
            
        #self.top_filter.setEnabled
    def getExpandedStates(self, treeview, parent_index=None):
        all_data = {}
        if parent_index is None:
            parent_index = QModelIndex()
        model = treeview.model()
        num_rows = model.rowCount(parent_index)
        for row in range(num_rows):
            index = model.index(row, 0, parent_index)
            name_data = model.data(index)
            
            file_index = model.index(row, 1, parent_index)
            index_index = model.index(row, 2, parent_index)
            file_data = model.data(file_index)
            index_data = model.data(index_index)
            expanded = treeview.isExpanded(index)
            if self.verbose:
                print(f"Got expanded: {expanded}")
            all_data[ (name_data, file_data, index_data) ] = expanded
            
            if model.hasChildren(index):
                all_data.update(self.getExpandedStates(treeview, parent_index=index))
        return all_data
                
    def restoreExpandedStates(self, treeview, previous_states, parent_index=None):
        if parent_index is None:
            parent_index = QModelIndex()
        model = treeview.model()
        num_rows = model.rowCount(parent_index)
        for row in range(num_rows):
            index = model.index(row, 0, parent_index)
            name_data = model.data(index)
            file_index = model.index(row, 1, parent_index)
            index_index = model.index(row, 2, parent_index)
            file_data = model.data(file_index)
            index_data = model.data(index_index)
            # expanded = treeview.isExpanded(index)
            key_tuple = (name_data, file_data, index_data)
            #print(f"Made key_tuple: {key_tuple}")
            if key_tuple in previous_states:
                stored_expanded_state = previous_states[key_tuple]
                if self.verbose:
                    print(f"Got stored_expanded_state: {stored_expanded_state}")
                if stored_expanded_state:
                    #print(f"Expanding index: {index}")
                    treeview.expand(index)
                else:
                    #print(f"Collapsing index: {index}")
                    treeview.collapse(index)
            elif self.verbose:
                print(f"did not find key_tuple in previous_states!: {key_tuple}")
            if model.hasChildren(index):
                self.restoreExpandedStates(treeview, previous_states, parent_index=index)
    
    def updateList(self, save_and_restore_expanded_state=True):
        
        previous_expanded_states = {}
        if save_and_restore_expanded_state:
            previous_expanded_states = self.getExpandedStates(self.tree)
                
        datacopy = copy.copy(self.data)
        
        do_sort = self.sort_checkbox.checkState()
        
        case_sensitive = self.casesensitive_checkbox.checkState()
        
        if do_sort:
            # print("Sorting by fractal name!..")
            if case_sensitive:
                datacopy = dict(sorted(list(datacopy.items()), key=lambda itemj: itemj[0].split(":")[-2]))
            else:
                datacopy = dict(sorted(list(datacopy.items()), key=lambda itemu: itemu[0].split(":")[-2].lower()))
            # print("\n".join( datacopy.keys()))
        
        do_reverse = self.reverse_checkbox.checkState()
        if do_reverse:
            # Should this do the reversal recursively?!
            datacopy = dict( reversed( list( datacopy.items() ) ) )
        
        do_filter = self.filter_checkbox.checkState()
        
        # Now filter the data, maybe this also needs to be recursive!?
        if do_filter :
            filter_str = self.top_filter.text()
            filtered_data = {}
            
            for item,val in datacopy.items():
                use_filter_str = filter_str if case_sensitive else filter_str.lower()
                item_fracname = item.split(":")[-2]
                use_item = item_fracname if case_sensitive else item_fracname.lower()
                
                # Make it work like substring match .. always allow stuff on the ends!
                if not use_filter_str:
                    use_filter_str = "*"
                else:
                    if not use_filter_str.startswith("*"):
                        use_filter_str = "*" + use_filter_str
                    if not use_filter_str.endswith("*"):
                        use_filter_str = use_filter_str + "*"
                
                filter_str_re = fnmatch.translate(use_filter_str)

                if re.match( filter_str_re, use_item ):
                    filtered_data[item] = val
                    
            datacopy = filtered_data

        self.setDataInTreeWidget(datacopy)

        # Rebuilding the tree from scratch above loses whatever order a header-click
        # sort had put it in, since QStandardItemModel doesn't keep newly-appended
        # rows in sorted order automatically. Re-apply it if one is active.
        sort_section = self.tree.header().sortIndicatorSection()
        sort_order = self.tree.header().sortIndicatorOrder()
        if sort_section >= 0:
            self.model.sort(sort_section, sort_order)

        auto_expand = self.expand_checkbox.checkState()
        if auto_expand:
            self.tree.expandAll()
        else:
            self.tree.collapseAll()

        if save_and_restore_expanded_state:
            self.restoreExpandedStates(self.tree,previous_expanded_states)

        # Needs to happen after expanding/collapsing, since resizeColumnToContents
        # only takes currently-visible (expanded) rows into account.
        self.tree.resizeColumnToContents(0)

    def setClipboard(self, with_text):
        # Need to add name and curly braces!
        text_str = with_text#.encode()
        clipboard = self.app.clipboard()
        clipboard.setText(text_str, QClipboard.Mode.Clipboard)

        if clipboard.supportsSelection():
            clipboard.setText(text_str, QClipboard.Mode.Selection)
        
        #QThread.msleep(self,1)
    
    @staticmethod
    def addFractalName(fractal_upr, fractal_name):
        return fractal_name + " {\n" + fractal_upr + "\n}"
                
    def treeDoubleClicked(self, index):
        self.openImages()

    def openImages(self):
        print("Start of openImages")
        images = []
        entries = self.getActiveSelectionEntries()
        missing_images = []

        open_progress = QProgressDialog()
        open_progress.setLabelText("Finding image files..")
        open_progress.setRange(0,len(entries))
        open_progress.setWindowModality(Qt.WindowModality.WindowModal)
        open_progress.setAutoClose(True)
        open_progress.forceShow() #exec_()

        for ixx,entry in enumerate(entries):
            if self.verbose:
                print(f"Iterating Row: {ixx}")
            open_progress.setValue( ixx+1 )

            if open_progress.wasCanceled():
                return

            image_path = entry.get('image_path')
            if image_path:
                images.append( image_path )
                if self.verbose:
                    print(f"  appended image {image_path}")
            else:
                missing_images.append( entry['upr_name'].split(":")[0] )

        if not images :
            self.displayMessage("No Images Found",
                                "No images were found for any of the\nselected fractals I'm afraid!",
                                None)
            return

        if len(images) < len(entries):
            msg = "We only found images for " + str(len(images)) + " out of " + str(len(entries)) + " selected fractals!"
            
            msg_dtl = "Missing:\n  "
            msg_dtl += "\n  ".join(missing_images)
            btn_labels = ["Open Images Anyway","Cancel"]
            btn_roles = [ QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.RejectRole ]
            msg_box = self.displayMessage("Some Images Were Not Found", 
                                          msg,
                                          msg_dtl,
                                          btn_labels,
                                          btn_roles,)
            clicked_btn = msg_box.clickedButton()
            button_role = msg_box.buttonRole( clicked_btn )
            print("Got button_role: " + str( button_role ))
            if button_role == QMessageBox.ButtonRole.RejectRole :
                return
           
        image_cmd = [ "gwenview", "-f", "-s", ]
        image_cmd.extend(images)
        image_cmd.append("&")
        
        _pope = subprocess.Popen( image_cmd, stdout=subprocess.PIPE )
        # lines = pope.stdout.readlines()
        
    def copyToClipboard(self):
        print("Copy UPR Clicked!")

        all_uprs_dict = self.upr_lib.getAllUPRs()

        # If multiple are selected, you get the last one that was selected -
        # same as the "Copy UPR to Clipboard" button's tooltip always promised.
        for entry in self.getActiveSelectionEntries():
            full_upr_path = entry['upr_file']
            upr_name_key = entry['upr_name']

            upr_data_dict = all_uprs_dict.get(full_upr_path)
            if upr_data_dict is None:
                print("ERROR: Upr file was not found in library! This is most odd! Bailing out.")
                return

            if upr_name_key in upr_data_dict:
                if self.verbose:
                    print("\nKey found!\n")
                # This is a string.. the actual fractal code in plaintext UPR
                upr_data = upr_data_dict[upr_name_key]
                fractal_with_name = self.addFractalName(upr_data, upr_name_key.split(":")[0])
                self.setClipboard( fractal_with_name )

    def quitButtonClicked(self, by_hotkey=False):
        if self.verbose:
            if not by_hotkey:
                print("\nQuit button pressed! Exiting.\n")
            else:
                print("\nQuit-by-hotkey callback, quitting!\n")
        sys.exit(0)
        
    def setDataInTreeWidget(self, data_val, root=None):
        if self.verbose:
            print("setDataInTreeWidget start..")
        
        self.model.setRowCount(0)
        if root is None:
            root = self.model.invisibleRootItem()
            
        seen = {}
        
        values = deque(data_val.values())
        if self.verbose > 4:
            print("values: " + str(data_val))

        # Filtering can keep an entry while dropping its parent. Such entries
        # go at the top level, since requeueing them to wait for a parent
        # that will never arrive loops forever.
        present_ix = { value['ix'] for value in data_val.values() }

        while values:
            value = values.popleft()

            if value['level'] == 0 or value['parent_id'] not in present_ix:
                parent = root
            else:
                pid = value['parent_id']
                if pid not in seen:
                    values.append(value)
                    continue
                parent = seen[pid]
                
            ijx = value['ix']
            
            upr_filex = value['upr_file']
            
            #print( "Appendrow!" + str( value ) )
            # Better be no other colons in there eh!!?
            upr_name_no_ix, upr_name_ix = value['upr_name'].split(":")
            
            icon_item = QStandardItem()
            # This worked! however we currently have to scroll to the right to see them!
            if 'thumb_path' in value :
                thumb_filex = value['thumb_path']
                #print(f"{thumb_filex=}")
                #test_png_path = "/home/dan/quid/ultraFields/.thumb.baseBlue_doublebasin_crnr_moreIterations.png"
                icon_item.setData(QIcon(thumb_filex), Qt.ItemDataRole.DecorationRole)

            num_layers = value.get('num_layers')
            compressed_size = value.get('compressed_size')
            upr_file_basename = os.path.basename(upr_filex)

            name_item = QtGui.QStandardItem( upr_name_no_ix )
            name_item.setData( upr_name_no_ix, Qt.ItemDataRole.UserRole )
            name_item.setData( value, UPR_ENTRY_ROLE )

            file_item = QtGui.QStandardItem( upr_file_basename )
            file_item.setData( upr_file_basename, Qt.ItemDataRole.UserRole )

            index_item = QtGui.QStandardItem( upr_name_ix )
            index_item.setData( upr_name_ix, Qt.ItemDataRole.UserRole )

            layers_item = QtGui.QStandardItem( str(num_layers) if num_layers is not None else "" )
            layers_item.setData( num_layers if num_layers is not None else "", Qt.ItemDataRole.UserRole )

            size_item = QtGui.QStandardItem( f"{compressed_size:,}" if compressed_size is not None else "" )
            size_item.setData( compressed_size if compressed_size is not None else "", Qt.ItemDataRole.UserRole )

            parent.appendRow([
                icon_item,
                name_item,
                file_item,
                index_item,
                layers_item,
                size_item,
                ])
            seen[ijx] = parent.child(parent.rowCount() - 1)

        # ix -> tree item, so graph<->tree selection syncing can find a given
        # entry's row without walking the whole model.
        self.ix_to_tree_item = seen

        if self.verbose:
            print("setDataInTreeWidget finished.")
    
def listsMatchShortest( alist, blist ) :
    runlen = min( len(alist), len(blist) )

    if runlen == 0 :
        return False

    for ixx in range(runlen) :
        if alist[ixx] != blist[ixx] :
            return False
    return True    

def rebuildParentingDataAsDict(data_val, shell_ui_enable=False, verbose=0) :
    spinnerj = logspinner.spinning_cursor() if shell_ui_enable else None
    
    # Flatten data, such that structure can
    # be reconstructed at the end.
    flat_data = {}
    _upr_count = 0
    upr_map = {}
    
    for iqx, upr_data in enumerate(data_val) :
        #for upr_data_name in upr_data :
        flatkey = upr_data['upr_file'] + ":" + upr_data['upr_name']
        flat_data[ flatkey ] = upr_data
        flat_data[ flatkey ]['ix'] = iqx

        # The point here is to get an index for each file by making a list
        if upr_data['upr_file'] not in upr_map :
            upr_map[ flatkey ] = 1
        
        #print("ix: " + str( ix ) )
        #print("datalen: " + str(len(data)))
        
        percent = iqx / max( (len(data_val) - 1), 1 )
        logspinner.write_spinner(spinnerj,
                                 prefix="Rebuilding fractal indices",
                                 percent=percent)

    # New flat ids: 'ix'!
    if verbose:
        print( "rebuildParentingDataAsDict, Total entries in flat_data: " + str( len( flat_data.keys() ) ) )
    # Make map of all dotdotdots.
    # Find all parents for dotdotdots.
    # Use deque so that if parent is not
    # found we can chuck the entry on the
    # end of the list for re-processing.
    # Modify parent id (a 2-part id now)
    # Fix level.
    ddd = '_dotdotdot_'
    count = 0
    flat_kp = {}

    # Use a deque of tuples here and check whether the parent is
    # included yet, and if not, push the current entry to the end of the queue.
    
    # End the spinner line
    logspinner.endSpinner(spinnerj)
    
    # Index every entry by its underscore-split name, so finding the entries
    # whose name is a strict underscore-prefix of another's takes a handful
    # of dict lookups instead of a scan of the whole library per entry
    # (that scan was O(n^2), ~55s for ~5k entries). Each list keeps
    # flat_data's insertion order, which matters below since the last
    # match wins.
    keys_by_name_split = {}
    for kpos, fupr_key in enumerate( flat_data.keys() ):
        name_split = tuple( fupr_key.split(":")[1].split("_") )
        keys_by_name_split.setdefault( name_split, [] ).append( (kpos, fupr_key) )

    dq = deque( flat_data.items() )
    dqiter_count = 0
    oldq = len(dq)
    
    if verbose > 2:
        print("Original double-ended-queue length: " + str(oldq))
        
    while dq:
        # TODO piss-off all these acronyms! too lazy!
        dqv = dq.popleft()
        upr_key, upr_data = dqv
        ldq = len( dq )
        # percent = 1 / (ldq - 1) if ldq > 1 else 1.0
        # This whole extendable-queue thing seems to be hard to make a
        # good percentage for.. but I noticed that we most often end up 
        # with less actual count than the original list had entries..
        # so trying this, dividing the current count by the original length
        # Guess it will never reach 100% unl;ess we do something about it..
        # so this should swap to 100% on the last iter..
        percent = dqiter_count / oldq if ldq > 1 else 1.0
        logspinner.write_spinner(spinnerj,
                                 prefix="Preparing Fractal Tree",
                                 percent=percent)
        #print("percent: " + str(percent))
        #sys.stdout.flush()
        
        #if verbose > 3:
        # sys.stdout.write('Processing ... ' + str(upr_key) + '\n')
        
        for uijx,upr_data_name in enumerate(upr_data):
            # Check for len zero!? no need, won't have iterated!
            _upercent = uijx / len(upr_data)

            if upr_data_name == 'upr_name':
                upr_name = upr_data[ upr_data_name ]
                
                if True: #not ddd in upr_name :
                    #count += 1
                    
                    # TODO: find if any other upr in whole list has shared prefix and parent to it
                    # .. somehow..  then extend this to the dotdotdot ones as well
                    
                    colonsplit = upr_name.split( ":" )
                    uscoresplit = colonsplit[0].split("_")
                    
                    if verbose:
                        print("got uscoresplit: " + str( uscoresplit ) )
                    
                    # Every other fractal whose name is a strict underscore-prefix of this one.
                    parent_matches = []
                    for prefix_len in range( 1, len( uscoresplit ) ):
                        parent_matches.extend( keys_by_name_split.get( tuple( uscoresplit[:prefix_len] ), () ) )
                    # Visit them in flat_data order, as the old full scan did, so the last match still wins.
                    parent_matches.sort()

                    for _kpos, fupr_key in parent_matches:
                        fupr_data = flat_data[ fupr_key ]
                        if 'ix' in fupr_data :
                            if verbose:
                                print( "Matched a parent! setting base-type parent_id to: " + str( fupr_data['ix'] ) )
                            upr_data['parent_id'] = fupr_data['ix']
                            upr_data['level'] = max(1, fupr_data['level'] + 1)
                            flat_kp[upr_key] = upr_data
                            flat_kp[fupr_key] = fupr_data
                            # print( "parent data: " + str( fupr_data ) )
                            #print( "upr data: " + str( upr_data ))
                                
                    # Add it whether it was modified or otherwise!
                    flat_kp[upr_key] = upr_data
                    count += 1
                    
                #for fupr_key in flat_kp:
                #    print("fupr_key:" + str( fupr_key ))

                # Just to fool pyCharm
                temp = False
                if temp: # ddd in upr_name: # Still not working yet!
                    
                    # TODO: make it so that this is an additional reparenting after the first name-based pass!
                    # Currently the tree is flat inside all dotdotdot parents!
                    
                    if verbose > 4 :
                        print("\n\nUPR name contains dotdotdot!: " + upr_name)
                        
                    dotsplit = upr_name.split( ddd )
                    
                    if len( dotsplit ) > 1 :
                        restspl = dotsplit[1].strip("_").split("_")
                        # print( "dotsearch: " + str( restspl[0] ) )
                        
                        # ORDER squared? warning!!! At least there's a bailout!?
                        # This is slow.
                        # Check all other fractal names for this prefix..
                        #
                        for ixf, fupr_key in enumerate( flat_data.keys() ):                            

                            # print( "Checking fupr_key:" + str( fupr_key ) )
                            #print( "Post dotdotdot is: " + str( restspl[0] ) )

                            # Must end with this! (underscore plus the bit before the dotdotdot!)
                            frac_key = "_" + restspl[0]
                            
                            frac_name = fupr_key.split(":")[1]
                            # Extra underscores at start and end be damned!! 
                            # Treat as error and remove.. probably a bug at some level!
                            frac_name_underscore_split = frac_name.strip("_").split("_")
                            
                            # First part(s) up to the dotdotdot need to match too!
                            # get ready to compare lists..
                            
                            pdspl = dotsplit[0].split("_")

                            # Probably need to find the longest match here instead of the
                            # first!!
                            # Conservative for the moment!
                            
                            # This condition says: We have found a fractal that ends with
                            # _example, where 'example' is the first word /after/ ddd: '_dotdotdot_'
                            # in the fractal name that we are searching-for-a-parent for, 
                            # AND (I think) it's not a sibling dotdotdot (will have ddd at minus two)
                            # AND the bit before ddd on both sides is equal.
                            
                            if frac_name.endswith( frac_key ) \
                                and frac_name_underscore_split[-2] != ddd \
                                and listsMatchShortest( pdspl, frac_name_underscore_split ) :
                                
                                if verbose > 4 :
                                    print("Pdspl is: " + str(pdspl) )
                                    print("frac_name_underscore_split is: " + str(frac_name_underscore_split) )
                                    print("frac_key is: " + frac_key)
                                    print("MATCHED parent upr key: " + fupr_key)
                                    
                                #print( "\nThis UPR: " +  upr_name.split(":")[0] )
                                #print("MATCHED parent upr key: " + os.path.basename(frac_name))
                                
                                fupr_data = flat_data[ fupr_key ]
                                if 'ix' in fupr_data :
                                    #print( "setting parent_id to: " + str( fupr_data['ix'] ) )
                                    upr_data['parent_id'] = fupr_data['ix']
                                    upr_data['level'] = max(1, fupr_data['level'] + 1)
                                    flat_kp[upr_key] = upr_data
                                    flat_kp[fupr_key] = fupr_data
                                # print( "parent data: " + str( fupr_data ) )
                                #print( "upr data: " + str( upr_data ))
                                count += 1
                                # early bailout for debug:
                                #if count > 100 :
                                #    return flat_kp
                            else:
                                pass
                                #print("no match!: " + frac_key )
        dqiter_count += 1
        
    # End the spinner line
    logspinner.endSpinner(spinnerj)
    if verbose:
        print("Final count: " + str( count ))

    return flat_kp

def addRenderInfoToData(data, renders, verbose=False):
    """Attaches each entry's renders (2k, 8k, 16k... see UPRLibrary.loadRenders)
    as a list under 'renders', largest first, including render-log stats
    where the image has a log."""
    renders_by_name = defaultdict(list)
    for render in renders:
        renders_by_name[ render['fractal_name'] ].append(render)

    name_counts = Counter( val['upr_name'].split(":")[0] for val in data.values() )
    num_with_renders = 0
    for val in data.values():
        clean_upr_name = val['upr_name'].split(":")[0]
        upr_file_basename = os.path.basename(val['upr_file'])
        candidates = renders_by_name.get(clean_upr_name, [])
        if name_counts[clean_upr_name] == 1:
            # Logs often name a different .upr file than the one the fractal
            # lives in now (e.g. ultraMeta.upr was renamed to
            # ultraMeta_YET_ANOTHER.upr), so a unique name is enough.
            matched = list(candidates)
        else:
            # Several files have a fractal of this name: only trust a render
            # whose log says which file it came from.
            matched = [ render for render in candidates if render['upr_file'] == upr_file_basename ]
        if matched:
            val['renders'] = sorted( matched, key=lambda render: -((render['width'] or 0) * (render['height'] or 0)) )
            num_with_renders += 1

    if verbose:
        print("Renders found for " + str(num_with_renders) + " of " + str(len(data)) + " entries.")
    return data

def addImagePathsToData(data, verbose=False):
    config_dict = UPRLibrary(verbose=verbose).getConfigDict()
    if verbose:
        print(f"{config_dict=}!")
    
    if not 'upr_image_root_glob' in config_dict:
        # TODO: complain!
        return data
    
    # DONE: allowed this to support a glob, so we can have:
    # /home/dan/quid/ultra*/ and ignore everything else that is
    # in that dir.
    image_root_glob = config_dict["upr_image_root_glob"]
    if verbose:
        print(f"\n Image root glob: {image_root_glob}")
    
    upr_image_extensions = config_dict["upr_image_extensions"].split(",")
    #print(f"\n upr_image_extensions: {upr_image_extensions}")
    
    # TODO: learn pathlib!
    dirglob = glob.glob(image_root_glob)
    total_count = 0
    glob_and_thumb_info = {}
    
    for globbed_dir in dirglob :
        if not os.path.isdir(globbed_dir):
            continue
        if verbose:
            print(f"  {globbed_dir}")
        for img_ext in upr_image_extensions:
            # Ignore subdirs for now!
            # eventually.. include specifically 16k/ .. but do that
            # when carefully associating lowres with highres renders of
            # the same thing!
            globbed_files = glob.glob(os.path.join(globbed_dir, f"*.{img_ext}"))
            count = 0
            thumb_count = 0
            for globbed_file in globbed_files :
                #if count > 0 :
                #    print("BAIL at 2!")
                #    break
                
                if not os.path.isfile(globbed_file):
                    if verbose:
                        print( f"Globbed thing was not a file! Skipping: {globbed_file}" )
                    continue
                    
                total_count += 1
                dirpart = os.path.dirname(globbed_file).strip()
                basepart = os.path.basename(globbed_file).strip()
                
                if os.path.basename(basepart).startswith(".thumb"):
                    # Skip any existing .thumb files!
                    continue
                
                # hmm.. magick doesn't really seem too fond of these files unforunately :/
                # magick_cmd = [ "magick",
                
                # TODO: replace ext with png or jpg here!
                thumb_file = os.path.join(dirpart, f".thumb.{basepart}")
                thumb_file_split = list(os.path.splitext(thumb_file))
                thumb_file_split[1] = "png"
                thumb_file = ".".join(thumb_file_split)
                
                if not os.path.isfile(thumb_file):
                    if verbose:
                        print("\n")
                        print(f"    {total_count} : {globbed_file}")                    
                        print(f"No thumb file found, regenerating: {thumb_file}")
                    
                        print( f"Before checking thumb file: {os.path.isfile(thumb_file)=}" )
                        print( f"Before checking thumb file: {glob.glob(thumb_file)=}" )

                    # ffmpeg's tiff decoder (and Pillow's own decompression-bomb guard)
                    # chokes on the huge 16k renders - skip those rather than retrying
                    # (and failing) every startup. Eventually we want to associate
                    # these with their 2k counterparts instead of thumbnailing them!
                    try:
                        with Image.open(globbed_file) as im:
                            img_w, img_h = im.size
                    except Image.DecompressionBombError:
                        if verbose:
                            print(f"Skipping (image too large to thumbnail): {globbed_file}")
                        continue
                    except Exception as e:
                        if verbose:
                            print(f"Skipping (could not read image dimensions): {globbed_file} ({e})")
                        continue

                    ff_cmd = [ "ffmpeg",
                               "-i", globbed_file, 
                               "-vf", "thumbnail,scale=64:-1",
                               thumb_file ]
                           
                    if verbose:
                        print( "Running: " + " ".join( ff_cmd ) )
                    pope = subprocess.Popen( ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE )
                    status = pope.wait()

                    if verbose:
                        print( f"Command status was: {status}" )

                    if verbose :
                        print( f"After generating thumb file: {os.path.isfile(thumb_file)=}" )
                        print( f"After generating thumb file: {glob.glob(thumb_file)=}" )
                    thumb_count += 1
                    
                count += 1
                
                if os.path.isfile(thumb_file) :
                    globbed_file_basename = os.path.basename( globbed_file )
                    gf_basename_splitext = os.path.splitext( globbed_file_basename )
                    if verbose :
                        print(f"Recording {gf_basename_splitext[0]=} thumb: {thumb_file}")
                    glob_and_thumb_info[ gf_basename_splitext[0] ] = [ globbed_file, thumb_file ]
    
    for key, val in data.items() :
        if 'upr_name' in val :
            upr_name_with_index = val['upr_name']
            clean_upr_name = upr_name_with_index.split(":")[0]
            
            if verbose:
                print(f"Val has clean_upr_name: {clean_upr_name}")
            
            if clean_upr_name in glob_and_thumb_info :
                if verbose:
                    print(f"Appending image and thumb for {clean_upr_name}")
                # Note!: Adding to a mutable entry here! Slightly dangerous!
                val[ 'image_path' ] = glob_and_thumb_info[ clean_upr_name ][0]
                val[ 'thumb_path' ] = glob_and_thumb_info[ clean_upr_name ][1]
            else :
                if verbose:
                    print(f"Not found in thumbs: {clean_upr_name}")
                
    #print("\n")
    #print(data)
    #print("\n")
    
    return data
    
if __name__ == "__main__":
    # TODO get some options here!
    # verbose
    # limit num uprs
    argparser = argparse.ArgumentParser()
    
    argparser.add_argument("-v", 
                           "--verbose", 
                           help="Turn on verbose-mode.",
                           default=0,
                           action="count" )
                           
    # TODO: eventually allow which upr file to use to be specified/
    # Note: becomes args.num_upr_file_limit (just to help with breadcrumbs)
    argparser.add_argument("-f", 
                           "--num-upr-file-limit", 
                           help="Limit the number of UPR /files/ to load overall.",
                           type=int,
                           default=None,
                           action="store" )
                           
    # TODO: Add the ability to filter/glob or match entries instead of just truncating the list!
    argparser.add_argument("-n", 
                           "--num-upr-limit", 
                           help="Limit the number of UPRs to load from each .upr file.",
                           type=int,
                           default=None,
                           action="store" )
                           
    argparser.add_argument("-c", 
                           "--config_filename", 
                           help="Use specified filename as config, default is 'uprLibrary.config'.",
                           type=str,
                           default=None,
                           action="store" )
                      
    argparser.add_argument("-u", 
                           "--disable-shell-ui", 
                           help="Turn off shell ui like spinners/etc while loading.",
                           default=False,
                           action="store_true" )
                           
    argparser.add_argument("-g", 
                           "--disable-gui", 
                           help="Turn off main ui.. for debugging or when you only want to see shell output.",
                           default=False,
                           action="store_true" )

    argparser.add_argument("--no-cache",
                           help="Don't read or write the parsed-UPR cache, always re-parse every .upr file.",
                           default=False,
                           action="store_true" )

    argparser.add_argument("--rebuild-cache",
                           help="Discard the parsed-UPR cache and re-parse every .upr file into a fresh one.",
                           default=False,
                           action="store_true" )

    args = argparser.parse_args()
    
    shell_ui = not args.disable_shell_ui
    if shell_ui:
        # Print this whether verbose or otherwise when shell_ui is on!
        # Otherwise expectation is complete silence!
        print("\nUprLib starting up..")
    
    if args.verbose:
        print("\nVerbosity has nonzero value: " + str( args.verbose) )
    # home_directory = expanduser('~')
    
    app = QApplication(["UPRLib"])
    
    app.setStyle("Fusion")

    # Now use a palette to switch to dark colors:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.black)
    palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
    # noinspection PyArgumentList
    app.setPalette(palette)
    # QToolTip::setPalette(palette); for the settings to take effect on tooltips
    
    #style_file = QFile(":/dark.qss")
    #style_file.open(QFile.ReadOnly | QFile.Text)
    #stream = QTextStream(style_file)
    #app.setStyleSheet(stream.readAll())
    
    style_hints = app.styleHints()
    
    #print( "\n" + "\n".join( dir( style_hints ) ) + "\n" )
    
    # No property afaict
    #ahp = style_hints.property("QT::ColorScheme")
    #print("ahp!:" + str( ahp ))

    
    # Could pass a config file maybe?
    # new arg: config_filename
    
    upr_lib = UPRLibrary( num_upr_file_limit=args.num_upr_file_limit,
                          num_upr_limit=args.num_upr_limit,
                          config_filename=args.config_filename,
                          shell_ui=shell_ui,
                          use_cache=not args.no_cache,
                          rebuild_cache=args.rebuild_cache,
                          verbose=args.verbose )
    
    # Move this into a button-press!
    all_upr = upr_lib.loadAllUpr()
    # curr_upr = upr_lib.getCurrentUpr()
    # print("Current UPR: " + curr_upr)
    # upr_file = UPRFile(curr_upr)
    # print("This UPR file has: " + str(len(upr_file.lines)) + " lines.")
    upr_dict = upr_lib.getAllUPRs()
    upr_meta_dict = upr_lib.getAllUPRMeta()
    # upr_file.getUprDict()
    upr_dict_keys = upr_dict.keys()

    if args.verbose:
        print( "Found " + str( len( upr_dict_keys ) )  + " upr files.")
        
    raw_data = []
    totalix = 0
    
    
    spinner = logspinner.spinning_cursor() if shell_ui else None
    
    for uix, upr_file in enumerate( sorted( upr_dict_keys ) ):
        for ix, uprname in enumerate( upr_dict[ upr_file ].keys() ):
            uprname_clean = uprname.split(":")[0]
            upr_meta = upr_meta_dict.get(upr_file, {}).get(uprname, {})
            raw_data.append( { 'level':0,
                           'dbid':totalix,
                           'uprid':str(uix)+":"+str(ix),
                           'parent_id':-1,
                           'upr_name': uprname,
                           'upr_file': upr_file,
                           'num_layers': upr_meta.get('num_layers'),
                           'compressed_size': upr_meta.get('compressed_size') } )
            totalix += 1
            
    if args.verbose:
        print("Raw data had " + str( len( raw_data ) )  + " entries..")
    
    # Note, this method also /flattens/ the data to a single dict where
    # previously it was a list of dicts!
    
    data = rebuildParentingDataAsDict(raw_data, shell_ui_enable=shell_ui, verbose=args.verbose)
    
    if args.verbose:
        print("rebuildParentingDataAsDict returned " + str( len( data ) )  + " entries..")
    
    if args.verbose:
        print("\n")
        for key, val in data.items():
            print( f"{key}" )
            for ikey, ival in val.items() :
                print( f"    {ikey} : {ival}" )
    
    data = addImagePathsToData(data, verbose=args.verbose)

    data = addRenderInfoToData(data, upr_lib.loadRenders(), verbose=args.verbose)

    data = dict(sorted(list(data.items()) , key=lambda item: item[0]))
    
    if args.disable_gui:
        if args.verbose:
            print("\nDisable GUI arg specified, bailing out instead of showing the UI!\n")
        sys.exit(0)
        
    sdwindow = UPRLibWindow(upr_lib, data, app, verbose=args.verbose)
    
    screen = app.primaryScreen()
    screenGeometry = screen.size()
    # Eventually make this a pref:
    # Make the window as big as the main
    # screen, and centered on it.
    xsize = screenGeometry.width()
    ysize = screenGeometry.height()
    sdwindow.resize( xsize, ysize )
    # Move to the middle
    vgeo = sdwindow.frameGeometry()
    center_point = screen.availableGeometry().center()
    vgeo.moveCenter(center_point)
    sdwindow.move( vgeo.topLeft() )
    sdwindow.show()
    if args.verbose > 1:
        print("Init all done, starting app!")
    app.exec()
    # Seems like any prints on lines after this might never happen (Even with flush!)
