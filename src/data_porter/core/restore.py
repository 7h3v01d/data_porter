"""Safe, resumable restoration of a verified Data Porter package."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .capture import (
    HASH_NONE,
    _compute_hash_for_kind,
    _hash_file_full,
    _infer_hash_kind,
    _now,
)
from .known_folders import resolve_known_folder
from .manifest import MigrationManifest, known_folder_name_for_restore_target
from .package import init_package_db
from .safety import path_is_within, paths_overlap, safe_join

RESTORE_STAGING_SUFFIX = ".dataporter-restore-partial"
VALID_CONFLICT_POLICIES = ("skip", "replace", "replace_if_newer", "keep_both")


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
    custom_overrides = custom_overrides or {}
    resolved: dict[str, ResolvedDestination] = {}

    for item in manifest.items:
        if item.restore_target == "CUSTOM":
            if item.logical_folder in custom_overrides:
                destination = os.path.abspath(os.path.expanduser(custom_overrides[item.logical_folder]))
                resolved[item.logical_folder] = ResolvedDestination(
                    item.logical_folder,
                    item.restore_target,
                    destination,
                    "user_override",
                    True,
                )
            else:
                resolved[item.logical_folder] = ResolvedDestination(
                    item.logical_folder,
                    item.restore_target,
                    item.source_path,
                    "source_path_fallback",
                    False,
                    "Custom folder has no confirmed destination. Supply an explicit override.",
                )
            continue

        known_name = known_folder_name_for_restore_target(item.restore_target)
        if known_name is None:
            resolved[item.logical_folder] = ResolvedDestination(
                item.logical_folder,
                item.restore_target,
                None,
                "unknown_target",
                False,
                f"Unrecognised restore_target: {item.restore_target!r}",
            )
            continue

        rf = resolve_known_folder(known_name)
        is_resolved = bool(rf.path)
        warning = None if is_resolved else f"Could not safely resolve {known_name} on this machine"
        resolved[item.logical_folder] = ResolvedDestination(
            item.logical_folder,
            item.restore_target,
            rf.path,
            rf.method,
            is_resolved,
            warning,
        )

    return resolved


def _rel_within_item(package_rel_path: str, item_package_path: str) -> str:
    stored = package_rel_path.replace("\\", "/")
    prefix = item_package_path.replace("\\", "/").rstrip("/") + "/"
    if not stored.startswith(prefix):
        raise ValueError(
            f"package file {package_rel_path!r} is not within item path {item_package_path!r}"
        )
    rel = stored[len(prefix) :]
    if not rel:
        raise ValueError("package file has an empty item-relative path")
    return rel


def _dest_path_for(dest_root: str, package_rel_path: str, item_package_path: str) -> str:
    return safe_join(dest_root, _rel_within_item(package_rel_path, item_package_path))


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
    init_package_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_RESTORE_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _seed_restore_state(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO restore_state (file_id, status, updated_utc)
        SELECT id, 'pending', ? FROM files WHERE status IN ('copied', 'verified')
        """,
        (_now(),),
    )
    return cur.rowcount


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
            "items": [dataclasses.asdict(item) for item in self.items],
            "required_bytes": self.required_bytes,
            "free_bytes_by_root": self.free_bytes_by_root,
            "unresolved_items": self.unresolved_items,
        }


def _existing_parent(path: str) -> str:
    current = os.path.abspath(path)
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            return "."
        current = parent
    return current


def _block_package_destination_overlap(
    package_dir: str, destinations: dict[str, ResolvedDestination]
) -> None:
    for dest in destinations.values():
        if dest.resolved and dest.path and paths_overlap(package_dir, dest.path):
            dest.resolved = False
            dest.warning = (
                "Destination overlaps the migration package. Move the package outside "
                "the folder being restored and run the preview again."
            )


