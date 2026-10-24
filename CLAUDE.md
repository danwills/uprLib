# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small PyQt6 desktop tool for browsing Ultra Fractal `.upr` parameter files. `.upr` files contain many named fractal parameter blocks ("UPRs"/"fractals"), some stored as plaintext and some as a custom base64+zlib-compressed blob. The app parses one or more `.upr` files, decodes the compressed entries, infers a parent/child tree from fractal naming conventions, matches entries to pre-rendered images/thumbnails, and shows the result in a filterable `QTreeView` with copy-to-clipboard and "open image" actions.

There is no package manifest, build system, or test suite in this repo — it's a single-user utility run directly with the system Python.

## Running

```
python3 uprLib.py                      # launch the GUI
python3 uprLib.py -v                   # verbose (repeat -v / use -vv, -vvv for more)
python3 uprLib.py -g                   # disable GUI, just run the data pipeline (debugging)
python3 uprLib.py -u                   # disable shell spinner output
python3 uprLib.py -n 50                # limit number of UPR entries loaded per file
python3 uprLib.py -f 2                 # limit number of .upr files loaded
python3 uprLib.py -c myconfig.config   # use an alternate config file
python3 uprLib.py --rebuild-cache      # discard the parsed-UPR cache and re-parse everything
python3 uprLib.py --no-cache           # bypass the cache entirely (e.g. when debugging the parser)
```

Requires `PyQt6` and `Pillow` (used to cheaply read image headers so oversized renders are skipped for thumbnailing). `numpy` is optional: if it's installed, the graph view's layout simulation uses it (see below), and if not, it falls back to pure Python. External tools invoked at runtime: `ffmpeg` (thumbnail generation) and `gwenview` (opening full-size images) must be on `PATH` for those features to work.

There are no automated tests. Verify changes to `uprLibrary.py` by running the `-g` (no-GUI) path and checking printed counts/output; verify GUI changes by actually launching the window.

## Configuration

Runtime behavior is driven by `uprLibrary.config` (plain `key="value"` lines, `#`-prefixed full-line comments only — see `configLinesToDict` in `uprLibrary.py`). Key settings:

- `upr_root_dir` — directory containing `.upr` files
- `upr_active_files` — comma-separated list of `.upr` filenames to load (order matters for display)
- `upr_current_file` — the "current" file (used by `UPRLibrary.getCurrentUpr`)
- `upr_image_root_dir` / `upr_image_root_glob` — where rendered fractal images live
- `upr_image_extensions` — comma-separated extensions to search for (e.g. `tif,png`)
- `upr_large_image_viewer` — optional; viewer for "Open Largest Render" (e.g. the 16k), default `feh`
- `upr_cache_file` — optional; path of the parsed-UPR SQLite cache (defaults to `uprLibrary.cache.sqlite` next to the config file, gitignored)

Config values are copied onto matching attribute names on `UPRLibrary` via `configDictToVars` (an attribute only gets set if `UPRLibrary` already declares it in `__init__`), so adding a new config key requires adding a matching `self.<key> = None` attribute to `UPRLibrary.__init__`.

## Architecture

