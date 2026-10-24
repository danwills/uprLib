#!/usr/bin/env python3
"""16k picker: a local web page for triaging which fractals deserve a 16k
render.

    python3 picker16k.py            # then open http://localhost:8016 (or this machine's LAN address)
    python3 picker16k.py --host 127.0.0.1    # this machine only
    python3 picker16k.py --port 9000 --workers 6

Loads the library with uprLib's own pipeline (loadLibraryData), so entries,
images and render logs match what uprLib shows. On startup it builds what's
missing from its cache (uprLibrary.picker/ next to the config): a 512px JPEG
preview and a visual fingerprint per image. The first run reads every 2k
image (~10 min); later runs only read new or changed ones.

Picks (yes/maybe/no plus a note) go in uprLibrary.picks.sqlite, separate from
every cache, because they're the user's work and must survive cache rebuilds.
A pick is keyed by (upr file basename, name, index). If the index moves
because the .upr file was edited, it's re-found by name while that name is
unique in its file.

Similarity has no ML model (no torch here): each image gets hand-made
fingerprint blocks, compared by cosine similarity with per-mode weights:
  - colour:  HSV histogram (12 hue x 3 sat x 4 value), square-rooted
  - layout:  8x8 grid of normalised brightness
  - texture: radial power spectrum (detail scale), rotation-invariant
             orientation structure, gradient-strength histogram
"""
import argparse
import hashlib
import io
import json
import os
import random
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

# numpy's BLAS goes through FlexiBLAS, and its OpenBLAS backend reserves 32GB
# per process. Under this machine's strict overcommit that all counts, so a
# couple of numpy processes starve everything else. The reference (NETLIB)
# backend needs ~30MB and is plenty fast at this size. Must be set before
# numpy loads.
os.environ.setdefault("FLEXIBLAS", "NETLIB")
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
PREVIEW_SIZE = 512
FULL_SIZE = 2048
FEATURE_VERSION = 1   # bump when computeFeatures changes
SIZE_16K = 16384
STATUSES = ("yes", "maybe", "no")

# Weights per similarity mode for the (colour, layout, texture) blocks.
SIMILARITY_MODES = {
    "overall": (1.0, 0.5, 1.0),
    "colour":  (1.0, 0.0, 0.15),
    "texture": (0.15, 0.25, 1.0),
    "layout":  (0.2, 1.0, 0.2),
}

Image.MAX_IMAGE_PIXELS = 400_000_000


# --- fingerprints (run in worker threads) -----------------------------------
# Threads, not processes: forking this big a process fails under the machine's
# strict overcommit, and Pillow/numpy release the GIL for the heavy parts.

def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def computeFeatures(im):
    """(colour, layout, texture) float32 blocks for a PIL image."""
    small = im.convert("RGB").resize((256, 256), Image.Resampling.BILINEAR)
    hsv = np.asarray(small.convert("HSV"), dtype=np.float32) / 256.0
    h = np.minimum((hsv[..., 0] * 12).astype(int), 11)
    s = np.minimum((hsv[..., 1] * 3).astype(int), 2)
    v = np.minimum((hsv[..., 2] * 4).astype(int), 3)
    colour = np.bincount((h * 12 + s * 4 + v).ravel(), minlength=144).astype(np.float32)
    colour = _unit(np.sqrt(colour / colour.sum()))

    gray = np.asarray(small.convert("L"), dtype=np.float32) / 255.0
    blocks = gray.reshape(8, 32, 8, 32).mean(axis=(1, 3)).ravel()
    layout = _unit((blocks - blocks.mean()) / (blocks.std() + 1e-3))

    # Radial power spectrum: how much detail at each scale.
    power = np.abs(np.fft.fftshift(np.fft.fft2(gray - gray.mean()))) ** 2
    yy, xx = np.indices(power.shape)
    radius = np.hypot(yy - 128, xx - 128)
    edges = np.geomspace(1, 128, 17)
    spectrum = np.array([power[(radius >= lo) & (radius < hi)].mean() for lo, hi in zip(edges[:-1], edges[1:])])
    spectrum = np.log(spectrum + 1e-9)
    spectrum = _unit(spectrum - spectrum.mean())

    gy, gx = np.gradient(gray)
    mag = np.hypot(gx, gy)
    angle = np.mod(np.arctan2(gy, gx), np.pi)
    orient = np.bincount(np.minimum((angle / np.pi * 12).astype(int), 11).ravel(),
                         weights=mag.ravel(), minlength=12)
    # |DFT| of the orientation histogram doesn't change when the image rotates,
    # but still says whether edges run in one direction, two, or every way.
    orient = _unit(np.abs(np.fft.rfft(orient / (orient.sum() + 1e-9)))[1:])
    strength = np.histogram(np.log10(mag.ravel() + 1e-4), bins=10, range=(-4, 0))[0].astype(np.float32)
    strength = _unit(np.sqrt(strength / strength.sum()))
    texture = _unit(np.concatenate([spectrum, 0.7 * orient, strength]))
    return colour, layout, texture


