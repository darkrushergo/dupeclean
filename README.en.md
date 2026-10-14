# Dupeclean

[![tests](https://github.com/darkrushergo/dupeclean/actions/workflows/tests.yml/badge.svg)](https://github.com/darkrushergo/dupeclean/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

A **read-only**, two-phase tool to find duplicate files across multiple disks,
including disks that **cannot be mounted at the same time**.

> **Rule #1: it is read-only.** It never deletes, moves or modifies any original
> file. Reports are just lists; deleting is always a separate, explicit step.

> ## ⚠️ Important warning · Disclaimer
> `scan` / `compare` / `relabel` / `web` are read-only; however the **delete-list CSVs** and the bundled **delete scripts permanently delete files with no recovery** (external drives usually have no trash). Detection is **heuristic and can be wrong**. **Before using the delete scripts: dry-run first, review the list, keep at least one copy, and back up. You assume all risk.**
> Full text: **[DISCLAIMER.md](DISCLAIMER.md)**.

Typical use case: you have several USB/NTFS backup drives and want to find
duplicates **across** them (and within each). Because they can't all be plugged
in at once, the work is split into two phases:

1. **`scan`** — scan each disk and write a self-contained index CSV.
2. **`compare`** — with all disks unplugged, read several index CSVs and produce
   a Markdown report + classified CSVs.

There is also a **local web UI** (`web`), a **label fixer** (`relabel`), and
separate, explicit **delete scripts** for Linux and Windows.

---

## 1. Requirements

- Python 3.8+ (Linux; also works on macOS/Windows for most features).
- **No mandatory third-party dependencies** (standard library only).
- Optional speedups (auto-detected, gracefully falls back):
  - [`rapidfuzz`](https://pypi.org/project/rapidfuzz/) — much faster similarity.
  - [`xxhash`](https://pypi.org/project/xxhash/) — only if you use `--hash --algo xxh64/xxh128`.

```bash
pip install rapidfuzz     # recommended (optional)
pip install xxhash        # only with --hash --algo xxh64/xxh128
```

## 2. Quick start

```bash
# Phase 1: scan each disk (hashing is OFF by default; only files >= 50MiB)
python3 dupeclean.py scan /media/user/DISK_A -o results/indexes/DISK_A.index.csv
python3 dupeclean.py scan /media/user/DISK_B -o results/indexes/DISK_B.index.csv

# Phase 2: all disks can be unplugged; compare the indexes
python3 dupeclean.py compare results/indexes/DISK_A.index.csv \
                            results/indexes/DISK_B.index.csv \
                    -o results/reports/run1
```

Output of `compare` (in `results/reports/run1/`):

| File | Contents |
|---|---|
| `report.md` | Human-readable report (overview + A + A2 + B) |
| `duplicates-exact.csv` | A / A2 group details |
| `similar-candidates.csv` | B similar candidates |
| `groups.json` | Machine-readable groups (used by the web UI) |

Language: use `--lang auto|zh|en` or `DUPCLEAN_LANGUAGE`; default is auto-detected
from the system locale.

## 3. Web UI (optional, recommended)

Standard-library `http.server`, zero dependencies:

```bash
python3 dupeclean.py web                 # http://127.0.0.1:8765/ (opens browser)
python3 dupeclean.py web --port 9000
python3 dupeclean.py web --workdir /data/dc
python3 dupeclean.py web --no-open       # headless
```

Everything is stored under the **results directory** (default `results/` next to
the script; override with `--workdir` or `DUPCLEAN_WORKDIR`):

```
results/
├─ indexes/    # *.index.csv from scans
└─ reports/    # report.md / *.csv / groups.json from compares
```

Three tabs:

1. **Scan** — click a detected **real disk** (snap/loop, `overlay`/`tmpfs`, `/`,
   `/boot/efi`, `/snap/*` … are filtered out; toggle "system mounts" to show them).
   The source label and index name follow the chosen disk automatically.
2. **Compare** — click to select existing index CSVs (no need to type paths).
3. **Review · delete list** — duplicate candidates are shown as group cards, with:
   - **Disk filter** to clean one disk first ("chosen disk first", "check all copies
     on the chosen disk").
   - **Category filter**: All / exact-like / suspicious.
   - **Sorting** by group (max size / count / similarity / kind) and within a group
     (**size ↓** / ↑ / time / path), with toggle "hide files inside selected folders".
   - **Duplicate folders** group: folders whose **entire subtree** matches the other
     copy (computed from the original scan records, path-independent).
   - Check files/folders, then **"Generate delete list CSV"** → per-disk
     `delete-list-<disk>.csv`.

Safety: binds to `127.0.0.1` only, no auth (do not expose publicly); one job at a
time; preview/download limited to files under the results directory; read-only.

Eye-comfort themes: 7 palettes (warm dark by default, plus neutral dark,
Solarized, Gruvbox, Nord, warm paper, Solarized Light). Stored in localStorage.

## 4. CLI reference

### 4.1 `scan`

```bash
python3 dupeclean.py scan <dirs...> -o <index.csv> [options]
```

| Option | Default | Meaning |
|---|---|---|
| `-o, --output` | required | output index CSV (atomic write) |
| `--label` | inferred | source disk label (mount-path basename) |
| `--min-size` | `50M` | ignore files smaller than this (`50M`, `1.5G`, bytes…) |
| `--ignore` | built-in | extra ignored directory names (repeatable/comma-separated) |
| `--ignore-ext` | built-in | extra ignored extensions |
| `--ext` | | extension whitelist (only these are indexed) |
| `--include-hidden` | off | include hidden files/dirs |
| `--follow-symlinks` | off | follow symlinks |
| `--hash` | **off** | compute hashes (enables hash-level group A in compare) |
| `--algo` | `sha256` | hash algorithm |
| `--chunk-size` | `1M` | hash chunk size |
| `--quiet` / `--print-path` | off | reduce stderr / print output path to stdout |

### 4.2 `compare`

| Option | Default | Meaning |
|---|---|---|
| `-o, --output` | required | output directory |
| `--threshold` | `0.75` | similarity threshold |
| `--cluster` | `pair` | B output: `pair` or `group` (connected components) |
| `--sim-algo` | `auto` | `auto` / `rapidfuzz` / `difflib` |
| `--max-bucket` | `100` | max inverted-index bucket size (avoids O(n²)) |
| `--max-pairs` | `5000000` | global candidate-pair cap |
| `--report-limit` | `500` | max groups/pairs shown in `report.md` |
| `--max-size-diff` | `50M` | **non-video** files differing by more count as different; **video ignores size** |
| `--no-size-gate` | off | disable the size filter |
| `--video-ext` | built-in | extra extensions treated as video |

### 4.3 `relabel`

Fix a wrong `source_label` in an index CSV so it matches the disk name at the
front of the filename (`DISK_A.index.csv` → `DISK_A`):

```bash
python3 dupeclean.py relabel results/indexes/*.csv
python3 dupeclean.py relabel --dry-run results/indexes/*.csv
```

Only rewrites dupeclean-generated index CSVs (metadata line and `source_label`
column); never touches original data. Backups (`.bak`) are created by default.
You must list the files explicitly (it does not scan directories).

### 4.4 `web`

See section 3. Options: `--host`, `--port`, `--workdir`, `--open/--no-open`.

## 5. How to read the report

- **A. Exact duplicates**
  - without hashing: same normalized filename **and** same size (inferred).
  - with `--hash`: identical hash (byte-level evidence).
- **A2. Same name, different content/size** — e.g. 720p vs IMAX releases.
- **B. Similar candidates** — filename similarity ≥ threshold, listed as pairs by
  default.
  - **Size rule**: non-video files differing by more than `--max-size-diff`
    (default 50MiB) are not candidates; **video files ignore size differences**
    (so 720p vs IMAX of the same movie still match).

Normalization: Unicode NFC + casefold; season/episode numbers (`S01E02`, `1x02`,
`第3集`, `EP12`) are detected and penalized so different episodes don't match.

## 6. Delete lists and delete scripts

The **main tool never deletes.** The delete list is written per disk:

```
reports/run1/
├─ delete-list-DISK_A.csv
├─ delete-list-DISK_B.csv
└─ …
```

Columns: `group_id,kind,action,source_label,path,filename,size,size_human,mtime_iso,hash`.
`action` is `delete` (delete the file) or `rmdir` (delete the directory **only if
empty**; used to clean up duplicate folders).

Deletion is done by separate scripts (default **dry-run**; an explicit flag is
required to delete):

| OS | Script |
|---|---|
| Linux/macOS | `scripts/delete-files.sh` |
| Windows | `scripts/delete-files.ps1` (launcher `scripts/delete-files.bat`) |

```bash
# Linux: dry-run, then delete
./scripts/delete-files.sh --map "/run/media/user/DISK_A=/mnt/disk_a" results/reports/run1
./scripts/delete-files.sh --yes --map "/run/media/user/DISK_A=/mnt/disk_a" results/reports/run1
```

```powershell
# Windows
.\scripts\delete-files.ps1 -Map "/run/media/user/DISK_A=E:\" -Path results\reports\run1
.\scripts\delete-files.ps1 -Yes -Map "/run/media/user/DISK_A=E:\" -Path results\reports\run1
```

Safety: processes directory targets by globbing `delete-list-*.csv`; only
`action=delete`/`rmdir` rows; never deletes directories recursively; dry-run by
default; path-prefix remap via `--map`/`-Map` or a `delete-map.txt` file
(see `scripts/delete-map.txt.sample`).

## 7. Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers helpers, scan filters, groups, hashing, folder detection (recursive),
size gate, web API, delete scripts, and a read-only guarantee.

## 8. Read-only guarantee

- Scanning only uses `lstat` and read-only `open(..., "rb")`.
- Comparing does not touch original files at all.
- All output is written to the chosen `-o` path (atomic replace).
- `relabel` only edits dupeclean-generated index CSVs.
- Real deletion exists only in the separate `scripts/delete-files.*`.
