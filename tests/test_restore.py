import os
import sys
import tempfile
import shutil
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data_porter.core.capture import run_capture
from data_porter.core.package import build_migration_plan, create_package
from data_porter.core.report import run_source_scan
from data_porter.core.restore import (
    RESTORE_STAGING_SUFFIX,
    build_restore_preview,
    restore_status,
    run_restore,
    verify_restore,
)


class TestRestore(unittest.TestCase):
    """Exercises scan -> plan -> capture -> restore end to end against
    throwaway source and destination trees, covering every conflict
    policy and resumability."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp(prefix="dp_src_")
        self.package_dir = tempfile.mkdtemp(prefix="dp_pkg_")
        self.dest_dir = tempfile.mkdtemp(prefix="dp_dest_")

        os.makedirs(os.path.join(self.source_dir, "Documents", "Sub"))
        os.makedirs(os.path.join(self.source_dir, "Pictures"))

        self._write(os.path.join(self.source_dir, "Documents", "a.txt"), b"hello world")
        self._write(os.path.join(self.source_dir, "Documents", "Sub", "b.txt"), b"x" * 5000)
        self._write(os.path.join(self.source_dir, "Pictures", "photo.jpg"), b"y" * 20000)

    def tearDown(self):
        shutil.rmtree(self.source_dir, ignore_errors=True)
        shutil.rmtree(self.package_dir, ignore_errors=True)
        shutil.rmtree(self.dest_dir, ignore_errors=True)

    def _write(self, path, content):
        with open(path, "wb") as f:
            f.write(content)

    def _scan_plan_capture(self):
        """Builds a package with two CUSTOM items (Documents, Pictures),
        pointed at custom_overrides destinations in the test, since
        real Known Folder resolution isn't meaningful off-Windows."""
        report = run_source_scan(
            custom_paths=[
                os.path.join(self.source_dir, "Documents"),
                os.path.join(self.source_dir, "Pictures"),
            ],
            include_known_folders=False,
            discover_forgotten=False,
        )
        manifest = build_migration_plan(report)
        create_package(self.package_dir, manifest, scan_report=report)
        run_capture(self.package_dir, manifest, hash_level="full")
        return manifest

    def _overrides(self, manifest):
        # CUSTOM items keep their logical_folder as the original source
        # path (see package.py / scanner), so map each to a destination
        # subfolder under self.dest_dir.
        overrides = {}
        for item in manifest.items:
            overrides[item.logical_folder] = os.path.join(
                self.dest_dir, os.path.basename(item.logical_folder.rstrip(os.sep))
            )
        return overrides

    def test_restore_preview_resolves_custom_overrides(self):
        manifest = self._scan_plan_capture()
        overrides = self._overrides(manifest)
        preview = build_restore_preview(self.package_dir, manifest, custom_overrides=overrides)

        self.assertEqual(len(preview.unresolved_items), 0)
        self.assertEqual(len(preview.items), 2)
        total_files = sum(i.file_count for i in preview.items)
        self.assertEqual(total_files, 3)

    def test_restore_preview_flags_unresolved_custom_item(self):
        manifest = self._scan_plan_capture()
        # No overrides supplied -- CUSTOM items fall back to their source
        # path and are flagged unresolved.
        preview = build_restore_preview(self.package_dir, manifest)
        self.assertEqual(len(preview.unresolved_items), 2)
        for item in preview.items:
            self.assertFalse(item.destination_resolved)
            self.assertIsNotNone(item.destination_warning)

    def test_restore_writes_all_files_no_conflicts(self):
        manifest = self._scan_plan_capture()
        overrides = self._overrides(manifest)

        summary = run_restore(self.package_dir, manifest, conflict_policy="replace", custom_overrides=overrides)
        self.assertEqual(summary.restored, 3)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.blocked_items, [])

        restored_files = []
        for root, _, files in os.walk(self.dest_dir):
            for fname in files:
                self.assertFalse(fname.endswith(RESTORE_STAGING_SUFFIX))
                restored_files.append(os.path.join(root, fname))
        self.assertEqual(len(restored_files), 3)

        with open(os.path.join(self.dest_dir, "Documents", "a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"hello world")

    def test_restore_is_resumable(self):
        manifest = self._scan_plan_capture()
        overrides = self._overrides(manifest)

        first = run_restore(self.package_dir, manifest, conflict_policy="replace", custom_overrides=overrides)
        self.assertEqual(first.restored, 3)

        second = run_restore(self.package_dir, manifest, conflict_policy="replace", custom_overrides=overrides)
        self.assertEqual(second.restored, 0)
        self.assertEqual(second.already_done, 3)

    def test_conflict_policy_skip(self):
        manifest = self._scan_plan_capture()
        overrides = self._overrides(manifest)

        os.makedirs(os.path.join(self.dest_dir, "Documents"), exist_ok=True)
        existing_path = os.path.join(self.dest_dir, "Documents", "a.txt")
        self._write(existing_path, b"PRE-EXISTING CONTENT")

        summary = run_restore(self.package_dir, manifest, conflict_policy="skip", custom_overrides=overrides)
        self.assertEqual(summary.skipped_policy, 1)
        self.assertEqual(summary.restored, 2)

        with open(existing_path, "rb") as f:
            self.assertEqual(f.read(), b"PRE-EXISTING CONTENT")

    def test_conflict_policy_replace(self):
        manifest = self._scan_plan_capture()
        overrides = self._overrides(manifest)

        os.makedirs(os.path.join(self.dest_dir, "Documents"), exist_ok=True)
        existing_path = os.path.join(self.dest_dir, "Documents", "a.txt")
        self._write(existing_path, b"PRE-EXISTING CONTENT")

        summary = run_restore(self.package_dir, manifest, conflict_policy="replace", custom_overrides=overrides)
        self.assertEqual(summary.restored, 3)

        with open(existing_path, "rb") as f:
            self.assertEqual(f.read(), b"hello world")

    def test_conflict_policy_replace_if_newer(self):
        manifest = self._scan_plan_capture()
        overrides = self._overrides(manifest)

        os.makedirs(os.path.join(self.dest_dir, "Documents"), exist_ok=True)
        existing_path = os.path.join(self.dest_dir, "Documents", "a.txt")
        self._write(existing_path, b"NEWER THAN MIGRATED FILE")
        # Ensure the existing destination file's mtime is unambiguously
        # after the source file's recorded modified_utc.
        future = time.time() + 3600
        os.utime(existing_path, (future, future))

        summary = run_restore(
            self.package_dir, manifest, conflict_policy="replace_if_newer", custom_overrides=overrides
        )
        self.assertEqual(summary.skipped_policy, 1)
        self.assertEqual(summary.restored, 2)

        with open(existing_path, "rb") as f:
            self.assertEqual(f.read(), b"NEWER THAN MIGRATED FILE")

    def test_conflict_policy_keep_both(self):
        manifest = self._scan_plan_capture()
        overrides = self._overrides(manifest)

        os.makedirs(os.path.join(self.dest_dir, "Documents"), exist_ok=True)
        existing_path = os.path.join(self.dest_dir, "Documents", "a.txt")
        self._write(existing_path, b"PRE-EXISTING CONTENT")

        summary = run_restore(self.package_dir, manifest, conflict_policy="keep_both", custom_overrides=overrides)
        self.assertEqual(summary.restored, 3)
        self.assertEqual(summary.conflicts_renamed, 1)

        # Original file untouched...
        with open(existing_path, "rb") as f:
            self.assertEqual(f.read(), b"PRE-EXISTING CONTENT")

        # ...and the migrated file landed under a disambiguated name.
        siblings = os.listdir(os.path.join(self.dest_dir, "Documents"))
        renamed = [n for n in siblings if n != "a.txt" and n.startswith("a - migrated")]
        self.assertEqual(len(renamed), 1)
        with open(os.path.join(self.dest_dir, "Documents", renamed[0]), "rb") as f:
            self.assertEqual(f.read(), b"hello world")

    def test_blocked_item_is_reported_and_not_silently_written(self):
        manifest = self._scan_plan_capture()
        # Only override one of the two CUSTOM items -- the other should be
        # blocked, not guessed at.
        one_item = manifest.items[0]
        overrides = {one_item.logical_folder: os.path.join(self.dest_dir, "OnlyOne")}

        summary = run_restore(self.package_dir, manifest, conflict_policy="replace", custom_overrides=overrides)
        self.assertEqual(len(summary.blocked_items), 1)
        self.assertNotIn(one_item.logical_folder, summary.blocked_items)

    def test_restore_status_reports_counts(self):
        manifest = self._scan_plan_capture()
        overrides = self._overrides(manifest)
        run_restore(self.package_dir, manifest, conflict_policy="replace", custom_overrides=overrides)

        status = restore_status(self.package_dir)
        self.assertEqual(status.get("restored"), 3)

    def test_verify_restore_detects_destination_corruption(self):
        manifest = self._scan_plan_capture()
        overrides = self._overrides(manifest)
        run_restore(self.package_dir, manifest, conflict_policy="replace", custom_overrides=overrides)

        target = os.path.join(self.dest_dir, "Pictures", "photo.jpg")
        with open(target, "ab") as f:
            f.write(b"corruption-after-restore")

        summary = verify_restore(self.package_dir, level="full")
        self.assertEqual(summary.checked, 3)
        self.assertEqual(summary.failed, 1)
        self.assertTrue(any("photo.jpg" in (p["path"] or "") for p in summary.problems))

    def test_verify_restore_detects_missing_destination_file(self):
        manifest = self._scan_plan_capture()
        overrides = self._overrides(manifest)
        run_restore(self.package_dir, manifest, conflict_policy="replace", custom_overrides=overrides)

        os.remove(os.path.join(self.dest_dir, "Documents", "a.txt"))

        summary = verify_restore(self.package_dir, level="fast")
        self.assertEqual(summary.missing, 1)


if __name__ == "__main__":
    unittest.main()
