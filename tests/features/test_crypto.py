"""Tests for :mod:`backnest.crypto`.

The DSH sandbox on this Windows host denies writes inside directories created
with mode 0o700 (which is what ``tempfile.TemporaryDirectory`` uses), so these
tests create their scratch directories manually with the default mode.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backnest import crypto  # noqa: E402  (path setup must run first)

TEMP_BASE = ROOT / ".backnest" / "test-tmp"

# Generated once with the bundled Node.js v24.21.0:
#   crypto.pbkdf2Sync(password, salt, 120000, 32, "sha256")
#   crypto.createCipheriv("aes-256-gcm", key, nonce); setAAD(aad)
# The AAD is the 32-byte BNE1 header for this plaintext.  Node is not invoked
# by this test; the vector is static.
NODE_VECTOR = {
    "password": "correct horse battery staple",
    "salt": "00112233445566778899aabbccddeeff",
    "iterations": 120000,
    "key": "7a474ded296075ecfe01f963746072bcf1df588e3dcc0f6cb6cfb2fac68573ba",
    "nonce": "0b0a09080706050403020100",
    "plaintext": "4261636b4e65737420e4b8ade696872047434d20766563746f720a",
    "aad": "424e453101010100c0d4010010000c0010001b00000000000000000000000000",
    "ciphertext": "6117e8a1b713222443637ae4275193a56f656a7b6eb5413671117c",
    "tag": "944ef62df1a9947e0aa13718d62fd43f",
}


def make_temp_dir(prefix: str = "crypto-") -> Path:
    """Create a scratch directory with the default (sandbox-friendly) mode."""
    TEMP_BASE.mkdir(parents=True, exist_ok=True)
    path = TEMP_BASE / (prefix + uuid.uuid4().hex)
    path.mkdir()  # deliberately no mode=0o700
    return path


class CryptoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir()
        self.source = self.root / "source.bin"
        self.encrypted = self.root / "source.bne1"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def encrypt_sample(
        self,
        data: bytes,
        *,
        password="test-password-123",
        iterations: int = 10_000,
        destination: Path | None = None,
    ) -> tuple[Path, Path]:
        self.source.write_bytes(data)
        destination = destination or self.encrypted
        crypto.encrypt_file(
            self.source,
            destination,
            password,
            iterations=iterations,
        )
        return self.source, destination

    def assert_no_partials(self) -> None:
        self.assertEqual(list(self.root.glob("*partial*")), [])

    def round_trip(self, data: bytes, password="test-password-123") -> dict:
        self.source.write_bytes(data)
        encrypt_result = crypto.encrypt_file(
            self.source, self.encrypted, password, iterations=10_000
        )
        self.assertEqual(encrypt_result["files"], 1)
        self.assertEqual(encrypt_result["directories"], 0)
        self.assertTrue(encrypt_result["verified"])

        restored = self.root / "restored.bin"
        decrypt_result = crypto.decrypt_file(self.encrypted, restored, password)
        self.assertEqual(restored.read_bytes(), data)
        self.assertEqual(decrypt_result["path"], str(restored))
        self.assertEqual(decrypt_result["bytes"], len(data))
        self.assertEqual(decrypt_result["files"], 1)
        self.assertEqual(decrypt_result["directories"], 0)
        self.assertTrue(decrypt_result["verified"])
        self.assert_no_partials()
        return decrypt_result

    # -- backend and format ---------------------------------------------

    @unittest.skipUnless(sys.platform == "win32", "Windows CNG check")
    def test_available_is_true_with_cng_on_windows(self):
        available, backend = crypto.available()
        self.assertTrue(available, backend)
        self.assertIn("CNG", backend)

    def test_header_fields_and_exact_file_length(self):
        data = b"format check"
        _, encrypted = self.encrypt_sample(data)
        blob = encrypted.read_bytes()

        self.assertEqual(blob[:4], b"BNE1")
        fields = crypto.HEADER.unpack(blob[:crypto.HEADER_SIZE])
        self.assertEqual(
            fields,
            (
                b"BNE1",
                1,
                1,
                1,
                0,
                10_000,
                16,
                12,
                16,
                len(data),
            ),
        )
        self.assertEqual(blob[26:32], b"\x00" * 6)
        self.assertEqual(
            len(blob),
            crypto.HEADER_SIZE
            + crypto.SALT_LENGTH
            + crypto.NONCE_LENGTH
            + len(data)
            + crypto.TAG_LENGTH,
        )

    # -- round trips -----------------------------------------------------

    def test_round_trip_empty_file(self):
        self.round_trip(b"")
        self.assertEqual(
            self.encrypted.stat().st_size,
            crypto.HEADER_SIZE
            + crypto.SALT_LENGTH
            + crypto.NONCE_LENGTH
            + crypto.TAG_LENGTH,
        )

    def test_round_trip_one_byte(self):
        self.round_trip(b"\x00")

    def test_round_trip_chinese_text(self):
        self.round_trip("备份测试：中文内容\n第二行\n".encode("utf-8"))

    def test_round_trip_one_mib_random(self):
        self.round_trip(os.urandom(1024 * 1024))

    def test_password_accepts_bytes(self):
        self.round_trip(b"bytes password content", password=b"pw\x00\xff-with-bytes")

    # -- authentication failures ----------------------------------------

    def test_wrong_password_fails_without_creating_output_or_leaking_password(self):
        destination = self.root / "wrong-password.out"
        correct = "correct-unique-password"
        wrong = "wrong-unique-password"

        self.encrypt_sample(
            b"authenticated secret", password=correct, destination=self.encrypted
        )
        with self.assertRaises(crypto.CryptoError) as caught:
            crypto.decrypt_file(self.encrypted, destination, wrong)
        message = str(caught.exception)
        self.assertIn("password", message.lower())
        self.assertIn("tampered", message.lower())
        self.assertNotIn(correct, message)
        self.assertNotIn(wrong, message)
        self.assertFalse(destination.exists())
        self.assert_no_partials()

    def test_tampered_salt_nonce_ciphertext_tag_and_header_fail(self):
        self.encrypt_sample(b"tamper target payload")
        blob = bytearray(self.encrypted.read_bytes())
        cases = {
            "salt": crypto.HEADER_SIZE,
            "nonce": crypto.HEADER_SIZE + crypto.SALT_LENGTH,
            "ciphertext": crypto.HEADER_SIZE + crypto.SALT_LENGTH + crypto.NONCE_LENGTH,
            "tag": len(blob) - 1,
            "header_iterations": 8,
            "header_magic": 0,
            "header_padding": 26,
        }
        for name, offset in cases.items():
            with self.subTest(name=name):
                tampered = bytearray(blob)
                tampered[offset] ^= 0x01
                tampered_path = self.root / f"tampered-{name}.bne1"
                tampered_path.write_bytes(tampered)
                destination = self.root / f"tampered-{name}.out"
                with self.assertRaises(crypto.CryptoError) as caught:
                    crypto.decrypt_file(tampered_path, destination, "test-password-123")
                message = str(caught.exception).lower()
                self.assertTrue("password" in message or "tampered" in message, message)
                self.assertFalse(destination.exists())
        self.assert_no_partials()

    def test_truncated_and_trailing_data_fail(self):
        self.encrypt_sample(b"truncation target")
        blob = self.encrypted.read_bytes()
        variants = {
            "one_byte_missing": blob[:-1],
            "header_only": blob[:12],
            "trailing_byte": blob + b"\x00",
        }
        for name, contents in variants.items():
            with self.subTest(name=name):
                path = self.root / f"{name}.bne1"
                path.write_bytes(contents)
                destination = self.root / f"{name}.out"
                with self.assertRaises(crypto.CryptoError):
                    crypto.decrypt_file(path, destination, "test-password-123")
                self.assertFalse(destination.exists())
        self.assert_no_partials()

    # -- destination safety and limits ----------------------------------

    def test_existing_destination_is_rejected_without_partials(self):
        self.source.write_bytes(b"source data")
        existing = self.root / "existing.bin"
        existing.write_bytes(b"keep me")
        with self.assertRaises(crypto.CryptoError):
            crypto.encrypt_file(
                self.source, existing, "test-password-123", iterations=10_000
            )
        self.assertEqual(existing.read_bytes(), b"keep me")
        self.assert_no_partials()

        self.encrypt_sample(b"source data")
        existing_restore = self.root / "existing-restore.bin"
        existing_restore.write_bytes(b"keep restore")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_file(
                self.encrypted, existing_restore, "test-password-123"
            )
        self.assertEqual(existing_restore.read_bytes(), b"keep restore")
        self.assert_no_partials()

    def test_destination_directory_and_missing_parent_are_rejected(self):
        self.source.write_bytes(b"source data")
        target_directory = self.root / "target-directory"
        target_directory.mkdir()
        with self.assertRaises(crypto.CryptoError):
            crypto.encrypt_file(
                self.source, target_directory, "test-password-123", iterations=10_000
            )
        with self.assertRaises(crypto.CryptoError):
            crypto.encrypt_file(
                self.source,
                self.root / "missing-parent" / "output.bne1",
                "test-password-123",
                iterations=10_000,
            )
        self.assertFalse((self.root / "missing-parent").exists())
        self.assert_no_partials()

    def test_max_output_bytes_rejects_large_plaintext(self):
        data = os.urandom(1024 * 1024)
        self.encrypt_sample(data)

        rejected = self.root / "rejected.out"
        with self.assertRaises(crypto.CryptoError) as caught:
            crypto.decrypt_file(
                self.encrypted, rejected, "test-password-123", max_output_bytes=1024
            )
        self.assertIn("max_output_bytes", str(caught.exception))
        self.assertFalse(rejected.exists())
        self.assert_no_partials()

        unlimited = self.root / "unlimited.out"
        result = crypto.decrypt_file(
            self.encrypted, unlimited, "test-password-123", max_output_bytes=0
        )
        self.assertEqual(result["bytes"], len(data))
        self.assertEqual(unlimited.read_bytes(), data)

        exact = self.root / "exact.out"
        result = crypto.decrypt_file(
            self.encrypted,
            exact,
            "test-password-123",
            max_output_bytes=len(data),
        )
        self.assertEqual(result["bytes"], len(data))

        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_file(
                self.encrypted,
                self.root / "negative.out",
                "test-password-123",
                max_output_bytes=-1,
            )

    # -- key derivation and validation ----------------------------------

    def test_derive_key_matches_hashlib_reference(self):
        salt = bytes(range(16))
        for password in ("pässwörd-测试", b"binary-password\x00\xff"):
            with self.subTest(password=password):
                reference = hashlib.pbkdf2_hmac(
                    "sha256",
                    password.encode("utf-8") if isinstance(password, str) else password,
                    salt,
                    12_345,
                    dklen=32,
                )
                self.assertEqual(
                    crypto.derive_key(password, salt, 12_345), reference
                )

    def test_iterations_validation(self):
        bad_values = (0, 9_999, 2_000_001, -1, True, False, 10_000.0, "10000", None)
        self.source.write_bytes(b"iterations")
        for bad in bad_values:
            with self.subTest(iterations=bad):
                with self.assertRaises(crypto.CryptoError):
                    crypto.derive_key("password", b"salt", bad)
                with self.assertRaises(crypto.CryptoError):
                    crypto.encrypt_file(
                        self.source,
                        self.root / "never.bne1",
                        "password",
                        iterations=bad,
                    )
                self.assertFalse((self.root / "never.bne1").exists())
        # Both boundaries are accepted.
        self.assertEqual(len(crypto.derive_key("password", b"salt", 10_000)), 32)
        self.assertEqual(len(crypto.derive_key("password", b"salt", 2_000_000)), 32)

    def test_empty_or_invalid_password_is_rejected(self):
        self.source.write_bytes(b"password validation")
        for password in ("", b"", bytearray()):
            with self.subTest(password=password):
                with self.assertRaises(crypto.CryptoError):
                    crypto.derive_key(password, b"salt", 10_000)
                with self.assertRaises(crypto.CryptoError):
                    crypto.encrypt_file(
                        self.source,
                        self.root / "never.bne1",
                        password,
                        iterations=10_000,
                    )
        for password in (None, 123, ["list"]):
            with self.subTest(password=password):
                with self.assertRaises(crypto.CryptoError):
                    crypto.derive_key(password, b"salt", 10_000)

        self.encrypt_sample(b"password validation")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_file(self.encrypted, self.root / "empty.out", "")

    # -- source validation ----------------------------------------------

    def test_missing_directory_and_same_path_sources_are_rejected(self):
        self.source.write_bytes(b"source validation")
        with self.assertRaises(crypto.CryptoError):
            crypto.encrypt_file(
                self.root / "missing.bin",
                self.root / "out.bne1",
                "test-password-123",
                iterations=10_000,
            )
        with self.assertRaises(crypto.CryptoError):
            crypto.encrypt_file(
                self.root, self.root / "out.bne1", "test-password-123", iterations=10_000
            )
        with self.assertRaises(crypto.CryptoError):
            crypto.encrypt_file(
                self.source, self.source, "test-password-123", iterations=10_000
            )
        self.assert_no_partials()

    def test_symlink_source_is_rejected(self):
        self.source.write_bytes(b"symlink source")
        link = self.root / "source-link.bin"
        try:
            os.symlink(self.source, link)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks are unavailable: {error}")
        with self.assertRaises(crypto.CryptoError):
            crypto.encrypt_file(
                link, self.root / "output.bne1", "test-password-123", iterations=10_000
            )
        self.assertFalse((self.root / "output.bne1").exists())
        self.assert_no_partials()

    # -- progress --------------------------------------------------------

    def test_progress_is_monotonic_and_reaches_total(self):
        data = os.urandom(300_000)
        self.source.write_bytes(data)
        encrypt_calls: list[tuple[int, int]] = []

        def encrypt_progress(processed: int, total: int) -> None:
            encrypt_calls.append((processed, total))

        crypto.encrypt_file(
            self.source,
            self.encrypted,
            "test-password-123",
            iterations=10_000,
            progress=encrypt_progress,
        )
        self.assertTrue(encrypt_calls)
        self.assertEqual(encrypt_calls[-1], (len(data), len(data)))
        self.assertEqual(encrypt_calls, sorted(encrypt_calls))
        self.assertTrue(all(0 <= done <= total for done, total in encrypt_calls))

        decrypt_calls: list[tuple[int, int]] = []

        def decrypt_progress(processed: int, total: int) -> None:
            decrypt_calls.append((processed, total))

        restored = self.root / "progress-restored.bin"
        crypto.decrypt_file(
            self.encrypted,
            restored,
            "test-password-123",
            progress=decrypt_progress,
        )
        self.assertTrue(decrypt_calls)
        self.assertEqual(decrypt_calls[-1], (len(data), len(data)))
        self.assertEqual(decrypt_calls, sorted(decrypt_calls))
        self.assertTrue(all(0 <= done <= total for done, total in decrypt_calls))
        self.assertEqual(restored.read_bytes(), data)

    # -- independent implementation vector ------------------------------

    def test_node_aes_gcm_cross_implementation_vector(self):
        vector = NODE_VECTOR
        password = vector["password"]
        salt = bytes.fromhex(vector["salt"])
        iterations = vector["iterations"]
        key = bytes.fromhex(vector["key"])
        nonce = bytes.fromhex(vector["nonce"])
        plaintext = bytes.fromhex(vector["plaintext"])
        aad = bytes.fromhex(vector["aad"])
        ciphertext = bytes.fromhex(vector["ciphertext"])
        tag = bytes.fromhex(vector["tag"])
        sealed = ciphertext + tag

        self.assertEqual(crypto.derive_key(password, salt, iterations), key)
        self.assertEqual(
            crypto.HEADER.pack(
                b"BNE1", 1, 1, 1, 0, iterations, 16, 12, 16, len(plaintext)
            ),
            aad,
        )
        self.assertEqual(crypto._gcm_encrypt(key, nonce, plaintext, aad), sealed)
        self.assertEqual(crypto._gcm_decrypt(key, nonce, sealed, aad), plaintext)
        self.assertEqual(
            crypto._gcm_decrypt(key, nonce, ciphertext, aad, tag=tag), plaintext
        )

        tampered = sealed[:-1] + bytes([sealed[-1] ^ 0x01])
        with self.assertRaises(crypto.CryptoError):
            crypto._gcm_decrypt(key, nonce, tampered, aad)

    # -- implementation provenance --------------------------------------

    def test_no_hand_written_cipher_and_cng_avoids_cryptography(self):
        if sys.platform == "win32":
            available, backend = crypto.available()
            self.assertTrue(available, backend)
            if "CNG" in backend:
                self.assertNotIn("cryptography", sys.modules)

        source = Path(crypto.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for marker in ("sbox", "s-box", "rijndael", "rcon", "aes_tables"):
            self.assertNotIn(marker, lowered)
        # A hand-written AES implementation would need a 256-entry lookup
        # table of two-digit hex constants.
        self.assertNotRegex(source, r"(?:0x[0-9a-fA-F]{2}\s*,\s*){15,}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
