"""Tests for the self-implemented BNLZ block LZSS codec.

``zlib`` is imported here only as an independent CRC-32 reference; the codec
module itself must never import a compression library or a subprocess helper.
"""

import ast
import inspect
import io
import random
import shutil
import struct
import sys
import unittest
import uuid
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backnest.compress as codec
from backnest.compress import (
    ALGORITHMS,
    CodecError,
    compress_file,
    compress_stream,
    decompress_file,
    decompress_stream,
)

# The sandbox can deny writes inside owner-only temporary directories, so the
# tests create their scratch directories manually with default permissions.
TEMP_BASE = ROOT / ".backnest" / "test-tmp"


def new_temp_dir(prefix):
    TEMP_BASE.mkdir(parents=True, exist_ok=True)
    while True:
        path = TEMP_BASE / (prefix + uuid.uuid4().hex)
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path


def remove_temp_dir(path):
    shutil.rmtree(path, ignore_errors=True)
    try:
        TEMP_BASE.rmdir()
    except OSError:
        pass


class CompressCodecTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = new_temp_dir("compress-")
        self.addCleanup(remove_temp_dir, self.temp_dir)
        self.src_dir = self.temp_dir / "src"
        self.out_dir = self.temp_dir / "out"
        self.bad_dir = self.temp_dir / "bad"
        for directory in (self.src_dir, self.out_dir, self.bad_dir):
            directory.mkdir()

    # -- helpers ---------------------------------------------------------

    def source(self, data, name="input.bin"):
        path = self.src_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def partials(self, directory=None):
        root = directory or self.temp_dir
        return sorted(
            str(path)
            for path in root.rglob(".*.partial-*")
            if path.is_file()
        )

    def roundtrip(self, data, name="input.bin", block_bytes=65536):
        source = self.source(data, name)
        container = self.out_dir / (name + ".bnlz")
        restored = self.out_dir / (name + ".restored")
        compressed = compress_file(
            source, container, block_bytes=block_bytes
        )
        decompressed = decompress_file(container, restored)
        actual = restored.read_bytes()

        self.assertEqual(actual, data)
        self.assertEqual(
            zlib.crc32(actual) & 0xFFFFFFFF,
            zlib.crc32(data) & 0xFFFFFFFF,
        )
        self.assertEqual(compressed["path"], str(container))
        self.assertEqual(compressed["bytes"], container.stat().st_size)
        self.assertEqual(compressed["files"], 1)
        self.assertEqual(compressed["directories"], 0)
        self.assertIs(compressed["verified"], True)
        self.assertEqual(decompressed["path"], str(restored))
        self.assertEqual(decompressed["bytes"], len(data))
        self.assertEqual(decompressed["files"], 1)
        self.assertEqual(decompressed["directories"], 0)
        self.assertIs(decompressed["verified"], True)
        self.assertEqual(self.partials(), [])
        return container, restored, compressed, decompressed

    def reject_decompression(self, blob, name):
        bad_source = self.bad_dir / name
        bad_source.write_bytes(blob)
        destination = self.out_dir / (name + ".out")
        with self.assertRaises(CodecError):
            decompress_file(bad_source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(self.partials(self.out_dir), [])
        return destination

    # -- API and CRC -----------------------------------------------------

    def test_public_api_and_self_implementation(self):
        self.assertEqual(
            ALGORITHMS,
            [
                {
                    "id": "lzss-v1",
                    "version": "1.0",
                    "selfImplemented": True,
                    "supportedPlatforms": ["windows", "macos", "linux"],
                }
            ],
        )
        self.assertIs(ALGORITHMS[0]["selfImplemented"], True)
        self.assertTrue(issubclass(CodecError, ValueError))

        tree = ast.parse(inspect.getsource(codec))
        forbidden = {"zlib", "gzip", "bz2", "lzma", "subprocess"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        self.assertEqual(imported & forbidden, set())

    def test_crc32_matches_reference(self):
        rng = random.Random(20240601)
        cases = [
            b"",
            b"a",
            bytes(range(256)),
            bytes(range(256)) * 33,
            rng.randbytes(100_000),
        ]
        for data in cases:
            expected = zlib.crc32(data) & 0xFFFFFFFF
            self.assertEqual(codec.crc32(data), expected)
            split = len(data) // 2
            first = codec.crc32(data[:split])
            self.assertEqual(codec.crc32(data[split:], first), expected)

    # -- round trips -----------------------------------------------------

    def test_empty_file_roundtrip(self):
        container, _, _, _ = self.roundtrip(b"", "empty.bin")
        self.assertEqual(
            container.read_bytes(),
            struct.pack("<4sBBHI", b"BNLZ", 1, 0, 0, 65536),
        )
        self.assertEqual(container.stat().st_size, 12)

    def test_single_repeated_byte(self):
        data = b"A" * 100_000
        container, _, _, _ = self.roundtrip(data, "single-byte.bin")
        raw = container.read_bytes()
        self.assertEqual(struct.unpack_from("<B", raw, 12)[0], 1)
        self.assertLess(container.stat().st_size, len(data) // 2)

    def test_single_symbol_long_run(self):
        data = b"Z" * 500_000
        container, _, _, _ = self.roundtrip(data, "long-run.bin")
        self.assertLess(container.stat().st_size, len(data) // 4)

    def test_all_256_byte_values(self):
        data = bytes(range(256)) * 4096
        self.assertEqual(len(data), 1024 * 1024)
        self.roundtrip(data, "all-values.bin")

    def test_utf8_chinese_filenames(self):
        text = "你好，世界！BackNest 备份。\n" * 500
        data = text.encode("utf-8")
        container, restored, _, _ = self.roundtrip(
            data, "备份 数据 中文.bin"
        )
        self.assertIn("中文", container.name)
        self.assertEqual(restored.read_bytes(), data)

    def test_random_2mib_file(self):
        data = random.Random(12345).randbytes(2 * 1024 * 1024)
        container, _, _, _ = self.roundtrip(data, "random-2mib.bin")
        raw = container.read_bytes()
        self.assertEqual(struct.unpack_from("<B", raw, 12)[0], 0)

    def test_incompressible_stored_blocks(self):
        data = random.Random(77).randbytes(512 * 1024)
        container, _, _, _ = self.roundtrip(data, "stored.bin")
        raw = container.read_bytes()

        offset = 12
        block_start = 0
        blocks = 0
        while offset < len(raw):
            mode, original, payload, expected_crc = struct.unpack_from(
                "<BIII", raw, offset
            )
            offset += 13
            self.assertEqual(mode, 0)
            self.assertEqual(original, 65536)
            self.assertEqual(payload, original)
            self.assertEqual(
                expected_crc,
                zlib.crc32(data[block_start:block_start + original])
                & 0xFFFFFFFF,
            )
            offset += payload
            block_start += original
            blocks += 1
        self.assertEqual(blocks, 8)
        self.assertEqual(block_start, len(data))
        self.assertEqual(offset, len(raw))

    def test_highly_repetitive_4mib_file(self):
        data = b"0123456789ABCDEF" * (4 * 1024 * 1024 // 16)
        self.assertEqual(len(data), 4 * 1024 * 1024)
        container, _, _, _ = self.roundtrip(data, "repetitive-4mib.bin")
        self.assertLess(container.stat().st_size, len(data) // 4)
        self.assertEqual(struct.unpack_from("<B", container.read_bytes(), 12)[0], 1)

    def test_header_and_lzss_block_layout(self):
        data = b"ABCD" * 16384
        source = self.source(data, "layout.bin")
        container = self.out_dir / "layout.bnlz"
        compress_file(source, container)
        raw = container.read_bytes()

        magic, version, flags, reserved, block_bytes = struct.unpack_from(
            "<4sBBHI", raw, 0
        )
        self.assertEqual(magic, b"BNLZ")
        self.assertEqual(version, 1)
        self.assertEqual(flags, 0)
        self.assertEqual(reserved, 0)
        self.assertEqual(block_bytes, 65536)

        mode, original, payload, expected_crc = struct.unpack_from(
            "<BIII", raw, 12
        )
        self.assertEqual(mode, 1)
        self.assertEqual(original, 65536)
        self.assertLess(payload, original)
        self.assertEqual(
            expected_crc, zlib.crc32(data[:65536]) & 0xFFFFFFFF
        )
        self.assertEqual(len(raw), 12 + 13 + payload)

    # -- malformed streams ----------------------------------------------

    def test_corrupted_streams_raise_and_leave_no_destination(self):
        data = b"compressible-block-" * 4000
        source = self.source(data, "corrupt-source.bin")
        container = self.out_dir / "corrupt-source.bnlz"
        compress_file(source, container)
        valid = bytearray(container.read_bytes())
        self.assertEqual(struct.unpack_from("<B", valid, 12)[0], 1)

        crc_offset = 12 + 1 + 4 + 4
        payload_offset = 12 + 13

        corrupted_crc = bytearray(valid)
        corrupted_crc[crc_offset] ^= 0x7F
        self.reject_decompression(bytes(corrupted_crc), "bad-crc.bnlz")

        corrupted_payload = bytearray(valid)
        corrupted_payload[payload_offset] ^= 0xFF
        self.reject_decompression(bytes(corrupted_payload), "bad-payload.bnlz")

        self.reject_decompression(bytes(valid[:-7]), "truncated.bnlz")
        self.reject_decompression(bytes(valid[:5]), "short-header.bnlz")
        self.reject_decompression(b"", "empty-stream.bnlz")
        self.reject_decompression(b"NOTL" + bytes(valid[4:]), "bad-magic.bnlz")
        self.reject_decompression(b"BNLZ" + b"\x02" + bytes(valid[5:]), "bad-version.bnlz")

        bad_payload_length = bytearray(valid)
        struct.pack_into("<I", bad_payload_length, 12 + 1 + 4, 0xFFFFFFFF)
        self.reject_decompression(
            bytes(bad_payload_length), "bad-payload-length.bnlz"
        )

        oversized_original = bytearray(valid)
        struct.pack_into("<I", oversized_original, 12 + 1, 0xFFFFFFFF)
        self.reject_decompression(
            bytes(oversized_original), "oversized-original.bnlz"
        )

        bad_mode = bytearray(valid)
        bad_mode[12] = 7
        self.reject_decompression(bytes(bad_mode), "bad-mode.bnlz")

        bad_block_bytes = bytearray(valid)
        struct.pack_into("<I", bad_block_bytes, 8, 0)
        self.reject_decompression(
            bytes(bad_block_bytes), "bad-block-bytes.bnlz"
        )

    # -- destination and source validation ------------------------------

    def test_destination_rejections_and_no_partials(self):
        data = b"destination checks " * 1000
        source = self.source(data, "destination-source.bin")
        container = self.out_dir / "destination-source.bnlz"
        compress_file(source, container)

        existing = self.out_dir / "existing.bnlz"
        existing.write_bytes(b"keep me")
        with self.assertRaises(CodecError):
            compress_file(source, existing)
        self.assertEqual(existing.read_bytes(), b"keep me")
        self.assertEqual(self.partials(self.out_dir), [])

        existing_output = self.out_dir / "existing-output.bin"
        existing_output.write_bytes(b"keep me too")
        with self.assertRaises(CodecError):
            decompress_file(container, existing_output)
        self.assertEqual(existing_output.read_bytes(), b"keep me too")
        self.assertEqual(self.partials(self.out_dir), [])

        directory_target = self.out_dir / "a-directory"
        directory_target.mkdir()
        with self.assertRaises(CodecError):
            compress_file(source, directory_target)
        with self.assertRaises(CodecError):
            decompress_file(container, directory_target)

        with self.assertRaises(CodecError):
            compress_file(source, source)
        self.assertEqual(source.read_bytes(), data)
        with self.assertRaises(CodecError):
            decompress_file(container, container)

        missing_parent = self.temp_dir / "missing" / "output.bnlz"
        with self.assertRaises(CodecError):
            compress_file(source, missing_parent)

        with self.assertRaises(CodecError):
            compress_file(self.src_dir / "missing.bin", self.out_dir / "x.bnlz")
        with self.assertRaises(CodecError):
            compress_file(self.src_dir, self.out_dir / "dir-source.bnlz")

        self.assertEqual(self.partials(), [])

    def test_source_symlink_rejected(self):
        data = b"symlink source"
        source = self.source(data, "symlink-source.bin")
        link = self.src_dir / "source-link.bin"
        try:
            link.symlink_to(source)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are unavailable")

        destination = self.out_dir / "symlink.bnlz"
        with self.assertRaises(CodecError):
            compress_file(link, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(self.partials(), [])

    # -- resource limits -------------------------------------------------

    def test_max_output_bytes(self):
        data = b"0123456789" * 20_000
        source = self.source(data, "limits.bin")
        container = self.out_dir / "limits.bnlz"
        compress_file(source, container, block_bytes=16384)

        too_small = self.out_dir / "too-small.bin"
        with self.assertRaises(CodecError):
            decompress_file(container, too_small, max_output_bytes=50_000)
        self.assertFalse(too_small.exists())
        self.assertEqual(self.partials(self.out_dir), [])

        exact = self.out_dir / "exact.bin"
        result = decompress_file(
            container, exact, max_output_bytes=len(data)
        )
        self.assertEqual(exact.read_bytes(), data)
        self.assertEqual(result["bytes"], len(data))

        stream_output = io.BytesIO()
        with self.assertRaises(CodecError):
            decompress_stream(
                io.BytesIO(container.read_bytes()),
                stream_output,
                max_output_bytes=1_000,
            )
        self.assertLessEqual(len(stream_output.getvalue()), 1_000)

        small_blocks = self.out_dir / "limits-small-blocks.bnlz"
        compress_file(source, small_blocks, block_bytes=1024)
        partial_output = io.BytesIO()
        with self.assertRaises(CodecError):
            decompress_stream(
                io.BytesIO(small_blocks.read_bytes()),
                partial_output,
                max_output_bytes=5_000,
            )
        self.assertGreater(len(partial_output.getvalue()), 0)
        self.assertLessEqual(len(partial_output.getvalue()), 5_000)

        for index, invalid in enumerate(
            [-1, 1.5, True, "10", None]
        ):
            destination = self.out_dir / f"invalid-max-{index}.bin"
            with self.assertRaises(CodecError):
                decompress_file(
                    container, destination, max_output_bytes=invalid
                )
            self.assertFalse(destination.exists())
        self.assertEqual(self.partials(self.out_dir), [])

    def test_atomic_temp_file_during_progress(self):
        data = b"atomic temp file " * 5000
        source = self.source(data, "atomic.bin")
        destination = self.out_dir / "atomic.bnlz"
        observations = []

        def observe(processed, total):
            temp_files = list(
                self.out_dir.glob(f".{destination.name}.partial-*")
            )
            observations.append((destination.exists(), len(temp_files)))

        compress_file(
            source,
            destination,
            block_bytes=4096,
            progress=observe,
        )
        self.assertTrue(observations)
        for destination_existed, temp_count in observations:
            self.assertFalse(destination_existed)
            self.assertEqual(temp_count, 1)
        self.assertTrue(destination.exists())
        self.assertEqual(self.partials(self.out_dir), [])

    def test_failure_cleanup_on_progress_error(self):
        data = b"cleanup " * 5000
        source = self.source(data, "cleanup.bin")
        destination = self.out_dir / "cleanup.bnlz"
        calls = []

        def explode(processed, total):
            calls.append(processed)
            raise CodecError("simulated progress failure")

        with self.assertRaises(CodecError):
            compress_file(
                source,
                destination,
                block_bytes=1024,
                progress=explode,
            )
        self.assertTrue(calls)
        self.assertFalse(destination.exists())
        self.assertEqual(self.partials(self.out_dir), [])

        container = self.out_dir / "cleanup-valid.bnlz"
        compress_file(source, container)
        restored = self.out_dir / "cleanup.restored"
        with self.assertRaises(CodecError):
            decompress_file(container, restored, progress=explode)
        self.assertFalse(restored.exists())
        self.assertEqual(self.partials(self.out_dir), [])

    def test_progress_is_called_and_monotonic(self):
        data = b"progress-data-" * 10_000
        source = self.source(data, "progress.bin")
        container = self.out_dir / "progress.bnlz"

        compress_calls = []
        compress_file(
            source,
            container,
            block_bytes=16384,
            progress=lambda processed, total: compress_calls.append(
                (processed, total)
            ),
        )
        self.assertTrue(compress_calls)
        processed = [value[0] for value in compress_calls]
        self.assertTrue(all(a < b for a, b in zip(processed, processed[1:])))
        self.assertEqual(processed[-1], len(data))
        self.assertTrue(
            all(total == len(data) for _, total in compress_calls)
        )

        restored = self.out_dir / "progress.restored"
        decompress_calls = []
        decompress_file(
            container,
            restored,
            progress=lambda processed, total: decompress_calls.append(
                (processed, total)
            ),
        )
        self.assertTrue(decompress_calls)
        processed = [value[0] for value in decompress_calls]
        self.assertTrue(all(a < b for a, b in zip(processed, processed[1:])))
        self.assertEqual(processed[-1], len(data))
        self.assertTrue(all(total == 0 for _, total in decompress_calls))

        empty_source = self.source(b"", "empty-progress.bin")
        empty_container = self.out_dir / "empty-progress.bnlz"
        empty_calls = []
        compress_file(
            empty_source,
            empty_container,
            progress=lambda processed, total: empty_calls.append(
                (processed, total)
            ),
        )
        self.assertEqual(empty_calls, [(0, 0)])

        empty_restored = self.out_dir / "empty-progress.out"
        empty_decompress_calls = []
        decompress_file(
            empty_container,
            empty_restored,
            progress=lambda processed, total: empty_decompress_calls.append(
                (processed, total)
            ),
        )
        self.assertEqual(empty_decompress_calls, [(0, 0)])
        self.assertEqual(empty_restored.read_bytes(), b"")

    def test_block_bytes_validation(self):
        data = b"block bytes validation " * 1000
        source = self.source(data, "block-bytes.bin")

        for block_bytes in (1024, 1024 * 1024):
            container = self.out_dir / f"valid-{block_bytes}.bnlz"
            restored = self.out_dir / f"valid-{block_bytes}.bin"
            compress_file(source, container, block_bytes=block_bytes)
            decompress_file(container, restored)
            self.assertEqual(restored.read_bytes(), data)

        invalid_values = [0, 1023, 1024 * 1024 + 1, -1, 1.5, True, "1024", None]
        for index, invalid in enumerate(invalid_values):
            destination = self.out_dir / f"invalid-block-{index}.bnlz"
            with self.assertRaises(CodecError):
                compress_file(source, destination, block_bytes=invalid)
            self.assertFalse(destination.exists())
            with self.assertRaises(CodecError):
                compress_stream(
                    io.BytesIO(b"x"),
                    io.BytesIO(),
                    block_bytes=invalid,
                )
        self.assertEqual(self.partials(), [])

    # -- stream API ------------------------------------------------------

    def test_stream_api_roundtrip(self):
        cases = [
            b"",
            b"A",
            b"A" * 1000,
            b"hello world " * 5000,
            bytes(range(256)) * 100,
        ]
        for data in cases:
            compressed = io.BytesIO()
            compress_stream(
                io.BytesIO(data), compressed, block_bytes=1024
            )
            payload = compressed.getvalue()
            self.assertEqual(payload[:4], b"BNLZ")
            self.assertEqual(
                struct.unpack_from("<I", payload, 8)[0], 1024
            )

            compressed.seek(0)
            restored = io.BytesIO()
            decompress_stream(compressed, restored)
            self.assertEqual(restored.getvalue(), data)

    def test_stream_progress_for_unknown_total(self):
        calls = []
        compressed = io.BytesIO()
        compress_stream(
            io.BytesIO(b"stream progress " * 5000),
            compressed,
            block_bytes=1024,
            progress=lambda processed, total: calls.append(
                (processed, total)
            ),
        )
        self.assertTrue(calls)
        self.assertTrue(all(total == 0 for _, total in calls))
        self.assertEqual(
            [processed for processed, _ in calls],
            sorted(processed for processed, _ in calls),
        )


if __name__ == "__main__":
    unittest.main()