**`uprLibrary.py`** — data/parsing layer, no Qt dependency:
- `UPRFile` reads one `.upr` file and extracts fractal blocks with a regex (`name { payload }`), building a dict keyed `"name:index"`. Payloads starting with `::` are treated as compressed and passed through `decodeAndUnzipUPR`. It also records metadata for each entry: the layer count (parsed from `layers=N`) and the compressed size.
- `decodeAndUnzipUPR` / `quadDecode` implement a non-standard base64 variant (bytes within each decoded triplet are reportedly swapped relative to the Ultra Fractal author's original encoder — see comments) followed by zlib decompression. This is reverse-engineered and fragile; don't "fix" it to match standard base64 without re-verifying against real `.upr` data.
- `UPRCache` is a SQLite cache of each file's parsed entries and metadata. A file's cached entries are reused only while its `mtime_ns` and size match and `UPRCache.PARSER_VERSION` is unchanged. **Bump `PARSER_VERSION` whenever you change what `UPRFile` produces** (regex, decoding, metadata), or stale cached output will keep being served. A parse limited by `-n` is never written to the cache, but a cached file is truncated to the `-n` limit when read.
- `UPRLibrary.loadRenders` scans the image directories into the render index (see `renderIndex.py` below) and returns every render, linked to its fractal where possible.
- `UPRLibrary` loads config, globs `*.upr` files under `upr_root_dir`, filters to `upr_active_files`, and builds `UPRFile` instances (respecting `num_upr_file_limit` / `num_upr_limit`).

**`renderIndex.py`** — index of rendered images and Ultra Fractal render logs, no Qt dependency:
- `RenderIndex` scans every directory matching `upr_image_root_glob` **recursively**, so the `16k/`/`8k/` subfolders are included, unlike `addImagePathsToData`, which only looks at the top level. It stores image pixel sizes and parsed `.log` files in the same SQLite file as `UPRCache`, re-reading only files whose mtime or size changed. Bump `RENDER_INDEX_VERSION` when the parsing or linking output changes.
- A render's log names its fractal (and usually its `.upr` file, as a Windows/Wine path from whichever machine rendered it; only the basename is kept). That is how 8k/16k renders link back to entries. Without a log, the name is guessed from the file name (`imageNameFromPath`).
- `parseRenderLog` needs care, so read its docstring before changing it:
  - Timestamps are **D/M/Y up to ~2009 and M/D/Y after**, with nothing in the log saying which. `guessDayFirst` decides from a field over 12, else from the machine's path style, else from the year. It was validated against file mtimes with zero disagreements over ~6.9k logs.
  - Many jobs are interrupted and resumed. Render time is kept as raw parts: confirmed `active_seconds`, plus `crash_gaps` (a resume while still rendering, meaning it crashed at some unknown point). `estimatedRenderSeconds` counts half of each gap, capped at 12h.
  - Backwards clock steps (UTC/local switches) are counted as `clock_anomalies` and add no time.
  - It was ported from the user's older `uprMash/renderTimeFromLogFile.py`, fixing its 12h-per-day and negative-diff bugs. It matches that script exactly on the logs where the script's assumptions hold.

**`zoomIndex.py`** — detects entries that are zoom-ins on other entries, no Qt dependency:
- Each layer's `mapping:` gives `center=x/y magn=… angle=…`. The convention was **measured against renders**, not assumed: visible height = **4/magn**, width = height × width/height, the imaginary axis points up the image, and `angle` rotates counter-clockwise. `childCorners` implements it.
- Views go as deep as magn ~1e27 with ~90-digit centers. Centers stay `Decimal`, offsets are only computed relative to a parent, and the deep maths runs under an explicit `localcontext(DECIMAL_CONTEXT)`. Decimal contexts are per-thread and the index is built on a background thread, so a module-level `getcontext().prec` would silently not apply there.
- Tiers: `TIER_EXACT` (identical apart from the view), `TIER_RECOLOR` (a layer shares formula and parameters), `TIER_VARIANT` (a layer shares the formula). Formulas named `*Pixel*` are ignored, because they're image-space helper layers in ~800 entries. Pixel-checked against the renders, exact zooms matched 57/60 and recolors 41/60, but variants matched no better than chance, so the UI defaults to exact + recolor.
- A parent is the most-magnified related entry whose view contains the child's. There's a separate forest per max tier (`ZoomIndex.parents[max_tier]`), so a tier toggle never orphans anything.
- Building takes ~9s, mostly parsing ~30MB of payloads. `ZoomIndex.save/load` caches it in the SQLite file against `UPRLibrary.getUprFingerprint()`, and the app builds it on a `ZoomIndexBuilder` QThread when stale. Bump `ZOOM_INDEX_VERSION` when parsing or matching output changes.

**`zoomView.py`** — the Zoom tab (`ZoomTab` = tier picker + `ZoomCanvas` + info panel):
- `ZoomCanvas` is a plain QWidget with its own `paintEvent`, not a QGraphicsView. Each image's scene has height 1 and width = aspect. The camera is a complex similarity `w = a*z + b`, so composing, inverting and interpolating cameras are one-liners.
- Only the current image is drawn in its own coordinates, plus its dimmed parent and its children placed relative to it. On every zoom the canvas re-roots on the new image, which keeps very deep zooms numerically fine.
- Zoom animation interpolates the camera along `K^t` (K = end camera ∘ start⁻¹), giving a constant-speed log-scale zoom about K's fixed point. That's also the basis for the planned "smooth zoom" mode that drops the outlines.
- Images load off the GUI thread as `QImage` (never `QPixmap`) via `QImageReader`, capped at 2048px, into a small LRU. A zoom in or out waits up to `LOAD_WAIT_MS` for its target image before animating, and moves that load ahead of queued preloads. `imagePathForEntry` prefers the top-level (2k) image and never picks an 8k/16k.

**`uprLib.py`** — PyQt6 application layer (the file actually run):
- `rebuildParentingDataAsDict` turns the flat list of fractal entries into a parent/child tree purely from naming: it splits fractal names on `_` and treats one fractal as a child of another if its name's underscore-split prefix matches another fractal's name exactly. Matching works across files and uses a dict index keyed on the underscore-split tuple, so the run time is close to linear. When several entries match, the last one in insertion order wins. `level` depends on processing order: a parent that comes later in the order is still at level 0 when its child is processed. This was preserved exactly when the old O(n²) scan was replaced, so check any change against the old output. There's a half-implemented alternate scheme based on a `_dotdotdot_` naming marker, currently dead code (guarded by `if temp:` / `if False:`) — do not assume it runs.
- `addImagePathsToData` globs image directories (`upr_image_root_glob`), generates missing thumbnails via `ffmpeg` into `.thumb.<name>.png` files next to the source images, and attaches `image_path`/`thumb_path` onto matching data entries by stripping the `:<index>` suffix from the fractal name.
- `addRenderInfoToData` attaches each entry's renders (largest first, with log stats) as `renders`. Logs often name an older `.upr` file than the one the fractal lives in now (e.g. `ultraMeta.upr` was renamed to `ultraMeta_YET_ANOTHER.upr`), so a unique fractal name is matched regardless of file. A name found in several files only matches renders whose log names that exact file.
- Each entry in the flat data is a dict with keys including `ix` (a flat integer id assigned in `rebuildParentingDataAsDict`), `parent_id`, `level` (0 = root), `num_layers` / `compressed_size` (metadata captured in `uprLibrary.py`), and later `image_path` / `thumb_path` and `renders`. `ix` is the id used to link the tree and the graph view.
- `UPRLibWindow` is the main widget, a `QTabWidget` with three tabs (Library, Graph, Zoom). `getActiveSelectionEntries` resolves "the selection" for whichever tab is active, so the shared actions and shortcuts (copy UPR/names, open images, open largest render) work from all three:
  - **Library**: a `QStandardItemModel`-backed tree (built by `setDataInTreeWidget`, which also fills `self.ix_to_tree_item`), a filter/sort/reverse/expand toolbar, and actions to copy selected fractal text (re-wrapped as `name { ... }`) or names to the clipboard, and to open associated images via `gwenview` (Ctrl+R, or double-click, which replaces the default expand-on-double-click). Header-click sorting uses `Qt.ItemDataRole.UserRole` as the sort role, so numeric columns sort by value; any new column must set its UserRole data too. Expand/collapse state is preserved across list rebuilds via `getExpandedStates`/`restoreExpandedStates`, keyed by a `(name, file, index)` tuple, because the model is rebuilt from scratch on every filter/sort change.
  - **Graph**: `GraphView`/`GraphScene`/`GraphNode`/`GraphEdge` show a draggable node graph of whatever was sent from the tree via "Show Selected In Graph View". `GraphScene.tick` runs a spring-embedder on a `QTimer` on the GUI thread, and picks one of three implementations by node count: pure Python, numpy all-pairs, or a Barnes-Hut quadtree above `BARNES_HUT_MIN_NODES`. The class constants and comments record profiling results; read them before changing performance-sensitive code. The main known bottleneck is PyQt call overhead from `setPos`, not the arithmetic. `GraphView` handles its own pan, zoom, and rubber-band selection (Shift = add, Ctrl = remove, Ctrl+Shift = toggle) instead of using Qt's built-in drag modes.
  - **Zoom**: see `zoomView.py`. Reached via the tree's "Show In Zoom View" action, or from the list of outermost views that shows when nothing is open. The tree also has "Render Time" and "Largest Render" columns, from `renders`, and a "Zooms" column (direct and total zoom-ins for the Zoom tab's tier). `updateZoomCounts` fills that column in place when the index arrives or the tier changes, because rebuilding the list takes ~2s.
  - Selection is synced both ways between the tree and the graph (`onTreeSelectionChanged`/`onGraphSelectionChanged`, keyed by `ix`). A `_syncing_selection` flag prevents the two handlers from triggering each other in a loop. The tree's context-menu `QAction`s are shared with the graph view.
