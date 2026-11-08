"""Tests for the Python compatibility engine and incremental snapshots."""
import os
from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backnest import engine
from support import WorkspaceTemporaryDirectory


def tree(path: Path) -> dict:
    return {
        str(item.relative_to(path)): ("dir", b"") if item.is_dir() else ("file", item.read_bytes())
        for item in path.rglob("*")
    }


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemporaryDirectory(prefix="engine-")
        self.root = Path(self.temp.name)
        self.source = self.root / "源 目录"
        self.source.mkdir()
        (self.source / "空目录").mkdir()
        (self.source / "子目录").mkdir()
        (self.source / "空.txt").touch()
        (self.source / "中文 文件.txt").write_text("hello 世界\n", encoding="utf-8")
        (self.source / "子目录" / "data.bin").write_bytes(os.urandom(250_000))
        self.backups = self.root / "backups"

    def tearDown(self):
        self.temp.cleanup()

    def test_full_backup_verify_restore(self):
        result = engine.backup(self.source, self.backups)
        self.assertTrue(result["verified"])
        self.assertEqual(result["files"], 3)
        snapshot = Path(result["path"])
        manifest = engine.verify(snapshot)
        self.assertEqual(manifest.files, 3)
        destination = self.root / "restored"
        restored = engine.restore(snapshot, destination)
        self.assertTrue(restored["verified"])
        self.assertEqual(tree(self.source), tree(destination))
        self.assertFalse(list(self.backups.glob(".partial-*")))

    def test_incremental_hardlinks_unchanged_and_keeps_versions_restorable(self):
        first = engine.backup(self.source, self.backups)
        (self.source / "中文 文件.txt").write_text("changed\n", encoding="utf-8")
        (self.source / "new.txt").write_text("new\n", encoding="utf-8")
        (self.source / "空.txt").unlink()
        preview = engine.incremental_preview(self.source, self.backups)
        self.assertEqual([item["path"] for item in preview["added"]], ["new.txt"])
        self.assertEqual([item["path"] for item in preview["changed"]], ["中文 文件.txt"])
        self.assertEqual([item["path"] for item in preview["deleted"]], ["空.txt"])
        second = engine.backup(self.source, self.backups, incremental=True)
        unchanged = Path(second["path"]) / "data" / "子目录" / "data.bin"
        self.assertGreaterEqual(unchanged.stat().st_nlink, 2)
        for snapshot in (first["path"], second["path"]):
            engine.verify(snapshot)
        shutil.rmtree(first["path"])
        destination = self.root / "after-base-delete"
        engine.restore(second["path"], destination)
        self.assertEqual((destination / "子目录" / "data.bin").read_bytes(),
                         (self.source / "子目录" / "data.bin").read_bytes())

    def test_corruption_missing_and_extra_entries_rejected(self):
        snapshot = Path(engine.backup(self.source, self.backups)["path"])
        (snapshot / "data" / "中文 文件.txt").write_bytes(b"damaged")
        with self.assertRaises(engine.EngineError):
            engine.verify(snapshot)
        (snapshot / "data" / "extra").write_text("x")
        with self.assertRaises(engine.EngineError):
            engine.verify(snapshot)

    def test_non_empty_restore_target_is_not_modified(self):
        snapshot = Path(engine.backup(self.source, self.backups)["path"])
        target = self.root / "occupied"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        with self.assertRaises(engine.EngineError):
            engine.restore(snapshot, target)
        self.assertEqual((target / "keep.txt").read_text(), "keep")
        self.assertEqual(list(target.iterdir()), [target / "keep.txt"])

    def test_empty_source_and_empty_restore_target(self):
        empty = self.root / "empty"
        empty.mkdir()
        result = engine.backup(empty, self.root / "empty-backups")
        self.assertEqual((result["files"], result["directories"]), (0, 0))
        target = self.root / "empty-target"
        target.mkdir()
        engine.restore(result["path"], target)
        self.assertEqual(list(target.iterdir()), [])

    def test_source_and_destination_overlap_rejected(self):
        with self.assertRaises(engine.EngineError):
            engine.backup(self.source, self.source / "backups")
        self.assertFalse((self.source / "backups").exists())

    def test_manifest_is_backnest_v1_and_rejects_traversal(self):
        snapshot = Path(engine.backup(self.source, self.backups)["path"])
        text = (snapshot / "manifest.bkn").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("BACKNEST 1\n"))
        self.assertIn("D \"空目录\"", text)
        manifest = snapshot / "manifest.bkn"
        manifest.write_text('BACKNEST 1\n1 0 0\n"source"\nF 0 0 "../escaped.txt"\n', encoding="utf-8")
        with self.assertRaises(engine.EngineError):
            engine.verify(snapshot)
        manifest.write_text('BACKNEST 99\n0 0 0\n"source"\n', encoding="utf-8")
        with self.assertRaises(engine.EngineError):
            engine.verify(snapshot)

    def test_read_manifest_accepts_std_quoted_escapes(self):
        self.assertEqual(engine._unquote('"a\\"b.txt"'), 'a"b.txt')
        self.assertEqual(engine._unquote('"a\\\\b.txt"'), "a\\b.txt")
        tokens, quoted = engine._split_quoted('F 5 123 "a b.txt"', 3)
        self.assertEqual(tokens, ["F", "5", "123"])
        self.assertEqual(engine._unquote(quoted), "a b.txt")

    def test_progress_and_cancel(self):
        events = []
        result = engine.backup(self.source, self.backups,
                               progress=lambda phase, done, total, files, total_files, path: events.append((phase, path)))
        self.assertTrue(events)
        self.assertIn("copying", {phase for phase, _path in events})
        snapshot = Path(result["path"])
        import threading
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(engine.EngineError):
            engine.verify(snapshot, cancel=cancel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
