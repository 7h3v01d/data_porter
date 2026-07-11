"""
Core data models for Data Porter's scan engine.

These are intentionally simple, JSON-serialisable dataclasses so that scan
results can be written straight to disk (for the manifest / report layer)
without a separate translation step.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FolderOrigin(str, Enum):
    """Where a scanned location came from, for later UI grouping."""

    KNOWN_FOLDER = "known_folder"
    CUSTOM_SELECTION = "custom_selection"
    DISCOVERED = "discovered"  # e.g. "you may have forgotten this" candidates


class SkipReason(str, Enum):
    PERMISSION_DENIED = "permission_denied"
    UNREADABLE = "unreadable"
    REPARSE_POINT = "reparse_point"
    TOO_LONG_PATH = "too_long_path"
    CLOUD_PLACEHOLDER_ONLY = "cloud_placeholder_only"
    OTHER = "other"


@dataclass
class FileEntry:
    """A single file discovered during a scan."""

    path: str  # absolute path, as string (kept OS-native)
    size_bytes: int
    modified_utc: Optional[str] = None
    is_reparse_point: bool = False
    is_cloud_placeholder: bool = False  # OneDrive/cloud "smart files" style stub
    is_hidden_or_system: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SkippedEntry:
    """A path that was seen but not included in the scan totals."""

    path: str
    reason: SkipReason
    detail: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["reason"] = self.reason.value
        return d


@dataclass
class FolderScanResult:
    """
    Result of scanning a single logical location (e.g. "Documents", or a
    user-chosen custom folder).
    """

    logical_name: str  # e.g. "Documents", "D:\\Family Photos"
    source_path: str
    origin: FolderOrigin
    exists: bool = True

    file_count: int = 0
    total_bytes: int = 0

    largest_files: list[FileEntry] = field(default_factory=list)
    newest_files: list[FileEntry] = field(default_factory=list)

    skipped: list[SkippedEntry] = field(default_factory=list)

    reparse_points_skipped: int = 0
    unreadable_count: int = 0
    cloud_placeholder_count: int = 0

    last_modified_utc: Optional[str] = None
    error: Optional[str] = None  # set if the folder couldn't be scanned at all

    def to_dict(self) -> dict:
        d = asdict(self)
        d["origin"] = self.origin.value
        d["largest_files"] = [f.to_dict() for f in self.largest_files]
        d["newest_files"] = [f.to_dict() for f in self.newest_files]
        d["skipped"] = [s.to_dict() for s in self.skipped]
        return d


@dataclass
class DiscoveredCandidate:
    """
    A location the scanner noticed but which is not part of the standard
    Known Folder set -- the "Show me what I may have forgotten" list.
    """

    path: str
    size_bytes: int
    file_count: int
    last_modified_utc: Optional[str]
    reason: str  # human-readable: "large folder on secondary drive", etc.

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceEnvironment:
    computer_name: str
    os_name: str  # "Windows 10" / "Windows 11" / "Linux" (dev/test) / etc.
    os_version_raw: str
    user_name: str
    scan_started_utc: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanReport:
    """Top-level container for a full source scan."""

    schema_version: str
    environment: SourceEnvironment
    folders: list[FolderScanResult] = field(default_factory=list)
    discovered_candidates: list[DiscoveredCandidate] = field(default_factory=list)

    scan_finished_utc: Optional[str] = None

    @property
    def total_files(self) -> int:
        return sum(f.file_count for f in self.folders)

    @property
    def total_bytes(self) -> int:
        return sum(f.total_bytes for f in self.folders)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "environment": self.environment.to_dict(),
            "folders": [f.to_dict() for f in self.folders],
            "discovered_candidates": [c.to_dict() for c in self.discovered_candidates],
            "scan_finished_utc": self.scan_finished_utc,
            "totals": {
                "total_files": self.total_files,
                "total_bytes": self.total_bytes,
            },
        }
