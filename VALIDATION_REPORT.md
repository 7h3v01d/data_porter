# Data Porter 0.1.1 — Validation Report

Date: 11 July 2026

## Automated validation

- Python compile check: passed.
- Unit and integration-style test suite: **32/32 passed**.
- Original tests retained: **25/25 still pass unchanged**.
- New integrity regression tests: **7/7 passed**.

The new regression tests cover:

- package-inside-source rejection;
- unrelated plan overwrite rejection;
- modified-source recapture;
- sampled-hash/full-verification interoperability;
- interrupted restore reconciliation;
- restore path traversal blocking;
- repair after destination corruption is detected.

## End-to-end synthetic validation

A complete command-line workflow passed against a temporary source and destination:

`scan → plan → capture(full) → verify(full) → restore-preview → restore(keep_both) → restore-verify(full)`

The restored document and binary test file matched their source files byte-for-byte.

## Environment limitation

The validation environment was Linux-based. The migration engine, journalling, hashing, path containment, resume behaviour, reports, and CLI workflow were exercised. The following Windows-specific integrations could not be executed directly here:

- `SHGetKnownFolderPath` through pywin32 or `ctypes`;
- actual Windows 10/11 redirected Known Folders;
- live OneDrive placeholder hydration;
- real NTFS/exFAT/FAT32 external-drive behaviour;
- Windows file locks and antivirus interference.

For that reason, the first run on Dad's old PC should be treated as a controlled production smoke test. Do not erase or modify the old PC after capture. Review `source_report.html`, require a successful package verification, and retain an independent copy until the new-PC restore and manual inspection pass.