def _buildOne(task):
    key, src, preview_path = task
    try:
        with Image.open(src) as im:
            im.load()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            preview = im.copy()
            preview.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE), Image.Resampling.LANCZOS)
        preview = preview.convert("RGB")
        preview.save(preview_path, "JPEG", quality=85)
        colour, layout, texture = computeFeatures(preview)
        return key, colour.tobytes(), layout.tobytes(), texture.tobytes(), None
    except Exception as e:
        return key, None, None, None, f"{type(e).__name__}: {e}"


# --- library data -----------------------------------------------------------

def loadEntries():
    from uprLibrary import UPRLibrary
    from uprLib import loadLibraryData
    from zoomView import imagePathForEntry
    upr_lib = UPRLibrary(shell_ui=False)
    data = loadLibraryData(upr_lib)
    by_ix = { val['ix']: val for val in data.values() }

    entries = []
    for val in data.values():
        name, _, index = val['upr_name'].rpartition(":")
        renders = val.get('renders', [])
        largest = max((max(r['width'] or 0, r['height'] or 0) for r in renders), default=0)
        root = val
        while root.get('parent_id', -1) not in (-1, None) and root['parent_id'] in by_ix and root['parent_id'] != root['ix']:
            root = by_ix[root['parent_id']]
        entries.append({
            "id": val['ix'],
            "name": name,
            "index": int(index),
            "file": os.path.basename(val['upr_file']),
            "layers": val.get('num_layers'),
            "size": val.get('compressed_size'),
            "level": val.get('level', 0),
            "parent": val.get('parent_id', -1),
            "root": root['ix'],
            "image": imagePathForEntry(val),
            "largest": largest,
            "renders": [ { "w": r['width'], "h": r['height'],
                           "hours": round(r['estimated_seconds'] / 3600, 2) if r.get('estimated_seconds') else None,
                           "finished": r.get('finished'), "path": r['path'] }
                         for r in renders ],
            "est16k_hours": estimate16kHours(renders),
        })
    return upr_lib, entries


def estimate16kHours(renders):
    """Hours a 16k would take, scaled by pixel count from the biggest finished
    render with a log (the bigger the sample, the better it predicts)."""
    timed = [ r for r in renders if r.get('estimated_seconds') and r.get('finished') and r['width'] and r['height'] ]
    if not timed:
        return None
    r = max(timed, key=lambda r: r['width'] * r['height'])
    scale = (SIZE_16K / max(r['width'], r['height'])) ** 2
    return round(r['estimated_seconds'] * scale / 3600, 1)


# --- storage ----------------------------------------------------------------

