"""Plain guided launcher for a time-sensitive Windows PC migration.

No GUI and no hidden decisions.  It drives the same tested core used by the
CLI, with conservative defaults and explicit stop points.
"""

from __future__ import annotations

import os
import sys
import traceback
import shutil
import platform
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from data_porter.core.capture import capture_status, run_capture
from data_porter.core.package import build_migration_plan, create_package, load_manifest
from data_porter.core.report import (
    run_source_scan,
    write_html_report,
    write_json_report,
    write_restore_report_html,
)
from data_porter.core.restore import (
    build_restore_preview,
    restore_status,
    run_restore,
    verify_restore,
)
from data_porter.core.safety import SafetyError
from data_porter.core.verify import verify_package



def nearest_existing_parent(path: str) -> str:
    current = os.path.abspath(path)
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            return "."
        current = parent
    return current


def windows_filesystem_type(path: str) -> str | None:
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        root_buffer = ctypes.create_unicode_buffer(260)
        if not ctypes.windll.kernel32.GetVolumePathNameW(
            os.path.abspath(path), root_buffer, len(root_buffer)
        ):
            return None
        fs_buffer = ctypes.create_unicode_buffer(64)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root_buffer.value, None, 0, None, None, None, fs_buffer, len(fs_buffer)
        )
        return fs_buffer.value.upper() if ok else None
    except Exception:
        return None


def preflight_capture_destination(package_dir: str, report) -> None:
    parent = nearest_existing_parent(package_dir)
    usage = shutil.disk_usage(parent)
    margin = max(1024**3, int(report.total_bytes * 0.02))
    required = report.total_bytes + margin
    print(
        f"\nDestination free space: {human_size(usage.free)}; "
        f"planned data plus safety margin: {human_size(required)}"
    )
    if usage.free < required:
        raise SafetyError(
            "the package destination does not have enough free space for the selected data "
            "plus a safety margin"
        )

    fs_type = windows_filesystem_type(parent)
    if fs_type:
        print(f"Destination filesystem: {fs_type}")
    if fs_type == "FAT32":
        too_large = []
        for folder in report.folders:
            too_large.extend(
                entry.path for entry in folder.largest_files if entry.size_bytes >= 4 * 1024**3
            )
        if too_large:
            sample = "\n  ".join(too_large[:5])
            raise SafetyError(
                "the package drive is FAT32 and cannot store files of 4 GB or larger. "
                f"Use NTFS or exFAT. Large file(s) include:\n  {sample}"
            )


def preflight_restore_space(preview) -> None:
    required_by_root: dict[str, int] = {}
    for item in preview.items:
        if item.destination_resolved and item.destination_root:
            required_by_root[item.destination_root] = (
                required_by_root.get(item.destination_root, 0) + item.total_bytes
            )
    for root, required in required_by_root.items():
        free = preview.free_bytes_by_root.get(root)
        if free is not None and free < required:
            raise SafetyError(
                f"not enough free space at {root}: need {human_size(required)}, "
                f"have {human_size(free)}"
            )

def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def ask_path(prompt: str, must_exist: bool = False) -> str:
    while True:
        raw = input(prompt).strip().strip('"')
        if not raw:
            print("A path is required.")
            continue
        path = os.path.abspath(os.path.expanduser(raw))
        if must_exist and not os.path.isdir(path):
            print(f"Folder does not exist: {path}")
            continue
        return path


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def print_scan(report) -> None:
    print("\nSCAN SUMMARY")
    print("=" * 72)
    for folder in report.folders:
        if not folder.exists:
            print(f"[missing] {folder.logical_name:<16} {folder.error}")
            continue
        flags = []
        if folder.unreadable_count:
            flags.append(f"{folder.unreadable_count} unreadable")
        if folder.cloud_placeholder_count:
            flags.append(f"{folder.cloud_placeholder_count} cloud placeholder(s)")
        suffix = f"  ! {', '.join(flags)}" if flags else ""
        print(
            f"{folder.logical_name:<18} {folder.file_count:>9,} files  "
            f"{human_size(folder.total_bytes):>11}  {folder.source_path}{suffix}"
        )
    print("-" * 72)
    print(f"TOTAL               {report.total_files:>9,} files  {human_size(report.total_bytes):>11}")


