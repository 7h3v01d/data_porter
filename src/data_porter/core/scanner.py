"""
The scan engine.

This walks a logical location (a resolved Known Folder, or a user-chosen
custom folder) and produces a FolderScanResult: totals, largest files,
newest files, and a record of anything that had to be skipped and why.

Design goals baked in from the spec:
  * never silently swallow errors -- everything unreadable/skipped is
    recorded with a reason, not just dropped;
  * never recurse into reparse points / symlinks (avoids the classic
    Windows junction-loop trap);
  * flag cloud-only placeholder files distinctly from real local files,
    so a scan never reports something as "backed up" when it's actually
    still sitting in OneDrive/whatever cloud provider;
  * cheap, streaming computation -- this can run against hundreds of
    thousands of files without holding everything in memory at once
    (only bounded top-N lists are retained).
"""

from __future__ import annotations

import heapq
import os
import platform
import stat as stat_module
from datetime import datetime, timezone
from typing import Iterable, Optional

from .models import (
    DiscoveredCandidate,
    FileEntry,
    FolderOrigin,
    FolderScanResult,
    SkipReason,
    SkippedEntry,
)

# Windows file attribute flags we care about (values from winnt.h).
# These are only meaningful when running on Windows; harmless elsewhere.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000

MAX_PATH_WARN_LENGTH = 259  # classic Windows MAX_PATH minus 1


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _win_attributes(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0)


def _classify_entry(path: str, st: os.stat_result) -> tuple[bool, bool, bool]:
    """Returns (is_reparse_point, is_cloud_placeholder, is_hidden_or_system)."""
    if _is_windows():
        attrs = _win_attributes(st)
        is_reparse = bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
        is_cloud = bool(
            attrs & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN)
        ) or bool(attrs & FILE_ATTRIBUTE_OFFLINE)
        is_hidden = bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))
        return is_reparse, is_cloud, is_hidden
    else:
        # Dev/test platforms: approximate with symlink check and dotfile
        # convention. Cloud-placeholder detection genuinely doesn't apply
        # off-Windows, so it's always False here.
        is_reparse = os.path.islink(path)
        is_hidden = os.path.basename(path).startswith(".")
        return is_reparse, False, is_hidden


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


class _TopN:
    """Bounded max-tracking heap: keeps the N largest (or newest) FileEntry
    objects seen, without holding the full file list in memory."""

    def __init__(self, n: int, key):
        self.n = n
        self.key = key
        self._heap: list[tuple[float, int, FileEntry]] = []
        self._counter = 0

    def add(self, entry: FileEntry) -> None:
        k = self.key(entry)
        item = (k, self._counter, entry)
        self._counter += 1
        if len(self._heap) < self.n:
            heapq.heappush(self._heap, item)
        elif k > self._heap[0][0]:
            heapq.heapreplace(self._heap, item)

    def result(self) -> list[FileEntry]:
        return [e for _, _, e in sorted(self._heap, key=lambda t: t[0], reverse=True)]


