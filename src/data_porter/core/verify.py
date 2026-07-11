"""
Post-capture verification: re-checks what's actually sitting in the
package against what package_state.sqlite recorded, at one of three
effort levels (mirrors the spec's Fast / Balanced / Full options).

This is deliberately a separate pass from capture -- it's also what you'd
run after moving the package to another drive, or before trusting it for
restore.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .capture import _compute_hash, _now


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
    assert level in ("fast", "balanced", "full")

    db_path = os.path.join(package_dir, "package_state.sqlite")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    summary = VerifySummary()

    try:
        rows = conn.execute(
            "SELECT * FROM files WHERE status IN ('copied', 'verified', 'failed') ORDER BY id"
        ).fetchall()

        for row in rows:
            summary.checked += 1
            dest_path = os.path.join(package_dir, row["package_rel_path"])

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
                summary.problems.append(
                    {
                        "path": row["package_rel_path"],
                        "problem": f"size mismatch: expected {row['size_bytes']}, found {actual_size}",
                    }
                )
                conn.execute(
                    "UPDATE files SET status='failed', error=?, updated_utc=? WHERE id=?",
                    (f"size mismatch at verification ({actual_size} vs {row['size_bytes']})", _now(), row["id"]),
                )
                continue

            if level == "fast":
                summary.verified += 1
                conn.execute(
                    "UPDATE files SET status='verified', updated_utc=? WHERE id=?",
                    (_now(), row["id"]),
                )
                continue

            recomputed = _compute_hash(dest_path, actual_size, "full" if level == "full" else "balanced")
            stored = row["sha256"]

            if stored is None:
                # File was captured under "fast" hashing -- nothing to
                # compare against; size match is the best we can say.
                summary.verified += 1
                conn.execute(
                    "UPDATE files SET status='verified', sha256=?, updated_utc=? WHERE id=?",
                    (recomputed, _now(), row["id"]),
                )
                continue

            if recomputed == stored:
                summary.verified += 1
                conn.execute(
                    "UPDATE files SET status='verified', updated_utc=? WHERE id=?",
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