- The `__main__` block wires CLI args to `UPRLibrary`, runs the full pipeline (load → `rebuildParentingDataAsDict` → `addImagePathsToData` → `addRenderInfoToData` → sort), applies a dark `QPalette` (the `dark.qss` stylesheet file exists but is currently not loaded — see the commented-out block), and shows the window.

**`picker16k.py` + `picker16k.html`** — the 16k picker, a local web page (`python3 picker16k.py`, then http://localhost:8016, or the machine's LAN address, since it listens on all interfaces unless given `--host 127.0.0.1`) for triaging which entries deserve a 16k render:
- It loads the library through `uprLib.loadLibraryData` (the same pipeline as the GUI's `__main__`), so it imports `uprLib.py` and `zoomView.py`. Importing PyQt6 is fine; it never creates a `QApplication`.
- Its cache is `uprLibrary.picker/`: 512px JPEG previews, plus a `features.sqlite` of hand-made fingerprints (colour / layout / texture blocks, see `computeFeatures`). Entries are keyed by image path + mtime + size + `FEATURE_VERSION`, so bump that when the fingerprint changes. There's no ML model because torch isn't installed and the GTX 960 is too old for it. Similarity is cosine over the blocks, weighted per mode (`SIMILARITY_MODES`).
- The "Families" sort mixes formula similarity (tf-idf over which formula is in which section, plus parameter values, see `formulaTokens`) with visual similarity, both z-scored (`FAMILY_FORMULA_WEIGHT`), then orders entries along a greedy nearest-neighbour path so similar ones sit together. It's computed on first use per "Similar by" mode (~7s) and kept in memory.
- Building the cache uses threads, not processes, because forking fails under this machine's strict overcommit (ENOMEM). The first build is disk-bound at ~1 image/s.
- Picks (yes/maybe/no + note) live in `uprLibrary.picks.sqlite`, keyed by (upr file basename, name, index) and re-found by name if the index moves. They're the user's work, so never put them in a cache file that might be rebuilt. Both files are gitignored.
- The estimated 16k time scales the biggest finished, logged render by pixel count. The page's "Export yes" gives a TSV with `fractal`/`upr_file`/`index` columns, which the desktop-automation skill's `uprlib_jobs.py` accepts.

**Other files:**
- `uprLib.PYQT5_py` is an older variant/backup of `uprLib.py` (fewer features, e.g. no icon/thumbnail column). It is not imported or run by anything — treat `uprLib.py` as the source of truth and don't duplicate changes into this file unless asked.
- `logspinner.py` is a tiny standalone CLI progress-spinner helper used throughout the loading pipeline for shell feedback; has no Qt dependency.
- `treeTest.py` is a standalone scratch/experiment script for `QTreeView` + `QStandardItemModel` with hardcoded dummy data — not wired into the main app.
- `dark.qss` is an unused Qt stylesheet (dark theme currently applied via `QPalette` in code instead).

## Working in this codebase

- Fractal entries are identified throughout by composite keys like `"<name>:<index>"` or `(name, upr_file, index)` tuples — when touching matching/lookup logic, check both halves are handled (the `:` split in particular assumes fractal names never contain a colon).
- Much of `uprLib.py`'s data-munging code assumes dicts preserve insertion order and gets re-sorted/re-filtered by copying and rebuilding (`copy.copy(self.data)` then filtering) rather than mutating in place — follow that pattern rather than mutating `self.data`.
- There's a lot of commented-out exploratory code and inline TODOs left in place intentionally as notes; don't feel obligated to clean these up unless the task is specifically about the code they sit in.
