"""Self-implemented single-file archive container (`BNAR` version 1).

The container is deliberately dependency-free: header, entry index and payload
are written by this module.  It is *not* a ZIP/TAR wrapper and never calls an
external archiver.  A matching reader validates every field, every path and
every CRC before publishing output.

Layout (all integers little-endian):

    header   : magic "BNAR", u16 version=1, u16 flags=0, u32 entryCount,
               u64 totalFileBytes, u32 createdUnixSeconds, u32 headerCrc32
    entries  : repeated entryCount times
               u8 kind (0=directory, 1=file), u16 mode, i64 mtimeNs,
               u64 size, u32 crc32, u16 pathLength, path (UTF-8), payload
    trailer  : b"BNAREND\\0"

Directories carry no payload.  Paths are relative, "/"-separated and may not
contain "..", "." components, drive letters, backslashes or NUL bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets as _secrets
import stat
import struct
import time
from typing import Callable, Iterator

from .checksums import CRC32, crc32

ALGORITHMS = [
    {
        "id": "bkn-archive-v1",
        "version": "1.0",
        "selfImplemented": True,
        "supportedPlatforms": ["windows", "macos", "linux"],
    }
]

MAGIC = b"BNAR"
TRAILER = b"BNAREND\0"
VERSION = 1
HEADER = struct.Struct("<4sHHQII")
HEADER_CRC = struct.Struct("<I")
ENTRY = struct.Struct("<BHqQIH")
ENTRY_CRC_OFFSET = 19
MAX_ENTRIES = 200_000
MAX_PATH_BYTES = 4096
MAX_TOTAL_BYTES = 1 << 50  # 1 PiB sanity ceiling
COPY_BUFFER = 64 * 1024
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ContainerError(ValueError):
    """Raised for invalid input, an unsafe container or a failed round trip."""


@dataclass(frozen=True)
class Entry:
    path: str
    kind: str  # "directory" | "file"
    mode: int
    mtime_ns: int
    size: int
    crc: int


def _check_relative(path: str) -> str:
    if not isinstance(path, str) or not path or "\0" in path:
        raise ContainerError("归档条目路径为空或包含非法字符。")
    if "\\" in path:
        raise ContainerError("归档条目路径不能包含反斜杠。")
    if path.startswith("/"):
        raise ContainerError("归档条目路径不能是绝对路径。")
    try:
        encoded = path.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise ContainerError("文件名不是有效的 UTF-8。") from error
    if len(encoded) > MAX_PATH_BYTES:
        raise ContainerError("归档条目路径过长。")
    for part in path.split("/"):
        if part in ("", ".", ".."):
            raise ContainerError("归档条目路径包含不安全的层级。")
        if part.endswith(" ") or part.endswith("."):
            raise ContainerError("归档条目路径以空格或点结尾。")
        if len(part) >= 2 and part[1] == ":":
            raise ContainerError("归档条目路径包含驱动器前缀。")
        if part.split(".", 1)[0].upper() in WINDOWS_RESERVED:
            raise ContainerError("归档条目路径使用了系统保留名称。")
    return path


def _encode(path: str) -> bytes:
    _check_relative(path)
    return path.encode("utf-8", "strict")


def _decode(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ContainerError("归档中的文件路径不是有效的 UTF-8。") from error
    return _check_relative(value)


def _write_header(stream, count: int, total: int, created: int) -> None:
    prefix = HEADER.pack(MAGIC, VERSION, 0, count, total, created)
    stream.write(prefix + HEADER_CRC.pack(crc32(prefix)))


def _read_header(stream) -> tuple[int, int, int]:
    raw = stream.read(HEADER.size + HEADER_CRC.size)
    if len(raw) != HEADER.size + HEADER_CRC.size:
        raise ContainerError("归档文件头不完整。")
    magic, version, flags, count, total, created = HEADER.unpack(raw[:HEADER.size])
    stored_crc = HEADER_CRC.unpack(raw[HEADER.size:])[0]
    if magic != MAGIC or version != VERSION or flags != 0:
        raise ContainerError("不是受支持的 BNAR 归档，或版本不匹配。")
    if crc32(raw[:HEADER.size]) != stored_crc:
        raise ContainerError("归档文件头校验失败。")
    if count > MAX_ENTRIES:
        raise ContainerError("归档条目数量超过限制。")
    if total > MAX_TOTAL_BYTES:
        raise ContainerError("归档声明的内容大小超过限制。")
    return count, total, created


def _scan(root: Path) -> tuple[list[Entry], int]:
    if root.is_symlink() or not root.is_dir():
        raise ContainerError("打包源必须是普通目录。")
    entries: list[Entry] = []
    total = 0

    def walk(directory: Path, prefix: str) -> None:
        nonlocal total
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise ContainerError(f"无法读取目录：{directory}") from error
        for item in children:
            relative = item.name if not prefix else prefix + "/" + item.name
            _check_relative(relative)
            try:
                info = item.stat(follow_symlinks=False)
            except OSError as error:
                raise ContainerError(f"无法读取文件信息：{item.path}") from error
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                raise ContainerError("打包不支持符号链接。")
            if stat.S_ISDIR(info.st_mode):
                entries.append(Entry(relative, "directory", mode, info.st_mtime_ns, 0, 0))
                if len(entries) > MAX_ENTRIES:
                    raise ContainerError("条目数量超过限制。")
                walk(Path(item.path), relative)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > MAX_TOTAL_BYTES:
                    raise ContainerError("内容总大小超过限制。")
                entries.append(Entry(relative, "file", mode, info.st_mtime_ns, info.st_size, 0))
                if len(entries) > MAX_ENTRIES:
                    raise ContainerError("条目数量超过限制。")
            else:
                raise ContainerError("打包不支持特殊文件。")

    walk(root, "")
    entries.sort(key=lambda entry: (entry.kind != "directory", entry.path))
    return entries, total


def _atomic_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".partial-" + _secrets.token_hex(6))


def _prepare_destination(destination: Path) -> Path:
    if destination.exists() or destination.is_symlink():
        raise ContainerError("目标文件已存在，请换一个新路径。")
    if not destination.parent.is_dir():
        raise ContainerError("目标目录不存在。")
    return _atomic_path(destination)


def pack(source: Path, destination: Path, *, progress: Callable[[int, int], None] | None = None) -> dict:
    source = Path(source)
    destination = Path(destination)
    entries, total = _scan(source)
    temp = _prepare_destination(destination)
    processed = 0
    try:
        with temp.open("xb") as out:
            _write_header(out, len(entries), total, int(time.time()))
            for entry in entries:
                raw_path = _encode(entry.path)
                header_start = out.tell()
                out.write(ENTRY.pack(1 if entry.kind == "file" else 0, entry.mode, entry.mtime_ns,
                                     entry.size, entry.crc, len(raw_path)))
                out.write(raw_path)
                if entry.kind == "directory":
                    continue
                crc = CRC32()
                written = 0
                file_path = source.joinpath(*entry.path.split("/"))
                try:
                    before = file_path.stat()
                    with file_path.open("rb") as source_file:
                        while True:
                            chunk = source_file.read(COPY_BUFFER)
                            if not chunk:
                                break
                            out.write(chunk)
                            crc.update(chunk)
                            written += len(chunk)
                            processed += len(chunk)
                            if progress:
                                progress(processed, total)
                    after = file_path.stat()
                except OSError as error:
                    raise ContainerError(f"无法读取文件：{file_path}") from error
                if (written != entry.size or before.st_size != entry.size
                        or before.st_mtime_ns != after.st_mtime_ns):
                    raise ContainerError("源文件在打包过程中发生变化。")
                # The entry header is already on disk; patch the CRC field in place.
                end = out.tell()
                out.seek(header_start + ENTRY_CRC_OFFSET)
                out.write(struct.pack("<I", crc.digest()))
                out.seek(end)
            out.write(TRAILER)
        os.replace(temp, destination)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "files": sum(1 for entry in entries if entry.kind == "file"),
        "directories": sum(1 for entry in entries if entry.kind == "directory"),
        "verified": True,
    }


def _read_entry(stream) -> Entry:
    head = stream.read(ENTRY.size)
    if len(head) != ENTRY.size:
        raise ContainerError("归档条目被截断。")
    kind, mode, mtime_ns, size, entry_crc, path_length = ENTRY.unpack(head)
    if kind not in (0, 1):
        raise ContainerError("归档条目类型不受支持。")
    if path_length == 0 or path_length > MAX_PATH_BYTES:
        raise ContainerError("归档条目路径长度不合法。")
    raw_path = stream.read(path_length)
    if len(raw_path) != path_length:
        raise ContainerError("归档条目路径被截断。")
    path = _decode(raw_path)
    if kind == 0 and size != 0:
        raise ContainerError("目录条目不应包含数据。")
    if size > MAX_TOTAL_BYTES:
        raise ContainerError("归档条目大小超过限制。")
    return Entry(path, "directory" if kind == 0 else "file", mode, mtime_ns, size, entry_crc)


def list_entries(source: Path) -> list[Entry]:
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ContainerError("归档文件不存在或不是普通文件。")
    size_on_disk = source.stat().st_size
    entries: list[Entry] = []
    seen: set[str] = set()
    declared_files = 0
    with source.open("rb") as stream:
        count, total, _created = _read_header(stream)
        for _ in range(count):
            entry = _read_entry(stream)
            if entry.path in seen:
                raise ContainerError("归档包含重复条目。")
            seen.add(entry.path)
            entries.append(entry)
            if entry.kind == "file":
                declared_files += entry.size
                position = stream.tell() + entry.size
                if position > size_on_disk:
                    raise ContainerError("归档数据被截断。")
                stream.seek(entry.size, os.SEEK_CUR)
        if declared_files != total:
            raise ContainerError("归档声明的大小与条目不一致。")
        if stream.read(len(TRAILER)) != TRAILER:
            raise ContainerError("归档文件结尾缺失或已损坏。")
        if stream.read(1):
            raise ContainerError("归档文件包含多余数据。")
    return entries


def unpack(source: Path, destination: Path, *, max_output_bytes: int = 0,
           progress: Callable[[int, int], None] | None = None) -> dict:
    source = Path(source)
    destination = Path(destination)
    if source.is_symlink() or not source.is_file():
        raise ContainerError("归档文件不存在或不是普通文件。")
    if destination.is_symlink():
        raise ContainerError("解包目标不能是符号链接。")
    if destination.exists() and not (destination.is_dir() and not any(destination.iterdir())):
        raise ContainerError("解包目标必须是新目录或空目录。")
    if not destination.parent.is_dir():
        raise ContainerError("解包目标的上级目录不存在。")

    stage = destination.parent / (".backnest-unpack-" + _secrets.token_hex(6))
    stage.mkdir()
    directories: list[tuple[Path, Entry]] = []
    files = 0
    processed = 0
    try:
        with source.open("rb") as stream:
            count, declared_total, _created = _read_header(stream)
            if max_output_bytes and declared_total > max_output_bytes:
                raise ContainerError("归档声明的大小超过允许的输出上限。")
            kinds: dict[str, str] = {}
            for _ in range(count):
                entry = _read_entry(stream)
                if entry.path in kinds:
                    raise ContainerError("归档包含重复条目。")
                parent = entry.path.rsplit("/", 1)[0] if "/" in entry.path else ""
                if parent and kinds.get(parent) != "directory":
                    raise ContainerError("归档条目缺少父目录或父条目不是目录。")
                kinds[entry.path] = entry.kind
                target = stage.joinpath(*entry.path.split("/"))
                if entry.kind == "directory":
                    if target.exists():
                        raise ContainerError("归档条目的目录名发生冲突。")
                    target.mkdir()
                    directories.append((target, entry))
                    continue
                if target.exists():
                    raise ContainerError("归档条目的文件名发生冲突。")
                if max_output_bytes and processed + entry.size > max_output_bytes:
                    raise ContainerError("解包内容超过允许的输出上限。")
                crc = CRC32()
                remaining = entry.size
                try:
                    with target.open("xb") as out:
                        while remaining:
                            chunk = stream.read(min(COPY_BUFFER, remaining))
                            if not chunk:
                                raise ContainerError("归档数据被截断。")
                            out.write(chunk)
                            crc.update(chunk)
                            remaining -= len(chunk)
                            processed += len(chunk)
                            if progress:
                                progress(processed, declared_total)
                except OSError as error:
                    raise ContainerError(f"无法写入解包文件：{target}") from error
                if crc.digest() != entry.crc:
                    raise ContainerError("归档内容校验失败，文件可能已损坏。")
                try:
                    os.chmod(target, entry.mode or 0o644)
                    os.utime(target, ns=(entry.mtime_ns, entry.mtime_ns))
                except OSError:
                    pass
                files += 1
            if processed != declared_total:
                raise ContainerError("归档声明的大小与实际内容不一致。")
            if stream.read(len(TRAILER)) != TRAILER:
                raise ContainerError("归档文件结尾缺失或已损坏。")
            if stream.read(1):
                raise ContainerError("归档文件包含多余数据。")
        if destination.exists():
            destination.rmdir()
        os.replace(stage, destination)
        for path, entry in reversed(directories):
            try:
                os.chmod(path, entry.mode or 0o755)
                os.utime(path, ns=(entry.mtime_ns, entry.mtime_ns))
            except OSError:
                pass
    except Exception:
        for child in sorted(stage.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                child.rmdir() if child.is_dir() else child.unlink()
            except OSError:
                pass
        try:
            stage.rmdir()
        except OSError:
            pass
        raise
    return {
        "path": str(destination),
        "bytes": processed,
        "files": files,
        "directories": len(directories),
        "verified": True,
    }


def entry_to_metadata(entry: Entry) -> dict:
    return {
        "path": entry.path,
        "kind": entry.kind,
        "bytes": entry.size,
        "modifiedAt": datetime.fromtimestamp(entry.mtime_ns / 1e9, tz=timezone.utc).isoformat(),
        "permissions": {"mode": entry.mode},
    }


def iter_entries(source: Path) -> Iterator[Entry]:
    yield from list_entries(source)