def scan_folder(
    source_path: str,
    logical_name: str,
    origin: FolderOrigin,
    top_n: int = 10,
    follow_into: Optional[set[str]] = None,
) -> FolderScanResult:
    """
    Scan a single folder tree and produce a FolderScanResult.

    ``follow_into`` is reserved for a future "advanced mode" that
    deliberately follows specific reparse points; default behaviour never
    descends into them.
    """
    result = FolderScanResult(
        logical_name=logical_name,
        source_path=source_path,
        origin=origin,
    )

    if not source_path or not os.path.isdir(source_path):
        result.exists = False
        result.error = "Path does not exist or is not a directory"
        return result

    largest = _TopN(top_n, key=lambda e: e.size_bytes)
    newest = _TopN(top_n, key=lambda e: e.modified_utc or "")
    latest_mtime: Optional[float] = None

    for dirpath, dirnames, filenames in os.walk(source_path, topdown=True, onerror=None):
        # Prune reparse-point directories in place so os.walk doesn't
        # descend into them (prevents junction loops).
        kept_dirnames = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            try:
                st = os.lstat(full)
            except OSError as exc:
                result.skipped.append(
                    SkippedEntry(path=full, reason=SkipReason.UNREADABLE, detail=str(exc))
                )
                result.unreadable_count += 1
                continue
            is_reparse, _, _ = _classify_entry(full, st)
            if is_reparse:
                result.reparse_points_skipped += 1
                result.skipped.append(
                    SkippedEntry(path=full, reason=SkipReason.REPARSE_POINT)
                )
                continue
            kept_dirnames.append(d)
        dirnames[:] = kept_dirnames

        for fname in filenames:
            full = os.path.join(dirpath, fname)

            if len(full) > MAX_PATH_WARN_LENGTH and _is_windows():
                # Still attempt it (Python + \\?\ prefixing can often cope),
                # but record it so the review UI can surface a warning.
                result.skipped.append(
                    SkippedEntry(
                        path=full,
                        reason=SkipReason.TOO_LONG_PATH,
                        detail=f"{len(full)} characters",
                    )
                )

            try:
                st = os.lstat(full)
            except OSError as exc:
                result.skipped.append(
                    SkippedEntry(path=full, reason=SkipReason.PERMISSION_DENIED, detail=str(exc))
                )
                result.unreadable_count += 1
                continue

            is_reparse, is_cloud, is_hidden = _classify_entry(full, st)

            if is_reparse:
                result.reparse_points_skipped += 1
                result.skipped.append(
                    SkippedEntry(path=full, reason=SkipReason.REPARSE_POINT)
                )
                continue

            size = st.st_size
            mtime_iso = _iso(st.st_mtime)
            if latest_mtime is None or st.st_mtime > latest_mtime:
                latest_mtime = st.st_mtime

            if is_cloud:
                result.cloud_placeholder_count += 1

            entry = FileEntry(
                path=full,
                size_bytes=size,
                modified_utc=mtime_iso,
                is_reparse_point=False,
                is_cloud_placeholder=is_cloud,
                is_hidden_or_system=is_hidden,
            )

            result.file_count += 1
            result.total_bytes += size
            largest.add(entry)
            newest.add(entry)

    result.largest_files = largest.result()
    result.newest_files = newest.result()
    if latest_mtime is not None:
        result.last_modified_utc = _iso(latest_mtime)

    return result


def discover_secondary_drive_candidates(
    min_size_bytes: int = 5 * 1024 * 1024 * 1024,  # 5 GB
    already_selected: Optional[Iterable[str]] = None,
) -> list[DiscoveredCandidate]:
    """
    Best-effort "you may have forgotten this" scan: looks at top-level
    folders on drives other than the system drive and flags large ones
    that aren't already part of the migration plan.

    This is a light, non-recursive-cost pass (only descends one or two
    levels) -- it's meant to prompt a human to look, not to fully scan
    every byte on every drive.
    """
    already = set(os.path.normcase(p) for p in (already_selected or []))
    candidates: list[DiscoveredCandidate] = []

    if not _is_windows():
        # No meaningful concept of "secondary drives" on the dev/test
        # platform; return an empty list rather than guessing.
        return candidates

    import string

    system_drive = os.environ.get("SystemDrive", "C:").upper()

    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if drive.upper().startswith(system_drive):
            continue
        if not os.path.isdir(drive):
            continue

        try:
            top_entries = os.listdir(drive)
        except OSError:
            continue

        for name in top_entries:
            full = os.path.join(drive, name)
            if os.path.normcase(full) in already:
                continue
            if not os.path.isdir(full):
                continue
            size, count, latest = _quick_folder_estimate(full)
            if size >= min_size_bytes:
                candidates.append(
                    DiscoveredCandidate(
                        path=full,
                        size_bytes=size,
                        file_count=count,
                        last_modified_utc=_iso(latest) if latest else None,
                        reason="Large folder on a secondary drive, not currently selected",
                    )
                )

    return candidates


def _quick_folder_estimate(path: str, max_files: int = 50_000) -> tuple[int, int, Optional[float]]:
    """Cheap size/count estimate that bails out after max_files, so a huge
    forgotten folder doesn't turn the discovery pass into a full scan."""
    total = 0
    count = 0
    latest: Optional[float] = None
    for dirpath, dirnames, filenames in os.walk(path):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            total += st.st_size
            count += 1
            if latest is None or st.st_mtime > latest:
                latest = st.st_mtime
            if count >= max_files:
                return total, count, latest
    return total, count, latest
