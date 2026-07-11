"""
Minimal CLI to exercise the scan engine before there's a PyQt6 front end.

    python -m data_porter.cli scan --output ./out --custom "D:\\Family Photos"
"""

from __future__ import annotations

import argparse
import os
import sys

from .core.report import run_source_scan, write_html_report, write_json_report


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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
