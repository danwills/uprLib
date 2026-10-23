#!/usr/bin/env python
"""Detects UPR entries that are zoom-ins on other entries.

Every layer of a UPR has a 'mapping:' section giving its view of the complex
plane, e.g. "center=0.8469/-0.8276 magn=189.07 angle=-90". Measured against
real renders (comparing crops of parent images with child images), Ultra
Fractal's convention is: the image's visible height is 4/magn, its width is
that times width/height, the imaginary axis points up the image, and angle
rotates the view counter-clockwise (degrees).

Entries are only related as zooms if they're also the same picture in some
sense. Tiers, from strictest:
  TIER_EXACT   - identical apart from the view (and title/credits/size).
  TIER_RECOLOR - some layer has the same formula and formula parameters, so
                 the same fractal, but coloring/gradients/layers may differ.
  TIER_VARIANT - some layer uses the same formula, parameters may differ.

Views can be extremely deep (magn up to ~1e27, centers with ~90 digits), so
centers are kept as Decimal and positions are only ever computed relative
to a parent, in units of the parent's image height.
"""

import re
import math
import bisect
import sqlite3
from decimal import Decimal, InvalidOperation, Context, localcontext

# Enough digits for the deepest centers (~90 digits). Used explicitly via
# localcontext, since decimal contexts are per-thread and the index may be
# built on a background thread.
DECIMAL_CONTEXT = Context(prec=120)

# Bump whenever parseEntry's or findZoomParents' output changes, so a saved
# index from older code gets rebuilt instead of reused.
ZOOM_INDEX_VERSION = 1

TIER_EXACT = 0
TIER_RECOLOR = 1
TIER_VARIANT = 2
TIER_NAMES = { TIER_EXACT: "exact", TIER_RECOLOR: "recolor", TIER_VARIANT: "variant" }

# How far (as a fraction of the parent image) a child's corners may poke
# outside the parent and still count as inside it - the views were usually
# picked by hand, so allow for a little slop at the edges.
CONTAINMENT_TOLERANCE = 0.01
# A child must be at least this much more magnified than its parent; an
# equal-magn view that's just moved over is a pan, not a zoom.
MIN_ZOOM_RATIO = 1.01

section_rx = re.compile(r'^(\w+):\s*$', re.M)
continuation_rx = re.compile(r'\\\s*\n\s*')
# View settings and per-entry header values, all stripped (in one pass, as
# this runs over every payload) when comparing entries.
ignored_values_rx = re.compile(r'\b(?:center|magn|angle)=\S+|\b(?:title|credits)="[^"]*"|\b(?:width|height)=\d+')
# Iteration/periodicity settings change how a formula is calculated, not
# which fractal it is, so they don't count against TIER_RECOLOR.
calc_setting_rx = re.compile(r'\b(maxiter|percheck)=\S+')
formula_id_rx = re.compile(r'filename="[^"]*"\s+entry="[^"]*"')
# Pixel formulas (e.g. Standard.ufm's "Pixel", in ~800 entries) are image-
# space helpers for coloring/texture layers, not a fractal - sharing one
# says nothing about two entries being the same picture.
pixel_formula_rx = re.compile(r'\bentry="[^"]*pixel[^"]*"', re.I)


class View:
    """One entry's view: center (Decimal), magn, angle (degrees) and the
    image aspect ratio (width/height)."""
    __slots__ = ('center_x', 'center_y', 'center_x_float', 'center_y_float', 'magn', 'magn_float',
                 'angle', 'aspect', 'layers_agree')

    def __init__(self, center_x, center_y, magn, angle, aspect, layers_agree):
        self.center_x = center_x
        self.center_y = center_y
        self.center_x_float = float(center_x)
        self.center_y_float = float(center_y)
        self.magn = magn
        self.magn_float = float(magn)
        self.angle = angle
        self.aspect = aspect
        self.layers_agree = layers_agree


def _sections(payload):
    """List of (section_name, body_text) pairs in order."""
    marks = list(section_rx.finditer(payload))
    return [ (mark.group(1), payload[mark.end():(marks[i + 1].start() if i + 1 < len(marks) else len(payload))])
             for i, mark in enumerate(marks) ]


