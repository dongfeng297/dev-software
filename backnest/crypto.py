"""Authenticated file encryption for BackNest.

The product path never implements a cipher itself.  AES-256-GCM comes from the
operating system on Windows (CNG / ``bcrypt.dll``) and from the optional
``cryptography`` package on macOS and Linux.  PBKDF2-HMAC-SHA256 is provided by
Python's :mod:`hashlib`.

On-disk container ``BNE1`` (all integers little-endian)::

    offset  size  field
    0       4     magic b"BNE1"
    4       1     version = 1
    5       1     algorithm = 1 (AES-256-GCM)
    6       1     kdf = 1 (PBKDF2-HMAC-SHA256)
    7       1     reserved = 0
    8       4     PBKDF2 iterations
    12      2     salt length = 16
    14      2     nonce length = 12
    16      2     tag length = 16
    18      8     plaintext length
    26      6     zero padding, making the authenticated header 32 bytes

    Then: salt(16) || nonce(12) || ciphertext(plaintext length) || tag(16).

The complete 32-byte header is passed to AES-GCM as additional authenticated
data (AAD).  Every field and the exact file length are validated before any
plaintext is written.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import secrets
import stat
import struct
import sys
import tempfile
from typing import Callable

ALGORITHMS = [
    {
        "id": "aes-256-gcm",
        "version": "1.0",
        "selfImplemented": False,
        "supportedPlatforms": ["windows", "macos", "linux"],
    }
]

__all__ = [
    "ALGORITHMS",
    "CryptoError",
    "available",
    "derive_key",
    "decrypt_file",
    "encrypt_file",
]

MAGIC = b"BNE1"
VERSION = 1
ALGORITHM_AES_256_GCM = 1
KDF_PBKDF2_HMAC_SHA256 = 1
SALT_LENGTH = 16
NONCE_LENGTH = 12
TAG_LENGTH = 16
KEY_LENGTH = 32
MIN_ITERATIONS = 10_000
MAX_ITERATIONS = 2_000_000
DEFAULT_ITERATIONS = 200_000
COPY_BUFFER = 64 * 1024

# The named fields occupy 26 bytes; six zero bytes pad the header to the
# specified 32-byte authenticated header size.
HEADER = struct.Struct("<4sBBBB I HHH Q 6x")
HEADER_SIZE = HEADER.size
HEADER_FIELDS_SIZE = 26
PADDING = b"\x00" * (HEADER_SIZE - HEADER_FIELDS_SIZE)

_AUTH_ERROR = "Wrong password, or the file was tampered with."
_INSTALL_HINT = (
    "AES-256-GCM on this platform requires the optional 'cryptography' package; "
    "install it with 'python -m pip install cryptography'."
)
_BACKEND_NAMES = {
    "cng": "Windows CNG (bcrypt.dll, AES-256-GCM)",
    "cryptography": "cryptography (AESGCM)",
}


class CryptoError(ValueError):
    """Raised for invalid input, unavailable backends or authentication failure."""


# ---------------------------------------------------------------------------
# Windows CNG (bcrypt.dll) bindings
# ---------------------------------------------------------------------------
#
# The structures below use fixed-width ctypes types so that their layout also
# matches the native BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO structure on 64-bit
# Windows.  They are defined at module level so the bindings stay readable.

_BCRYPT_CHAIN_MODE_GCM = "ChainingModeGCM"
_BCRYPT_OBJECT_LENGTH = "ObjectLength"
_BCRYPT_AUTH_MODE_INFO_VERSION = 1


class _BCryptAuthenticatedCipherModeInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("dwInfoVersion", ctypes.c_uint32),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", ctypes.c_uint32),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", ctypes.c_uint32),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", ctypes.c_uint32),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", ctypes.c_uint32),
        ("cbAAD", ctypes.c_uint32),
        ("cbData", ctypes.c_uint64),
        ("dwFlags", ctypes.c_uint32),
    ]


def _configure_bcrypt(lib: "ctypes.WinDLL") -> None:
    """Declare the bcrypt.dll prototypes used by this module."""
    u8p = ctypes.POINTER(ctypes.c_ubyte)
    u32p = ctypes.POINTER(ctypes.c_uint32)
    voidp = ctypes.c_void_p

    lib.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(voidp), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    lib.BCryptOpenAlgorithmProvider.restype = ctypes.c_long
    lib.BCryptCloseAlgorithmProvider.argtypes = [voidp, ctypes.c_uint32]
    lib.BCryptCloseAlgorithmProvider.restype = ctypes.c_long
    lib.BCryptSetProperty.argtypes = [voidp, ctypes.c_wchar_p, u8p, ctypes.c_uint32, ctypes.c_uint32]
    lib.BCryptSetProperty.restype = ctypes.c_long
    lib.BCryptGetProperty.argtypes = [voidp, ctypes.c_wchar_p, u8p, ctypes.c_uint32, u32p, ctypes.c_uint32]
    lib.BCryptGetProperty.restype = ctypes.c_long
    lib.BCryptGenerateSymmetricKey.argtypes = [voidp, ctypes.POINTER(voidp), u8p, ctypes.c_uint32, u8p, ctypes.c_uint32, ctypes.c_uint32]
    lib.BCryptGenerateSymmetricKey.restype = ctypes.c_long
    lib.BCryptDestroyKey.argtypes = [voidp]
    lib.BCryptDestroyKey.restype = ctypes.c_long
    lib.BCryptEncrypt.argtypes = [voidp, u8p, ctypes.c_uint32, voidp, u8p, ctypes.c_uint32, u8p, ctypes.c_uint32, u32p, ctypes.c_uint32]
    lib.BCryptEncrypt.restype = ctypes.c_long
    lib.BCryptDecrypt.argtypes = [voidp, u8p, ctypes.c_uint32, voidp, u8p, ctypes.c_uint32, u8p, ctypes.c_uint32, u32p, ctypes.c_uint32]
    lib.BCryptDecrypt.restype = ctypes.c_long


_BCRYPT_LIB = None


def _bcrypt_lib():
    """Load and configure bcrypt.dll lazily (Windows only)."""
    global _BCRYPT_LIB
    if _BCRYPT_LIB is not None:
        return _BCRYPT_LIB
    if sys.platform != "win32":
        raise CryptoError("Windows CNG is only available on Windows.")
    try:
        lib = ctypes.WinDLL("bcrypt", use_last_error=True)
    except OSError as error:
        raise CryptoError("Windows CNG (bcrypt.dll) could not be loaded.") from error
    _configure_bcrypt(lib)
    _BCRYPT_LIB = lib
    return lib


def _cng_status(status: int, action: str) -> None:
    if status != 0:
        raise CryptoError(f"{action} failed (NTSTATUS 0x{status & 0xFFFFFFFF:08X}).")


def _u8_buffer(data: bytes) -> "ctypes.Array":
    return (ctypes.c_ubyte * max(1, len(data))).from_buffer_copy(data or b"\x00")


def _u8_pointer(buffer: "ctypes.Array") -> "ctypes.POINTER(ctypes.c_ubyte)":
    return ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))


def _open_cng_provider(lib) -> "ctypes.c_void_p":
    provider = ctypes.c_void_p()
    status = lib.BCryptOpenAlgorithmProvider(ctypes.byref(provider), "AES", None, 0)
    _cng_status(status, "BCryptOpenAlgorithmProvider")
    try:
        mode = ctypes.create_unicode_buffer(_BCRYPT_CHAIN_MODE_GCM)
        status = lib.BCryptSetProperty(
            provider,
            "ChainingMode",
            ctypes.cast(mode, ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.sizeof(mode),
            0,
        )
        _cng_status(status, "BCryptSetProperty(ChainingMode)")

        value = (ctypes.c_ubyte * 4)()
        result = ctypes.c_uint32()
        status = lib.BCryptGetProperty(
            provider,
            _BCRYPT_OBJECT_LENGTH,
            ctypes.cast(value, ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.sizeof(value),
            ctypes.byref(result),
            0,
        )
        _cng_status(status, "BCryptGetProperty(ObjectLength)")
        if result.value != ctypes.sizeof(value):
            raise CryptoError("BCryptGetProperty(ObjectLength) returned an unexpected size.")
        if struct.unpack("<I", bytes(value))[0] == 0:
            raise CryptoError("Windows CNG reported an invalid AES object length.")
    except BaseException:
        lib.BCryptCloseAlgorithmProvider(provider, 0)
        raise
    return provider


def _generate_cng_key(lib, provider, key: bytes) -> "ctypes.c_void_p":
    key_buffer = _u8_buffer(key)
    handle = ctypes.c_void_p()
    status = lib.BCryptGenerateSymmetricKey(
        provider,
        ctypes.byref(handle),
        None,
        0,
        _u8_pointer(key_buffer),
        len(key),
        0,
    )
    _cng_status(status, "BCryptGenerateSymmetricKey")
    return handle


def _auth_info(nonce_buffer, nonce_length: int, aad_buffer, aad_length: int, tag_pointer):
    info = _BCryptAuthenticatedCipherModeInfo()
    ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
    info.cbSize = ctypes.sizeof(info)
    info.dwInfoVersion = _BCRYPT_AUTH_MODE_INFO_VERSION
    info.pbNonce = _u8_pointer(nonce_buffer)
    info.cbNonce = nonce_length
    if aad_length:
        info.pbAuthData = _u8_pointer(aad_buffer)
        info.cbAuthData = aad_length
    info.pbTag = tag_pointer
    info.cbTag = TAG_LENGTH
    return info


def _cng_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    lib = _bcrypt_lib()
    provider = _open_cng_provider(lib)
    key_handle = ctypes.c_void_p()
    try:
        key_handle = _generate_cng_key(lib, provider, key)
        nonce_buffer = _u8_buffer(nonce)
        aad_buffer = _u8_buffer(aad)
        plain_buffer = _u8_buffer(plaintext)
        output = (ctypes.c_ubyte * (len(plaintext) + TAG_LENGTH))()
        tag_pointer = ctypes.cast(
            ctypes.byref(output, len(plaintext)),
            ctypes.POINTER(ctypes.c_ubyte),
        )
        info = _auth_info(nonce_buffer, len(nonce), aad_buffer, len(aad), tag_pointer)
        result = ctypes.c_uint32()
        status = lib.BCryptEncrypt(
            key_handle,
            _u8_pointer(plain_buffer),
            len(plaintext),
            ctypes.cast(ctypes.byref(info), ctypes.c_void_p),
            None,
            0,
            ctypes.cast(output, ctypes.POINTER(ctypes.c_ubyte)),
            len(plaintext) + TAG_LENGTH,
            ctypes.byref(result),
            0,
        )
        _cng_status(status, "BCryptEncrypt")
        if result.value != len(plaintext):
            raise CryptoError("Windows CNG returned an unexpected ciphertext length.")
        return bytes(output)
    finally:
        if key_handle.value:
            lib.BCryptDestroyKey(key_handle)
        lib.BCryptCloseAlgorithmProvider(provider, 0)


def _cng_gcm_decrypt(key: bytes, nonce: bytes, combined: bytes, aad: bytes) -> bytes:
    if len(combined) < TAG_LENGTH:
        raise CryptoError(_AUTH_ERROR)
    ciphertext_length = len(combined) - TAG_LENGTH
    lib = _bcrypt_lib()
    provider = _open_cng_provider(lib)
    key_handle = ctypes.c_void_p()
    try:
        key_handle = _generate_cng_key(lib, provider, key)
        nonce_buffer = _u8_buffer(nonce)
        aad_buffer = _u8_buffer(aad)
        # The tag must be mutable for CNG; keep the authenticated input in a
        # private copy and point pbTag at its final 16 bytes.
        input_buffer = _u8_buffer(combined)
        tag_pointer = ctypes.cast(
            ctypes.byref(input_buffer, ciphertext_length),
            ctypes.POINTER(ctypes.c_ubyte),
        )
        output = (ctypes.c_ubyte * max(1, ciphertext_length))()
        info = _auth_info(nonce_buffer, len(nonce), aad_buffer, len(aad), tag_pointer)
        result = ctypes.c_uint32()
        status = lib.BCryptDecrypt(
            key_handle,
            _u8_pointer(input_buffer),
            ciphertext_length,
            ctypes.cast(ctypes.byref(info), ctypes.c_void_p),
            None,
            0,
            ctypes.cast(output, ctypes.POINTER(ctypes.c_ubyte)),
            max(1, ciphertext_length),
            ctypes.byref(result),
            0,
        )
        if status != 0:
            raise CryptoError(_AUTH_ERROR)
        if result.value != ciphertext_length:
            raise CryptoError(_AUTH_ERROR)
        return bytes(output[:result.value])
    finally:
        if key_handle.value:
            lib.BCryptDestroyKey(key_handle)
        lib.BCryptCloseAlgorithmProvider(provider, 0)


def _probe_cng() -> None:
    """Verify that the Windows CNG AES-GCM path actually works."""
    result = _cng_gcm_encrypt(b"\x00" * KEY_LENGTH, b"\x00" * NONCE_LENGTH, b"", b"")
    if len(result) != TAG_LENGTH:
        raise CryptoError("Windows CNG AES-256-GCM self-test failed.")


# ---------------------------------------------------------------------------
# Optional cryptography backend (macOS / Linux, and Windows fallback)
# ---------------------------------------------------------------------------

def _load_cryptography():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as error:  # ImportError, or a broken installation
        raise CryptoError(_INSTALL_HINT) from error
    return AESGCM, InvalidTag


def _choose_backend() -> str:
    if sys.platform == "win32":
        try:
            _probe_cng()
            return "cng"
        except Exception as cng_error:
            cng_reason = str(cng_error)
        try:
            _load_cryptography()
            return "cryptography"
        except CryptoError as fallback_error:
            raise CryptoError(
                f"No AES-256-GCM backend is available. Windows CNG failed: {cng_reason} "
                f"{_INSTALL_HINT} ({fallback_error})"
            ) from fallback_error
    _load_cryptography()
    return "cryptography"


def available() -> tuple[bool, str]:
    """Return ``(True, backend name)`` or ``(False, reason)``."""
    try:
        backend = _choose_backend()
    except CryptoError as error:
        return False, str(error)
    return True, _BACKEND_NAMES[backend]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _password_bytes(password) -> bytes:
    if isinstance(password, str):
        try:
            raw = password.encode("utf-8")
        except UnicodeEncodeError as error:
            raise CryptoError("password is not valid UTF-8.") from error
    elif isinstance(password, (bytes, bytearray, memoryview)):
        raw = bytes(password)
    else:
        raise CryptoError("password must be a str or bytes value.")
    if not raw:
        raise CryptoError("password must not be empty.")
    return raw


def _validated_iterations(iterations: int) -> int:
    if isinstance(iterations, bool) or not isinstance(iterations, int):
        raise CryptoError("iterations must be an integer.")
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise CryptoError(
            f"iterations must be between {MIN_ITERATIONS} and {MAX_ITERATIONS}."
        )
    return iterations


def _validated_max_output(max_output_bytes: int) -> int:
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
        raise CryptoError("max_output_bytes must be an integer.")
    if max_output_bytes < 0:
        raise CryptoError("max_output_bytes must be zero (unlimited) or positive.")
    return max_output_bytes


def _validated_progress(progress) -> None:
    if progress is not None and not callable(progress):
        raise CryptoError("progress must be callable or None.")


def _bytes_argument(value, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise CryptoError(f"{name} must be bytes-like.")
    return bytes(value)


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(first))) == os.path.normcase(
        os.path.abspath(os.fspath(second))
    )


def _validate_source(source: Path) -> os.stat_result:
    try:
        if source.is_symlink():
            raise CryptoError("Source must be a regular file, not a symbolic link.")
        info = os.stat(source, follow_symlinks=False)
    except FileNotFoundError as error:
        raise CryptoError(f"Source file does not exist: {source}") from error
    except OSError as error:
        raise CryptoError(f"Cannot access source file: {source}") from error
    if not stat.S_ISREG(info.st_mode):
        raise CryptoError("Source must be a regular file.")
    return info


def _validate_destination(destination: Path, source: Path) -> None:
    if _same_path(source, destination):
        raise CryptoError("Source and destination must be different paths.")
    if destination.is_symlink() or destination.exists():
        if destination.is_dir() and not destination.is_symlink():
            raise CryptoError("Destination is a directory; a new file path is required.")
        raise CryptoError("Destination already exists; refusing to overwrite it.")
    if not destination.parent.is_dir():
        raise CryptoError(f"Destination directory does not exist: {destination.parent}")


def _write_atomic(destination: Path, data: bytes) -> None:
    if destination.is_symlink() or destination.exists():
        raise CryptoError("Destination already exists; refusing to overwrite it.")
    fd = None
    temp_path = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".partial",
            dir=str(destination.parent),
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
        temp_path = None
    except OSError as error:
        raise CryptoError(f"Could not write destination file: {destination}") from error
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# AEAD helpers (also used by the cross-implementation test vector)
# ---------------------------------------------------------------------------

def _gcm_encrypt(key, nonce, plaintext, aad=b"") -> bytes:
    """Encrypt with AES-256-GCM and return ``ciphertext || tag``."""
    key = _bytes_argument(key, "key")
    nonce = _bytes_argument(nonce, "nonce")
    plaintext = _bytes_argument(plaintext, "plaintext")
    aad = _bytes_argument(aad, "aad") if aad is not None else b""
    if len(key) != KEY_LENGTH:
        raise CryptoError("AES-256-GCM requires a 32-byte key.")
    if len(nonce) != NONCE_LENGTH:
        raise CryptoError("AES-256-GCM requires a 12-byte nonce.")
    if _choose_backend() == "cng":
        return _cng_gcm_encrypt(key, nonce, plaintext, aad)
    AESGCM, _InvalidTag = _load_cryptography()
    try:
        return AESGCM(key).encrypt(nonce, plaintext, aad)
    except Exception as error:
        raise CryptoError("AES-GCM encryption failed.") from error


def _gcm_decrypt(key, nonce, ciphertext, aad=b"", tag=None) -> bytes:
    """Decrypt ``ciphertext || tag`` (or a separate *tag*) with AES-256-GCM."""
    key = _bytes_argument(key, "key")
    nonce = _bytes_argument(nonce, "nonce")
    ciphertext = _bytes_argument(ciphertext, "ciphertext")
    aad = _bytes_argument(aad, "aad") if aad is not None else b""
    if len(key) != KEY_LENGTH:
        raise CryptoError("AES-256-GCM requires a 32-byte key.")
    if len(nonce) != NONCE_LENGTH:
        raise CryptoError("AES-256-GCM requires a 12-byte nonce.")
    combined = ciphertext + _bytes_argument(tag, "tag") if tag is not None else ciphertext
    if len(combined) < TAG_LENGTH:
        raise CryptoError(_AUTH_ERROR)
    if _choose_backend() == "cng":
        return _cng_gcm_decrypt(key, nonce, combined, aad)
    AESGCM, InvalidTag = _load_cryptography()
    try:
        return AESGCM(key).decrypt(nonce, combined, aad)
    except InvalidTag as error:
        raise CryptoError(_AUTH_ERROR) from error
    except Exception as error:
        raise CryptoError(_AUTH_ERROR) from error


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def derive_key(password, salt: bytes, iterations: int) -> bytes:
    """Derive a 32-byte AES key with PBKDF2-HMAC-SHA256."""
    raw_password = _password_bytes(password)
    raw_salt = _bytes_argument(salt, "salt")
    if not raw_salt:
        raise CryptoError("salt must not be empty.")
    iterations = _validated_iterations(iterations)
    return hashlib.pbkdf2_hmac("sha256", raw_password, raw_salt, iterations, dklen=KEY_LENGTH)


def encrypt_file(
    source: Path,
    destination: Path,
    password,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Encrypt *source* to a new *destination* using AES-256-GCM."""
    try:
        source = Path(source)
        destination = Path(destination)
    except TypeError as error:
        raise CryptoError("source and destination must be filesystem paths.") from error
    raw_password = _password_bytes(password)
    iterations = _validated_iterations(iterations)
    _validated_progress(progress)
    _validate_source(source)
    _validate_destination(destination, source)

    try:
        with source.open("rb") as stream:
            plaintext = stream.read()
    except OSError as error:
        raise CryptoError(f"Could not read source file: {source}") from error

    salt = secrets.token_bytes(SALT_LENGTH)
    nonce = secrets.token_bytes(NONCE_LENGTH)
    key = derive_key(raw_password, salt, iterations)
    header = HEADER.pack(
        MAGIC,
        VERSION,
        ALGORITHM_AES_256_GCM,
        KDF_PBKDF2_HMAC_SHA256,
        0,
        iterations,
        SALT_LENGTH,
        NONCE_LENGTH,
        TAG_LENGTH,
        len(plaintext),
    )
    sealed = _gcm_encrypt(key, nonce, plaintext, header)
    if len(sealed) != len(plaintext) + TAG_LENGTH:
        raise CryptoError("AES-GCM produced an unexpected ciphertext length.")

    total = len(plaintext)
    if progress is not None:
        if total == 0:
            progress(0, 0)
        else:
            for offset in range(0, total, COPY_BUFFER):
                progress(min(offset + COPY_BUFFER, total), total)

    _write_atomic(destination, header + salt + nonce + sealed)
    return {
        "path": str(destination),
        "bytes": len(header) + len(salt) + len(nonce) + len(sealed),
        "files": 1,
        "directories": 0,
        "verified": True,
    }


