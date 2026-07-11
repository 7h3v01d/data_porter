import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data_porter.core.capture import run_capture
from data_porter.core.manifest import MigrationItem, MigrationManifest
from data_porter.core.models import FolderOrigin, SourceEnvironment
from data_porter.core.package import create_package
from data_porter.core.restore import init_restore_state, run_restore
from data_porter.core.safety import SafetyError
from data_porter.core.verify import verify_package


class TestIntegrityHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dp_hardening_")
        self.source = os.path.join(self.tmp, "source")
        self.package = os.path.join(self.tmp, "package")
        self.dest = os.path.join(self.tmp, "destination")
        os.makedirs(self.source)
        os.makedirs(self.dest)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manifest(self, logical="Documents"):
        return MigrationManifest(
            migration_id="hardening-test",
            source=SourceEnvironment(
                computer_name="SOURCE-PC",
                os_name="Windows 11",
                os_version_raw="test",
                user_name="Dad",
            ),
            items=[
                MigrationItem(
                    logical_folder=logical,
                    source_path=self.source,
                    package_path=f"data/{logical}",
                    origin=FolderOrigin.CUSTOM_SELECTION,
                    restore_target="CUSTOM",
                )
            ],
        )

    def _write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def _captured(self, content=b"original"):
        source_file = os.path.join(self.source, "file.txt")
        self._write(source_file, content)
        manifest = self._manifest()
        create_package(self.package, manifest)
        summary = run_capture(self.package, manifest, hash_level="full")
        self.assertEqual(summary.failed, 0)
        return manifest, source_file

    def test_package_inside_source_is_blocked(self):
        manifest = self._manifest()
        unsafe_package = os.path.join(self.source, "Dad-Migration")
        with self.assertRaises(SafetyError):
            create_package(unsafe_package, manifest)
        self.assertFalse(os.path.exists(unsafe_package))

    def test_modified_source_is_requeued_and_recaptured(self):
        manifest, source_file = self._captured(b"old content")
        package_file = os.path.join(self.package, "data", "Documents", "file.txt")
        with open(package_file, "rb") as handle:
            self.assertEqual(handle.read(), b"old content")

        self._write(source_file, b"new content")
        # Ensure the source identity metadata changes even on coarse filesystems.
        future_ns = time.time_ns() + 2_000_000_000
        os.utime(source_file, ns=(future_ns, future_ns))

        summary = run_capture(self.package, manifest, hash_level="full")
        self.assertEqual(summary.copied, 1)
        self.assertEqual(summary.failed, 0)
        with open(package_file, "rb") as handle:
            self.assertEqual(handle.read(), b"new content")

    def test_full_verify_accepts_recorded_sampled_hash(self):
        source_file = os.path.join(self.source, "large.bin")
        self._write(source_file, b"0123456789" * 100)
        manifest = self._manifest()
        create_package(self.package, manifest)

        with patch("data_porter.core.capture.BALANCED_FULL_HASH_MAX_BYTES", 10):
            captured = run_capture(self.package, manifest, hash_level="balanced")
        self.assertEqual(captured.failed, 0)

        conn = sqlite3.connect(os.path.join(self.package, "package_state.sqlite"))
        try:
            digest, kind = conn.execute("SELECT sha256, hash_kind FROM files").fetchone()
        finally:
            conn.close()
        self.assertTrue(digest.startswith("partial:"))
        self.assertEqual(kind, "sha256_sampled_v1")

        verified = verify_package(self.package, level="full")
        self.assertEqual(verified.failed, 0)
        self.assertEqual(verified.verified, 1)

    def test_interrupted_restore_state_is_reconciled(self):
        manifest, _ = self._captured(b"recover me")
        db_path = os.path.join(self.package, "package_state.sqlite")
        init_restore_state(db_path)

        package_file = os.path.join(self.package, "data", "Documents", "file.txt")
        dest_root = os.path.join(self.dest, "Documents")
        dest_file = os.path.join(dest_root, "file.txt")
        with open(package_file, "rb") as handle:
            package_bytes = handle.read()
        self._write(dest_file, package_bytes)

        conn = sqlite3.connect(db_path)
        try:
            file_id = conn.execute("SELECT id FROM files").fetchone()[0]
            conn.execute(
                "INSERT OR REPLACE INTO restore_state(file_id,dest_path,status,updated_utc) "
                "VALUES(?,?,'restoring','test')",
                (file_id, dest_file),
            )
            conn.commit()
        finally:
            conn.close()

        summary = run_restore(
            self.package,
            manifest,
            conflict_policy="keep_both",
            custom_overrides={"Documents": dest_root},
        )
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.restored, 0)
        self.assertEqual(summary.already_done, 1)
        self.assertEqual(os.listdir(dest_root), ["file.txt"])

    def test_manifest_path_traversal_cannot_escape_destination(self):
        manifest, _ = self._captured(b"do not escape")
        db_path = os.path.join(self.package, "package_state.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE files SET package_rel_path='data/Documents/../../escaped.txt'"
            )
            conn.commit()
        finally:
            conn.close()

        dest_root = os.path.join(self.dest, "Documents")
        summary = run_restore(
            self.package,
            manifest,
            conflict_policy="replace",
            custom_overrides={"Documents": dest_root},
        )
        self.assertEqual(summary.failed, 1)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "escaped.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escaped.txt")))

    def test_existing_package_cannot_be_replaced_by_different_plan(self):
        first = self._manifest()
        create_package(self.package, first)
        second = self._manifest()
        second.migration_id = "different-migration"
        with self.assertRaises(SafetyError):
            create_package(self.package, second)

    def test_failed_destination_verification_is_repaired_on_rerun(self):
        from data_porter.core.restore import verify_restore

        manifest, _ = self._captured(b"correct bytes")
        dest_root = os.path.join(self.dest, "Documents")
        overrides = {"Documents": dest_root}
        first = run_restore(
            self.package,
            manifest,
            conflict_policy="replace",
            custom_overrides=overrides,
        )
        self.assertEqual(first.failed, 0)
        dest_file = os.path.join(dest_root, "file.txt")
        self._write(dest_file, b"corrupted destination")

        check = verify_restore(self.package, level="full")
        self.assertEqual(check.failed, 1)

        # Even a normal 'skip existing' policy must not preserve a file that
        # Data Porter itself has proven corrupt.
        repaired = run_restore(
            self.package,
            manifest,
            conflict_policy="skip",
            custom_overrides=overrides,
        )
        self.assertEqual(repaired.restored, 1)
        with open(dest_file, "rb") as handle:
            self.assertEqual(handle.read(), b"correct bytes")


if __name__ == "__main__":
    unittest.main()