def capture_guided() -> int:
    print("\nCAPTURE OLD PC DATA")
    print("The migration package should be on an external drive or another safe drive.")
    package_dir = ask_path("Migration package folder (example E:\\Dad-PC-Migration): ")

    existing_manifest = os.path.join(package_dir, "migration.json")
    if os.path.isfile(existing_manifest):
        print("\nAn existing Data Porter package was found. This run will resume it; no plan is overwritten.")
        manifest = load_manifest(package_dir)
        choice = input("Hashing for retried/new files: [F]ull recommended, [B]alanced [F]: ").strip().lower()
        hash_level = "balanced" if choice == "b" else "full"
        captured = run_capture(package_dir, manifest, hash_level=hash_level)
        print(
            f"Copied {captured.copied:,}; already complete {captured.already_done:,}; "
            f"failed {captured.failed:,}; data copied {human_size(captured.total_bytes_copied)}"
        )
        if captured.failed:
            print("SAFE STOP: Some files still failed. The package remains resumable.")
            return 2
        verified = verify_package(package_dir, level=hash_level)
        print(
            f"Checked {verified.checked:,}; verified {verified.verified:,}; "
            f"failed {verified.failed:,}; missing {verified.missing:,}"
        )
        if verified.failed or verified.missing:
            print("SAFE STOP: Verification did not pass. Keep the old PC unchanged.")
            return 2
        print("\nCAPTURE COMPLETE AND VERIFIED")
        return 0

    if os.path.isdir(package_dir) and os.listdir(package_dir):
        raise SafetyError(
            "the selected package folder is not empty and is not a Data Porter package. "
            "Choose a new empty folder."
        )

    custom_paths: list[str] = []
    print("\nOptional extra folders can be added now. Leave blank when finished.")
    while True:
        raw = input("Extra folder: ").strip().strip('"')
        if not raw:
            break
        path = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(path):
            print(f"Not a folder: {path}")
            continue
        custom_paths.append(path)

    print("\nScanning standard personal folders. Nothing is copied yet...")
    report = run_source_scan(
        custom_paths=custom_paths,
        include_known_folders=True,
        discover_forgotten=True,
        top_n=20,
    )
    print_scan(report)

    if report.discovered_candidates:
        print("\nPOSSIBLY FORGOTTEN LOCATIONS (not automatically included)")
        for candidate in report.discovered_candidates:
            print(f"  {candidate.path}  {human_size(candidate.size_bytes)}")
        print("Run capture again and add any required location as an Extra folder.")

    cloud_count = sum(folder.cloud_placeholder_count for folder in report.folders)
    unreadable_count = sum(folder.unreadable_count for folder in report.folders)
    if cloud_count:
        print(
            f"\nWARNING: {cloud_count} cloud placeholder(s) were detected. Keep the old PC "
            "online during capture so Windows can retrieve them. Any file that cannot be "
            "read will be reported as a failure."
        )
    if unreadable_count:
        print(f"\nWARNING: {unreadable_count} unreadable path(s) were found. Review the HTML report.")

    preflight_capture_destination(package_dir, report)

    if not ask_yes_no("Create this migration package and begin copying?", default=False):
        print("Cancelled before copying. No package was created.")
        return 1

    manifest = build_migration_plan(report)
    create_package(package_dir, manifest, scan_report=report)
    write_json_report(report, os.path.join(package_dir, "source_report.json"))
    write_html_report(report, os.path.join(package_dir, "source_report.html"))

    choice = input("Hashing: [F]ull recommended, [B]alanced faster. Choose [F]: ").strip().lower()
    hash_level = "balanced" if choice == "b" else "full"

    print(f"\nCapturing with {hash_level} integrity checking. Re-running safely resumes.")
    captured = run_capture(package_dir, manifest, hash_level=hash_level)
    print(
        f"Copied {captured.copied:,}; already complete {captured.already_done:,}; "
        f"failed {captured.failed:,}; data copied {human_size(captured.total_bytes_copied)}"
    )
    if captured.failed:
        print("SAFE STOP: Some files failed. Review metadata\\errors.jsonl, fix the cause, then run capture again.")
        return 2

    print("\nVerifying the completed package...")
    verified = verify_package(package_dir, level=hash_level)
    print(
        f"Checked {verified.checked:,}; verified {verified.verified:,}; "
        f"failed {verified.failed:,}; missing {verified.missing:,}"
    )
    if verified.failed or verified.missing:
        print("SAFE STOP: Verification did not pass. Do not erase or alter the old PC.")
        return 2

    print("\nCAPTURE COMPLETE AND VERIFIED")
    print(f"Package: {package_dir}")
    print("Keep the old PC unchanged until restore verification passes on the new PC.")
    return 0


def collect_custom_overrides(manifest) -> dict[str, str]:
    overrides: dict[str, str] = {}
    custom_items = [item for item in manifest.items if item.restore_target == "CUSTOM"]
    if not custom_items:
        return overrides
    print("\nCUSTOM FOLDER DESTINATIONS")
    print("Each custom folder requires an explicit destination. No path will be guessed.")
    for item in custom_items:
        print(f"\n{item.logical_folder}\n  Old location: {item.source_path}")
        overrides[item.logical_folder] = ask_path("  New destination folder: ")
    return overrides