class PickStore:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS picks (
            upr_file TEXT NOT NULL, name TEXT NOT NULL, idx INTEGER NOT NULL,
            status TEXT, note TEXT, updated_at REAL,
            PRIMARY KEY (upr_file, name, idx))""")
        self.db.commit()

    def attach(self, entries):
        """Sets status/note on entries, re-finding moved indexes by name."""
        rows = self.db.execute("SELECT upr_file, name, idx, status, note FROM picks").fetchall()
        exact = { (f, n, i): (s, note) for f, n, i, s, note in rows }
        name_counts = {}
        for e in entries:
            name_counts[(e['file'], e['name'])] = name_counts.get((e['file'], e['name']), 0) + 1
        by_name = {}
        for f, n, i, s, note in rows:
            by_name.setdefault((f, n), []).append((i, s, note))
        for e in entries:
            pick = exact.get((e['file'], e['name'], e['index']))
            if pick is None and name_counts[(e['file'], e['name'])] == 1 and len(by_name.get((e['file'], e['name']), [])) == 1:
                old_index, s, note = by_name[(e['file'], e['name'])][0]
                pick = (s, note)
                with self.lock:
                    self.db.execute("UPDATE picks SET idx=? WHERE upr_file=? AND name=? AND idx=?",
                                    (e['index'], e['file'], e['name'], old_index))
                    self.db.commit()
            e['status'], e['note'] = pick if pick else (None, None)

    def set(self, entry, status=None, note=None):
        with self.lock:
            self.db.execute("""INSERT INTO picks (upr_file, name, idx, status, note, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?)
                               ON CONFLICT (upr_file, name, idx) DO UPDATE
                               SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at""",
                            (entry['file'], entry['name'], entry['index'], status, note, time.time()))
            self.db.commit()


class FeatureStore:
    def __init__(self, cache_dir):
        self.preview_dir = os.path.join(cache_dir, "previews")
        os.makedirs(self.preview_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(cache_dir, "features.sqlite"), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS features (
            key TEXT PRIMARY KEY, colour BLOB, layout BLOB, texture BLOB, error TEXT)""")
        self.db.commit()

    @staticmethod
    def keyFor(path):
        st = os.stat(path)
        raw = f"{FEATURE_VERSION}|{path}|{st.st_mtime_ns}|{st.st_size}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def previewPath(self, key):
        return os.path.join(self.preview_dir, key + ".jpg")

    def build(self, entries, workers, dry_run=False):
        """Fills in missing previews/features; sets e['key'] on each entry."""
        known = { k for (k,) in self.db.execute("SELECT key FROM features") }
        todo = {}
        for e in entries:
            e['key'] = None
            if e['image'] and os.path.isfile(e['image']):
                e['key'] = self.keyFor(e['image'])
                if e['key'] not in known or not os.path.isfile(self.previewPath(e['key'])):
                    todo[e['key']] = e['image']
        if todo and dry_run:
            print(f"Skipping {len(todo)} images not cached yet (--no-build).")
        elif todo:
            print(f"Building previews and fingerprints for {len(todo)} images with {workers} workers..")
            started = time.time()
            tasks = [ (k, src, self.previewPath(k)) for k, src in todo.items() ]
            with ThreadPoolExecutor(workers) as pool:
                for n, (key, c, l, t, err) in enumerate(pool.map(_buildOne, tasks), 1):
                    self.db.execute("INSERT OR REPLACE INTO features VALUES (?, ?, ?, ?, ?)", (key, c, l, t, err))
                    if err:
                        print(f"\n  failed: {todo[key]}: {err}")
                    if n % 50 == 0 or n == len(tasks):
                        self.db.commit()
                        rate = n / (time.time() - started)
                        print(f"\r  {n}/{len(tasks)}  ({rate:.1f}/s, ~{(len(tasks) - n) / rate / 60:.1f} min left)  ", end="", flush=True)
            print()
            self.db.commit()

    def matrix(self, entries):
        """{block: (len(entries) x dims) array} plus a has-features mask."""
        rows = { k: (c, l, t) for k, c, l, t in
                 self.db.execute("SELECT key, colour, layout, texture FROM features WHERE error IS NULL") }
        dims = (144, 64, 32)
        blocks = [ np.zeros((len(entries), d), dtype=np.float32) for d in dims ]
        mask = np.zeros(len(entries), dtype=bool)
        for i, e in enumerate(entries):
            row = rows.get(e.get('key'))
            if row:
                for b, blob in enumerate(row):
                    blocks[b][i] = np.frombuffer(blob, dtype=np.float32)
                mask[i] = True
        return blocks, mask


# --- server -----------------------------------------------------------------

