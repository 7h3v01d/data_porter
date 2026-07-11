"""
Turns a ScanReport + user selections into a MigrationManifest, and lays
down the actual package directory on disk:

    Dad-PC-Migration/
    ├── migration.json
    ├── package_state.sqlite
    ├── checksums.jsonl
    ├── source_report.html
    ├── data/
    │   ├── Documents/
    │   └── ...
    ├── metadata/
    │   ├── known_folders.json
    │   ├── selections.json
    │   ├── skipped_files.jsonl
    │   └── errors.jsonl
    └── logs/

This module only builds the *shape* of the package and seeds its file
inventory; the actual byte-copying lives in capture.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from typing import Optional

from .manifest import (
    MigrationItem,
    MigrationManifest,
    make_unique_names,
    restore_target_for,
    sanitize_folder_name,
)
from .models import FolderScanResult, ScanReport
from .safety import safe_join, validate_package_location, validate_source_roots

PACKAGE_SCHEMA_VERSION = "1.1"


def build_migration_plan(
    scan_report: ScanReport,
    selected_logical_names: Optional[set[str]] = None,
) -> MigrationManifest:
    """
    Build a MigrationManifest from a completed ScanReport.

    ``selected_logical_names``: if given, only folders whose logical_name
    is in this set are included (this is the "review screen" selection
    from the spec). If None, every folder that actually exists is included.
    """
    candidate_folders: list[FolderScanResult] = [
        f
        for f in scan_report.folders
        if f.exists
        and (selected_logical_names is None or f.logical_name in selected_logical_names)
    ]

    # A source may only belong to one migration item. Parent/child or exact
    # duplicate selections are ambiguous and are blocked rather than silently
    # allowing the SQLite UNIQUE(source_path) constraint to choose a winner.
    validate_source_roots(f.source_path for f in candidate_folders)

    raw_names = [sanitize_folder_name(f.logical_name) for f in candidate_folders]
    unique_names = make_unique_names(raw_names)

    items: list[MigrationItem] = []
    for folder, safe_name in zip(candidate_folders, unique_names):
        items.append(
            MigrationItem(
                logical_folder=folder.logical_name,
                source_path=folder.source_path,
                package_path=f"data/{safe_name}",
                origin=folder.origin,
                restore_target=restore_target_for(folder.logical_name, folder.origin),
                file_count=folder.file_count,
                total_bytes=folder.total_bytes,
            )
        )

    return MigrationManifest(
        migration_id=str(uuid.uuid4()),
        source=scan_report.environment,
        items=items,
    )


_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_logical_folder TEXT NOT NULL,
    source_path TEXT NOT NULL UNIQUE,
    package_rel_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    modified_utc TEXT,
    source_mtime_ns INTEGER,
    is_cloud_placeholder INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    sha256 TEXT,
    hash_kind TEXT NOT NULL DEFAULT 'none',
    error TEXT,
    updated_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_item ON files(item_logical_folder);

CREATE TABLE IF NOT EXISTS package_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_package_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_DB_SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
        if "hash_kind" not in columns:
            conn.execute("ALTER TABLE files ADD COLUMN hash_kind TEXT NOT NULL DEFAULT 'none'")
            conn.execute(
                "UPDATE files SET hash_kind = CASE "
                "WHEN sha256 LIKE 'partial:%' THEN 'sha256_sampled_v1' "
                "WHEN sha256 IS NOT NULL THEN 'sha256_full' ELSE 'none' END"
            )
        if "source_mtime_ns" not in columns:
            conn.execute("ALTER TABLE files ADD COLUMN source_mtime_ns INTEGER")
        conn.commit()
    finally:
        conn.close()


def create_package(
    package_dir: str,
    manifest: MigrationManifest,
    scan_report: Optional[ScanReport] = None,
) -> str:
    """
    Lay down the package directory structure and write migration.json +
    metadata. Returns the path to migration.json.

    Safe to call again on an existing package_dir (e.g. after adding a
    folder to the plan) -- it won't clobber already-captured data/ content,
    it only ensures directories exist and rewrites the manifest + metadata.
    """
    validate_source_roots(item.source_path for item in manifest.items)
    validate_package_location(package_dir, (item.source_path for item in manifest.items))

    existing_manifest_path = os.path.join(package_dir, "migration.json")
    if os.path.isfile(existing_manifest_path):
        try:
            with open(existing_manifest_path, "r", encoding="utf-8") as existing_file:
                existing_data = json.load(existing_file)
            existing_id = existing_data.get("migration_id")
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"existing package manifest cannot be read safely: {existing_manifest_path}: {exc}"
            ) from exc
        if existing_id and existing_id != manifest.migration_id:
            from .safety import SafetyError
            raise SafetyError(
                "refusing to overwrite an existing migration package with a different plan. "
                "Choose a new empty package folder or resume the existing package."
            )

    os.makedirs(package_dir, exist_ok=True)
    for sub in ("data", "metadata", "logs"):
        os.makedirs(safe_join(package_dir, sub), exist_ok=True)
    for item in manifest.items:
        os.makedirs(safe_join(package_dir, item.package_path), exist_ok=True)

    manifest_path = os.path.join(package_dir, "migration.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, indent=2)

    selections_path = os.path.join(package_dir, "metadata", "selections.json")
    with open(selections_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "selected_logical_folders": [i.logical_folder for i in manifest.items],
                "item_count": len(manifest.items),
            },
            f,
            indent=2,
        )

    if scan_report is not None:
        known_folders_path = os.path.join(package_dir, "metadata", "known_folders.json")
        with open(known_folders_path, "w", encoding="utf-8") as f:
            json.dump(scan_report.to_dict(), f, indent=2)

    db_path = os.path.join(package_dir, "package_state.sqlite")
    init_package_db(db_path)

    return manifest_path


def load_manifest(package_dir: str) -> MigrationManifest:
    manifest_path = os.path.join(package_dir, "migration.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        return MigrationManifest.from_dict(json.load(f))
