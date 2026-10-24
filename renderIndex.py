#!/usr/bin/env python
"""Index of rendered fractal images and their Ultra Fractal render logs.

Every render job writes a .log next to its image, e.g.

    5/5/2012 8:05:32 PM: Starting job someFractal.
    5/5/2012 8:05:33 PM: Calculating someFractal in Z:\\home\\dan\\...\\ultraLucia.upr.
    5/5/2012 11:34:39 PM: Saving to Z:\\home\\dan\\quid\\ultraLucia\\someFractal.tif.
    5/5/2012 11:34:39 PM: Job finished.

Logs say which fractal (and usually which .upr file) an image came from,
which is how 8k/16k renders get tied back to their entry, and how long the
render took. Render time is the sum of the active stretches between
"Starting/Resuming job" and "Job interrupted/finished". Interpreting them
needs care - see parseRenderLog. Ported from (and fixing a few bugs in) the
older uprMash/renderTimeFromLogFile.py.
"""

import os
import re
import glob
import json
import sqlite3
import datetime

from PIL import Image

# Bump whenever parseRenderLog's or imageNameFromPath's output changes, so
# results indexed by older code get re-parsed instead of silently reused.
RENDER_INDEX_VERSION = 1

# A "Resuming job" while a render is still running means Ultra Fractal
# crashed (or the machine went down) at some unknown point in between. Guess
# that half of that gap was rendering, but never more than 12h per gap: gaps
# run up to weeks long, and half of 74 days is not a believable render time.
CRASH_GAP_RENDER_FRACTION = 0.5
CRASH_GAP_MAX_RENDER_SECONDS = 12 * 60 * 60

log_line_rx = re.compile(r'^(\d+)/(\d+)/(\d+) (\d+):(\d+):(\d+) ([AP]M): (.*)$', re.M)
calculating_rx = re.compile(r'^Calculating (.+?) in (.+?\.upr)\.$')
# Older (UF4-era) logs don't name the .upr file.
calculating_no_file_rx = re.compile(r'^Calculating (.+?)\.$')
size_suffix_rx = re.compile(r'[-_]\d+k$')


def guessDayFirst(stamps, text):
    """Returns (day_first, how_we_know) for a log's timestamps.

    Logs up to ~2009 were written D/M/Y and later ones M/D/Y (different
    machines/locales), with nothing in the log saying which. Checked against
    file mtimes, this rule had zero disagreements over ~6.9k logs."""
    if any(int(stamp[0]) > 12 for stamp in stamps):
        return True, "explicit"
    if any(int(stamp[1]) > 12 for stamp in stamps):
        return False, "explicit"
    # Otherwise the machine it was rendered on gives it away, via the path
    # style of the .upr file it names.
    match = re.search(r'Calculating .+? in (.+?\.upr)\.', text)
    if match:
        upr_path = match.group(1).lower()
        if upr_path.startswith("c:\\documents and settings"):
            return True, "path"
        if upr_path.startswith("c:\\users") or upr_path.startswith("z:\\home"):
            return False, "path"
    return int(stamps[0][2]) <= 2009, "year"


def stampToDatetime(stamp, day_first):
    day, month = (int(stamp[0]), int(stamp[1])) if day_first else (int(stamp[1]), int(stamp[0]))
    hour = int(stamp[3]) % 12 + (12 if stamp[6] == "PM" else 0)
    return datetime.datetime(int(stamp[2]), month, day, hour, int(stamp[4]), int(stamp[5]))


