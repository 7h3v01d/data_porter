"""Resumable, staged capture of selected personal files into a package."""

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
from .package import init_package_db
from .safety import safe_join, validate_package_location, validate_source_roots
from .scanner import walk_tree_entries

STAGING_SUFFIX = ".dataporter-partial"
BALANCED_FULL_HASH_MAX_BYTES = 200 * 1024 * 1024
PARTIAL_HASH_SAMPLE_BYTES = 1 * 1024 * 1024

HASH_NONE = "none"
HASH_SHA256_FULL = "sha256_full"
HASH_SHA256_SAMPLED_V1 = "sha256_sampled_v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _relpath_for_item(source_root: str, file_path: str) -> str:
    rel = os.path.relpath(file_path, source_root)
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


def _hash_file_full(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_file_partial(path: str, size: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        h.update(handle.read(PARTIAL_HASH_SAMPLE_BYTES))
        if size > PARTIAL_HASH_SAMPLE_BYTES:
            handle.seek(max(size - PARTIAL_HASH_SAMPLE_BYTES, 0))
            h.update(handle.read(PARTIAL_HASH_SAMPLE_BYTES))
    h.update(str(size).encode("ascii"))
    return "partial:" + h.hexdigest()


def _hash_kind_for_level(hash_level: str, size: int) -> str:
    if hash_level == "fast":
        return HASH_NONE
    if hash_level == "full" or size <= BALANCED_FULL_HASH_MAX_BYTES:
        return HASH_SHA256_FULL
    return HASH_SHA256_SAMPLED_V1


def _infer_hash_kind(stored_hash: Optional[str], recorded_kind: Optional[str] = None) -> str:
    if recorded_kind in (HASH_NONE, HASH_SHA256_FULL, HASH_SHA256_SAMPLED_V1):
        if recorded_kind != HASH_NONE or stored_hash is None:
            return recorded_kind
    if stored_hash is None:
        return HASH_NONE
    if str(stored_hash).startswith("partial:"):
        return HASH_SHA256_SAMPLED_V1
    return HASH_SHA256_FULL


def _compute_hash_for_kind(path: str, size: int, hash_kind: str) -> Optional[str]:
    if hash_kind == HASH_NONE:
        return None
    if hash_kind == HASH_SHA256_FULL:
        return _hash_file_full(path)
    if hash_kind == HASH_SHA256_SAMPLED_V1:
        return _hash_file_partial(path, size)
    raise ValueError(f"unsupported hash kind: {hash_kind!r}")


def _compute_hash(path: str, size: int, hash_level: str) -> Optional[str]:
    """Backward-compatible helper used by restore/verify callers."""
    return _compute_hash_for_kind(path, size, _hash_kind_for_level(hash_level, size))


def seed_package_files(package_dir: str, manifest: MigrationManifest) -> int:
    """Seed new files and requeue files whose source metadata has changed."""
    validate_source_roots(item.source_path for item in manifest.items)
    validate_package_location(package_dir, (item.source_path for item in manifest.items))

    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_package_db(db_path)
    skipped_path = os.path.join(package_dir, "metadata", "skipped_files.jsonl")
    os.makedirs(os.path.dirname(skipped_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    seeded_or_requeued = 0
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
                    # Validate before anything from the manifest reaches join().
                    safe_join(package_dir, package_rel_path)
                    try:
                        mtime_ns = os.stat(entry.path, follow_symlinks=False).st_mtime_ns
                    except OSError:
                        mtime_ns = None

                    existing = conn.execute(
                        "SELECT * FROM files WHERE source_path = ?", (entry.path,)
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO files
                            (item_logical_folder, source_path, package_rel_path,
                             size_bytes, modified_utc, source_mtime_ns,
                             is_cloud_placeholder, status, hash_kind, updated_utc)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'none', ?)
                            """,
                            (
                                item.logical_folder,
                                entry.path,
                                package_rel_path,
                                entry.size_bytes,
                                entry.modified_utc,
                                mtime_ns,
                                1 if entry.is_cloud_placeholder else 0,
                                _now(),
                            ),
                        )
                        seeded_or_requeued += 1
                        continue

                    changed = any(
                        (
                            existing["item_logical_folder"] != item.logical_folder,
                            existing["package_rel_path"] != package_rel_path,
                            existing["size_bytes"] != entry.size_bytes,
                            existing["source_mtime_ns"] != mtime_ns,
                        )
                    )
                    if changed:
                        old_rel = existing["package_rel_path"]
                        conn.execute(
                            """
                            UPDATE files
                            SET item_logical_folder=?, package_rel_path=?, size_bytes=?,
                                modified_utc=?, source_mtime_ns=?, is_cloud_placeholder=?,
                                status='pending', sha256=NULL, hash_kind='none', error=NULL,
                                updated_utc=?
                            WHERE id=?
                            """,
                            (
                                item.logical_folder,
                                package_rel_path,
                                entry.size_bytes,
                                entry.modified_utc,
                                mtime_ns,
                                1 if entry.is_cloud_placeholder else 0,
                                _now(),
                                existing["id"],
                            ),
                        )
                        # If this package has already been restored somewhere,
                        # a newly recaptured source must be eligible for restore
                        # again rather than remaining falsely "restored".
                        has_restore_state = conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='restore_state'"
                        ).fetchone()
                        if has_restore_state:
                            conn.execute(
                                "UPDATE restore_state SET status='pending', dest_path=NULL, "
                                "conflict=NULL, error=NULL, updated_utc=? WHERE file_id=?",
                                (_now(), existing["id"]),
                            )
                        if old_rel != package_rel_path:
                            try:
                                old_path = safe_join(package_dir, old_rel)
                                if os.path.isfile(old_path):
                                    os.remove(old_path)
                            except (OSError, ValueError):
                                pass
                        seeded_or_requeued += 1
        conn.commit()
    finally:
        conn.close()
    return seeded_or_requeued


def run_capture(
    package_dir: str,
    manifest: MigrationManifest,
    hash_level: str = "balanced",
    reseed: bool = True,
) -> CaptureSummary:
    if hash_level not in ("fast", "balanced", "full"):
        raise ValueError("hash_level must be fast, balanced, or full")

    validate_source_roots(item.source_path for item in manifest.items)
    validate_package_location(package_dir, (item.source_path for item in manifest.items))

    summary = CaptureSummary()
    if reseed:
        summary.seeded = seed_package_files(package_dir, manifest)

    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_package_db(db_path)
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
                try:
                    dest_path = safe_join(package_dir, package_rel_path)
                except ValueError as exc:
                    conn.execute(
                        "UPDATE files SET status='failed', error=?, updated_utc=? WHERE id=?",
                        (str(exc), _now(), row["id"]),
                    )
                    conn.commit()
                    summary.failed += 1
                    summary.errors.append({"source_path": source_path, "error": str(exc)})
                    continue
                staged_path = dest_path + STAGING_SUFFIX

                conn.execute(
                    "UPDATE files SET status='copying', updated_utc=? WHERE id=?",
                    (_now(), row["id"]),
                )
                conn.commit()

                try:
                    before = os.stat(source_path, follow_symlinks=False)
                    if not os.path.isfile(source_path):
                        raise FileNotFoundError(f"source file no longer present: {source_path}")

                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    if os.path.exists(staged_path):
                        os.remove(staged_path)
                    shutil.copy2(source_path, staged_path)
                    after = os.stat(source_path, follow_symlinks=False)

                    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                        raise IOError("source file changed during capture; it will be retried")

                    staged_size = os.path.getsize(staged_path)
                    if staged_size != after.st_size:
                        raise IOError(
                            f"size mismatch after copy: source={after.st_size} staged={staged_size}"
                        )

                    hash_kind = _hash_kind_for_level(hash_level, staged_size)
                    file_hash = _compute_hash_for_kind(staged_path, staged_size, hash_kind)
                    os.replace(staged_path, dest_path)

                    conn.execute(
                        """
                        UPDATE files
                        SET status='copied', sha256=?, hash_kind=?, size_bytes=?,
                            source_mtime_ns=?, error=NULL, updated_utc=?
                        WHERE id=?
                        """,
                        (
                            file_hash,
                            hash_kind,
                            staged_size,
                            after.st_mtime_ns,
                            _now(),
                            row["id"],
                        ),
                    )
                    conn.commit()

                    checksum_log.write(
                        json.dumps(
                            {
                                "source_path": source_path,
                                "package_rel_path": package_rel_path,
                                "size_bytes": staged_size,
                                "sha256": file_hash,
                                "hash_kind": hash_kind,
                                "captured_utc": _now(),
                            }
                        )
                        + "\n"
                    )
                    checksum_log.flush()
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
                    error_log.flush()
                    summary.failed += 1
                    summary.errors.append({"source_path": source_path, "error": str(exc)})

        done = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status IN ('copied', 'verified')"
        ).fetchone()[0]
        summary.already_done = max(0, done - summary.copied)
    finally:
        conn.close()

    return summary


def capture_status(package_dir: str) -> dict:
    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_package_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n, SUM(size_bytes) as bytes FROM files GROUP BY status"
        ).fetchall()
        return {status: {"count": n, "bytes": b or 0} for status, n, b in rows}
    finally:
        conn.close()
