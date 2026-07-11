"""
Capture: copies the files described by a migration manifest into the
package, safely.

Rules from the spec baked in here:
  * files are written under a ``.dataporter-partial`` staged name and only
    renamed to their real name after the copy (and any hashing) succeeds --
    an interrupted capture can never leave a half-written file looking valid;
  * every completed file is journaled to ``package_state.sqlite``
    immediately, so re-running capture on the same package_dir resumes
    (only pending/failed rows are (re)attempted) instead of starting over;
  * a failure on one file is recorded and capture moves on -- it does not
    abort the whole run;
  * hashing has three levels (fast / balanced / full) trading thoroughness
    for time, matching the spec's verification-effort options.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .manifest import MigrationManifest
from .models import SkipReason
from .scanner import walk_tree_entries

STAGING_SUFFIX = ".dataporter-partial"

# Above this size, "balanced" mode uses a cheap partial hash instead of a
# full SHA-256, so a handful of huge video files don't dominate capture time.
BALANCED_FULL_HASH_MAX_BYTES = 200 * 1024 * 1024  # 200 MB
PARTIAL_HASH_SAMPLE_BYTES = 1 * 1024 * 1024  # 1 MB from each end


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _relpath_for_item(source_root: str, file_path: str) -> str:
    rel = os.path.relpath(file_path, source_root)
    # Keep forward slashes in stored/relative form for portability, even
    # though the actual filesystem copy uses OS-native os.path.join.
    return rel.replace(os.sep, "/")


@dataclass
class CaptureSummary:
    seeded: int = 0
    copied: int = 0
    already_done: int = 0
    failed: int = 0
    skipped_at_source: int = 0
    total_bytes_copied: int = 0
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def seed_package_files(package_dir: str, manifest: MigrationManifest) -> int:
    """
    Walk each item's source tree and insert a row per file into
    package_state.sqlite (INSERT OR IGNORE, so re-seeding is safe and
    additive). Also appends anything the walk had to skip to
    metadata/skipped_files.jsonl. Returns the number of newly seeded rows.
    """
    db_path = os.path.join(package_dir, "package_state.sqlite")
    skipped_path = os.path.join(package_dir, "metadata", "skipped_files.jsonl")

    conn = sqlite3.connect(db_path)
    seeded = 0
    try:
        with open(skipped_path, "a", encoding="utf-8") as skip_log:
            for item in manifest.items:
                if not os.path.isdir(item.source_path):
                    continue
                for kind, entry in walk_tree_entries(item.source_path):
                    if kind == "skip":
                        record = entry.to_dict()
                        record["item_logical_folder"] = item.logical_folder
                        skip_log.write(json.dumps(record) + "\n")
                        continue

                    rel = _relpath_for_item(item.source_path, entry.path)
                    package_rel_path = f"{item.package_path}/{rel}"
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO files
                        (item_logical_folder, source_path, package_rel_path,
                         size_bytes, modified_utc, is_cloud_placeholder, status, updated_utc)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            item.logical_folder,
                            entry.path,
                            package_rel_path,
                            entry.size_bytes,
                            entry.modified_utc,
                            1 if entry.is_cloud_placeholder else 0,
                            _now(),
                        ),
                    )
                    seeded += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return seeded


def _hash_file_full(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_file_partial(path: str, size: int) -> str:
    """Cheap integrity signal for very large files: hash the first and
    last PARTIAL_HASH_SAMPLE_BYTES plus the size. Not a substitute for a
    full hash -- stored with a "partial:" prefix so it's never confused
    with one."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        head = f.read(PARTIAL_HASH_SAMPLE_BYTES)
        h.update(head)
        if size > PARTIAL_HASH_SAMPLE_BYTES:
            f.seek(max(size - PARTIAL_HASH_SAMPLE_BYTES, 0))
            tail = f.read(PARTIAL_HASH_SAMPLE_BYTES)
            h.update(tail)
    h.update(str(size).encode())
    return "partial:" + h.hexdigest()