def _normalize(text):
    return " ".join(ignored_values_rx.sub("", text).split())


def parseEntry(payload):
    """Returns (view, exact_sig, recolor_sigs, variant_sigs) for one UPR
    payload. view is None when the entry has no usable mapping (none at all,
    or an animated one - those are quoted keyframe strings)."""
    # Long values (deep-zoom centers especially) are wrapped onto following
    # lines with a trailing backslash.
    if '\\' in payload:
        payload = continuation_rx.sub('', payload)
    sections = _sections(payload)

    mappings = []
    for name, body in sections:
        if name != "mapping":
            continue
        center = re.search(r'\bcenter=(\S+)/(\S+)', body)
        magn = re.search(r'\bmagn=(\S+)', body)
        angle = re.search(r'\bangle=(\S+)', body)
        try:
            mappings.append(( Decimal(center.group(1)), Decimal(center.group(2)),
                              Decimal(magn.group(1)) if magn else Decimal(1),
                              float(angle.group(1)) if angle else 0.0 ))
        except (AttributeError, InvalidOperation, ValueError):
            mappings.append(None)

    view = None
    if mappings and mappings[0] is not None and mappings[0][2] > 0:
        header = dict(sections).get("fractal", "")
        width = re.search(r'\bwidth=(\d+)', header)
        height = re.search(r'\bheight=(\d+)', header)
        aspect = int(width.group(1)) / int(height.group(1)) if width and height and int(height.group(1)) else 1.0
        # Layers can have their own mappings; the first layer's is used, and
        # layers_agree says whether that describes the whole image.
        view = View(*mappings[0], aspect=aspect, layers_agree=len(set(mappings)) == 1)

    formula_bodies = [ body for name, body in sections
                       if name == "formula" and not pixel_formula_rx.search(body) ]
    exact_sig = _normalize(payload)
    recolor_sigs = frozenset( _normalize(calc_setting_rx.sub("", body)) for body in formula_bodies )
    variant_sigs = frozenset( match.group(0) for match in
                              (formula_id_rx.search(" ".join(body.split())) for body in formula_bodies) if match )
    return view, exact_sig, recolor_sigs, variant_sigs


def childCorners(parent, child):
    """Where the child's image corners land in the parent's image, in the
    parent's normalized image coords (u right, v down, both 0..1 across the
    image). Returns the child's (0,0), (1,0), (0,1) and (1,1) corners."""
    # Everything in units of the parent's image height (4/parent magn), with
    # the offset between centers done in Decimal so deep zooms stay exact.
    with localcontext(DECIMAL_CONTEXT):
        scale = parent.magn / 4
        dx = float((child.center_x - parent.center_x) * scale)
        dy = float((child.center_y - parent.center_y) * scale)
        ratio = float(parent.magn / child.magn)
    child_cos = math.cos(math.radians(child.angle))
    child_sin = math.sin(math.radians(child.angle))
    parent_cos = math.cos(math.radians(-parent.angle))
    parent_sin = math.sin(math.radians(-parent.angle))
    corners = []
    for u, v in ((0, 0), (1, 0), (0, 1), (1, 1)):
        # Child image coords -> complex-plane offset from the child's center.
        local_x = (u - 0.5) * child.aspect * ratio
        local_y = -(v - 0.5) * ratio
        plane_x = dx + local_x * child_cos - local_y * child_sin
        plane_y = dy + local_x * child_sin + local_y * child_cos
        # Complex-plane offset from the parent's center -> parent image coords.
        rel_x = plane_x * parent_cos - plane_y * parent_sin
        rel_y = plane_x * parent_sin + plane_y * parent_cos
        corners.append(( rel_x / parent.aspect + 0.5, -rel_y + 0.5 ))
    return corners


# Below this parent magn, float centers are accurate to well under a pixel
# of the parent image, so they can be used to cheaply reject far-away views.
FLOAT_SAFE_MAGN = 1e10