def build_restore_preview(
    package_dir: str,
    manifest: MigrationManifest,
    custom_overrides: Optional[dict[str, str]] = None,
) -> RestorePreview:
    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_restore_state(db_path)
    destinations = resolve_destination_roots(manifest, custom_overrides)
    _block_package_destination_overlap(package_dir, destinations)

    preview = RestorePreview(
        manifest.migration_id,
        manifest.source.computer_name,
        manifest.created_utc,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for item in manifest.items:
            dest = destinations[item.logical_folder]
            rows = conn.execute(
                "SELECT package_rel_path, size_bytes FROM files "
                "WHERE item_logical_folder=? AND status IN ('copied', 'verified')",
                (item.logical_folder,),
            ).fetchall()
            item_bytes = sum(row["size_bytes"] for row in rows)
            conflicts = 0
            if dest.resolved and dest.path:
                for row in rows:
                    # Validate both the package path and destination path now.
                    safe_join(package_dir, row["package_rel_path"])
                    candidate = _dest_path_for(dest.path, row["package_rel_path"], item.package_path)
                    if os.path.exists(candidate):
                        conflicts += 1

            preview.items.append(
                RestoreItemPreview(
                    item.logical_folder,
                    item.restore_target,
                    dest.path,
                    dest.resolved,
                    dest.warning,
                    len(rows),
                    item_bytes,
                    conflicts,
                )
            )
            if not dest.resolved:
                preview.unresolved_items.append(item.logical_folder)
            else:
                preview.required_bytes += item_bytes
                if dest.path not in preview.free_bytes_by_root:
                    try:
                        preview.free_bytes_by_root[dest.path] = shutil.disk_usage(
                            _existing_parent(dest.path)
                        ).free
                    except OSError:
                        preview.free_bytes_by_root[dest.path] = None
    finally:
        conn.close()

    return preview


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
    number = 2
    while True:
        candidate = f"{base} - migrated {date_str} ({number}){ext}"
        if not os.path.exists(candidate):
            return candidate
        number += 1


def _paths_have_same_content(
    package_path: str,
    destination_path: str,
    expected_size: int,
    stored_hash: Optional[str],
    hash_kind: Optional[str],
) -> bool:
    if not os.path.isfile(package_path) or not os.path.isfile(destination_path):
        return False
    if os.path.getsize(package_path) != expected_size or os.path.getsize(destination_path) != expected_size:
        return False
    kind = _infer_hash_kind(stored_hash, hash_kind)
    if stored_hash and kind != HASH_NONE:
        return _compute_hash_for_kind(destination_path, expected_size, kind) == stored_hash
    # Recovery-only cost: when capture had no digest, compare both files fully
    # before declaring an interrupted restore committed.
    return _hash_file_full(package_path) == _hash_file_full(destination_path)


def _reconcile_interrupted_restores(
    conn: sqlite3.Connection,
    package_dir: str,
    manifest: MigrationManifest,
    destinations: dict[str, ResolvedDestination],
) -> None:
    item_by_logical = {item.logical_folder: item for item in manifest.items}
    rows = conn.execute(
        """
        SELECT rs.file_id, rs.dest_path, f.item_logical_folder, f.package_rel_path,
               f.size_bytes, f.sha256, f.hash_kind
        FROM restore_state rs JOIN files f ON f.id=rs.file_id
        WHERE rs.status='restoring'
        """
    ).fetchall()
    for row in rows:
        item = item_by_logical.get(row["item_logical_folder"])
        dest = destinations.get(row["item_logical_folder"])
        if not item or not dest or not dest.resolved or not dest.path:
            conn.execute(
                "UPDATE restore_state SET status='pending', error=NULL, updated_utc=? WHERE file_id=?",
                (_now(), row["file_id"]),
            )
            continue

        package_path = safe_join(package_dir, row["package_rel_path"])
        stored_dest = row["dest_path"]
        if not stored_dest or not path_is_within(dest.path, stored_dest) or path_is_within(package_dir, stored_dest):
            conn.execute(
                "UPDATE restore_state SET status='pending', dest_path=NULL, error=NULL, updated_utc=? "
                "WHERE file_id=?",
                (_now(), row["file_id"]),
            )
            continue

        staged = stored_dest + RESTORE_STAGING_SUFFIX
        if _paths_have_same_content(
            package_path,
            stored_dest,
            row["size_bytes"],
            row["sha256"],
            row["hash_kind"],
        ):
            if os.path.exists(staged):
                try:
                    os.remove(staged)
                except OSError:
                    pass
            conn.execute(
                "UPDATE restore_state SET status='restored', error=NULL, updated_utc=? WHERE file_id=?",
                (_now(), row["file_id"]),
            )
        else:
            if os.path.exists(staged):
                try:
                    os.remove(staged)
                except OSError:
                    pass
            conn.execute(
                "UPDATE restore_state SET status='pending', error=NULL, updated_utc=? WHERE file_id=?",
                (_now(), row["file_id"]),
            )
    conn.commit()


def run_restore(
    package_dir: str,
    manifest: MigrationManifest,
    conflict_policy: str = "replace_if_newer",
    custom_overrides: Optional[dict[str, str]] = None,
    hash_check: bool = True,
    reseed: bool = True,
) -> RestoreSummary:
    if conflict_policy not in VALID_CONFLICT_POLICIES:
        raise ValueError(f"invalid conflict policy: {conflict_policy!r}")

    summary = RestoreSummary()
    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_restore_state(db_path)
    destinations = resolve_destination_roots(manifest, custom_overrides)
    _block_package_destination_overlap(package_dir, destinations)
    item_by_logical = {item.logical_folder: item for item in manifest.items}
    summary.blocked_items = [name for name, dest in destinations.items() if not dest.resolved]

    errors_path = os.path.join(package_dir, "metadata", "restore_errors.jsonl")
    os.makedirs(os.path.dirname(errors_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if reseed:
            summary.seeded = _seed_restore_state(conn)
            conn.commit()
        _reconcile_interrupted_restores(conn, package_dir, manifest, destinations)

        rows = conn.execute(
            """
            SELECT f.id AS file_id, f.item_logical_folder, f.package_rel_path,
                   f.size_bytes, f.modified_utc, f.sha256, f.hash_kind,
                   rs.error AS restore_error
            FROM files f JOIN restore_state rs ON rs.file_id=f.id
            WHERE rs.status IN ('pending', 'failed') ORDER BY f.id
            """
        ).fetchall()

        with open(errors_path, "a", encoding="utf-8") as error_log:
            for row in rows:
                logical = row["item_logical_folder"]
                item = item_by_logical.get(logical)
                dest = destinations.get(logical)
                if item is None or dest is None or not dest.resolved or not dest.path:
                    continue

                try:
                    package_file_path = safe_join(package_dir, row["package_rel_path"])
                    dest_path = _dest_path_for(dest.path, row["package_rel_path"], item.package_path)
                    if path_is_within(package_dir, dest_path):
                        raise ValueError("restore destination is inside the migration package")
                except Exception as exc:
                    conn.execute(
                        "UPDATE restore_state SET status='failed', error=?, updated_utc=? WHERE file_id=?",
                        (str(exc), _now(), row["file_id"]),
                    )
                    conn.commit()
                    summary.failed += 1
                    summary.errors.append({"package_rel_path": row["package_rel_path"], "error": str(exc)})
                    continue

                conflict = None
                final_dest_path = dest_path
                verification_repair = str(row["restore_error"] or "").startswith("verification:")
                if os.path.exists(dest_path) and verification_repair:
                    # Post-restore verification proved this destination is
                    # damaged. Repair it regardless of the normal conflict
                    # policy; otherwise replace_if_newer could preserve the
                    # corrupt file merely because its timestamp is newer.
                    conflict = "verification_repair"
                elif os.path.exists(dest_path):
                    if conflict_policy == "skip":
                        conn.execute(
                            "UPDATE restore_state SET status='skipped', dest_path=?, conflict='existing', "
                            "error=NULL, updated_utc=? WHERE file_id=?",
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
                                source_mtime = datetime.fromisoformat(row["modified_utc"]).timestamp()
                            except ValueError:
                                source_mtime = None
                        if source_mtime is None or source_mtime <= dest_mtime:
                            conn.execute(
                                "UPDATE restore_state SET status='skipped', dest_path=?, "
                                "conflict='existing_newer_or_equal', error=NULL, updated_utc=? WHERE file_id=?",
                                (dest_path, _now(), row["file_id"]),
                            )
                            conn.commit()
                            summary.skipped_policy += 1
                            continue
                        conflict = "existing_older_replaced"
                    elif conflict_policy == "keep_both":
                        final_dest_path = _keep_both_path(dest_path)
                        conflict = "existing_kept_both"
                    else:
                        conflict = "existing_replaced"

                staged_path = final_dest_path + RESTORE_STAGING_SUFFIX
                conn.execute(
                    "UPDATE restore_state SET status='restoring', dest_path=?, conflict=?, error=NULL, "
                    "updated_utc=? WHERE file_id=?",
                    (final_dest_path, conflict, _now(), row["file_id"]),
                )
                conn.commit()

                try:
                    if not os.path.isfile(package_file_path):
                        raise FileNotFoundError(f"package file missing: {package_file_path}")
                    os.makedirs(os.path.dirname(final_dest_path), exist_ok=True)
                    if os.path.exists(staged_path):
                        os.remove(staged_path)
                    shutil.copy2(package_file_path, staged_path)
                    staged_size = os.path.getsize(staged_path)
                    if staged_size != row["size_bytes"]:
                        raise IOError(
                            f"size mismatch after restore: expected={row['size_bytes']} actual={staged_size}"
                        )

                    if hash_check:
                        kind = _infer_hash_kind(row["sha256"], row["hash_kind"])
                        if row["sha256"] and kind != HASH_NONE:
                            if _compute_hash_for_kind(staged_path, staged_size, kind) != row["sha256"]:
                                raise IOError("hash mismatch after restore copy")
                        elif _hash_file_full(package_file_path) != _hash_file_full(staged_path):
                            raise IOError("package/staged content mismatch after restore copy")

                    os.replace(staged_path, final_dest_path)
                    conn.execute(
                        "UPDATE restore_state SET status='restored', dest_path=?, error=NULL, updated_utc=? "
                        "WHERE file_id=?",
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
                        "UPDATE restore_state SET status='failed', error=?, updated_utc=? WHERE file_id=?",
                        (str(exc), _now(), row["file_id"]),
                    )
                    conn.commit()
                    error_log.write(
                        json.dumps(
                            {
                                "package_rel_path": row["package_rel_path"],
                                "dest_path": final_dest_path,
                                "error": str(exc),
                                "at_utc": _now(),
                            }
                        )
                        + "\n"
                    )
                    error_log.flush()
                    summary.failed += 1
                    summary.errors.append({"package_rel_path": row["package_rel_path"], "error": str(exc)})

        done = conn.execute(
            "SELECT COUNT(*) FROM restore_state WHERE status='restored'"
        ).fetchone()[0]
        summary.already_done = max(0, done - summary.restored)
    finally:
        conn.close()
    return summary


def restore_status(package_dir: str) -> dict:
    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_restore_state(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM restore_state GROUP BY status"
        ).fetchall()
        return {status: count for status, count in rows}
    finally:
        conn.close()


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
    if level not in ("fast", "balanced", "full"):
        raise ValueError("level must be fast, balanced, or full")
    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_restore_state(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    summary = RestoreVerifySummary()
    try:
        rows = conn.execute(
            """
            SELECT rs.file_id, rs.dest_path, f.size_bytes, f.sha256, f.hash_kind
            FROM restore_state rs JOIN files f ON f.id=rs.file_id
            WHERE rs.status='restored' ORDER BY rs.file_id
            """
        ).fetchall()
        for row in rows:
            summary.checked += 1
            dest_path = row["dest_path"]
            problem = None
            if not dest_path or not os.path.isfile(dest_path):
                summary.missing += 1
                problem = "missing at destination"
            else:
                actual_size = os.path.getsize(dest_path)
                if actual_size != row["size_bytes"]:
                    summary.failed += 1
                    problem = f"size mismatch: expected {row['size_bytes']}, found {actual_size}"
                elif level != "fast" and row["sha256"]:
                    kind = _infer_hash_kind(row["sha256"], row["hash_kind"])
                    if kind != HASH_NONE and _compute_hash_for_kind(dest_path, actual_size, kind) != row["sha256"]:
                        summary.failed += 1
                        problem = "hash mismatch"

            if problem:
                summary.problems.append({"path": dest_path, "problem": problem})
                # Make a subsequent restore capable of repairing the damaged
                # or missing destination automatically.
                conn.execute(
                    "UPDATE restore_state SET status='failed', error=?, updated_utc=? WHERE file_id=?",
                    (f"verification: {problem}", _now(), row["file_id"]),
                )
            else:
                summary.verified += 1
        conn.commit()
    finally:
        conn.close()
    return summary
