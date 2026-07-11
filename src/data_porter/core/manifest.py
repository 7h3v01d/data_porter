"""
The migration manifest: describes a package's contents and, crucially, how
each item should be restored on the destination machine.

Key design rule from the spec: restore targets are *logical*
(``KNOWN_FOLDER_DOCUMENTS``), never a baked-in path
(``C:\\Users\\Dad\\Documents``). The restore step resolves the logical
target against Known Folders on the destination machine at restore time,
which is what makes Win10->Win11, different account names, and redirected
folders all work without special-casing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

from .models import FolderOrigin, SourceEnvironment, utc_now_iso

SCHEMA_VERSION = "1.0"

# Logical names that map directly onto a KNOWN_FOLDER_* restore target.
# Must match the logical_name values produced by known_folders.py.
_KNOWN_FOLDER_RESTORE_TARGETS = {
    "Desktop": "KNOWN_FOLDER_DESKTOP",
    "Documents": "KNOWN_FOLDER_DOCUMENTS",
    "Downloads": "KNOWN_FOLDER_DOWNLOADS",
    "Pictures": "KNOWN_FOLDER_PICTURES",
    "Music": "KNOWN_FOLDER_MUSIC",
    "Videos": "KNOWN_FOLDER_VIDEOS",
    "SavedGames": "KNOWN_FOLDER_SAVEDGAMES",
    "Favorites": "KNOWN_FOLDER_FAVORITES",
    "Contacts": "KNOWN_FOLDER_CONTACTS",
}

CUSTOM_RESTORE_TARGET = "CUSTOM"


class RestoreTarget(str, Enum):
    """Convenience wrapper; manifests store the plain string value so the
    JSON stays simple, but code can compare against this enum."""

    DESKTOP = "KNOWN_FOLDER_DESKTOP"
    DOCUMENTS = "KNOWN_FOLDER_DOCUMENTS"
    DOWNLOADS = "KNOWN_FOLDER_DOWNLOADS"
    PICTURES = "KNOWN_FOLDER_PICTURES"
    MUSIC = "KNOWN_FOLDER_MUSIC"
    VIDEOS = "KNOWN_FOLDER_VIDEOS"
    SAVEDGAMES = "KNOWN_FOLDER_SAVEDGAMES"
    FAVORITES = "KNOWN_FOLDER_FAVORITES"
    CONTACTS = "KNOWN_FOLDER_CONTACTS"
    CUSTOM = "CUSTOM"


def restore_target_for(logical_name: str, origin: FolderOrigin) -> str:
    if origin == FolderOrigin.KNOWN_FOLDER and logical_name in _KNOWN_FOLDER_RESTORE_TARGETS:
        return _KNOWN_FOLDER_RESTORE_TARGETS[logical_name]
    return CUSTOM_RESTORE_TARGET


_SANITIZE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_folder_name(name: str) -> str:
    """Turn an arbitrary logical name / path into a safe single path
    segment for use under data/ in the package."""
    # Strip drive letters and separators, keep something recognisable.
    base = name.replace("\\", "/").rstrip("/").split("/")[-1] or "root"
    base = _SANITIZE_RE.sub("_", base).strip()
    return base or "item"


def make_unique_names(names: list[str]) -> list[str]:
    """Given candidate folder names (possibly with duplicates), return a
    parallel list of disambiguated names, e.g. ["Photos", "Photos_2"]."""
    seen: dict[str, int] = {}
    result = []
    for name in names:
        if name not in seen:
            seen[name] = 1
            result.append(name)
        else:
            seen[name] += 1
            result.append(f"{name}_{seen[name]}")
    return result


class FileStatus(str, Enum):
    PENDING = "pending"
    COPYING = "copying"
    COPIED = "copied"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class MigrationItem:
    """One logical location within a migration (a Known Folder, or a
    user-chosen custom folder)."""

    logical_folder: str
    source_path: str
    package_path: str  # relative path within the package, e.g. "data/Documents"
    origin: FolderOrigin
    restore_target: str  # KNOWN_FOLDER_* or CUSTOM
    file_count: int = 0
    total_bytes: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["origin"] = self.origin.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "MigrationItem":
        return MigrationItem(
            logical_folder=d["logical_folder"],
            source_path=d["source_path"],
            package_path=d["package_path"],
            origin=FolderOrigin(d["origin"]),
            restore_target=d["restore_target"],
            file_count=d.get("file_count", 0),
            total_bytes=d.get("total_bytes", 0),
        )


@dataclass
class MigrationManifest:
    migration_id: str
    source: SourceEnvironment
    items: list[MigrationItem] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    created_utc: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "migration_id": self.migration_id,
            "created_utc": self.created_utc,
            "source": self.source.to_dict(),
            "items": [i.to_dict() for i in self.items],
        }

    @staticmethod
    def from_dict(d: dict) -> "MigrationManifest":
        src = d["source"]
        source = SourceEnvironment(
            computer_name=src["computer_name"],
            os_name=src["os_name"],
            os_version_raw=src.get("os_version_raw", ""),
            user_name=src["user_name"],
            scan_started_utc=src.get("scan_started_utc", d.get("created_utc", "")),
        )
        return MigrationManifest(
            migration_id=d["migration_id"],
            source=source,
            items=[MigrationItem.from_dict(i) for i in d["items"]],
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            created_utc=d.get("created_utc", utc_now_iso()),
        )