def parseRenderLog(text):
    """Parses one render log into a dict of what's known about the job.

    Render time is kept as its raw parts rather than one number:
    'active_seconds' is time between a start/resume and an interrupt/finish
    (known to be rendering), while each of 'crash_gaps' is a stretch between
    a start/resume and the *next* resume with no interrupt in between - it
    crashed somewhere in there, but when is unknown. estimatedRenderSeconds
    turns these into a best guess. Steps where the clock goes backwards
    (seen in a few logs, likely a UTC/local time switch) are counted in
    'clock_anomalies' and contribute no time. A log that ends mid-render
    (still running, or crashed and never resumed) sets 'open_since'."""
    stamps = log_line_rx.findall(text)
    result = {
        'job_name': None, 'fractal_name': None, 'upr_file': None,
        'day_first': None, 'day_first_source': None,
        'started_at': None, 'finished_at': None, 'finished': False,
        'active_seconds': 0.0, 'crash_gaps': [], 'interruptions': 0,
        'clock_anomalies': 0, 'open_since': None, 'errors': [],
        'antialiasing': None, 'motion_blur': None, 'output_format': None,
        'saved_to': None,
    }
    if not stamps:
        return result

    day_first, how = guessDayFirst(stamps, text)
    result['day_first'] = day_first
    result['day_first_source'] = how

    rendering = False
    segment_start = None

    def closeSegment(when):
        span = (when - segment_start).total_seconds()
        if span < 0:
            result['clock_anomalies'] += 1
        else:
            result['active_seconds'] += span

    for stamp in stamps:
        message = stamp[7].strip()
        try:
            when = stampToDatetime(stamp, day_first)
        except ValueError:
            result['errors'].append("Unparseable timestamp: " + "/".join(stamp[:3]))
            continue

        if message.startswith("Starting job "):
            result['job_name'] = message[len("Starting job "):].rstrip(".")
            if result['started_at'] is None:
                result['started_at'] = when
            rendering = True
            segment_start = when
        elif message.startswith("Resuming job "):
            if rendering:
                gap = (when - segment_start).total_seconds()
                if gap < 0:
                    result['clock_anomalies'] += 1
                else:
                    result['crash_gaps'].append(gap)
            rendering = True
            segment_start = when
        elif message.startswith("Job interrupted"):
            if rendering:
                closeSegment(when)
            result['interruptions'] += 1
            rendering = False
        elif message.startswith("Job finished"):
            if rendering:
                closeSegment(when)
            result['finished'] = True
            result['finished_at'] = when
            rendering = False
        elif message.startswith("Calculating ") and result['fractal_name'] is None:
            match = calculating_rx.match(message)
            if match:
                result['fractal_name'] = match.group(1)
                # The .upr path is from whichever machine rendered it
                # (C:\..., Z:\...), so only the file name is meaningful here.
                result['upr_file'] = re.split(r'[\\/]', match.group(2))[-1]
            else:
                match = calculating_no_file_rx.match(message)
                if match:
                    result['fractal_name'] = match.group(1)
        elif message.startswith("Saving to "):
            result['saved_to'] = message[len("Saving to "):].rstrip(".")
        elif message.startswith("Error"):
            result['errors'].append(message)
        elif message.startswith("Anti-aliasing") and result['antialiasing'] is None:
            result['antialiasing'] = message
        elif message.startswith("Motion blur") and result['motion_blur'] is None:
            result['motion_blur'] = message
        elif message.startswith("Rendering in ") and result['output_format'] is None:
            result['output_format'] = message[len("Rendering in "):].split()[0]

    if rendering:
        result['open_since'] = segment_start
    return result


def estimatedRenderSeconds(active_seconds, crash_gaps):
    return active_seconds + sum(min(gap * CRASH_GAP_RENDER_FRACTION, CRASH_GAP_MAX_RENDER_SECONDS)
                                for gap in crash_gaps)


