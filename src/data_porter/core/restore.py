"""
Restore: writes a captured (and ideally verified) migration package back
onto a destination machine.

The one rule that makes Win10<->Win11, different account names, and
redirected folders all work without special-casing (per the spec): every
KNOWN_FOLDER_* item is resolved against *this* machine's real Known
Folders at restore time via known_folders.py -- never against any path
recorded back on the source machine. CUSTOM items have no such guarantee
(a custom path like "D:\\Family Photos" may not exist in the same place on
the new PC), so they fall back to their original source_path and are
flagged unresolved unless the caller supplies an explicit override.

Safety pattern mirrors capture.py: every file is written to a
``.dataporter-restore-partial`` staged name in the destination and only
renamed into place after the write (and size check) succeeds, so an
interrupted restore can never leave a half-written destination file
looking valid, and a failed restore can't corrupt a pre-existing file
that was about to be replaced.

Conflict policy (spec section 6):
  * "skip"             - leave an existing destination file untouched
  * "replace"          - always overwrite the destination file
  * "replace_if_newer" - overwrite only if the migrated file is newer
                          than what's already there
  * "keep_both"        - restore the migrated file under a disambiguated
                          name, e.g. "Budget - migrated 2026-07-11.xlsx"

"Ask for each conflict" from the spec is a UI-layer concern: a caller
that wants that resolves the policy per file before calling restore_one()
directly, rather than driving the whole run_restore() loop.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .capture import STAGING_SUFFIX, _compute_hash, _now
from .known_folders import resolve_known_folder
from .manifest import MigrationManifest, known_folder_name_for_restore_target

RESTORE_STAGING_SUFFIX = ".dataporter-restore-partial"

VALID_CONFLICT_POLICIES = ("skip", "replace", "replace_if_newer", "keep_both")


# --------------------------------------------------------------------------
# Destination resolution
# --------------------------------------------------------------------------


@dataclass
class ResolvedDestination:
    logical_folder: str
    restore_target: str
    path: Optional[str]
    method: str
    resolved: bool
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def resolve_destination_roots(
    manifest: MigrationManifest,
    custom_overrides: Optional[dict[str, str]] = None,
) -> dict[str, ResolvedDestination]:
    """
    For every item in the manifest, work out where it should land on this
    (destination) machine. Returns a dict keyed by logical_folder.
    """
    custom_overrides = custom_overrides or {}
    resolved: dict[str, ResolvedDestination] = {}

    for item in manifest.items:
        if item.restore_target == "CUSTOM":
            if item.logical_folder in custom_overrides:
                resolved[item.logical_folder] = ResolvedDestination(
                    logical_folder=item.logical_folder,
                    restore_target=item.restore_target,
                    path=custom_overrides[item.logical_folder],
                    method="user_override",
                    resolved=True,
                )
            else:
                resolved[item.logical_folder] = ResolvedDestination(
                    logical_folder=item.logical_folder,
                    restore_target=item.restore_target,
                    path=item.source_path,
                    method="source_path_fallback",
                    resolved=False,
                    warning=(
                        "Custom folder has no destination mapping -- falling back to "
                        f"its original path ({item.source_path}). Confirm this is correct "
                        "on the destination machine, or supply a custom_overrides entry."
                    ),
                )
            continue

        known_name = known_folder_name_for_restore_target(item.restore_target)
        if known_name is None:
            resolved[item.logical_folder] = ResolvedDestination(
                logical_folder=item.logical_folder,
                restore_target=item.restore_target,
                path=None,
                method="unknown_target",
                resolved=False,
                warning=f"Unrecognised restore_target: {item.restore_target!r}",
            )
            continue

        rf = resolve_known_folder(known_name)
        resolved[item.logical_folder] = ResolvedDestination(
            logical_folder=item.logical_folder,
            restore_target=item.restore_target,
            path=rf.path,
            method=rf.method,
            resolved=bool(rf.path),
            warning=None if rf.path else f"Could not resolve {known_name} on this machine",
        )

    return resolved


def _rel_within_item(package_rel_path: str, item_package_path: str) -> str:
    prefix = item_package_path.rstrip("/") + "/"
    if package_rel_path.startswith(prefix):
        return package_rel_path[len(prefix):]
    return os.path.basename(package_rel_path)


def _dest_path_for(dest_root: str, package_rel_path: str, item_package_path: str) -> str:
    rel = _rel_within_item(package_rel_path, item_package_path)
    return os.path.join(dest_root, *rel.split("/"))


# --------------------------------------------------------------------------
# Restore-state tracking (a sibling table in the same package_state.sqlite)
# --------------------------------------------------------------------------

_RESTORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS restore_state (
    file_id INTEGER PRIMARY KEY,
    dest_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    conflict TEXT,
    error TEXT,
    updated_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_restore_status ON restore_state(status);
"""


