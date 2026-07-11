# Data Porter — v0.1 (scan → plan → capture → verify → restore)

All six pipeline stages from the v0.1 MVP boundary are now implemented:
**discovery, packaging, capture, verification, restore, and post-restore
verification.** End to end, "I have a verified, portable package" and "that
package has been safely written back onto the new machine" both work.

```
scan  →  plan  →  capture  →  verify  →  restore  →  restore-verify
 │         │          │           │           │              │
 finds    turns      copies      re-checks   resolves       re-checks
 folders  selection  files in,   package vs  Known Folders  destination
          into a     staged +   recorded    on *this*       files vs
          package    journaled  state       machine,        recorded
          skeleton                          conflict-       state
                                             checks, then
                                             staged-writes
                                             each file
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
     mistaken for a real Windows resolution once you run it on your PC.

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

- **`core/restore.py`** — the new piece. Resolves each item's *logical*
  `restore_target` against **this** (destination) machine's real Known
  Folders via `known_folders.py` — never against any path recorded on the
  source machine, which is what makes Win10→Win11, different account
  names, and redirected folders all just work. `CUSTOM` items (folders
  that aren't a Known Folder, e.g. `D:\Family Photos`) have no such
  guarantee on a different machine, so they fall back to their original
  source path and are flagged **unresolved** unless the caller supplies
  a `custom_overrides` mapping — nothing is ever guessed at silently.

  Mirrors `capture.py`'s safety pattern exactly: every file is written to
  a `.dataporter-restore-partial` staged name and only renamed into place
  after the copy (and, by default, a hash check) succeeds, so an
  interrupted restore can never leave a half-written destination file
  looking valid, and progress is journaled per-file to a `restore_state`
  table (a sibling of `files` in the same `package_state.sqlite`), so
  **re-running restore on the same package_dir resumes** rather than
  restarting.

  Implements every conflict policy from the spec (section 6) except
  "ask for each conflict", which is a UI-layer concern:
  - `skip` — leave the existing destination file untouched
  - `replace` — always overwrite
  - `replace_if_newer` — overwrite only if the migrated file is newer
  - `keep_both` — restore under a disambiguated name, e.g.
    `Budget - migrated 2026-07-11.xlsx`

  Also provides:
  - `build_restore_preview()` — the "Restore preview" screen from the
    spec (section 5): resolved destinations, existing conflicts, required
    bytes, and free space per destination root, all before writing a
    single byte.
  - `verify_restore()` — post-restore verification (section 7): re-checks
    what's actually sitting at each file's *destination* path (not the
    package) against the recorded size/hash, at the same fast/balanced/
    full effort levels as `verify.py`.

- **`cli.py`** — a thin CLI wiring all six stages together before there's
  a PyQt6 front end:

  ```bash
  python -m data_porter.cli scan            --output ./scan --custom "D:\Family Photos"
  python -m data_porter.cli plan            --scan-json ./scan/source_report.json --package-dir ./package
  python -m data_porter.cli capture         --package-dir ./package --hash-level balanced
  python -m data_porter.cli status          --package-dir ./package
  python -m data_porter.cli verify          --package-dir ./package --level full

  # on the destination machine, once the package has arrived there:
  python -m data_porter.cli restore-preview --package-dir ./package
  python -m data_porter.cli restore         --package-dir ./package \
                                             --conflict-policy replace_if_newer \
                                             --custom-override "D:\Family Photos=E:\Family Photos" \
                                             --report ./restore_report.html
  python -m data_porter.cli restore-status  --package-dir ./package
  python -m data_porter.cli restore-verify  --package-dir ./package --level full
  ```

  `capture`, `verify`, `restore`, and `restore-verify` all exit with code
  `2` (rather than `0`) if anything failed, so they're safe to
  script/chain.

## Running it

On Windows machine (once you copy this over):

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

25 tests currently:
- `test_scanner.py` (5): basic count/size, missing-folder handling, top-N
  ordering, symlink/reparse-point pruning, empty folders.
- `test_pipeline.py` (8): manifest helpers (name sanitizing/dedup), a full
  scan→plan→capture→verify run, capture resumability/idempotency, an
  **actually-simulated interrupted capture** (stale `.dataporter-partial`
  + a `copying`-status row left behind, then confirming recovery), verify
  correctly detecting on-disk corruption, and a source file vanishing
  mid-run being recorded as a failure rather than crashing the run.
- `test_restore.py` (12): destination resolution (both a clean
  `custom_overrides` resolution and the unresolved/flagged fallback path),
  a full restore with no conflicts, restore resumability/idempotency, all
  four conflict policies (`skip`, `replace`, `replace_if_newer`,
  `keep_both`) each verified against real on-disk before/after content, an
  item with no override being reported as **blocked** rather than guessed
  at, restore-status counts, and `verify_restore()` catching both
  destination-side corruption and a missing destination file.

I also manually exercised the whole pipeline against a synthetic profile
with a symlink loop (see conversation) to confirm end-to-end behaviour
outside of the unit tests, including intentionally corrupting a captured
file afterwards and confirming `verify` catches it, and separately ran the
full CLI chain (`scan` → `plan` → `capture` → `restore-preview` → `restore`
→ `restore-verify`) against a throwaway source/destination pair to confirm
the wiring in `cli.py` actually works, not just the underlying functions.

## Deliberately NOT in this slice

The v0.1 MVP boundary from the spec is now fully built. Everything below
is out of scope until v0.2:

- no OneDrive hydration policy (cloud files are *flagged* during scan/
  capture, but there's no explicit "hydrate first" or "exclude OneDrive"
  policy yet)
- no application-data adapters (Thunderbird, Outlook PST, etc.)
- no duplicate detection across folders
- no package encryption or direct LAN transfer
- no "ask for each conflict" — that's a UI-layer decision that feeds a
  per-file policy into the restore engine; the engine itself only takes
  one policy per run (or a caller can drive individual files itself for a
  true per-conflict prompt)
- no PyQt6 GUI — the CLI is a scaffold for exercising the engine only

## Suggested next slice

With the whole v0.1 pipeline (scan → plan → capture → verify → restore →
restore-verify) working end to end, the natural next steps are either:

1. **A minimal PyQt6 front end** over the existing CLI commands — the
   "review screen" (section 2) and "restore preview" (section 5) from the
   spec map almost directly onto `ScanReport` / `RestorePreview` already;
   or
2. **OneDrive-aware scanning** (v0.2, spec section "OneDrive is going to
   be one of the annoying parts") — `models.FileEntry` already has
   `is_cloud_placeholder`, so the groundwork is there, but there's no
   explicit hydrate/exclude policy wired through capture yet.

Given the move is Win11→Win11 with likely OneDrive-redirected folders,
I'd lean toward OneDrive-awareness before the GUI — it's the part of the
spec most likely to bite on his actual machine.
