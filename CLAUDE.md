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

Config values are copied onto matching attribute names on `UPRLibrary` via `configDictToVars` (an attribute only gets set if `UPRLibrary` already declares it in `__init__`), so adding a new config key requires adding a matching `self.<key> = None` attribute to `UPRLibrary.__init__`.

## Architecture

**`uprLibrary.py`** — data/parsing layer, no Qt dependency:
- `UPRFile` reads one `.upr` file and extracts fractal blocks with a regex (`name { payload }`), building a dict keyed `"name:index"`. Payloads starting with `::` are treated as compressed and passed through `decodeAndUnzipUPR`. It also records metadata for each entry: the layer count (parsed from `layers=N`) and the compressed size.
- `decodeAndUnzipUPR` / `quadDecode` implement a non-standard base64 variant (bytes within each decoded triplet are reportedly swapped relative to the Ultra Fractal author's original encoder — see comments) followed by zlib decompression. This is reverse-engineered and fragile; don't "fix" it to match standard base64 without re-verifying against real `.upr` data.
- `UPRLibrary` loads config, globs `*.upr` files under `upr_root_dir`, filters to `upr_active_files`, and builds `UPRFile` instances (respecting `num_upr_file_limit` / `num_upr_limit`).

**`uprLib.py`** — PyQt6 application layer (the file actually run):
- `rebuildParentingDataAsDict` turns the flat list of fractal entries into a parent/child tree purely from naming: it splits fractal names on `_` and treats one fractal as a child of another if its name's underscore-split prefix matches another fractal's name exactly. This is O(n²) over all entries and is the main cost for large libraries. There's a half-implemented alternate scheme based on a `_dotdotdot_` naming marker, currently dead code (guarded by `if temp:` / `if False:`) — do not assume it runs.
- `addImagePathsToData` globs image directories (`upr_image_root_glob`), generates missing thumbnails via `ffmpeg` into `.thumb.<name>.png` files next to the source images, and attaches `image_path`/`thumb_path` onto matching data entries by stripping the `:<index>` suffix from the fractal name.
- Each entry in the flat data is a dict with keys including `ix` (a flat integer id assigned in `rebuildParentingDataAsDict`), `parent_id`, `level` (0 = root), `num_layers` / `compressed_size` (metadata captured in `uprLibrary.py`), and later `image_path` / `thumb_path`. `ix` is the id used to link the tree and the graph view.
- `UPRLibWindow` is the main widget, a `QTabWidget` with two tabs:
  - **Library**: a `QStandardItemModel`-backed tree (built by `setDataInTreeWidget`, which also fills `self.ix_to_tree_item`), a filter/sort/reverse/expand toolbar, and actions to copy selected fractal text (re-wrapped as `name { ... }`) or names to the clipboard, and to open associated images via `gwenview` (Ctrl+R, or double-click, which replaces the default expand-on-double-click). Header-click sorting uses `Qt.ItemDataRole.UserRole` as the sort role, so numeric columns sort by value; any new column must set its UserRole data too. Expand/collapse state is preserved across list rebuilds via `getExpandedStates`/`restoreExpandedStates`, keyed by a `(name, file, index)` tuple, because the model is rebuilt from scratch on every filter/sort change.
  - **Graph**: `GraphView`/`GraphScene`/`GraphNode`/`GraphEdge` show a draggable node graph of whatever was sent from the tree via "Show Selected In Graph View". `GraphScene.tick` runs a spring-embedder on a `QTimer` on the GUI thread, and picks one of three implementations by node count: pure Python, numpy all-pairs, or a Barnes-Hut quadtree above `BARNES_HUT_MIN_NODES`. The class constants and comments record profiling results; read them before changing performance-sensitive code. The main known bottleneck is PyQt call overhead from `setPos`, not the arithmetic. `GraphView` handles its own pan, zoom, and rubber-band selection (Shift = add, Ctrl = remove, Ctrl+Shift = toggle) instead of using Qt's built-in drag modes.
  - Selection is synced both ways between the tree and the graph (`onTreeSelectionChanged`/`onGraphSelectionChanged`, keyed by `ix`). A `_syncing_selection` flag prevents the two handlers from triggering each other in a loop. The tree's context-menu `QAction`s are shared with the graph view.
- The `__main__` block wires CLI args to `UPRLibrary`, runs the full pipeline (load → `rebuildParentingDataAsDict` → `addImagePathsToData` → sort), applies a dark `QPalette` (the `dark.qss` stylesheet file exists but is currently not loaded — see the commented-out block), and shows the window.

**Other files:**
- `uprLib.PYQT5_py` is an older variant/backup of `uprLib.py` (fewer features, e.g. no icon/thumbnail column). It is not imported or run by anything — treat `uprLib.py` as the source of truth and don't duplicate changes into this file unless asked.
- `logspinner.py` is a tiny standalone CLI progress-spinner helper used throughout the loading pipeline for shell feedback; has no Qt dependency.
- `treeTest.py` is a standalone scratch/experiment script for `QTreeView` + `QStandardItemModel` with hardcoded dummy data — not wired into the main app.
- `dark.qss` is an unused Qt stylesheet (dark theme currently applied via `QPalette` in code instead).

## Working in this codebase

- Fractal entries are identified throughout by composite keys like `"<name>:<index>"` or `(name, upr_file, index)` tuples — when touching matching/lookup logic, check both halves are handled (the `:` split in particular assumes fractal names never contain a colon).
- Much of `uprLib.py`'s data-munging code assumes dicts preserve insertion order and gets re-sorted/re-filtered by copying and rebuilding (`copy.copy(self.data)` then filtering) rather than mutating in place — follow that pattern rather than mutating `self.data`.
- There's a lot of commented-out exploratory code and inline TODOs left in place intentionally as notes; don't feel obligated to clean these up unless the task is specifically about the code they sit in.
