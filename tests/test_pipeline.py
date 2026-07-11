import os
import sys
import sqlite3
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data_porter.core.capture import run_capture, capture_status, STAGING_SUFFIX
from data_porter.core.manifest import make_unique_names, sanitize_folder_name
from data_porter.core.models import FolderOrigin
from data_porter.core.package import build_migration_plan, create_package, load_manifest
from data_porter.core.report import run_source_scan
from data_porter.core.verify import verify_package


class TestManifestHelpers(unittest.TestCase):
    def test_sanitize_folder_name(self):
        self.assertEqual(sanitize_folder_name("D:\\Family Photos"), "Family Photos")
        self.assertEqual(sanitize_folder_name("Documents"), "Documents")
        self.assertEqual(sanitize_folder_name("/home/dad/Videos"), "Videos")

    def test_make_unique_names(self):
        result = make_unique_names(["Photos", "Photos", "Music", "Photos"])
        self.assertEqual(result, ["Photos", "Photos_2", "Music", "Photos_3"])
        self.assertEqual(len(set(result)), len(result))


class TestFullPipeline(unittest.TestCase):
    """Exercises scan -> plan -> capture -> verify end to end against a
    throwaway source tree, including a simulated interrupted capture."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp(prefix="dp_src_")
        self.package_dir = tempfile.mkdtemp(prefix="dp_pkg_")

        os.makedirs(os.path.join(self.source_dir, "Documents", "Sub"))
        os.makedirs(os.path.join(self.source_dir, "Pictures"))

        self._write(os.path.join(self.source_dir, "Documents", "a.txt"), b"hello world")
        self._write(os.path.join(self.source_dir, "Documents", "Sub", "b.txt"), b"x" * 5000)
        self._write(os.path.join(self.source_dir, "Pictures", "photo.jpg"), b"y" * 20000)

    def tearDown(self):
        shutil.rmtree(self.source_dir, ignore_errors=True)
        shutil.rmtree(self.package_dir, ignore_errors=True)

    def _write(self, path, content):
        with open(path, "wb") as f:
            f.write(content)

    def _scan_and_plan(self):
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
        return report, manifest

    def test_end_to_end_capture_and_verify(self):
        report, manifest = self._scan_and_plan()
        self.assertEqual(len(manifest.items), 2)

        summary = run_capture(self.package_dir, manifest, hash_level="full")
        self.assertEqual(summary.copied, 3)
        self.assertEqual(summary.failed, 0)

        # All real files should now exist, with no leftover staging files.
        copied_files = []
        for root, _, files in os.walk(os.path.join(self.package_dir, "data")):
            for fname in files:
                self.assertFalse(fname.endswith(STAGING_SUFFIX))
                copied_files.append(os.path.join(root, fname))
        self.assertEqual(len(copied_files), 3)

        verify_summary = verify_package(self.package_dir, level="full")
        self.assertEqual(verify_summary.verified, 3)
        self.assertEqual(verify_summary.failed, 0)
        self.assertEqual(verify_summary.missing, 0)

    def test_capture_is_resumable_and_idempotent(self):
        _, manifest = self._scan_and_plan()

        first = run_capture(self.package_dir, manifest, hash_level="fast")
        self.assertEqual(first.copied, 3)

        # Re-running should find nothing left to do.
        second = run_capture(self.package_dir, manifest, hash_level="fast")
        self.assertEqual(second.copied, 0)
        self.assertEqual(second.already_done, 3)

    def test_interrupted_capture_recovers_cleanly(self):
        _, manifest = self._scan_and_plan()
        run_capture(self.package_dir, manifest, hash_level="fast")

        # Simulate a crash mid-copy: mark one row 'copying' again and leave
        # a corrupt/incomplete staged file sitting next to the real one.
        db_path = os.path.join(self.package_dir, "package_state.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE files SET status='copying' WHERE source_path LIKE '%a.txt'")
        conn.commit()
        conn.close()

        real_path = os.path.join(self.package_dir, "data", "Documents", "a.txt")
        staged_path = real_path + STAGING_SUFFIX
        with open(staged_path, "wb") as f:
            f.write(b"garbage-from-a-crashed-run")

        summary = run_capture(self.package_dir, manifest, hash_level="fast")
        self.assertEqual(summary.copied, 1)
        self.assertEqual(summary.failed, 0)

        # The real file must be correct, and the stale staged file gone.
        with open(real_path, "rb") as f:
            self.assertEqual(f.read(), b"hello world")
        self.assertFalse(os.path.exists(staged_path))

    def test_verify_detects_corruption(self):
        _, manifest = self._scan_and_plan()
        run_capture(self.package_dir, manifest, hash_level="full")

        target = os.path.join(self.package_dir, "data", "Pictures", "photo.jpg")
        with open(target, "ab") as f:
            f.write(b"corruption")

        summary = verify_package(self.package_dir, level="full")
        self.assertEqual(summary.failed, 1)
        self.assertTrue(any("photo.jpg" in p["path"] for p in summary.problems))

    def test_capture_records_source_deleted_mid_run(self):
        from data_porter.core.capture import seed_package_files

        _, manifest = self._scan_and_plan()

        # Seed the file inventory first (as capture normally does at the
        # start of a run), *then* remove the source file -- simulating it
        # vanishing partway through a long capture run, after it was
        # already queued but before its turn to be copied.
        seed_package_files(self.package_dir, manifest)
        os.remove(os.path.join(self.source_dir, "Pictures", "photo.jpg"))

        summary = run_capture(self.package_dir, manifest, hash_level="fast", reseed=False)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.copied, 2)

    def test_status_reports_counts(self):
        _, manifest = self._scan_and_plan()
        run_capture(self.package_dir, manifest, hash_level="fast")
        status = capture_status(self.package_dir)
        self.assertIn("copied", status)
        self.assertEqual(status["copied"]["count"], 3)


if __name__ == "__main__":
    unittest.main()