def contains(parent, child):
    if child.magn_float < parent.magn_float * MIN_ZOOM_RATIO:
        return False
    if parent.magn_float < FLOAT_SAFE_MAGN:
        # A contained child's center is inside the parent image, so within
        # half its diagonal of the parent's center (in parent-height units).
        # Rejects nearly every candidate without the Decimal math below.
        scale = parent.magn_float / 4
        dx = (child.center_x_float - parent.center_x_float) * scale
        dy = (child.center_y_float - parent.center_y_float) * scale
        reach = 0.5 * math.hypot(parent.aspect, 1) + CONTAINMENT_TOLERANCE
        if dx * dx + dy * dy > reach * reach:
            return False
    low, high = -CONTAINMENT_TOLERANCE, 1 + CONTAINMENT_TOLERANCE
    return all(low <= u <= high and low <= v <= high for u, v in childCorners(parent, child))


def findZoomParents(parsed, max_tier):
    """parsed: { key: parseEntry(payload) } for entries with a view. Returns
    { child_key: (parent_key, tier) } where parent is the most-magnified
    entry related to the child at max_tier or stricter whose view fully
    contains the child's view, and tier is the strictest tier the two are
    related at."""
    # Group keys by every signature at each allowed tier.
    groups = {}
    for key, (view, exact_sig, recolor_sigs, variant_sigs) in parsed.items():
        sigs = [ (TIER_EXACT, exact_sig) ]
        if max_tier >= TIER_RECOLOR:
            sigs += [ (TIER_RECOLOR, sig) for sig in recolor_sigs ]
        if max_tier >= TIER_VARIANT:
            sigs += [ (TIER_VARIANT, sig) for sig in variant_sigs ]
        for sig in sigs:
            groups.setdefault(sig, []).append(key)

    # Each group sorted most magnified first, with its (negated) magns
    # alongside for bisecting.
    sorted_groups = []
    groups_of_key = {}
    for (tier, _sig), keys in groups.items():
        if len(keys) < 2:
            continue
        keys.sort(key=lambda key: -parsed[key][0].magn_float)
        group = (tier, keys, [ -parsed[key][0].magn_float for key in keys ])
        sorted_groups.append(group)
        for key in keys:
            groups_of_key.setdefault(key, []).append(group)

    parents = {}
    for key, key_groups in groups_of_key.items():
        view = parsed[key][0]
        best = None
        for tier, keys, negated_magns in key_groups:
            # Only less-magnified entries can contain this one: walk them
            # from the most magnified down, so the first container found is
            # this group's tightest.
            for other in keys[bisect.bisect_right(negated_magns, -view.magn_float):]:
                other_view = parsed[other][0]
                if best is not None and other_view.magn_float < best[0]:
                    break
                if contains(other_view, view):
                    # Tightest container wins; between equally tight ones,
                    # the stricter tier. The same parent found through a
                    # stricter group therefore reports that stricter tier.
                    if best is None or (other_view.magn_float, -tier) > (best[0], -best[2]):
                        best = (other_view.magn_float, other, tier)
                    break
        if best is not None:
            parents[key] = (best[1], best[2])
    return parents