class Picker:
    def __init__(self, entries, picks, features):
        self.entries = entries
        self.by_id = { e['id']: e for e in entries }
        self.row_of = { e['id']: i for i, e in enumerate(entries) }
        self.picks = picks
        self.features = features
        self.blocks, self.mask = features.matrix(entries)
        self.full_cache = OrderedDict()
        self.full_lock = threading.Lock()

    def combined(self, mode):
        w = SIMILARITY_MODES.get(mode, SIMILARITY_MODES["overall"])
        return np.concatenate([ b * np.sqrt(wi) for b, wi in zip(self.blocks, w) ], axis=1)

    def similar(self, entry_id, mode):
        m = self.combined(mode)
        anchor = m[self.row_of[entry_id]]
        scores = (m @ anchor) / (np.linalg.norm(m, axis=1) * np.linalg.norm(anchor) + 1e-9)
        scores[~self.mask] = -2
        return { e['id']: round(float(s), 4) for e, s in zip(self.entries, scores) }

    def likeYeses(self, mode):
        """Score = closest yes minus half the closest no, so it learns from both."""
        m = self.combined(mode)
        m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
        rows = lambda status: [ self.row_of[e['id']] for e in self.entries
                                if e['status'] == status and self.mask[self.row_of[e['id']]] ]
        yes, no = rows("yes"), rows("no")
        if not yes:
            return {}
        score = (m @ m[yes].T).max(axis=1)
        if no:
            score -= 0.5 * (m @ m[no].T).max(axis=1)
        score[~self.mask] = -2
        return { e['id']: round(float(s), 4) for e, s in zip(self.entries, score) }

    def fullImage(self, entry):
        path = entry['image']
        with self.full_lock:
            if path in self.full_cache:
                self.full_cache.move_to_end(path)
                return self.full_cache[path]
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((FULL_SIZE, FULL_SIZE), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
        data = buf.getvalue()
        with self.full_lock:
            self.full_cache[path] = data
            while len(self.full_cache) > 24:
                self.full_cache.popitem(last=False)
        return data

    def publicEntries(self):
        return [ { k: v for k, v in e.items() if k not in ("image", "key") } | { "has_preview": bool(e.get('key') and self.mask[self.row_of[e['id']]]) }
                 for e in self.entries ]

    def exportTsv(self, status):
        lines = [ "fractal\tupr_file\tindex\test16k_hours\tnote" ]
        for e in self.entries:
            if e['status'] == status:
                lines.append(f"{e['name']}\t{e['file']}\t{e['index']}\t{e['est16k_hours'] or ''}\t{(e['note'] or '').replace(chr(9), ' ')}")
        return "\n".join(lines) + "\n"


def makeHandler(picker):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send(self, body, content_type="application/json", status=200, extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def entryFrom(self, query):
            try:
                return picker.by_id[int(query.get("id", [""])[0])]
            except (ValueError, KeyError):
                return None

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            path = url.path
            if path == "/":
                # Read per request, so page edits show up on reload.
                with open(os.path.join(HERE, "picker16k.html"), "rb") as f:
                    return self.send(f.read(), "text/html; charset=utf-8")
            if path == "/api/entries":
                return self.send(picker.publicEntries())
            if path == "/api/similar":
                entry = self.entryFrom(query)
                if entry is None:
                    return self.send({"error": "no such entry"}, status=404)
                return self.send(picker.similar(entry['id'], query.get("mode", ["overall"])[0]))
            if path == "/api/like-yeses":
                return self.send(picker.likeYeses(query.get("mode", ["overall"])[0]))
            if path.startswith("/export/") and path.endswith(".tsv"):
                status = path[len("/export/"):-len(".tsv")]
                if status not in STATUSES:
                    return self.send({"error": "bad status"}, status=404)
                return self.send(picker.exportTsv(status), "text/tab-separated-values; charset=utf-8",
                                 extra={"Content-Disposition": f'attachment; filename="picks_16k_{status}.tsv"'})
            if path.startswith("/preview/") or path.startswith("/full/"):
                try:
                    entry = picker.by_id[int(path.rsplit("/", 1)[1].split(".")[0])]
                except (ValueError, KeyError):
                    return self.send({"error": "no such entry"}, status=404)
                if path.startswith("/preview/"):
                    if not entry.get('key') or not os.path.isfile(picker.features.previewPath(entry['key'])):
                        return self.send({"error": "no preview"}, status=404)
                    with open(picker.features.previewPath(entry['key']), "rb") as f:
                        return self.send(f.read(), "image/jpeg", extra={"Cache-Control": "max-age=86400"})
                return self.send(picker.fullImage(entry), "image/jpeg", extra={"Cache-Control": "max-age=3600"})
            return self.send({"error": "not found"}, status=404)

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self.send({"error": "bad json"}, status=400)
            if url.path == "/api/pick":
                ids = body.get("ids") or [body.get("id")]
                status = body.get("status")
                if status not in STATUSES + (None,):
                    return self.send({"error": "bad status"}, status=400)
                changed = []
                for entry_id in ids:
                    entry = picker.by_id.get(entry_id)
                    if entry is None:
                        continue
                    entry['status'] = status
                    if "note" in body:
                        entry['note'] = body["note"] or None
                    picker.picks.set(entry, entry['status'], entry['note'])
                    changed.append({ "id": entry['id'], "status": entry['status'], "note": entry['note'] })
                return self.send({"changed": changed})
            return self.send({"error": "not found"}, status=404)

    return Handler


def lanAddress():
    """This machine's address on the LAN (the one its default route uses)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))   # UDP: nothing is sent
            return s.getsockname()[0]
        except OSError:
            return socket.gethostname()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8016)
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all, so the LAN can reach it)")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 4))
    parser.add_argument("--build-only", action="store_true", help="build the cache and exit")
    parser.add_argument("--no-build", action="store_true", help="serve with whatever is cached already")
    args = parser.parse_args()

    os.chdir(HERE)   # uprLib's config path is relative
    sys.path.insert(0, HERE)
    print("Loading the library..")
    upr_lib, entries = loadEntries()
    config_dir = os.path.dirname(os.path.abspath(upr_lib.config_filename)) if getattr(upr_lib, 'config_filename', None) else HERE
    features = FeatureStore(os.path.join(config_dir, "uprLibrary.picker"))
    features.build(entries, args.workers, dry_run=args.no_build)
    if args.build_only:
        return
    picks = PickStore(os.path.join(config_dir, "uprLibrary.picks.sqlite"))
    picks.attach(entries)
    picker = Picker(entries, picks, features)
    no16k = sum(1 for e in entries if e['largest'] < SIZE_16K)
    print(f"{len(entries)} entries, {no16k} without a 16k, {int(picker.mask.sum())} with previews.")
    server = ThreadingHTTPServer((args.host, args.port), makeHandler(picker))
    print(f"16k picker on http://localhost:{args.port}")
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"  and on the LAN at http://{lanAddress()}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
