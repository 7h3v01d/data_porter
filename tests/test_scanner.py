import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data_porter.core.models import FolderOrigin, SkipReason
from data_porter.core.scanner import scan_folder


class TestScanFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dp_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel_path: str, content: bytes = b"hello"):
        full = os.path.join(self.tmp, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(content)
        return full

    def test_basic_count_and_size(self):
        self._write("a.txt", b"12345")
        self._write("sub/b.txt", b"1234567890")
        result = scan_folder(self.tmp, "Documents", FolderOrigin.KNOWN_FOLDER)

        self.assertTrue(result.exists)
        self.assertEqual(result.file_count, 2)
        self.assertEqual(result.total_bytes, 15)
        self.assertIsNone(result.error)

    def test_missing_folder_reports_error(self):
        result = scan_folder(
            os.path.join(self.tmp, "does-not-exist"), "Documents", FolderOrigin.KNOWN_FOLDER
        )
        self.assertFalse(result.exists)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.file_count, 0)

    def test_largest_files_ordering(self):
        self._write("small.txt", b"x" * 10)
        self._write("big.txt", b"x" * 1000)
        self._write("medium.txt", b"x" * 100)
        result = scan_folder(self.tmp, "Documents", FolderOrigin.KNOWN_FOLDER, top_n=2)

        self.assertEqual(len(result.largest_files), 2)
        sizes = [f.size_bytes for f in result.largest_files]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(sizes[0], 1000)

    def test_symlink_is_skipped_not_followed(self):
        real_dir = os.path.join(self.tmp, "real")
        os.makedirs(real_dir)
        self._write("real/inside.txt", b"data")

        link_dir = os.path.join(self.tmp, "link_to_real")
        try:
            os.symlink(real_dir, link_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported in this environment")

        result = scan_folder(self.tmp, "Documents", FolderOrigin.KNOWN_FOLDER)

        # Only the real file should be counted once, not doubled via the link.
        self.assertEqual(result.file_count, 1)
        self.assertGreaterEqual(result.reparse_points_skipped, 1)
        reasons = [s.reason for s in result.skipped]
        self.assertIn(SkipReason.REPARSE_POINT, reasons)

    def test_empty_folder(self):
        result = scan_folder(self.tmp, "Documents", FolderOrigin.KNOWN_FOLDER)
        self.assertTrue(result.exists)
        self.assertEqual(result.file_count, 0)
        self.assertEqual(result.total_bytes, 0)
        self.assertEqual(result.largest_files, [])


if __name__ == "__main__":
    unittest.main()