def _compute_hash(path: str, size: int, hash_level: str) -> Optional[str]:
    if hash_level == "fast":
        return None
    if hash_level == "full":
        return _hash_file_full(path)
    # balanced
    if size <= BALANCED_FULL_HASH_MAX_BYTES:
        return _hash_file_full(path)
    return _hash_file_partial(path, size)


def run_capture(
    package_dir: str,
    manifest: MigrationManifest,
    hash_level: str = "balanced",
    reseed: bool = True,
) -> CaptureSummary:
    """
    Copy every pending/failed file from its source location into the
    package, staging each write and journaling progress so this can be
    safely re-run (pause/resume/retry) against the same package_dir.
    """
    assert hash_level in ("fast", "balanced", "full")

    summary = CaptureSummary()
    if reseed:
        summary.seeded = seed_package_files(package_dir, manifest)

    db_path = os.path.join(package_dir, "package_state.sqlite")
    checksums_path = os.path.join(package_dir, "checksums.jsonl")
    errors_path = os.path.join(package_dir, "metadata", "errors.jsonl")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM files WHERE status IN ('pending', 'failed', 'copying') ORDER BY id"
        ).fetchall()

        with open(checksums_path, "a", encoding="utf-8") as checksum_log, open(
            errors_path, "a", encoding="utf-8"
        ) as error_log:
            for row in rows:
                source_path = row["source_path"]
                package_rel_path = row["package_rel_path"]
                dest_path = os.path.join(package_dir, package_rel_path)
                staged_path = dest_path + STAGING_SUFFIX

                conn.execute(
                    "UPDATE files SET status='copying', updated_utc=? WHERE id=?",
                    (_now(), row["id"]),
                )
                conn.commit()

                try:
                    if not os.path.isfile(source_path):
                        raise FileNotFoundError(
                            f"source file no longer present: {source_path}"
                        )

                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    shutil.copy2(source_path, staged_path)

                    staged_size = os.path.getsize(staged_path)
                    source_size = os.path.getsize(source_path)
                    if staged_size != source_size:
                        raise IOError(
                            f"size mismatch after copy: source={source_size} staged={staged_size} "
                            "(source may have changed during capture)"
                        )

                    file_hash = _compute_hash(staged_path, staged_size, hash_level)

                    os.replace(staged_path, dest_path)

                    conn.execute(
                        "UPDATE files SET status='copied', sha256=?, size_bytes=?, error=NULL, "
                        "updated_utc=? WHERE id=?",
                        (file_hash, staged_size, _now(), row["id"]),
                    )
                    conn.commit()

                    checksum_log.write(
                        json.dumps(
                            {
                                "source_path": source_path,
                                "package_rel_path": package_rel_path,
                                "size_bytes": staged_size,
                                "sha256": file_hash,
                                "captured_utc": _now(),
                            }
                        )
                        + "\n"
                    )

                    summary.copied += 1
                    summary.total_bytes_copied += staged_size

                except Exception as exc:
                    if os.path.exists(staged_path):
                        try:
                            os.remove(staged_path)
                        except OSError:
                            pass
                    conn.execute(
                        "UPDATE files SET status='failed', error=?, updated_utc=? WHERE id=?",
                        (str(exc), _now(), row["id"]),
                    )
                    conn.commit()
                    error_log.write(
                        json.dumps(
                            {"source_path": source_path, "error": str(exc), "at_utc": _now()}
                        )
                        + "\n"
                    )
                    summary.failed += 1
                    summary.errors.append({"source_path": source_path, "error": str(exc)})

        already_done = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status = 'copied'"
        ).fetchone()[0]
        summary.already_done = already_done - summary.copied
    finally:
        conn.close()

    return summary


def capture_status(package_dir: str) -> dict:
    """Quick status summary of a package's file table, for progress UIs."""
    db_path = os.path.join(package_dir, "package_state.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n, SUM(size_bytes) as bytes FROM files GROUP BY status"
        ).fetchall()
        return {status: {"count": n, "bytes": b or 0} for status, n, b in rows}
    finally:
        conn.close()