def print_preview(preview) -> None:
    print("\nRESTORE PREVIEW")
    print("=" * 72)
    for item in preview.items:
        status = "OK" if item.destination_resolved else "BLOCKED"
        print(f"[{status:<7}] {item.logical_folder:<18} -> {item.destination_root or '(unresolved)'}")
        print(
            f"          {item.file_count:,} files, {human_size(item.total_bytes)}, "
            f"{item.existing_conflicts} existing conflict(s)"
        )
        if item.destination_warning:
            print(f"          WARNING: {item.destination_warning}")
    print("-" * 72)
    print(f"Required data: {human_size(preview.required_bytes)}")
    for root, free in preview.free_bytes_by_root.items():
        print(f"Free at {root}: {human_size(free) if free is not None else 'unknown'}")


def restore_guided() -> int:
    print("\nRESTORE TO NEW PC")
    package_dir = ask_path("Migration package folder: ", must_exist=True)
    manifest = load_manifest(package_dir)

    print("\nVerifying package before any destination file is written...")
    package_check = verify_package(package_dir, level="full")
    print(
        f"Checked {package_check.checked:,}; verified {package_check.verified:,}; "
        f"failed {package_check.failed:,}; missing {package_check.missing:,}"
    )
    if package_check.failed or package_check.missing:
        print("SAFE STOP: Package verification failed. Restore has not started.")
        return 2

    overrides = collect_custom_overrides(manifest)
    preview = build_restore_preview(package_dir, manifest, custom_overrides=overrides)
    print_preview(preview)
    if preview.unresolved_items:
        print("SAFE STOP: One or more destinations are unresolved. Nothing was restored.")
        return 2
    preflight_restore_space(preview)

    print("\nConflict policy:")
    print("  1. Keep both (safest; never overwrites an existing file) [default]")
    print("  2. Replace only when migrated file is newer")
    print("  3. Replace existing files")
    print("  4. Skip all existing files")
    choice = input("Choose [1]: ").strip()
    policy = {"2": "replace_if_newer", "3": "replace", "4": "skip"}.get(choice, "keep_both")

    confirmation = input("\nType RESTORE to begin writing files: ").strip()
    if confirmation != "RESTORE":
        print("Cancelled before writing any files.")
        return 1

    summary = run_restore(
        package_dir,
        manifest,
        conflict_policy=policy,
        custom_overrides=overrides,
        hash_check=True,
    )
    report_path = os.path.join(package_dir, "restore_report.html")
    write_restore_report_html(preview, summary, report_path)
    print(
        f"\nRestored {summary.restored:,}; already complete {summary.already_done:,}; "
        f"skipped {summary.skipped_policy:,}; renamed {summary.conflicts_renamed:,}; "
        f"failed {summary.failed:,}"
    )
    if summary.failed or summary.blocked_items:
        print("SAFE STOP: Restore is incomplete. Re-run this option after fixing the reported issue.")
        return 2

    print("\nVerifying restored destination files...")
    check = verify_restore(package_dir, level="full")
    print(
        f"Checked {check.checked:,}; verified {check.verified:,}; "
        f"failed {check.failed:,}; missing {check.missing:,}"
    )
    if check.failed or check.missing:
        print("SAFE STOP: Destination verification failed. Keep both the package and old PC intact.")
        return 2

    print("\nRESTORE COMPLETE AND VERIFIED")
    print(f"Report: {report_path}")
    print("Do not erase the old PC until Dad has manually checked his important folders and files.")
    return 0


def show_status() -> int:
    package_dir = ask_path("Migration package folder: ", must_exist=True)
    print("\nCapture status:")
    for state, info in capture_status(package_dir).items():
        print(f"  {state:<10} {info['count']:>9,} files  {human_size(info['bytes'])}")
    try:
        states = restore_status(package_dir)
    except Exception:
        states = {}
    if states:
        print("\nRestore status:")
        for state, count in states.items():
            print(f"  {state:<10} {count:>9,} files")
    return 0


def main() -> int:
    print("=" * 72)
    print("DATA PORTER 0.1.1 - SAFE GUIDED MODE")
    print("=" * 72)
    print("1. Capture data from the OLD PC")
    print("2. Restore data onto the NEW PC")
    print("3. Show package status")
    print("4. Exit")
    choice = input("\nChoose: ").strip()
    if choice == "1":
        return capture_guided()
    if choice == "2":
        return restore_guided()
    if choice == "3":
        return show_status()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafetyError as exc:
        print(f"\nSAFE STOP: {exc}")
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\n\nCancelled safely. Re-run the same option to resume.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nSAFE STOP: Unexpected error: {exc}")
        logs = ROOT / "quick_launcher_error.txt"
        logs.write_text(traceback.format_exc(), encoding="utf-8")
        print(f"Technical details written to: {logs}")
        raise SystemExit(2)