def _parse_header(header: bytes) -> tuple[int, int, int, int, int]:
    if len(header) != HEADER_SIZE:
        raise CryptoError("Not a BNE1 encrypted file, or its header is truncated; "
                          "the password is wrong or the file was tampered with.")
    (
        magic,
        version,
        algorithm,
        kdf,
        reserved,
        iterations,
        salt_length,
        nonce_length,
        tag_length,
        plaintext_length,
    ) = HEADER.unpack(header)
    if magic != MAGIC:
        raise CryptoError("Not a BNE1 encrypted file; the password is wrong or the file was tampered with.")
    if version != VERSION:
        raise CryptoError("Unsupported BNE1 version; the file was tampered with.")
    if algorithm != ALGORITHM_AES_256_GCM:
        raise CryptoError("Unsupported BNE1 algorithm; the file was tampered with.")
    if kdf != KDF_PBKDF2_HMAC_SHA256:
        raise CryptoError("Unsupported BNE1 key-derivation function; the file was tampered with.")
    if reserved != 0:
        raise CryptoError("Invalid BNE1 header reserved byte; the file was tampered with.")
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise CryptoError("Invalid BNE1 iteration count; the file was tampered with.")
    if salt_length != SALT_LENGTH or nonce_length != NONCE_LENGTH or tag_length != TAG_LENGTH:
        raise CryptoError("Invalid BNE1 header lengths; the file was tampered with.")
    if header[HEADER_FIELDS_SIZE:] != PADDING:
        raise CryptoError("Invalid BNE1 header padding; the file was tampered with.")
    return iterations, salt_length, nonce_length, tag_length, plaintext_length


