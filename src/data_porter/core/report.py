"""
Orchestrates a full source scan (all Known Folders + optional custom
selections) into a ScanReport, and renders that report as JSON and a
simple standalone HTML file.
"""

from __future__ import annotations

import getpass
import html
import json
import os
import platform
from typing import Optional

from .known_folders import (
    ResolutionMethod,
    get_windows_version_label,
    resolve_all_known_folders,
)
from .models import FolderOrigin, ScanReport, SourceEnvironment, utc_now_iso
from .scanner import discover_secondary_drive_candidates, scan_folder

SCHEMA_VERSION = "1.0"


def run_source_scan(
    custom_paths: Optional[list[str]] = None,
    include_known_folders: bool = True,
    discover_forgotten: bool = True,
    top_n: int = 10,
) -> ScanReport:
    """
    Run a full scan: every resolvable Known Folder, plus any custom paths
    the caller supplies, plus (optionally) a lightweight pass looking for
    large folders on secondary drives that aren't part of the plan yet.
    """
    environment = SourceEnvironment(
        computer_name=platform.node() or "UNKNOWN-PC",
        os_name=get_windows_version_label(),
        os_version_raw=platform.version(),
        user_name=getpass.getuser(),
    )

    report = ScanReport(schema_version=SCHEMA_VERSION, environment=environment)

    selected_paths: list[str] = []

    if include_known_folders:
        for folder in resolve_all_known_folders():
            if folder.logical_name == "Profile":
                # The profile root itself is never offered as a scan
                # target -- see the "do not copy the entire user profile"
                # rule. It's resolved only so other tooling can compute
                # relative paths against it later if needed.
                continue
            if not folder.exists or not folder.path:
                # Still record it as a non-existent/unresolved result so
                # the UI can show *why* something is missing, rather than
                # just omitting it silently.
                from .models import FolderScanResult

                fr = FolderScanResult(
                    logical_name=folder.logical_name,
                    source_path=folder.path or "",
                    origin=FolderOrigin.KNOWN_FOLDER,
                    exists=False,
                    error=(
                        "Could not resolve this Known Folder "
                        f"(method attempted: {folder.method})"
                    ),
                )
                report.folders.append(fr)
                continue

            selected_paths.append(folder.path)
            fr = scan_folder(
                source_path=folder.path,
                logical_name=folder.logical_name,
                origin=FolderOrigin.KNOWN_FOLDER,
                top_n=top_n,
            )
            report.folders.append(fr)

    for custom_path in custom_paths or []:
        selected_paths.append(custom_path)
        fr = scan_folder(
            source_path=custom_path,
            logical_name=custom_path,
            origin=FolderOrigin.CUSTOM_SELECTION,
            top_n=top_n,
        )
        report.folders.append(fr)

    if discover_forgotten:
        report.discovered_candidates = discover_secondary_drive_candidates(
            already_selected=selected_paths
        )

    report.scan_finished_utc = utc_now_iso()
    return report


def write_json_report(report: ScanReport, output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2)


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def write_html_report(report: ScanReport, output_path: str) -> None:
    env = report.environment
    rows = []
    for folder in report.folders:
        if not folder.exists:
            rows.append(
                f"<tr class='missing'><td>{html.escape(folder.logical_name)}</td>"
                f"<td colspan='3'>{html.escape(folder.error or 'Not found')}</td></tr>"
            )
            continue
        rows.append(
            "<tr>"
            f"<td>{html.escape(folder.logical_name)}</td>"
            f"<td>{html.escape(folder.source_path)}</td>"
            f"<td>{folder.file_count:,}</td>"
            f"<td>{_human_size(folder.total_bytes)}</td>"
            f"<td>{folder.reparse_points_skipped}</td>"
            f"<td>{folder.cloud_placeholder_count}</td>"
            f"<td>{len(folder.skipped)}</td>"
            "</tr>"
        )

    candidate_rows = []
    for c in report.discovered_candidates:
        candidate_rows.append(
            "<tr>"
            f"<td>{html.escape(c.path)}</td>"
            f"<td>{_human_size(c.size_bytes)}</td>"
            f"<td>{c.file_count:,}</td>"
            f"<td>{html.escape(c.last_modified_utc or '-')}</td>"
            f"<td>{html.escape(c.reason)}</td>"
            "</tr>"
        )

    candidates_section = ""
    if candidate_rows:
        candidates_section = f"""
        <h2>You may have forgotten...</h2>
        <table>
          <tr><th>Path</th><th>Size</th><th>Files</th><th>Last modified</th><th>Why it's flagged</th></tr>
          {''.join(candidate_rows)}
        </table>
        """

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Data Porter -- Source Scan Report</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 2rem; color: #1a1a1a; background: #fafafa; }}
  h1 {{ margin-bottom: 0.2rem; }}
  .meta {{ color: #555; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; background: white; }}
  th, td {{ border: 1px solid #ddd; padding: 0.5rem 0.75rem; text-align: left; font-size: 0.92rem; }}
  th {{ background: #f0f0f0; }}
  tr.missing td {{ color: #a33; font-style: italic; }}
  .totals {{ font-size: 1.1rem; margin-bottom: 1.5rem; }}
  .totals b {{ font-size: 1.3rem; }}
</style>
</head>
<body>
  <h1>Data Porter &mdash; Source Scan Report</h1>
  <div class="meta">
    {html.escape(env.computer_name)} &middot; {html.escape(env.user_name)} &middot;
    {html.escape(env.os_name)} &middot; scanned {html.escape(report.scan_finished_utc or '')}
  </div>

  <div class="totals">
    Total files: <b>{report.total_files:,}</b> &nbsp;|&nbsp;
    Total size: <b>{_human_size(report.total_bytes)}</b>
  </div>

  <h2>Scanned locations</h2>
  <table>
    <tr>
      <th>Folder</th><th>Path</th><th>Files</th><th>Size</th>
      <th>Reparse points skipped</th><th>Cloud placeholders</th><th>Other skipped</th>
    </tr>
    {''.join(rows)}
  </table>

  {candidates_section}
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(doc)
