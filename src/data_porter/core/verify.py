"""Post-capture verification of package contents against recorded state."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from dataclasses import dataclass, field

from .capture import (
    HASH_NONE,
    _compute_hash_for_kind,
    _hash_kind_for_level,
    _infer_hash_kind,
    _now,
)
from .package import init_package_db
from .safety import safe_join


@dataclass
class VerifySummary:
    checked: int = 0
    verified: int = 0
    failed: int = 0
    missing: int = 0
    problems: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def verify_package(package_dir: str, level: str = "balanced") -> VerifySummary:
    if level not in ("fast", "balanced", "full"):
        raise ValueError("level must be fast, balanced, or full")

    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_package_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    summary = VerifySummary()

    try:
        rows = conn.execute("SELECT * FROM files ORDER BY id").fetchall()

        for row in rows:
            summary.checked += 1
            if row["status"] not in ("copied", "verified"):
                summary.failed += 1
                summary.problems.append(
                    {
                        "path": row["package_rel_path"],
                        "problem": f"capture status is {row['status']!r}; file is not complete",
                    }
                )
                continue
            try:
                dest_path = safe_join(package_dir, row["package_rel_path"])
            except ValueError as exc:
                summary.failed += 1
                summary.problems.append(
                    {"path": row["package_rel_path"], "problem": str(exc)}
                )
                conn.execute(
                    "UPDATE files SET status='failed', error=?, updated_utc=? WHERE id=?",
                    (str(exc), _now(), row["id"]),
                )
                continue

            if not os.path.isfile(dest_path):
                summary.missing += 1
                summary.problems.append(
                    {"path": row["package_rel_path"], "problem": "missing from package"}
                )
                conn.execute(
                    "UPDATE files SET status='failed', error=?, updated_utc=? WHERE id=?",
                    ("missing from package at verification time", _now(), row["id"]),
                )
                continue

            actual_size = os.path.getsize(dest_path)
            if actual_size != row["size_bytes"]:
                summary.failed += 1
                problem = (
                    f"size mismatch: expected {row['size_bytes']}, found {actual_size}"
                )
                summary.problems.append(
                    {"path": row["package_rel_path"], "problem": problem}
                )
                conn.execute(
                    "UPDATE files SET status='failed', error=?, updated_utc=? WHERE id=?",
                    (problem, _now(), row["id"]),
                )
                continue

            if level == "fast":
                summary.verified += 1
                conn.execute(
                    "UPDATE files SET status='verified', error=NULL, updated_utc=? WHERE id=?",
                    (_now(), row["id"]),
                )
                continue

            stored = row["sha256"]
            recorded_kind = row["hash_kind"] if "hash_kind" in row.keys() else None
            hash_kind = _infer_hash_kind(stored, recorded_kind)

            if stored is None or hash_kind == HASH_NONE:
                # A fast capture has no source-time digest. Create a package
                # integrity baseline now using the requested effort level.
                hash_kind = _hash_kind_for_level(level, actual_size)
                recomputed = _compute_hash_for_kind(dest_path, actual_size, hash_kind)
                summary.verified += 1
                conn.execute(
                    """
                    UPDATE files
                    SET status='verified', sha256=?, hash_kind=?, error=NULL, updated_utc=?
                    WHERE id=?
                    """,
                    (recomputed, hash_kind, _now(), row["id"]),
                )
                continue

            # Always recompute the algorithm that was recorded at capture.
            # A sampled digest and a full digest are different evidence and
            # must never be compared directly.
            recomputed = _compute_hash_for_kind(dest_path, actual_size, hash_kind)
            if recomputed == stored:
                summary.verified += 1
                conn.execute(
                    "UPDATE files SET status='verified', error=NULL, updated_utc=? WHERE id=?",
                    (_now(), row["id"]),
                )
            else:
                summary.failed += 1
                summary.problems.append(
                    {"path": row["package_rel_path"], "problem": "hash mismatch"}
                )
                conn.execute(
                    "UPDATE files SET status='failed', error='hash mismatch at verification', "
                    "updated_utc=? WHERE id=?",
                    (_now(), row["id"]),
                )

        conn.commit()
    finally:
        conn.close()

    return summary
