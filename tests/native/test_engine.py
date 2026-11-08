"""Real C++ executable integration tests. Only creates files in temporary directories."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from build import build
from support import WorkspaceTemporaryDirectory


def tree(path):
    return {
        str(item.relative_to(path)): ("directory" if item.is_dir() else hashlib.sha256(item.read_bytes()).hexdigest())
        for item in path.rglob("*")
    }


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.binary = build()
        except RuntimeError as error:
            raise unittest.SkipTest(str(error))

    def setUp(self):
        self.temp = WorkspaceTemporaryDirectory(prefix="backnest-test-")
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "源目录 with spaces"
        self.source.mkdir()
        self.backups = self.root / "backups"

    def tearDown(self):
        self.temp.cleanup()

    def command(self, *args, success=True):
        completed = subprocess.run([str(self.binary)] + list(map(str, args)), capture_output=True, text=True, encoding="utf-8", timeout=30)
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        if success:
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(events[-1]["event"], "result")
        else:
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertEqual(events[-1]["event"], "error")
        return events[-1]

    def sample(self):
        (self.source / "子目录" / "空目录").mkdir(parents=True)
        (self.source / "empty.txt").touch()
        (self.source / "中文 文件.txt").write_text("备份测试\n第二行\n", encoding="utf-8")
        (self.source / 'quotes " and spaces.txt').write_bytes(b"quote test")
        (self.source / "子目录" / "binary.bin").write_bytes(bytes(range(256)) * 8193)

    def test_round_trip_preserves_all_bytes_names_and_empty_directories(self):
        self.sample()
        expected = tree(self.source)
        backup = self.command("backup", self.source, self.backups)
        self.assertEqual(backup["files"], 4)
        self.assertEqual(backup["directories"], 2)
        snapshot = Path(backup["path"])
        self.assertTrue(snapshot.joinpath("manifest.bkn").is_file())
        destination = self.root / "还原目录"
        result = self.command("restore", snapshot, destination)
        self.assertTrue(result["verified"])
        self.assertEqual(tree(destination), expected)
        self.assertEqual(tree(self.source), expected)
        self.assertFalse(list(self.backups.glob(".partial-*")))

    def test_empty_source_and_existing_empty_restore_directory(self):
        snapshot = self.command("backup", self.source, self.backups)
        destination = self.root / "empty-restore"
        destination.mkdir()
        self.command("restore", snapshot["path"], destination)
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])

    def test_versions_are_independent(self):
        file = self.source / "document.txt"
        file.write_text("old")
        first = self.command("backup", self.source, self.backups)
        file.write_text("new")
        second = self.command("backup", self.source, self.backups)
        self.assertNotEqual(first["path"], second["path"])
        for index, (snapshot, expected) in enumerate([(first, "old"), (second, "new")]):
            destination = self.root / str(index)
            self.command("restore", snapshot["path"], destination)
            self.assertEqual((destination / file.name).read_text(), expected)

    def test_crc_matches_independent_standard_implementation(self):
        (self.source / "crc.txt").write_bytes(b"123456789")
        snapshot = self.command("backup", self.source, self.backups)
        manifest = (Path(snapshot["path"]) / "manifest.bkn").read_text()
        self.assertIn(f'F 9 {zlib.crc32(b"123456789")} "crc.txt"', manifest)

    def test_source_and_destination_overlap_rejected_without_writes(self):
        result = self.command("backup", self.source, self.source / "backups", success=False)
        self.assertIn("contain", result["message"])
        self.assertFalse((self.source / "backups").exists())
        self.command("backup", self.source, self.source, success=False)

    def test_nonempty_restore_target_not_modified(self):
        self.sample()
        snapshot = self.command("backup", self.source, self.backups)
        destination = self.root / "existing"
        destination.mkdir()
        (destination / "important.txt").write_text("keep me")
        before = tree(destination)
        self.command("restore", snapshot["path"], destination, success=False)
        self.assertEqual(tree(destination), before)

    def test_corrupted_backup_detected_before_creating_restore_target(self):
        self.sample()
        snapshot = self.command("backup", self.source, self.backups)
        (Path(snapshot["path"]) / "data" / "中文 文件.txt").write_bytes(b"corrupted")
        destination = self.root / "must-not-exist"
        result = self.command("restore", snapshot["path"], destination, success=False)
        self.assertIn("checksum", result["message"])
        self.assertFalse(destination.exists())

    def test_missing_and_extra_payload_entries_rejected(self):
        (self.source / "file.txt").write_text("hello")
        snapshot = Path(self.command("backup", self.source, self.backups)["path"])
        (snapshot / "data" / "extra").mkdir()
        self.command("verify", snapshot, success=False)
        (snapshot / "data" / "extra").rmdir()
        (snapshot / "data" / "file.txt").unlink()
        self.command("verify", snapshot, success=False)

    def test_unsafe_and_truncated_manifests_rejected(self):
        snapshot = self.root / "malicious"
        (snapshot / "data").mkdir(parents=True)
        manifest = snapshot / "manifest.bkn"
        for contents in [
            'BACKNEST 1\n1 0 0\n"source"\nF 0 0 "../escaped.txt"\n',
            'BACKNEST 1\n1 0 0\n"source"\nF 0 0 "/absolute.txt"\n',
            'BACKNEST 1\n1 0 0\n"source"\n',
            'BACKNEST 99\n0 0 0\n"source"\n',
            'BACKNEST 1\n2 0 0\n"source"\nF 0 0 "same"\nF 0 0 "same"\n',
            'BACKNEST 1\n0 0 0\n"source"\ntrailing junk\n',
        ]:
            with self.subTest(contents=contents):
                manifest.write_text(contents)
                self.command("restore", snapshot, self.root / "output", success=False)
                self.assertFalse((self.root / "output").exists())
                self.assertFalse((self.root / "escaped.txt").exists())

    @unittest.skipIf(os.name == "nt", "Symlink privileges vary on Windows")
    def test_symlinks_in_source_and_payload_rejected(self):
        (self.source / "real").write_text("original")
        (self.source / "link").symlink_to("real")
        self.command("backup", self.source, self.backups, success=False)
        (self.source / "link").unlink()
        snapshot = Path(self.command("backup", self.source, self.backups)["path"])
        (snapshot / "data" / "real").unlink()
        (snapshot / "data" / "real").symlink_to(self.source / "real")
        self.command("verify", snapshot, success=False)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_unreadable_source_cleans_partial_version(self):
        if os.geteuid() == 0:
            self.skipTest("Root bypasses file permissions")
        file = self.source / "unreadable"
        file.write_text("private")
        file.chmod(0)
        try:
            self.command("backup", self.source, self.backups, success=False)
            self.assertFalse(list(self.backups.glob("snapshot-*")))
            self.assertFalse(list(self.backups.glob(".partial-*")))
        finally:
            file.chmod(0o600)

    def test_missing_source_and_relative_paths_rejected(self):
        self.command("backup", self.root / "missing", self.backups, success=False)
        self.command("backup", "relative", self.backups, success=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
