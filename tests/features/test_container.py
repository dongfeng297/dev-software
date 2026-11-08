"""Tests for the self-implemented BNAR single-file container."""
import os
from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backnest import container
from support import WorkspaceTemporaryDirectory


def tree(path: Path) -> dict:
    return {
        str(item.relative_to(path)): ("dir", b"") if item.is_dir() else ("file", item.read_bytes())
        for item in path.rglob("*")
    }


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemporaryDirectory(prefix="container-")
        self.root = Path(self.temp.name)
        self.source = self.root / "源 目录"
        self.source.mkdir()
        (self.source / "空目录").mkdir()
        (self.source / "子目录" / "更深").mkdir(parents=True)
        (self.source / "空.txt").touch()
        (self.source / "中文 文件.txt").write_text("你好，BackNest\n", encoding="utf-8")
        (self.source / "子目录" / "random.bin").write_bytes(os.urandom(120_000))

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_preserves_tree_bytes_and_metadata(self):
        archive = self.root / "bundle.bkn"
        result = container.pack(self.source, archive)
        self.assertTrue(result["verified"])
        self.assertGreater(result["files"], 0)
        entries = container.list_entries(archive)
        self.assertEqual(len(entries), result["files"] + result["directories"])
        self.assertTrue(all(entry.path == entry.path.replace("\\", "/") for entry in entries))
        destination = self.root / "解包 目录"
        unpacked = container.unpack(archive, destination)
        self.assertTrue(unpacked["verified"])
        self.assertEqual(tree(self.source), tree(destination))
        source_mtime = (self.source / "中文 文件.txt").stat().st_mtime_ns
        output_mtime = (destination / "中文 文件.txt").stat().st_mtime_ns
        self.assertEqual(source_mtime, output_mtime)

    def test_empty_directory_container(self):
        empty = self.root / "empty"
        empty.mkdir()
        archive = self.root / "empty.bkn"
        container.pack(empty, archive)
        self.assertEqual(container.list_entries(archive), [])
        target = self.root / "empty-out"
        container.unpack(archive, target)
        self.assertEqual(list(target.iterdir()), [])

    def test_existing_destination_and_nonempty_target_rejected_without_partials(self):
        archive = self.root / "bundle.bkn"
        container.pack(self.source, archive)
        with self.assertRaises(container.ContainerError):
            container.pack(self.source, archive)
        self.assertFalse(list(self.root.glob("*.partial-*")))
        occupied = self.root / "occupied"
        occupied.mkdir()
        (occupied / "keep.txt").write_text("keep")
        with self.assertRaises(container.ContainerError):
            container.unpack(archive, occupied)
        self.assertEqual((occupied / "keep.txt").read_text(), "keep")

    def test_corruption_trailer_and_truncation_are_detected(self):
        archive = self.root / "bundle.bkn"
        container.pack(self.source, archive)
        original = archive.read_bytes()
        variants = {
            "header": b"XXXX" + original[4:],
            "trailer": original[:-1] + b"X",
            "truncated": original[: len(original) // 2],
        }
        for name, data in variants.items():
            with self.subTest(name=name):
                damaged = self.root / (name + ".bkn")
                damaged.write_bytes(data)
                with self.assertRaises(container.ContainerError):
                    container.unpack(damaged, self.root / (name + "-out"))
                self.assertFalse((self.root / (name + "-out")).exists())

    def test_payload_crc_mismatch_is_detected(self):
        archive = self.root / "bundle.bkn"
        container.pack(self.source, archive)
        data = bytearray(archive.read_bytes())
        index = data.find(b"BackNest")
        self.assertGreater(index, 0)
        data[index] ^= 0x01
        damaged = self.root / "crc.bkn"
        damaged.write_bytes(data)
        with self.assertRaises(container.ContainerError):
            container.unpack(damaged, self.root / "crc-out")
        self.assertFalse((self.root / "crc-out").exists())

    def test_unsafe_entry_paths_are_rejected(self):
        for value in ["../escape", "/absolute", "C:/drive", "a\\b", "a//b", "a/./b", "CON", "trailing. "]:
            with self.subTest(value=value):
                with self.assertRaises(container.ContainerError):
                    container._check_relative(value)

    def test_max_output_bytes_is_enforced(self):
        archive = self.root / "bundle.bkn"
        container.pack(self.source, archive)
        with self.assertRaises(container.ContainerError):
            container.unpack(archive, self.root / "limited", max_output_bytes=10)
        self.assertFalse((self.root / "limited").exists())

    def test_entry_metadata_is_serializable(self):
        archive = self.root / "bundle.bkn"
        container.pack(self.source, archive)
        metadata = container.entry_to_metadata(container.list_entries(archive)[0])
        self.assertEqual(metadata["kind"], "directory")
        self.assertIn("path", metadata)
        self.assertIn("bytes", metadata)

    def test_archive_algorithm_is_marked_self_implemented(self):
        self.assertTrue(container.ALGORITHMS[0]["selfImplemented"])
        source = (ROOT / "backnest" / "container.py").read_text(encoding="utf-8")
        for forbidden in ["import zipfile", "import tarfile", "import gzip", "subprocess"]:
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
