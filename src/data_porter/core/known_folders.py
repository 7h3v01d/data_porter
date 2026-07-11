"""
Known Folder discovery.

Data Porter must never hardcode paths like C:\\Users\\Dad\\Documents -- folders
can be redirected (to OneDrive, another drive, a different name entirely).
The only reliable way to find "the current user's Documents folder" is to
ask Windows via the Known Folder API.

This module resolves Known Folders in three tiers, in order of preference:

  1. pywin32's shell helpers (``win32com.shell.shell.SHGetKnownFolderPath``),
     if pywin32 is installed -- this is the primary path for the shipped app.
  2. Raw ``ctypes`` call into ``shell32.dll`` -- works on any Windows machine
     even without pywin32, used as an automatic fallback.
  3. A "dev fallback" that approximates folder locations from the home
     directory. This only activates on non-Windows platforms (or Windows
     without any working API path), purely so the scan engine can be
     developed and unit-tested off-Windows. It is clearly flagged in the
     result so it's never mistaken for a real resolution.

Each entry is a well-known FOLDERID GUID as published by Microsoft, so this
does not depend on shell32 header availability or pywin32 version.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from typing import Optional

# Logical name -> (FOLDERID GUID, dev-fallback relative path)
# GUIDs are Microsoft's published constants for FOLDERID_*; see
# https://learn.microsoft.com/windows/win32/shell/knownfolderid
KNOWN_FOLDER_DEFINITIONS: dict[str, tuple[str, str]] = {
    "Desktop": ("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Desktop"),
    "Documents": ("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Documents"),
    "Downloads": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    "Pictures": ("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures"),
    "Music": ("{4BD8D571-6D19-48D3-BE97-422220080E43}", "Music"),
    "Videos": ("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos"),
    "SavedGames": ("{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}", "Saved Games"),
    "Favorites": ("{1777F761-68AD-4D8A-87BD-30B759FA33DD}", "Favorites"),
    "Contacts": ("{56784854-C6CB-462B-8169-88E350ACB882}", "Contacts"),
    "Profile": ("{5E6C858F-0E22-4760-9AFE-EA3317B67173}", ""),
}


class ResolutionMethod:
    PYWIN32 = "pywin32"
    CTYPES = "ctypes"
    DEV_FALLBACK = "dev_fallback"
    NOT_FOUND = "not_found"


@dataclass
class ResolvedFolder:
    logical_name: str
    path: Optional[str]
    method: str
    exists: bool


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _resolve_via_pywin32(guid: str) -> Optional[str]:
    try:
        import pythoncom  # type: ignore
        from win32com.shell import shell, shellcon  # type: ignore
    except ImportError:
        return None

    try:
        # SHGetKnownFolderPath takes an IID; pywin32 accepts the GUID string
        # directly via pythoncom.MakeIID.
        iid = pythoncom.MakeIID(guid)
        path = shell.SHGetKnownFolderPath(iid, 0, None)
        return path
    except Exception:
        return None


def _resolve_via_ctypes(guid: str) -> Optional[str]:
    if not _is_windows():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # Parse "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}" into a GUID struct
        raw = guid.strip("{}")
        parts = raw.split("-")
        data1 = int(parts[0], 16)
        data2 = int(parts[1], 16)
        data3 = int(parts[2], 16)
        data4_hi = bytes.fromhex(parts[3])
        data4_lo = bytes.fromhex(parts[4])
        data4 = (ctypes.c_ubyte * 8)(*(data4_hi + data4_lo))
        folder_id = GUID(data1, data2, data3, data4)

        SHGetKnownFolderPath = ctypes.windll.shell32.SHGetKnownFolderPath
        path_ptr = ctypes.c_wchar_p()
        result = SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, 0, ctypes.byref(path_ptr)
        )
        if result != 0:  # S_OK == 0
            return None
        path = path_ptr.value
        # Caller is responsible for freeing via CoTaskMemFree; do so.
        ctypes.windll.ole32.CoTaskMemFree(path_ptr)
        return path
    except Exception:
        return None


def _resolve_via_dev_fallback(relative: str) -> Optional[str]:
    home = os.path.expanduser("~")
    if relative == "":
        return home
    return os.path.join(home, relative)


def resolve_known_folder(logical_name: str) -> ResolvedFolder:
    """Resolve a single logical Known Folder name to a real path."""
    if logical_name not in KNOWN_FOLDER_DEFINITIONS:
        raise ValueError(f"Unknown logical folder name: {logical_name!r}")

    guid, fallback_relative = KNOWN_FOLDER_DEFINITIONS[logical_name]

    if _is_windows():
        path = _resolve_via_pywin32(guid)
        method = ResolutionMethod.PYWIN32
        if not path:
            path = _resolve_via_ctypes(guid)
            method = ResolutionMethod.CTYPES
        if not path:
            # On a real Windows migration, guessing a user-data destination
            # is unsafe. Report an unresolved folder and require the caller
            # to stop rather than silently writing to a home-directory guess.
            path = None
            method = ResolutionMethod.NOT_FOUND
    else:
        path = _resolve_via_dev_fallback(fallback_relative)
        method = ResolutionMethod.DEV_FALLBACK

    exists = bool(path) and os.path.isdir(path)
    return ResolvedFolder(
        logical_name=logical_name, path=path, method=method, exists=exists
    )


def resolve_all_known_folders() -> list[ResolvedFolder]:
    """Resolve every folder in KNOWN_FOLDER_DEFINITIONS."""
    return [resolve_known_folder(name) for name in KNOWN_FOLDER_DEFINITIONS]


def get_windows_version_label() -> str:
    """
    Best-effort human label: "Windows 11", "Windows 10", or the raw platform
    string on non-Windows (dev/test) machines.
    """
    if not _is_windows():
        return f"{platform.system()} {platform.release()} (dev/test)"

    # Windows 11 reports as build >= 22000 while platform.release() still
    # says "10" for both Windows 10 and 11 on many Python builds.
    try:
        build = int(platform.version().split(".")[-1])
    except (ValueError, IndexError):
        build = 0

    if build >= 22000:
        return "Windows 11"
    return "Windows 10"