def decrypt_file(
    source: Path,
    destination: Path,
    password,
    *,
    max_output_bytes: int = 0,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Authenticate and decrypt a BNE1 file to a new *destination*."""
    try:
        source = Path(source)
        destination = Path(destination)
    except TypeError as error:
        raise CryptoError("source and destination must be filesystem paths.") from error
    raw_password = _password_bytes(password)
    max_output_bytes = _validated_max_output(max_output_bytes)
    _validated_progress(progress)
    _validate_source(source)
    _validate_destination(destination, source)

    try:
        with source.open("rb") as stream:
            file_size = os.fstat(stream.fileno()).st_size
            header = stream.read(HEADER_SIZE)
            iterations, salt_length, nonce_length, tag_length, plaintext_length = _parse_header(header)
            if max_output_bytes and plaintext_length > max_output_bytes:
                raise CryptoError(
                    "Encrypted file declares a plaintext size larger than max_output_bytes; "
                    "refusing to decrypt it."
                )
            expected_size = HEADER_SIZE + salt_length + nonce_length + plaintext_length + tag_length
            if file_size != expected_size:
                raise CryptoError(
                    "Encrypted file length does not match its header; the file was truncated "
                    "or tampered with."
                )
            salt = stream.read(salt_length)
            nonce = stream.read(nonce_length)
            ciphertext = stream.read(plaintext_length)
            tag = stream.read(tag_length)
            if (len(salt) != salt_length or len(nonce) != nonce_length
                    or len(ciphertext) != plaintext_length or len(tag) != tag_length):
                raise CryptoError(
                    "Encrypted file was truncated; the file was tampered with."
                )
            if stream.read(1):
                raise CryptoError(
                    "Encrypted file has trailing data; the file was tampered with."
                )
    except OSError as error:
        raise CryptoError(f"Could not read encrypted file: {source}") from error

    key = derive_key(raw_password, salt, iterations)
    plaintext = _gcm_decrypt(key, nonce, ciphertext + tag, header)

    total = len(plaintext)
    if progress is not None:
        if total == 0:
            progress(0, 0)
        else:
            for offset in range(0, total, COPY_BUFFER):
                progress(min(offset + COPY_BUFFER, total), total)

    _write_atomic(destination, plaintext)
    return {
        "path": str(destination),
        "bytes": len(plaintext),
        "files": 1,
        "directories": 0,
        "verified": True,
    }
