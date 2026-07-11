# Dad PC Move — Safe Checklist

## Before leaving the old PC

1. Connect the external migration drive and confirm it has more free space than the scan total.
2. Close Outlook, photo tools, music players, editors, and anything that may be changing files.
3. Keep the PC online if OneDrive or another cloud provider is used.
4. Double-click `RUN_DATA_PORTER.bat` and choose **1 — Capture data from the OLD PC**.
5. Use **Full** hashing unless time or drive speed makes that impossible.
6. Do not proceed unless the launcher says **CAPTURE COMPLETE AND VERIFIED**.
7. Open `source_report.html` in the package and inspect warnings, unreadable paths, cloud placeholders, and possibly forgotten locations.
8. Leave the old PC unchanged. Do not reset, sell, clean, or erase it.

## On the new PC

1. Sign in as Dad and allow Windows/OneDrive to finish its initial setup.
2. Close programs that may use Documents, Pictures, Downloads, Music, or Videos.
3. Connect the migration drive.
4. Double-click `RUN_DATA_PORTER.bat` and choose **2 — Restore data onto the NEW PC**.
5. Use **Keep both** for the first restore unless you deliberately want a different conflict policy.
6. Do not proceed unless the launcher says **RESTORE COMPLETE AND VERIFIED**.
7. Open `restore_report.html` and inspect any skips, renamed conflicts, warnings, or failures.
8. Manually open a selection of important documents, family photos, music, and videos.
9. Keep the old PC and migration package intact until Dad has used the new PC and confirmed everything important is present.

## Hard stop conditions

Do not erase the old PC when any of these occur:

- capture failures;
- missing or unreadable files;
- failed package verification;
- unresolved custom destinations;
- failed restore verification;
- cloud-only files that were not retrieved;
- insufficient destination space;
- uncertainty about an important folder.
