"""Dependency-free block LZSS compression container used by BackNest.

The implementation is deliberately self-contained: it uses only the Python
standard library for file bookkeeping (``os``, ``struct``, ``pathlib``) and
implements both the LZSS encoder/decoder and the CRC-32 table itself.  No
external compressor, archive library, or external program is used.

On-disk format (``BNLZ`` version 1), all integers little-endian
----------------------------------------------------------------
Header (12 bytes)::

    magic           4 bytes  b"BNLZ"
    version         u8       1
    flags           u8       0
    reserved        u16      0
    block_bytes     u32      maximum original bytes per block

Then zero or more blocks until EOF, with no footer::

    mode            u8       0 = stored, 1 = LZSS
    original_length u32      uncompressed block length
    payload_length  u32      bytes of payload that follow
    crc32           u32      CRC-32 of the original (uncompressed) block
    payload         N bytes

LZSS bit stream
---------------
A 4096-byte sliding window, minimum match 3, maximum match 18.  Tokens are
grouped into sets of at most eight and introduced by one flag byte.  Bit ``i``
of the flag byte (least-significant bit first) describes token ``i``:
``1`` means the token is a literal byte, ``0`` means it is a match.  A match
is encoded as two little-endian bytes::

    value = (distance << 4) | (length - 3)

so the high 12 bits hold the backward distance and the low four bits hold
``length - 3`` (0..15).  A direct 12-bit field can represent distances 1..4095;
zero is rejected.  (The nominal window is 4096 bytes, but distance 4096 cannot
be represented directly in 12 bits, so the encoder does not search that final
byte of the window.)  Matching is allowed to overlap the current output
position, which the decoder handles byte-for-byte.

The encoder is greedy and uses a hash table keyed by three-byte prefixes.  If
the LZSS payload is not strictly smaller than the original block, the block is
stored verbatim (mode 0).
"""

from __future__ import annotations

import os
import stat
import struct
from pathlib import Path
from typing import BinaryIO, Callable

__all__ = [
    "ALGORITHMS",
    "CodecError",
    "compress_file",
    "decompress_file",
    "compress_stream",
    "decompress_stream",
    "crc32",
]

ALGORITHMS = [
    {
        "id": "lzss-v1",
        "version": "1.0",
        "selfImplemented": True,
        "supportedPlatforms": ["windows", "macos", "linux"],
    }
]

#: Container constants.
_MAGIC = b"BNLZ"
_VERSION = 1
_FLAGS = 0
_RESERVED = 0
_HEADER = struct.Struct("<4sBBHI")
_BLOCK_HEADER = struct.Struct("<BIII")

_MODE_STORED = 0
_MODE_LZSS = 1

_WINDOW_SIZE = 4096
_MAX_DISTANCE = _WINDOW_SIZE - 1  # a direct 12-bit distance field cannot hold 4096
_MIN_MATCH = 3
_MAX_MATCH = 18

#: Rebuild the prefix table once it grows past this many entries.  Entries
#: older than the window can never match again, so this keeps memory bounded
#: even for a 1 MiB incompressible block.
_TABLE_PRUNE_LIMIT = 32768

_MIN_BLOCK_BYTES = 1024
_MAX_BLOCK_BYTES = 1024 * 1024

ProgressCallback = Callable[[int, int], None]


class CodecError(ValueError):
    """Raised when a BNLZ stream or a codec argument is invalid."""


# ---------------------------------------------------------------------------
# CRC-32 (implemented locally; the polynomial is the standard CRC-32 one)
# ---------------------------------------------------------------------------

def _build_crc_table() -> tuple[int, ...]:
    table = []
    for index in range(256):
        value = index
        for _ in range(8):
            if value & 1:
                value = (value >> 1) ^ 0xEDB88320
            else:
                value >>= 1
        table.append(value)
    return tuple(table)


_CRC_TABLE = _build_crc_table()


