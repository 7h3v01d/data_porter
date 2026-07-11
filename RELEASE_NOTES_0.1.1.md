# Data Porter 0.1.1 — Integrity Hardening

This release is the plain, functional build prepared for Dad's Windows 11 to Windows 11 move, while retaining Windows 10 support through the same Windows Known Folder APIs.

## Use this entry point

Double-click `RUN_DATA_PORTER.bat`.

The guided mode supports:

- old-PC scan, review, capture, resume, and package verification;
- standard Windows personal folders plus explicitly selected extra folders;
- destination free-space checks and FAT32 large-file blocking;
- new-PC package verification before any restore write;
- explicit custom-folder destination mapping;
- restore preview and safe conflict policies;
- staged, resumable restore;
- post-restore verification and HTML reports.

## Critical fixes in this release

1. A migration package cannot be created or used inside a selected source folder.
2. Duplicate and parent/child source selections are rejected.
3. Package and restore paths are containment-checked; `..`, absolute paths, drive-qualified paths, and path escape attempts are rejected.
4. Hash algorithms are stored explicitly (`none`, `sha256_full`, `sha256_sampled_v1`).
5. Full and sampled digests are never compared as though they were the same algorithm.
6. Changed source files are returned to pending state and recaptured.
7. Files that change while copying fail safely and remain retryable.
8. Interrupted `restoring` rows are reconciled rather than stranded.
9. Corrupted or missing restored files are requeued and can be repaired on the next restore run.
10. An existing package cannot be overwritten by an unrelated new migration plan.
11. Windows Known Folder resolution no longer falls back to a guessed home-directory path when Windows APIs fail.
12. Restore destinations overlapping the package are blocked.
13. Directory enumeration errors are recorded instead of silently discarded.
14. Unresolved restore items produce a partial-failure exit result.

## Operational rule

A successful tool result is not permission to erase the old PC. Keep the old PC and migration package intact until:

- capture verification passes;
- restore verification passes;
- Dad manually opens and checks representative important files;
- cloud-synchronised folders have finished syncing and have been inspected.

## Current limitations

- Python 3.11 or newer must be installed; a standalone Windows executable is not included.
- OneDrive placeholders are detected, but there is no dedicated hydration policy. Capture may cause Windows to download them; unavailable files fail visibly.
- Application-specific data adapters, package encryption, and direct LAN transfer remain out of scope.
- Empty directories and NTFS ACLs are not preserved as first-class migration items.