def init_restore_state(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_RESTORE_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _seed_restore_state(conn: sqlite3.Connection) -> int:
    """INSERT OR IGNORE a pending restore_state row for every successfully
    captured file that doesn't have one yet. Safe to call repeatedly."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO restore_state (file_id, status, updated_utc)
        SELECT id, 'pending', ?
        FROM files
        WHERE status IN ('copied', 'verified')
        """,
        (_now(),),
    )
    return cur.rowcount


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


@dataclass
class RestoreItemPreview:
    logical_folder: str
    restore_target: str
    destination_root: Optional[str]
    destination_resolved: bool
    destination_warning: Optional[str]
    file_count: int
    total_bytes: int
    existing_conflicts: int


@dataclass
class RestorePreview:
    package_migration_id: str
    package_source_computer: str
    package_created_utc: str
    items: list[RestoreItemPreview] = field(default_factory=list)
    required_bytes: int = 0
    free_bytes_by_root: dict[str, Optional[int]] = field(default_factory=dict)
    unresolved_items: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "package_migration_id": self.package_migration_id,
            "package_source_computer": self.package_source_computer,
            "package_created_utc": self.package_created_utc,
            "items": [dataclasses.asdict(i) for i in self.items],
            "required_bytes": self.required_bytes,
            "free_bytes_by_root": self.free_bytes_by_root,
            "unresolved_items": self.unresolved_items,
        }


