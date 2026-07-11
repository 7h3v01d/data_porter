"""
Minimal CLI to exercise the scan engine before there's a PyQt6 front end.

    python -m data_porter.cli scan --output ./out --custom "D:\\Family Photos"
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .core.capture import capture_status, run_capture
from .core.manifest import MigrationManifest
from .core.models import ScanReport, SourceEnvironment, FolderScanResult, FolderOrigin
from .core.package import build_migration_plan, create_package, load_manifest
from .core.report import (
    run_source_scan,
    write_html_report,
    write_json_report,
    write_restore_report_html,
)
from .core.restore import (
    VALID_CONFLICT_POLICIES,
    build_restore_preview,
    restore_status,
    run_restore,
    verify_restore,
)
from .core.verify import verify_package


def _load_scan_report(json_path: str) -> ScanReport:
    with open(json_path, "r", encoding="utf-8") as f:
        d = json.load(f)
    env = d["environment"]
    report = ScanReport(
        schema_version=d.get("schema_version", "1.0"),
        environment=SourceEnvironment(
            computer_name=env["computer_name"],
            os_name=env["os_name"],
            os_version_raw=env.get("os_version_raw", ""),
            user_name=env["user_name"],
            scan_started_utc=env.get("scan_started_utc", ""),
        ),
        scan_finished_utc=d.get("scan_finished_utc"),
    )
    for fd in d["folders"]:
        report.folders.append(
            FolderScanResult(
                logical_name=fd["logical_name"],
                source_path=fd["source_path"],
                origin=FolderOrigin(fd["origin"]),
                exists=fd.get("exists", True),
                file_count=fd.get("file_count", 0),
                total_bytes=fd.get("total_bytes", 0),
                error=fd.get("error"),
            )
        )
    return report


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="data-porter", description="Data Porter scan engine")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="Scan Known Folders + custom paths")
    scan_p.add_argument(
        "--output", "-o", default="./data_porter_scan", help="Output directory for reports"
    )
    scan_p.add_argument(
        "--custom", "-c", action="append", default=[], help="Custom folder to include (repeatable)"
    )
    scan_p.add_argument(
        "--no-known-folders", action="store_true", help="Skip standard Known Folder scan"
    )
    scan_p.add_argument(
        "--no-discover", action="store_true", help="Skip 'you may have forgotten' discovery pass"
    )
    scan_p.add_argument(
        "--top-n", type=int, default=10, help="How many largest/newest files to record per folder"
    )

    plan_p = sub.add_parser("plan", help="Turn a scan report into a migration package skeleton")
    plan_p.add_argument("--scan-json", required=True, help="Path to source_report.json from `scan`")
    plan_p.add_argument("--package-dir", required=True, help="Where to create the migration package")
    plan_p.add_argument(
        "--include",
        action="append",
        default=None,
        help="Logical folder name to include (repeatable). Default: include everything found.",
    )

    capture_p = sub.add_parser("capture", help="Copy selected files into the package (resumable)")
    capture_p.add_argument("--package-dir", required=True)
    capture_p.add_argument(
        "--hash-level", choices=["fast", "balanced", "full"], default="balanced"
    )

    status_p = sub.add_parser("status", help="Show capture progress for a package")
    status_p.add_argument("--package-dir", required=True)

    verify_p = sub.add_parser("verify", help="Verify package contents against recorded state")
    verify_p.add_argument("--package-dir", required=True)
    verify_p.add_argument(
        "--level", choices=["fast", "balanced", "full"], default="balanced"
    )

    restore_preview_p = sub.add_parser(
        "restore-preview", help="Show where a package would restore to on this machine"
    )
    restore_preview_p.add_argument("--package-dir", required=True)
    restore_preview_p.add_argument(
        "--custom-override",
        action="append",
        default=[],
        metavar="LOGICAL_FOLDER=PATH",
        help="Destination override for a CUSTOM item (repeatable)",
    )

    restore_p = sub.add_parser("restore", help="Restore a package's files to this machine")
    restore_p.add_argument("--package-dir", required=True)
    restore_p.add_argument(
        "--conflict-policy", choices=list(VALID_CONFLICT_POLICIES), default="replace_if_newer"
    )
    restore_p.add_argument(
        "--custom-override",
        action="append",
        default=[],
        metavar="LOGICAL_FOLDER=PATH",
        help="Destination override for a CUSTOM item (repeatable)",
    )
    restore_p.add_argument(
        "--no-hash-check", action="store_true", help="Skip post-copy hash verification (faster)"
    )
    restore_p.add_argument(
        "--report", default=None, help="Optional path to write an HTML restore report"
    )

    restore_status_p = sub.add_parser("restore-status", help="Show restore progress for a package")
    restore_status_p.add_argument("--package-dir", required=True)

    restore_verify_p = sub.add_parser(
        "restore-verify", help="Verify restored files against their destination"
    )
    restore_verify_p.add_argument("--package-dir", required=True)
    restore_verify_p.add_argument(
        "--level", choices=["fast", "balanced", "full"], default="balanced"
    )

    args = parser.parse_args(argv)

    if args.command == "scan":
        os.makedirs(args.output, exist_ok=True)
        print("Scanning...", file=sys.stderr)
        report = run_source_scan(
            custom_paths=args.custom,
            include_known_folders=not args.no_known_folders,
            discover_forgotten=not args.no_discover,
            top_n=args.top_n,
        )

        json_path = os.path.join(args.output, "source_report.json")
        html_path = os.path.join(args.output, "source_report.html")
        write_json_report(report, json_path)
        write_html_report(report, html_path)

        print()
        print(f"Environment : {report.environment.computer_name} "
              f"({report.environment.os_name}), user {report.environment.user_name}")
        print(f"Total files : {report.total_files:,}")
        print(f"Total size  : {_human_size(report.total_bytes)}")
        print()
        for folder in report.folders:
            if not folder.exists:
                print(f"  [missing] {folder.logical_name}: {folder.error}")
                continue
            print(
                f"  {folder.logical_name:<12} {folder.file_count:>7,} files  "
                f"{_human_size(folder.total_bytes):>10}  "
                f"({folder.source_path})"
            )
        if report.discovered_candidates:
            print()
            print("You may have forgotten:")
            for c in report.discovered_candidates:
                print(f"  {c.path}: {_human_size(c.size_bytes)} ({c.reason})")

        print()
        print(f"Reports written to:\n  {json_path}\n  {html_path}")
        return 0

    if args.command == "plan":
        scan_report = _load_scan_report(args.scan_json)
        include = set(args.include) if args.include else None
        manifest = build_migration_plan(scan_report, selected_logical_names=include)
        manifest_path = create_package(args.package_dir, manifest, scan_report=scan_report)

        print(f"Package created at: {args.package_dir}")
        print(f"Manifest: {manifest_path}")
        print(f"Migration ID: {manifest.migration_id}")
        print()
        for item in manifest.items:
            print(
                f"  {item.logical_folder:<20} -> {item.package_path:<20} "
                f"{item.file_count:>7,} files, restore_target={item.restore_target}"
            )
        if not manifest.items:
            print("  (nothing selected -- check --include names against the scan report)")
        return 0

    if args.command == "capture":
        manifest = load_manifest(args.package_dir)
        print(f"Capturing into {args.package_dir} (hash level: {args.hash_level})...", file=sys.stderr)
        summary = run_capture(args.package_dir, manifest, hash_level=args.hash_level)

        print()
        print(f"Newly seeded : {summary.seeded}")
        print(f"Copied       : {summary.copied}  ({_human_size(summary.total_bytes_copied)})")
        print(f"Already done : {summary.already_done}")
        print(f"Failed       : {summary.failed}")
        if summary.errors:
            print()
            print("Errors:")
            for e in summary.errors[:20]:
                print(f"  {e['source_path']}: {e['error']}")
            if len(summary.errors) > 20:
                print(f"  ...and {len(summary.errors) - 20} more (see metadata/errors.jsonl)")
        return 0 if summary.failed == 0 else 2

    if args.command == "status":
        status = capture_status(args.package_dir)
        print(f"Status for {args.package_dir}:")
        for state, info in status.items():
            print(f"  {state:<10} {info['count']:>7,} files  {_human_size(info['bytes'])}")
        return 0

    if args.command == "verify":
        print(f"Verifying {args.package_dir} (level: {args.level})...", file=sys.stderr)
        summary = verify_package(args.package_dir, level=args.level)
        print()
        print(f"Checked  : {summary.checked}")
        print(f"Verified : {summary.verified}")
        print(f"Failed   : {summary.failed}")
        print(f"Missing  : {summary.missing}")
        if summary.problems:
            print()
            print("Problems:")
            for p in summary.problems[:20]:
                print(f"  {p['path']}: {p['problem']}")
        return 0 if summary.failed == 0 and summary.missing == 0 else 2

    if args.command in ("restore-preview", "restore"):
        overrides = {}
        for entry in args.custom_override:
            if "=" not in entry:
                print(f"Ignoring malformed --custom-override {entry!r} (expected LOGICAL_FOLDER=PATH)", file=sys.stderr)
                continue
            key, _, val = entry.partition("=")
            overrides[key] = val

    if args.command == "restore-preview":
        manifest = load_manifest(args.package_dir)
        preview = build_restore_preview(args.package_dir, manifest, custom_overrides=overrides)

        print(f"Package from {preview.package_source_computer}, created {preview.package_created_utc}")
        print()
        for item in preview.items:
            status = "OK" if item.destination_resolved else "UNRESOLVED"
            print(
                f"  [{status:<10}] {item.logical_folder:<20} -> {item.destination_root or '(none)'}"
            )
            print(
                f"               {item.file_count:>7,} files, {_human_size(item.total_bytes):>10}, "
                f"{item.existing_conflicts} existing conflict(s)"
            )
            if item.destination_warning:
                print(f"               ! {item.destination_warning}")
        print()
        print(f"Total to restore : {_human_size(preview.required_bytes)}")
        for root, free in preview.free_bytes_by_root.items():
            free_str = _human_size(free) if free is not None else "unknown"
            print(f"Free space at {root}: {free_str}")
        if preview.unresolved_items:
            print()
            print(f"UNRESOLVED (won't be restored until resolved): {', '.join(preview.unresolved_items)}")
        return 0

    if args.command == "restore":
        manifest = load_manifest(args.package_dir)
        preview = build_restore_preview(args.package_dir, manifest, custom_overrides=overrides)

        print(
            f"Restoring from {args.package_dir} (conflict policy: {args.conflict_policy})...",
            file=sys.stderr,
        )
        summary = run_restore(
            args.package_dir,
            manifest,
            conflict_policy=args.conflict_policy,
            custom_overrides=overrides,
            hash_check=not args.no_hash_check,
        )

        print()
        print(f"Newly seeded      : {summary.seeded}")
        print(f"Restored          : {summary.restored}  ({_human_size(summary.total_bytes_restored)})")
        print(f"Already done      : {summary.already_done}")
        print(f"Skipped by policy : {summary.skipped_policy}")
        print(f"Conflicts renamed : {summary.conflicts_renamed}")
        print(f"Failed            : {summary.failed}")
        if summary.blocked_items:
            print(f"Blocked items     : {', '.join(summary.blocked_items)} (destination unresolved)")
        if summary.errors:
            print()
            print("Errors:")
            for e in summary.errors[:20]:
                print(f"  {e['package_rel_path']}: {e['error']}")
            if len(summary.errors) > 20:
                print(f"  ...and {len(summary.errors) - 20} more (see metadata/restore_errors.jsonl)")

        if args.report:
            write_restore_report_html(preview, summary, args.report)
            print()
            print(f"Report written to: {args.report}")

        return 0 if summary.failed == 0 else 2

    if args.command == "restore-status":
        status = restore_status(args.package_dir)
        print(f"Restore status for {args.package_dir}:")
        for state, count in status.items():
            print(f"  {state:<10} {count:>7,} files")
        return 0

    if args.command == "restore-verify":
        print(f"Verifying restored files (level: {args.level})...", file=sys.stderr)
        summary = verify_restore(args.package_dir, level=args.level)
        print()
        print(f"Checked  : {summary.checked}")
        print(f"Verified : {summary.verified}")
        print(f"Failed   : {summary.failed}")
        print(f"Missing  : {summary.missing}")
        if summary.problems:
            print()
            print("Problems:")
            for p in summary.problems[:20]:
                print(f"  {p['path']}: {p['problem']}")
        return 0 if summary.failed == 0 and summary.missing == 0 else 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