def imageNameFromPath(path):
    """Best-guess fractal name from an image's file name alone, for images
    without a usable log: drops a "Copy of " prefix and an _8k/_16k/-8k
    style size suffix."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem.startswith("Copy of "):
        stem = stem[len("Copy of "):]
    return size_suffix_rx.sub("", stem)


def readImageSize(path):
    # Pillow refuses to even open (header only) images over its decompression
    # bomb limit, which 16k renders are; we only want the header here.
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None, None
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


def _isoOrNone(value):
    return value.isoformat() if value else None


class RenderIndex:
    """SQLite-backed index of image files and render logs found under the
    image directories. Like UPRCache, a file is only re-read when its mtime
    or size changes, and it shares the same database file."""

    def __init__(self, db_path, verbose=0):
        self.verbose = verbose
        self.db = sqlite3.connect(db_path)
        # See UPRCache - safe for a cache, and avoids an fsync per commit.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS render_images (
                path TEXT PRIMARY KEY,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                index_version INTEGER NOT NULL,
                width INTEGER,
                height INTEGER,
                name_from_filename TEXT NOT NULL,
                log_path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS render_logs (
                path TEXT PRIMARY KEY,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                index_version INTEGER NOT NULL,
                job_name TEXT,
                fractal_name TEXT,
                upr_file TEXT,
                day_first INTEGER,
                day_first_source TEXT,
                started_at TEXT,
                finished_at TEXT,
                finished INTEGER NOT NULL,
                active_seconds REAL NOT NULL,
                crash_gaps TEXT NOT NULL,
                interruptions INTEGER NOT NULL,
                clock_anomalies INTEGER NOT NULL,
                open_since TEXT,
                errors TEXT NOT NULL,
                antialiasing TEXT,
                motion_blur TEXT,
                output_format TEXT,
                saved_to TEXT
            );
        """)

    def _storedStats(self, table):
        # One query up front rather than one per file - with ~15k files the
        # per-file lookups were most of a warm scan's time.
        return { path: (mtime_ns, size, version) for path, mtime_ns, size, version
                 in self.db.execute(f"SELECT path, mtime_ns, size, index_version FROM {table}") }

    def _storeImage(self, path, file_stat):
        width, height = readImageSize(path)
        self.db.execute(
            "INSERT OR REPLACE INTO render_images "
            "(path, mtime_ns, size, index_version, width, height, name_from_filename, log_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (path, file_stat.st_mtime_ns, file_stat.st_size, RENDER_INDEX_VERSION, width, height,
             imageNameFromPath(path), os.path.splitext(path)[0] + ".log"))

    def _storeLog(self, path, file_stat):
        with open(path, encoding="ISO-8859-1") as log_file:
            parsed = parseRenderLog(log_file.read())
        self.db.execute(
            "INSERT OR REPLACE INTO render_logs "
            "(path, mtime_ns, size, index_version, job_name, fractal_name, upr_file, day_first, "
            " day_first_source, started_at, finished_at, finished, active_seconds, crash_gaps, "
            " interruptions, clock_anomalies, open_since, errors, antialiasing, motion_blur, "
            " output_format, saved_to) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (path, file_stat.st_mtime_ns, file_stat.st_size, RENDER_INDEX_VERSION,
             parsed['job_name'], parsed['fractal_name'], parsed['upr_file'], parsed['day_first'],
             parsed['day_first_source'], _isoOrNone(parsed['started_at']),
             _isoOrNone(parsed['finished_at']), parsed['finished'], parsed['active_seconds'],
             json.dumps(parsed['crash_gaps']), parsed['interruptions'], parsed['clock_anomalies'],
             _isoOrNone(parsed['open_since']), json.dumps(parsed['errors']),
             parsed['antialiasing'], parsed['motion_blur'], parsed['output_format'],
             parsed['saved_to']))

    def scan(self, root_glob, image_extensions):
        """Walks every directory matching root_glob (recursively, so 8k/16k
        subdirectories are included), indexing new or changed images and
        logs, and forgetting ones that no longer exist. Returns a dict of
        counts for reporting."""
        image_extensions = { "." + ext.strip().lower() for ext in image_extensions }
        seen = { 'render_images': set(), 'render_logs': set() }
        stored = { table: self._storedStats(table) for table in seen }
        counts = { 'images': 0, 'logs': 0, 'images_read': 0, 'logs_read': 0 }
        with self.db:
            for root_dir in glob.glob(root_glob):
                if not os.path.isdir(root_dir):
                    continue
                for dirpath, dirnames, filenames in os.walk(root_dir):
                    for filename in filenames:
                        # Skips our own .thumb.* files (and other dotfiles).
                        if filename.startswith("."):
                            continue
                        ext = os.path.splitext(filename)[1].lower()
                        if ext in image_extensions:
                            table, store, kind = 'render_images', self._storeImage, 'images'
                        elif ext == ".log":
                            table, store, kind = 'render_logs', self._storeLog, 'logs'
                        else:
                            continue
                        path = os.path.join(dirpath, filename)
                        try:
                            file_stat = os.stat(path)
                        except OSError:
                            continue
                        seen[table].add(path)
                        counts[kind] += 1
                        current = (file_stat.st_mtime_ns, file_stat.st_size, RENDER_INDEX_VERSION)
                        if stored[table].get(path) != current:
                            store(path, file_stat)
                            counts[kind + '_read'] += 1
                            if self.verbose > 1:
                                print("Indexed: " + path)
            for table, paths in seen.items():
                stale = [ (path,) for path in stored[table] if path not in paths ]
                self.db.executemany(f"DELETE FROM {table} WHERE path = ?", stale)
        return counts

    def getRenders(self):
        """Every indexed image, joined with its log (if it has one), as a
        list of dicts. 'fractal_name'/'upr_file' come from the log when it
        names the fractal, else 'fractal_name' is guessed from the file name
        and 'upr_file' is None ('link' says which)."""
        renders = []
        for row in self.db.execute("""
                SELECT i.path, i.width, i.height, i.name_from_filename,
                       l.path, l.fractal_name, l.upr_file, l.started_at, l.finished_at,
                       l.finished, l.active_seconds, l.crash_gaps, l.interruptions,
                       l.clock_anomalies, l.open_since, l.errors, l.antialiasing
                FROM render_images i LEFT JOIN render_logs l ON l.path = i.log_path"""):
            (path, width, height, name_from_filename, log_path, log_fractal_name, upr_file,
             started_at, finished_at, finished, active_seconds, crash_gaps, interruptions,
             clock_anomalies, open_since, errors, antialiasing) = row
            render = { 'path': path, 'width': width, 'height': height, 'log_path': log_path }
            if log_fractal_name:
                render.update(fractal_name=log_fractal_name, upr_file=upr_file, link='log')
            else:
                render.update(fractal_name=name_from_filename, upr_file=None, link='filename')
            if log_path:
                crash_gaps = json.loads(crash_gaps)
                render.update(
                    started_at=started_at, finished_at=finished_at, finished=bool(finished),
                    active_seconds=active_seconds, crash_gaps=crash_gaps,
                    estimated_seconds=estimatedRenderSeconds(active_seconds, crash_gaps),
                    interruptions=interruptions, clock_anomalies=clock_anomalies,
                    open_since=open_since, errors=json.loads(errors), antialiasing=antialiasing)
            renders.append(render)
        return renders

    def clear(self):
        with self.db:
            self.db.execute("DELETE FROM render_images")
            self.db.execute("DELETE FROM render_logs")

    def close(self):
        self.db.close()