class ZoomIndex:
    """Zoom parents/children for every tier setting, plus each entry's view.
    Keys are (upr_file_path, "name:index") tuples, matching the app's
    entries' 'upr_file'/'upr_name'.

    Building it parses every payload (~30MB of text for the whole library)
    and takes several seconds, so results are saved to the SQLite cache
    against a fingerprint of the loaded .upr files (see
    UPRLibrary.getUprFingerprint) and only rebuilt when that changes."""

    def __init__(self, views, parents):
        self.views = views
        self.parents = parents
        self.children = {}
        for max_tier, tier_parents in parents.items():
            children = {}
            for child, (parent, tier) in tier_parents.items():
                children.setdefault(parent, []).append((child, tier))
            for kids in children.values():
                kids.sort(key=lambda kid: self.views[kid[0]].magn_float)
            self.children[max_tier] = children

    @classmethod
    def build(cls, payloads):
        """payloads: { key: payload_text }."""
        views = {}
        parsed = {}
        for key, payload in payloads.items():
            view, exact_sig, recolor_sigs, variant_sigs = parseEntry(payload)
            if view is not None:
                views[key] = view
                parsed[key] = (view, exact_sig, recolor_sigs, variant_sigs)
        return cls(views, { max_tier: findZoomParents(parsed, max_tier) for max_tier in TIER_NAMES })

    @staticmethod
    def _connect(db_path):
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS zoom_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS zoom_views (
                upr_file TEXT NOT NULL,
                upr_name TEXT NOT NULL,
                center_x TEXT NOT NULL,
                center_y TEXT NOT NULL,
                magn TEXT NOT NULL,
                angle REAL NOT NULL,
                aspect REAL NOT NULL,
                layers_agree INTEGER NOT NULL,
                PRIMARY KEY (upr_file, upr_name)
            );
            CREATE TABLE IF NOT EXISTS zoom_parents (
                max_tier INTEGER NOT NULL,
                upr_file TEXT NOT NULL,
                upr_name TEXT NOT NULL,
                parent_upr_file TEXT NOT NULL,
                parent_upr_name TEXT NOT NULL,
                tier INTEGER NOT NULL,
                PRIMARY KEY (max_tier, upr_file, upr_name)
            );
        """)
        return db

    def save(self, db_path, fingerprint):
        db = self._connect(db_path)
        with db:
            db.execute("DELETE FROM zoom_views")
            db.execute("DELETE FROM zoom_parents")
            db.executemany("INSERT INTO zoom_views VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           [ (key[0], key[1], str(view.center_x), str(view.center_y), str(view.magn),
                              view.angle, view.aspect, view.layers_agree)
                             for key, view in self.views.items() ])
            db.executemany("INSERT INTO zoom_parents VALUES (?, ?, ?, ?, ?, ?)",
                           [ (max_tier, child[0], child[1], parent[0], parent[1], tier)
                             for max_tier, tier_parents in self.parents.items()
                             for child, (parent, tier) in tier_parents.items() ])
            db.execute("INSERT OR REPLACE INTO zoom_meta VALUES ('fingerprint', ?)",
                       (f"{ZOOM_INDEX_VERSION}:{fingerprint}",))
        db.close()

    @classmethod
    def load(cls, db_path, fingerprint):
        """The saved index, or None if there isn't one for this fingerprint."""
        db = cls._connect(db_path)
        try:
            row = db.execute("SELECT value FROM zoom_meta WHERE key = 'fingerprint'").fetchone()
            if row is None or row[0] != f"{ZOOM_INDEX_VERSION}:{fingerprint}":
                return None
            views = { (upr_file, upr_name): View(Decimal(center_x), Decimal(center_y), Decimal(magn),
                                                 angle, aspect, bool(layers_agree))
                      for upr_file, upr_name, center_x, center_y, magn, angle, aspect, layers_agree
                      in db.execute("SELECT * FROM zoom_views") }
            parents = { max_tier: {} for max_tier in TIER_NAMES }
            for max_tier, upr_file, upr_name, parent_upr_file, parent_upr_name, tier in db.execute(
                    "SELECT * FROM zoom_parents"):
                parents[max_tier][(upr_file, upr_name)] = ((parent_upr_file, parent_upr_name), tier)
            return cls(views, parents)
        finally:
            db.close()

    def parentOf(self, key, max_tier):
        """(parent_key, tier) or None."""
        return self.parents[max_tier].get(key)

    def childrenOf(self, key, max_tier):
        """[(child_key, tier), ...], least magnified first."""
        return self.children[max_tier].get(key, [])

    def ancestors(self, key, max_tier):
        """From the outermost zoom down to (not including) key."""
        chain = []
        link = self.parentOf(key, max_tier)
        while link:
            chain.append(link[0])
            link = self.parentOf(link[0], max_tier)
        return list(reversed(chain))

    def placement(self, parent_key, child_key):
        """The child's (0,0), (1,0), (0,1), (1,1) image corners in the parent's
        normalized image coords."""
        return childCorners(self.views[parent_key], self.views[child_key])