def crc32(data: bytes, value: int = 0) -> int:
    """Return the CRC-32 of *data*, optionally continuing from *value*.

    The signature mirrors the usual incremental CRC convention: passing a
    previous return value as *value* continues the checksum over more data.
    """

    checksum = (value ^ 0xFFFFFFFF) & 0xFFFFFFFF
    table = _CRC_TABLE
    for byte in data:
        checksum = table[(checksum ^ byte) & 0xFF] ^ (checksum >> 8)
    return (checksum ^ 0xFFFFFFFF) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

def _validate_block_bytes(block_bytes: int) -> int:
    if isinstance(block_bytes, bool) or not isinstance(block_bytes, int):
        raise CodecError("block_bytes must be an integer")
    if not (_MIN_BLOCK_BYTES <= block_bytes <= _MAX_BLOCK_BYTES):
        raise CodecError(
            "block_bytes must be between "
            f"{_MIN_BLOCK_BYTES} and {_MAX_BLOCK_BYTES}"
        )
    return block_bytes


def _validate_max_output(max_output_bytes: int) -> int:
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
        raise CodecError("max_output_bytes must be an integer")
    if max_output_bytes < 0:
        raise CodecError("max_output_bytes must not be negative")
    return max_output_bytes


def _validate_progress(progress: ProgressCallback | None) -> None:
    if progress is not None and not callable(progress):
        raise CodecError("progress must be callable or None")


