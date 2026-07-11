"""Filesystem safety helpers used by package creation, capture, and restore.

All paths stored in a migration package are treated as untrusted when they
are read back.  These helpers prevent a package from capturing itself and
prevent relative paths from escaping their authorised roots.
"""

from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import Iterable


class SafetyError(ValueError):
    """Raised when a migration path violates a hard safety invariant."""


def canonical_path(path: str) -> str:
    if not path:
        raise SafetyError("path is empty")
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def paths_overlap(path_a: str, path_b: str) -> bool:
    """Return True when either path is the same as, or beneath, the other."""
    a = canonical_path(path_a)
    b = canonical_path(path_b)
    try:
        common = os.path.commonpath([a, b])
    except ValueError:
        # Different Windows drives are not overlapping.
        return False
    return common == a or common == b


def path_is_within(root: str, candidate: str) -> bool:
    root_c = canonical_path(root)
    candidate_c = canonical_path(candidate)
    try:
        return os.path.commonpath([root_c, candidate_c]) == root_c
    except ValueError:
        return False


def safe_relative_parts(relative_path: str) -> tuple[str, ...]:
    """Validate a package-stored relative path and return safe components.

    Both slash styles are accepted as separators so a path crafted on one
    platform cannot become traversal syntax on another.
    """
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise SafetyError("relative path is empty")

    normalised = relative_path.replace("\\", "/")
    pure = PurePosixPath(normalised)
    if pure.is_absolute() or normalised.startswith("//"):
        raise SafetyError(f"absolute path is not allowed: {relative_path!r}")

    parts = pure.parts
    if not parts:
        raise SafetyError("relative path has no components")
    for part in parts:
        if part in ("", ".", ".."):
            raise SafetyError(f"unsafe path component {part!r} in {relative_path!r}")
        # Reject drive-qualified paths and NTFS alternate data stream syntax.
        if ":" in part:
            raise SafetyError(f"colon is not allowed in stored path component: {part!r}")
    return tuple(parts)


def safe_join(root: str, relative_path: str) -> str:
    parts = safe_relative_parts(relative_path)
    candidate = os.path.join(root, *parts)
    if not path_is_within(root, candidate):
        raise SafetyError(
            f"stored path escapes authorised root: {relative_path!r} from {root!r}"
        )
    return candidate


def validate_source_roots(source_paths: Iterable[str]) -> None:
    """Reject duplicate and parent/child source selections.

    Overlapping selections cause ambiguous ownership and can silently omit or
    duplicate data because each source file has one journal row.
    """
    paths = [(raw, canonical_path(raw)) for raw in source_paths if raw]
    for index, (raw_a, path_a) in enumerate(paths):
        for raw_b, path_b in paths[index + 1 :]:
            try:
                common = os.path.commonpath([path_a, path_b])
            except ValueError:
                continue
            if common == path_a or common == path_b:
                raise SafetyError(
                    "selected source folders overlap: "
                    f"{raw_a!r} and {raw_b!r}. Select only the parent or the child."
                )


def validate_package_location(package_dir: str, source_paths: Iterable[str]) -> None:
    package_c = canonical_path(package_dir)
    for source in source_paths:
        if not source:
            continue
        source_c = canonical_path(source)
        try:
            common = os.path.commonpath([package_c, source_c])
        except ValueError:
            continue
        if common == source_c:
            raise SafetyError(
                "migration package cannot be created inside a selected source folder: "
                f"package={package_dir!r}, source={source!r}"
            )
