# Data Porter — v0.1 core scan engine

This is the first slice of the v0.1 MVP from the spec: **discovery only**.
It finds Known Folders (the Windows-correct way, not hardcoded paths),
walks them safely, and produces JSON + HTML reports. No packaging, no
capture, no restore yet — that's the natural next slice once this is solid.

## What's implemented

- **`core/known_folders.py`** — resolves Known Folders (Documents, Pictures,
  Downloads, Music, Videos, Desktop, Saved Games, Favorites, Contacts) via
  the real Windows Known Folder API (`SHGetKnownFolderPath`), with two
  fallback tiers:
  1. `pywin32` (primary, used when installed)
  2. raw `ctypes` call into `shell32.dll` (works even without pywin32)
  3. a **dev fallback** (home-directory guess) that only activates on
     non-Windows platforms — this is what let me build and test the whole
     thing in this sandbox without a Windows box on hand. It's clearly
     tagged in the result (`method="dev_fallback"`) so it can never be
     mistaken for a real Windows resolution once you run it on your dad's
     PC.

- **`core/scanner.py`** — the actual walk. Per the spec's edge-case list,
  it already handles:
  - reparse points / symlinks: detected and **pruned before descending**,
    so junctions and symlink loops can't cause infinite recursion or
    double-counting (tested — see `test_symlink_is_skipped_not_followed`)
  - permission errors / unreadable files: recorded as `SkippedEntry`
    with a reason, never silently dropped
  - cloud placeholder files (OneDrive "files on demand" etc.): flagged
    via `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` / `OFFLINE` so a scan never
    claims a cloud stub is a real local file
  - overlong paths: flagged as a warning without aborting the scan
  - bounded top-N tracking for largest/newest files (heap-based, so this
    won't blow up memory on huge folders)
  - a lightweight "you may have forgotten..." pass over secondary drives

- **`core/report.py`** — orchestrates a full scan across all Known Folders
  + custom paths, and writes both a machine-readable `source_report.json`
  and a human-readable `source_report.html`.

- **`cli.py`** — a thin CLI so you can run and eyeball this before there's
  a PyQt6 front end:

  ```bash
  python -m data_porter.cli scan --output ./out --custom "D:\Family Photos"
  ```

## Running it

On your dad's Windows machine (once you copy this over):

```bash
pip install pywin32          # optional, primary resolution path
python -m data_porter.cli scan --output C:\DataPorterScan
```

It'll drop `source_report.json` and `source_report.html` in that folder.
Open the HTML one first — it's the same "review screen" data the spec
describes (folder, size, file count, skipped/reparse/cloud counts).

On Linux/macOS (dev machine, this sandbox, CI, etc.) it runs too — it'll
just resolve folders via the home-directory fallback instead of the real
Windows API, which is exactly what let me develop and unit-test this
without Windows at all.

## Tests

```bash
python -m unittest discover -s tests -v
```

5 tests currently, covering: basic count/size, missing-folder handling,
top-N ordering, symlink/reparse-point pruning, and empty folders.

## Deliberately NOT in this slice

Per the MVP boundary in the spec, this only does discovery:

- no package format / directory layout yet (`data/`, `migration.json`, etc.)
- no capture (copying with `.dataporter-partial` staging)
- no verification (checksums)
- no restore / conflict handling
- no OneDrive hydration policy (cloud files are *flagged*, not acted on)
- no application-data adapters (Thunderbird, Outlook PST, etc.)
- no PyQt6 GUI — the CLI is a scaffold for exercising the engine only

## Suggested next slice

Given the pieces above are solid, the natural next step is the **manifest
+ package format** (the `migration.json` / directory-tree layer from the
spec) — that's what turns "I scanned some folders" into "I have a portable
package that a restore step can act on." Capture (the actual file copy
with staging + resume) fits naturally right after that.