def _source_path(source: Path) -> Path:
    try:
        path = Path(source)
    except TypeError as exc:  # pragma: no cover - defensive
        raise CodecError("source path is invalid") from exc
    if path.is_symlink():
        raise CodecError(f"source must not be a symbolic link: {path}")
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise CodecError(f"source does not exist: {path}") from exc
    except OSError as exc:
        raise CodecError(f"cannot inspect source {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CodecError(f"source must be a regular file: {path}")
    return path


def _destination_path(source: Path, destination: Path) -> Path:
    try:
        path = Path(destination)
    except TypeError as exc:  # pragma: no cover - defensive
        raise CodecError("destination path is invalid") from exc

    # Compare canonical names so case-insensitive filesystems and ``..``
    # spellings cannot slip past the source == destination check.
    source_key = os.path.normcase(os.path.realpath(os.fspath(source)))
    destination_key = os.path.normcase(os.path.realpath(os.fspath(path)))
    if source_key == destination_key:
        raise CodecError("source and destination must be different files")

    if path.is_symlink() or path.exists():
        if path.is_dir() and not path.is_symlink():
            raise CodecError(f"destination is a directory: {path}")
        raise CodecError(f"destination already exists: {path}")

    parent = path.parent
    if not parent.is_dir():
        raise CodecError(f"destination directory does not exist: {parent}")
    return path


# ---------------------------------------------------------------------------
# LZSS block codec
# ---------------------------------------------------------------------------

def _lzss_encode(block: bytes) -> bytes | None:
    """Return an LZSS payload for *block*, or ``None`` if storing is better."""

    length = len(block)
    if length <= _MIN_MATCH:
        return None

    payload = bytearray()
    table: dict[int, int] = {}
    position = 0

    while position < length:
        flag_index = len(payload)
        payload.append(0)  # placeholder for this group's flag byte
        flags = 0

        for bit in range(8):
            if position >= length:
                break

            match_length = 0
            match_distance = 0

            if position + _MIN_MATCH <= length:
                key = (
                    (block[position] << 16)
                    | (block[position + 1] << 8)
                    | block[position + 2]
                )
                candidate = table.get(key)
                table[key] = position
                if len(table) > _TABLE_PRUNE_LIMIT:
                    cutoff = position - _WINDOW_SIZE
                    table = {
                        old_key: old_position
                        for old_key, old_position in table.items()
                        if old_position >= cutoff
                    }
                if candidate is not None:
                    distance = position - candidate
                    if 1 <= distance <= _MAX_DISTANCE:
                        maximum = min(_MAX_MATCH, length - position)
                        matched = _MIN_MATCH
                        while (
                            matched < maximum
                            and block[candidate + matched] == block[position + matched]
                        ):
                            matched += 1
                        if matched >= _MIN_MATCH:
                            match_length = matched
                            match_distance = distance

            if match_length:
                # Flag bit remains zero for a match token.
                packed = ((match_distance & 0xFFF) << 4) | (match_length - 3)
                payload.append(packed & 0xFF)
                payload.append((packed >> 8) & 0xFF)
                position += match_length
            else:
                flags |= 1 << bit
                payload.append(block[position])
                position += 1

            # The payload only ever grows from here.  Once it has reached the
            # original size while input remains, storing the block is certain
            # to be at least as small; stop wasting time.
            if len(payload) >= length and position < length:
                return None

        payload[flag_index] = flags

    if len(payload) < length:
        return bytes(payload)
    return None


def _lzss_decode(payload: bytes, original_length: int) -> bytes:
    """Decode one LZSS payload, rejecting malformed or over-long streams."""

    if original_length <= 0:
        raise CodecError("LZSS block has an invalid original length")
    if not payload:
        raise CodecError("LZSS block has an empty payload")

    output = bytearray()
    cursor = 0
    payload_length = len(payload)

    while len(output) < original_length:
        if cursor >= payload_length:
            raise CodecError("truncated LZSS bit stream: missing flag byte")
        flags = payload[cursor]
        cursor += 1

        for bit in range(8):
            if len(output) >= original_length:
                break
            if flags & (1 << bit):
                if cursor >= payload_length:
                    raise CodecError("truncated LZSS bit stream: missing literal")
                output.append(payload[cursor])
                cursor += 1
            else:
                if cursor + 2 > payload_length:
                    raise CodecError("truncated LZSS bit stream: missing match")
                packed = payload[cursor] | (payload[cursor + 1] << 8)
                cursor += 2
                distance = packed >> 4
                match_length = (packed & 0x0F) + _MIN_MATCH

                if not (1 <= distance <= _MAX_DISTANCE):
                    raise CodecError("invalid LZSS match distance")
                if distance > len(output):
                    raise CodecError("LZSS match points before the output start")
                if not (_MIN_MATCH <= match_length <= _MAX_MATCH):
                    raise CodecError("invalid LZSS match length")
                if len(output) + match_length > original_length:
                    raise CodecError("LZSS match overruns the declared block length")

                start = len(output) - distance
                if distance >= match_length:
                    output.extend(output[start:start + match_length])
                else:
                    # Overlapping copy: the source pattern repeats.
                    pattern = bytes(output[start:])
                    repeats, remainder = divmod(match_length, distance)
                    output.extend(pattern * repeats)
                    output.extend(pattern[:remainder])

    if len(output) != original_length:
        raise CodecError("LZSS block produced the wrong number of bytes")
    if cursor != payload_length:
        raise CodecError("LZSS block has trailing payload bytes")
    return bytes(output)


# ---------------------------------------------------------------------------
# Stream helpers
# ---------------------------------------------------------------------------

def _read_exact(stream: BinaryIO, size: int) -> bytes:
    """Read up to *size* bytes, tolerating short reads from the stream."""

    first = stream.read(size)
    if not first:
        return b""
    if len(first) == size:
        return first
    chunks = [first]
    remaining = size - len(first)
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_block(stream: BinaryIO, block_bytes: int) -> bytes:
    """Read at most one block, treating a clean EOF as an empty block."""

    return _read_exact(stream, block_bytes)


def _stream_total(src: BinaryIO) -> int:
    """Best-effort total input size for progress reporting; 0 if unknown."""

    try:
        descriptor = src.fileno()
        info = os.fstat(descriptor)
    except (AttributeError, OSError, ValueError):
        return 0
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    return 0


def _report(progress: ProgressCallback | None, processed: int, total: int) -> None:
    if progress is not None:
        progress(processed, total)


# ---------------------------------------------------------------------------
# Public stream API
# ---------------------------------------------------------------------------

def compress_stream(
    src: BinaryIO,
    dst: BinaryIO,
    *,
    block_bytes: int = 65536,
    progress: ProgressCallback | None = None,
) -> None:
    """Compress *src* to *dst* one block at a time."""

    block_bytes = _validate_block_bytes(block_bytes)
    _validate_progress(progress)
    total = _stream_total(src)

    dst.write(_HEADER.pack(_MAGIC, _VERSION, _FLAGS, _RESERVED, block_bytes))

    processed = 0
    wrote_block = False
    while True:
        block = _read_block(src, block_bytes)
        if not block:
            break
        if len(block) > block_bytes:  # pragma: no cover - _read_block caps size
            raise CodecError("read block is larger than block_bytes")

        encoded = _lzss_encode(block)
        if encoded is None or len(encoded) >= len(block):
            mode = _MODE_STORED
            payload = block
        else:
            mode = _MODE_LZSS
            payload = encoded

        header = _BLOCK_HEADER.pack(
            mode, len(block), len(payload), crc32(block)
        )
        dst.write(header)
        dst.write(payload)

        processed += len(block)
        wrote_block = True
        _report(progress, processed, total)

    if not wrote_block:
        _report(progress, 0, total)


def decompress_stream(
    src: BinaryIO,
    dst: BinaryIO,
    *,
    max_output_bytes: int = 0,
    progress: ProgressCallback | None = None,
) -> None:
    """Decompress *src* to *dst* one block at a time."""

    max_output_bytes = _validate_max_output(max_output_bytes)
    _validate_progress(progress)

    header = _read_exact(src, _HEADER.size)
    if len(header) != _HEADER.size:
        raise CodecError("truncated BNLZ header")
    magic, version, flags, reserved, block_bytes = _HEADER.unpack(header)
    if magic != _MAGIC:
        raise CodecError("not a BNLZ stream: bad magic")
    if version != _VERSION:
        raise CodecError(f"unsupported BNLZ version: {version}")
    if flags != _FLAGS:
        raise CodecError(f"unsupported BNLZ flags: {flags}")
    if reserved != _RESERVED:
        raise CodecError("invalid BNLZ reserved field")
    if not (_MIN_BLOCK_BYTES <= block_bytes <= _MAX_BLOCK_BYTES):
        raise CodecError("BNLZ header contains an invalid block size")

    processed = 0
    wrote_block = False
    while True:
        raw_header = _read_exact(src, _BLOCK_HEADER.size)
        if not raw_header:
            break
        if len(raw_header) != _BLOCK_HEADER.size:
            raise CodecError("truncated BNLZ block header")

        mode, original_length, payload_length, expected_crc = _BLOCK_HEADER.unpack(
            raw_header
        )
        if mode not in (_MODE_STORED, _MODE_LZSS):
            raise CodecError(f"unsupported BNLZ block mode: {mode}")
        if original_length == 0:
            raise CodecError("BNLZ block has a zero original length")
        if original_length > block_bytes:
            raise CodecError("BNLZ block original length exceeds block_bytes")
        if payload_length == 0:
            raise CodecError("BNLZ block has an empty payload")

        if mode == _MODE_STORED:
            if payload_length != original_length:
                raise CodecError("stored block payload length does not match")
        else:
            # The encoder only selects LZSS when the payload is strictly
            # smaller; enforcing that here also bounds expansion.
            if payload_length >= original_length:
                raise CodecError(
                    "LZSS block payload is not smaller than its original length"
                )
            if payload_length > block_bytes:
                raise CodecError("LZSS payload exceeds block_bytes")

        if max_output_bytes and processed + original_length > max_output_bytes:
            raise CodecError(
                "decompressed data would exceed max_output_bytes"
            )

        payload = _read_exact(src, payload_length)
        if len(payload) != payload_length:
            raise CodecError("truncated BNLZ block payload")

        if mode == _MODE_STORED:
            block = payload
        else:
            block = _lzss_decode(payload, original_length)

        if len(block) != original_length:
            raise CodecError("BNLZ block length mismatch after decoding")
        actual_crc = crc32(block)
        if actual_crc != expected_crc:
            raise CodecError(
                f"BNLZ block CRC mismatch (expected {expected_crc:08x}, "
                f"got {actual_crc:08x})"
            )

        dst.write(block)
        processed += len(block)
        wrote_block = True
        _report(progress, processed, 0)

    if not wrote_block:
        _report(progress, 0, 0)


# ---------------------------------------------------------------------------
# Atomic file API
# ---------------------------------------------------------------------------

class _AtomicOutput:
    """Write to ``.<name>.partial-<random>`` and replace on success.

    The temporary file is created with normal default permissions rather than
    the owner-only mode used by some temporary-file helpers, and it always
    lives in the destination directory so ``os.replace`` is atomic.  Any
    failure removes the temporary file and never touches the destination.
    """

    def __init__(self, destination: Path):
        self.destination = destination
        self.temp_path: Path | None = None
        self._fd: int = -1
        self._handle: BinaryIO | None = None
        self._replaced = False

    def __enter__(self) -> BinaryIO:
        parent = self.destination.parent
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        for _ in range(64):
            token = os.urandom(8).hex()
            candidate = parent / (
                f".{self.destination.name}.partial-{token}"
            )
            try:
                self._fd = os.open(candidate, flags, 0o666)
            except FileExistsError:
                continue
            except OSError as exc:
                raise CodecError(
                    f"cannot create temporary file in {parent}: {exc}"
                ) from exc
            self.temp_path = candidate
            break
        else:  # pragma: no cover - 64 random collisions are inconceivable
            raise CodecError("cannot create a unique temporary file")

        try:
            self._handle = os.fdopen(self._fd, "wb")
        except OSError as exc:  # pragma: no cover - defensive
            os.close(self._fd)
            self._fd = -1
            self._cleanup()
            raise CodecError(f"cannot open temporary file: {exc}") from exc
        self._fd = -1
        return self._handle

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if self._handle is not None:
                try:
                    if exc_type is None:
                        self._handle.flush()
                        os.fsync(self._handle.fileno())
                finally:
                    self._handle.close()
                    self._handle = None
            if exc_type is None:
                os.replace(self.temp_path, self.destination)
                self._replaced = True
        finally:
            if self._fd != -1:
                try:
                    os.close(self._fd)
                except OSError:  # pragma: no cover - defensive
                    pass
                self._fd = -1
            self._cleanup()
        return False

    def _cleanup(self) -> None:
        if self._replaced or self.temp_path is None:
            return
        try:
            self.temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        self.temp_path = None


def compress_file(
    source: Path,
    destination: Path,
    *,
    block_bytes: int = 65536,
    progress: ProgressCallback | None = None,
) -> dict:
    """Compress *source* to *destination* atomically.

    The destination must not exist, must not be a directory, and must differ
    from the source.  A temporary file in the destination directory is
    replaced onto the final name only after a complete, successful write.
    """

    block_bytes = _validate_block_bytes(block_bytes)
    _validate_progress(progress)
    src_path = _source_path(source)
    dst_path = _destination_path(src_path, destination)

    try:
        with src_path.open("rb") as src:
            with _AtomicOutput(dst_path) as dst:
                compress_stream(
                    src, dst, block_bytes=block_bytes, progress=progress
                )
        output_bytes = dst_path.stat().st_size
    except CodecError:
        raise
    except OSError as exc:
        raise CodecError(f"compression failed: {exc}") from exc

    return {
        "path": str(destination),
        "bytes": output_bytes,
        "files": 1,
        "directories": 0,
        "verified": True,
    }


def decompress_file(
    source: Path,
    destination: Path,
    *,
    max_output_bytes: int = 0,
    progress: ProgressCallback | None = None,
) -> dict:
    """Decompress *source* to *destination* atomically.

    If *max_output_bytes* is positive, no block is written once the output
    would exceed that limit.  A value of zero means unlimited output.
    """

    max_output_bytes = _validate_max_output(max_output_bytes)
    _validate_progress(progress)
    src_path = _source_path(source)
    dst_path = _destination_path(src_path, destination)

    try:
        with src_path.open("rb") as src:
            with _AtomicOutput(dst_path) as dst:
                decompress_stream(
                    src,
                    dst,
                    max_output_bytes=max_output_bytes,
                    progress=progress,
                )
        output_bytes = dst_path.stat().st_size
    except CodecError:
        raise
    except OSError as exc:
        raise CodecError(f"decompression failed: {exc}") from exc

    return {
        "path": str(destination),
        "bytes": output_bytes,
        "files": 1,
        "directories": 0,
        "verified": True,
    }
