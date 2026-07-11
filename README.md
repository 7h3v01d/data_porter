# Data Porter — v0.1 (scan → plan → capture → verify)

Four pipeline stages are implemented so far: **discovery, packaging,
capture, and verification**. Restore (writing back to a destination
machine's Known Folders) is the one piece of the v0.1 MVP boundary not
built yet — everything up to "I have a verified, portable package" works
end to end.

```
scan  →  plan  →  capture  →  verify
 │         │          │           │
 finds    turns      copies      re-checks
 folders  selection  files in,   package vs
          into a     staged +   recorded
          package    journaled  state
          skeleton
```

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

- **`core/manifest.py`** — the `migration.json` data model. Crucially,
  each item's `restore_target` is a **logical** value
  (`KNOWN_FOLDER_DOCUMENTS`), never a baked-in path — that's what makes
  Win10→Win11, different account names, and redirected folders all work
  without special-casing, per the spec. Custom (non-Known-Folder)
  selections get `restore_target = "CUSTOM"` and keep their original
  source path around for a future restore step to work with.

- **`core/package.py`** — turns a `ScanReport` + a selection into a
  `MigrationManifest`, and lays down the actual package directory:

  ```
  package_dir/
  ├── migration.json
  ├── package_state.sqlite   # per-file capture status, for resume
  ├── checksums.jsonl        # append-only, one line per captured file
  ├── data/
  │   ├── Documents/
  │   └── Pictures/
  └── metadata/
      ├── known_folders.json
      ├── selections.json
      ├── skipped_files.jsonl
      └── errors.jsonl
  ```

- **`core/capture.py`** — the actual copy. Every file goes to a
  `.dataporter-partial` staged name first and is only renamed to its real
  name after the copy succeeds (and is hashed, per the chosen level) — an
  interrupted run can never leave a half-written file looking valid.
  Every completed file is journaled to `package_state.sqlite`
  immediately, so **re-running capture on the same package_dir resumes**
  rather than starting over: only `pending`/`failed`/`copying` rows are
  (re)attempted. A failure on one file (permission error, source deleted
  mid-run, etc.) is recorded and capture moves on to the rest rather than
  aborting. Three hash levels: `fast` (no hashing, size-only), `balanced`
  (full SHA-256 under 200MB, cheap sampled hash above that), `full`
  (SHA-256 every file, no matter the size).

- **`core/verify.py`** — a separate pass that re-checks what's actually
  sitting in the package against `package_state.sqlite` — useful after
  moving the package to another drive, or as a final check before trusting
  it enough to restore from. Same three effort levels as capture.

- **`cli.py`** — a thin CLI wiring all four stages together before there's
  a PyQt6 front end:

  ```bash
  python -m data_porter.cli scan    --output ./scan --custom "D:\Family Photos"
  python -m data_porter.cli plan    --scan-json ./scan/source_report.json --package-dir ./package
  python -m data_porter.cli capture --package-dir ./package --hash-level balanced
  python -m data_porter.cli status  --package-dir ./package
  python -m data_porter.cli verify  --package-dir ./package --level full
  ```

  `capture` and `verify` both exit with code `2` (rather than `0`) if
  anything failed, so they're safe to script/chain.

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

13 tests currently:
- `test_scanner.py` (5): basic count/size, missing-folder handling, top-N
  ordering, symlink/reparse-point pruning, empty folders.
- `test_pipeline.py` (8): manifest helpers (name sanitizing/dedup), a full
  scan→plan→capture→verify run, capture resumability/idempotency, an
  **actually-simulated interrupted capture** (stale `.dataporter-partial`
  + a `copying`-status row left behind, then confirming recovery), verify
  correctly detecting on-disk corruption, and a source file vanishing
  mid-run being recorded as a failure rather than crashing the run.

I also manually exercised the whole pipeline against a synthetic profile
with a symlink loop (see conversation) to confirm end-to-end behaviour
outside of the unit tests, including intentionally corrupting a captured
file afterwards and confirming `verify` catches it.

## Deliberately NOT in this slice

Per the MVP boundary in the spec, restore is the one piece left:

- no restore step (resolving `restore_target` against the destination
  machine's real Known Folders and writing files back)
- no conflict handling (skip / replace / keep-both / ask-per-conflict)
- no restore preview or post-restore verification report
- no OneDrive hydration policy (cloud files are *flagged* during scan/
  capture, but there's no explicit "hydrate first" or "exclude OneDrive"
  policy yet)
- no application-data adapters (Thunderbird, Outlook PST, etc.)
- no PyQt6 GUI — the CLI is a scaffold for exercising the engine only

## Suggested next slice

**Restore** is the natural next piece — it's the other half of what makes
this actually useful, and the manifest was deliberately designed for it:
resolve each item's `restore_target` against the *destination* machine's
Known Folders (reusing `known_folders.py` as-is), then work through
conflict policy (skip/replace/keep-both) file by file, using the same
staged-write-then-rename pattern as capture so a bad restore can't corrupt
existing destination files either.