def build_restore_preview(
    package_dir: str,
    manifest: MigrationManifest,
    custom_overrides: Optional[dict[str, str]] = None,
) -> RestorePreview:
    """
    Read-only: resolves every item's destination, counts what would be
    written, flags pre-existing conflicts, and checks free space -- all
    before a single byte is restored. This is the "Restore preview"
    screen from the spec (section 5).
    """
    db_path = os.path.join(package_dir, "package_state.sqlite")
    destinations = resolve_destination_roots(manifest, custom_overrides)

    preview = RestorePreview(
        package_migration_id=manifest.migration_id,
        package_source_computer=manifest.source.computer_name,
        package_created_utc=manifest.created_utc,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for item in manifest.items:
            dest = destinations[item.logical_folder]

            rows = conn.execute(
                "SELECT package_rel_path, size_bytes FROM files "
                "WHERE item_logical_folder = ? AND status IN ('copied', 'verified')",
                (item.logical_folder,),
            ).fetchall()

            item_bytes = sum(r["size_bytes"] for r in rows)
            conflicts = 0
            if dest.path:
                for r in rows:
                    dp = _dest_path_for(dest.path, r["package_rel_path"], item.package_path)
                    if os.path.exists(dp):
                        conflicts += 1

            preview.items.append(
                RestoreItemPreview(
                    logical_folder=item.logical_folder,
                    restore_target=item.restore_target,
                    destination_root=dest.path,
                    destination_resolved=dest.resolved,
                    destination_warning=dest.warning,
                    file_count=len(rows),
                    total_bytes=item_bytes,
                    existing_conflicts=conflicts,
                )
            )

            if not dest.resolved:
                preview.unresolved_items.append(item.logical_folder)
            else:
                preview.required_bytes += item_bytes
                if dest.path not in preview.free_bytes_by_root:
                    try:
                        preview.free_bytes_by_root[dest.path] = shutil.disk_usage(
                            dest.path if os.path.isdir(dest.path) else os.path.dirname(dest.path) or "."
                        ).free
                    except OSError:
                        preview.free_bytes_by_root[dest.path] = None
    finally:
        conn.close()

    return preview


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------


@dataclass
class RestoreSummary:
    seeded: int = 0
    restored: int = 0
    already_done: int = 0
    skipped_policy: int = 0
    conflicts_renamed: int = 0
    failed: int = 0
    blocked_items: list[str] = field(default_factory=list)
    total_bytes_restored: int = 0
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _keep_both_path(dest_path: str) -> str:
    base, ext = os.path.splitext(dest_path)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    candidate = f"{base} - migrated {date_str}{ext}"
    if not os.path.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate2 = f"{base} - migrated {date_str} ({n}){ext}"
        if not os.path.exists(candidate2):
            return candidate2
        n += 1


def run_restore(
    package_dir: str,
    manifest: MigrationManifest,
    conflict_policy: str = "replace_if_newer",
    custom_overrides: Optional[dict[str, str]] = None,
    hash_check: bool = True,
    reseed: bool = True,
) -> RestoreSummary:
    """
    Restore every captured file to its resolved destination, honouring
    ``conflict_policy`` for anything already sitting at the destination.

    Resumable, exactly like capture: progress is journaled per-file to
    ``restore_state`` in the same package_state.sqlite, so re-running this
    against the same package_dir picks up where a previous run left off
    (only pending/failed rows are (re)attempted) instead of restarting.
    """
    assert conflict_policy in VALID_CONFLICT_POLICIES, (
        f"conflict_policy must be one of {VALID_CONFLICT_POLICIES}, got {conflict_policy!r}"
    )

    summary = RestoreSummary()
    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_restore_state(db_path)

    destinations = resolve_destination_roots(manifest, custom_overrides)
    item_by_logical = {item.logical_folder: item for item in manifest.items}
    for logical_folder, dest in destinations.items():
        if not dest.resolved:
            summary.blocked_items.append(logical_folder)

    errors_path = os.path.join(package_dir, "metadata", "restore_errors.jsonl")
    os.makedirs(os.path.dirname(errors_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if reseed:
            summary.seeded = _seed_restore_state(conn)
            conn.commit()

        rows = conn.execute(
            """
            SELECT f.id AS file_id, f.item_logical_folder, f.package_rel_path,
                   f.size_bytes, f.modified_utc, f.sha256
            FROM files f
            JOIN restore_state rs ON rs.file_id = f.id
            WHERE rs.status IN ('pending', 'failed')
            ORDER BY f.id
            """
        ).fetchall()

        import json as _json

        with open(errors_path, "a", encoding="utf-8") as error_log:
            for row in rows:
                logical_folder = row["item_logical_folder"]
                item = item_by_logical.get(logical_folder)
                dest = destinations.get(logical_folder)

                if item is None or dest is None or not dest.resolved:
                    # Destination couldn't be resolved for this item at all
                    # -- don't guess, leave it pending for a future run
                    # once the caller supplies an override / the folder
                    # becomes resolvable.
                    continue

                package_file_path = os.path.join(package_dir, row["package_rel_path"])
                dest_path = _dest_path_for(dest.path, row["package_rel_path"], item.package_path)

                conflict = None
                final_dest_path = dest_path

                if os.path.exists(dest_path):
                    if conflict_policy == "skip":
                        conn.execute(
                            "UPDATE restore_state SET status='skipped', dest_path=?, "
                            "conflict='existing', updated_utc=? WHERE file_id=?",
                            (dest_path, _now(), row["file_id"]),
                        )
                        conn.commit()
                        summary.skipped_policy += 1
                        continue

                    if conflict_policy == "replace_if_newer":
                        dest_mtime = os.path.getmtime(dest_path)
                        source_mtime = None
                        if row["modified_utc"]:
                            try:
                                source_mtime = datetime.fromisoformat(
                                    row["modified_utc"]
                                ).timestamp()
                            except ValueError:
                                source_mtime = None
                        if source_mtime is None or source_mtime <= dest_mtime:
                            conn.execute(
                                "UPDATE restore_state SET status='skipped', dest_path=?, "
                                "conflict='existing_newer_or_equal', updated_utc=? WHERE file_id=?",
                                (dest_path, _now(), row["file_id"]),
                            )
                            conn.commit()
                            summary.skipped_policy += 1
                            continue
                        conflict = "existing_older_replaced"

                    elif conflict_policy == "keep_both":
                        final_dest_path = _keep_both_path(dest_path)
                        conflict = "existing_kept_both"

                    elif conflict_policy == "replace":
                        conflict = "existing_replaced"

                staged_path = final_dest_path + RESTORE_STAGING_SUFFIX

                conn.execute(
                    "UPDATE restore_state SET status='restoring', dest_path=?, conflict=?, "
                    "updated_utc=? WHERE file_id=?",
                    (final_dest_path, conflict, _now(), row["file_id"]),
                )
                conn.commit()

                try:
                    if not os.path.isfile(package_file_path):
                        raise FileNotFoundError(
                            f"package file missing: {package_file_path}"
                        )

                    os.makedirs(os.path.dirname(final_dest_path), exist_ok=True)
                    shutil.copy2(package_file_path, staged_path)

                    staged_size = os.path.getsize(staged_path)
                    if staged_size != row["size_bytes"]:
                        raise IOError(
                            f"size mismatch after restore copy: expected={row['size_bytes']} "
                            f"actual={staged_size}"
                        )

                    if hash_check and row["sha256"] and not str(row["sha256"]).startswith("partial:"):
                        recomputed = _compute_hash(staged_path, staged_size, "full")
                        if recomputed != row["sha256"]:
                            raise IOError("hash mismatch after restore copy")

                    os.replace(staged_path, final_dest_path)

                    conn.execute(
                        "UPDATE restore_state SET status='restored', dest_path=?, error=NULL, "
                        "updated_utc=? WHERE file_id=?",
                        (final_dest_path, _now(), row["file_id"]),
                    )
                    conn.commit()

                    summary.restored += 1
                    summary.total_bytes_restored += staged_size
                    if conflict == "existing_kept_both":
                        summary.conflicts_renamed += 1

                except Exception as exc:
                    if os.path.exists(staged_path):
                        try:
                            os.remove(staged_path)
                        except OSError:
                            pass
                    conn.execute(
                        "UPDATE restore_state SET status='failed', error=?, updated_utc=? "
                        "WHERE file_id=?",
                        (str(exc), _now(), row["file_id"]),
                    )
                    conn.commit()
                    error_log.write(
                        _json.dumps(
                            {
                                "package_rel_path": row["package_rel_path"],
                                "dest_path": final_dest_path,
                                "error": str(exc),
                                "at_utc": _now(),
                            }
                        )
                        + "\n"
                    )
                    summary.failed += 1
                    summary.errors.append(
                        {"package_rel_path": row["package_rel_path"], "error": str(exc)}
                    )

        already_done = conn.execute(
            "SELECT COUNT(*) FROM restore_state WHERE status = 'restored'"
        ).fetchone()[0]
        summary.already_done = already_done - summary.restored
    finally:
        conn.close()

    return summary


def restore_status(package_dir: str) -> dict:
    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_restore_state(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM restore_state GROUP BY status"
        ).fetchall()
        return {status: n for status, n in rows}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Post-restore verification
# --------------------------------------------------------------------------


@dataclass
class RestoreVerifySummary:
    checked: int = 0
    verified: int = 0
    failed: int = 0
    missing: int = 0
    problems: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def verify_restore(package_dir: str, level: str = "balanced") -> RestoreVerifySummary:
    """
    Re-checks every file this run actually restored against what's really
    sitting at its destination path now -- the "post-restore verification"
    step from the spec (section 7). Distinct from verify.py, which checks
    the *package*, not the destination the files were written to.
    """
    assert level in ("fast", "balanced", "full")

    db_path = os.path.join(package_dir, "package_state.sqlite")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    summary = RestoreVerifySummary()

    try:
        rows = conn.execute(
            """
            SELECT rs.file_id, rs.dest_path, f.size_bytes, f.sha256
            FROM restore_state rs
            JOIN files f ON f.id = rs.file_id
            WHERE rs.status = 'restored'
            ORDER BY rs.file_id
            """
        ).fetchall()

        for row in rows:
            summary.checked += 1
            dest_path = row["dest_path"]

            if not dest_path or not os.path.isfile(dest_path):
                summary.missing += 1
                summary.problems.append({"path": dest_path, "problem": "missing at destination"})
                continue

            actual_size = os.path.getsize(dest_path)
            if actual_size != row["size_bytes"]:
                summary.failed += 1
                summary.problems.append(
                    {
                        "path": dest_path,
                        "problem": f"size mismatch: expected {row['size_bytes']}, found {actual_size}",
                    }
                )
                continue

            if level == "fast":
                summary.verified += 1
                continue

            stored = row["sha256"]
            if not stored or str(stored).startswith("partial:"):
                # Nothing trustworthy to compare a full hash against --
                # size match is the best available signal.
                summary.verified += 1
                continue

            recomputed = _compute_hash(dest_path, actual_size, "full" if level == "full" else "balanced")
            if recomputed == stored:
                summary.verified += 1
            else:
                summary.failed += 1
                summary.problems.append({"path": dest_path, "problem": "hash mismatch"})
    finally:
        conn.close()

    return summary
