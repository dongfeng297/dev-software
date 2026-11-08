"""Dependency-free backup engine used by the Python bridge.

The on-disk snapshot format is the same `BACKNEST 1` format written by the C++
engine, so snapshots are interchangeable: the C++ executable can verify and
restore snapshots created here, and this module can verify/restore snapshots
created by the C++ engine.  Full backups copy every file.  Incremental backups
compare size+CRC with the latest snapshot and hard-link unchanged payloads
(falling back to a copy), so every retained version remains self-contained and
restorable even if an older snapshot is removed.

No symlinks or special files are accepted.  Every version is staged in
`.partial-*`, verified, and only then renamed to `snapshot-*`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets as _secrets
import shutil
import stat
import time
from typing import Callable, Iterable

from .checksums import CRC32

COPY_BUFFER = 64 * 1024
MAX_ENTRIES = 200_000
MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.bkn"
DATA_NAME = "data"


class EngineError(RuntimeError):
    """Raised for invalid paths, unsafe manifests or failed file operations."""


@dataclass
class Entry:
    relative: str
    directory: bool
    size: int = 0
    crc: int = 0
    mtime_ns: int = 0


@dataclass
class Manifest:
    source: str
    entries: list[Entry] = field(default_factory=list)
    bytes: int = 0
    files: int = 0
    directories: int = 0


def _text(path: Path) -> str:
    try:
        return path.as_posix()
    except (UnicodeError, ValueError) as error:
        raise EngineError("路径无法转换为 UTF-8。") from error


def _resolve_existing(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise EngineError("请使用绝对路径。")
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise EngineError(f"路径不存在或不可访问：{path}") from error


def _resolve_target(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise EngineError("请使用绝对路径。")
    try:
        return path.resolve(strict=False)
    except OSError as error:
        raise EngineError(f"目标路径不可用：{path}") from error


def _no_links(path: Path) -> None:
    current = Path(path)
    for candidate in [current, *current.parents]:
        if candidate.exists() and candidate.is_symlink():
            raise EngineError(f"不支持符号链接路径：{candidate}")


def _disjoint(first: Path, second: Path) -> None:
    if first == second or first.is_relative_to(second) or second.is_relative_to(first):
        raise EngineError("源目录和备份位置不能相同或互相包含。")


def _safe_relative(name: str) -> str:
    if not name or "\0" in name or "\\" in name or "\n" in name or "\r" in name:
        raise EngineError("备份中包含不安全的相对路径。")
    if name.startswith("/"):
        raise EngineError("备份中不允许绝对路径。")
    for part in name.split("/"):
        if part in ("", ".", ".."):
            raise EngineError("备份中包含不安全的相对路径。")
    return name


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(value: str) -> str:
    if len(value) < 2 or not value.startswith('"') or not value.endswith('"'):
        raise EngineError("备份清单中的字符串格式不正确。")
    result = []
    escaped = False
    for character in value[1:-1]:
        if escaped:
            result.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        else:
            result.append(character)
    if escaped:
        raise EngineError("备份清单中的转义不完整。")
    return "".join(result)


def _split_quoted(line: str, parts: int) -> tuple[list[str], str]:
    """Split *parts* whitespace-delimited tokens, then one quoted token."""
    tokens: list[str] = []
    position = 0
    length = len(line)
    for _ in range(parts):
        while position < length and line[position] in " \t":
            position += 1
        start = position
        while position < length and line[position] not in " \t":
            position += 1
        if start == position:
            raise EngineError("备份清单格式不正确。")
        tokens.append(line[start:position])
    while position < length and line[position] in " \t":
        position += 1
    return tokens, line[position:]


def scan(source: Path) -> Manifest:
    source = _resolve_existing(source)
    _no_links(source)
    if not source.is_dir():
        raise EngineError("源目录不存在。")
    manifest = Manifest(source=_text(source))
    total = 0

    def walk(directory: Path, prefix: str) -> None:
        nonlocal total
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise EngineError(f"无法读取源目录：{directory}") from error
        for item in children:
            relative = item.name if not prefix else prefix + "/" + item.name
            _safe_relative(relative)
            try:
                info = item.stat(follow_symlinks=False)
            except OSError as error:
                raise EngineError(f"无法读取文件信息：{item.path}") from error
            if stat.S_ISLNK(info.st_mode):
                raise EngineError("基础版不支持符号链接。")
            if stat.S_ISDIR(info.st_mode):
                manifest.entries.append(Entry(relative, True, mtime_ns=info.st_mtime_ns))
                manifest.directories += 1
                if len(manifest.entries) > MAX_ENTRIES:
                    raise EngineError("条目数量超过限制。")
                walk(Path(item.path), relative)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > (1 << 63) - 1:
                    raise EngineError("文件总大小溢出。")
                manifest.entries.append(Entry(relative, False, info.st_size, 0, info.st_mtime_ns))
                manifest.bytes += info.st_size
                manifest.files += 1
                if len(manifest.entries) > MAX_ENTRIES:
                    raise EngineError("条目数量超过限制。")
            else:
                raise EngineError("基础版不支持特殊文件。")

    walk(source, "")
    manifest.entries.sort(key=lambda entry: (not entry.directory, entry.relative))
    return manifest


def _checksum(path: Path, *, cancel=None) -> tuple[int, int]:
    if path.is_symlink() or not path.is_file():
        raise EngineError(f"不是普通文件：{path}")
    crc = CRC32()
    size = 0
    try:
        with path.open("rb") as stream:
            while True:
                if cancel is not None and cancel.is_set():
                    raise EngineError("操作已取消。")
                chunk = stream.read(COPY_BUFFER)
                if not chunk:
                    break
                crc.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise EngineError(f"无法读取文件：{path}") from error
    return size, crc.digest()


def _copy_checked(source: Path, destination: Path, entry: Entry, *, cancel=None,
                  progress=None, total=0, processed=0, done=0, total_files=0) -> int:
    if source.is_symlink() or not source.is_file():
        raise EngineError(f"源文件已不是普通文件：{source}")
    if destination.exists():
        raise EngineError(f"目标文件已存在：{destination}")
    crc = CRC32()
    size = 0
    started = time.monotonic()
    try:
        before = source.stat()
        with source.open("rb") as reader, destination.open("xb") as writer:
            while True:
                if cancel is not None and cancel.is_set():
                    raise EngineError("操作已取消。")
                chunk = reader.read(COPY_BUFFER)
                if not chunk:
                    break
                writer.write(chunk)
                crc.update(chunk)
                size += len(chunk)
                if progress and time.monotonic() - started >= 0.1:
                    progress("copying", processed + size, total, done, total_files, entry.relative)
                    started = time.monotonic()
        after = source.stat()
    except OSError as error:
        raise EngineError(f"文件复制失败：{source} -> {destination}") from error
    if size != entry.size or before.st_size != entry.size:
        raise EngineError(f"源文件在复制过程中发生变化：{entry.relative}")
    if before.st_mtime_ns != after.st_mtime_ns:
        raise EngineError(f"源文件在复制过程中发生变化：{entry.relative}")
    if progress:
        progress("copying", processed + size, total, done, total_files, entry.relative)
    return crc.digest()


def write_manifest(path: Path, manifest: Manifest) -> None:
    lines = [f"BACKNEST {MANIFEST_VERSION}",
             f"{manifest.files} {manifest.directories} {manifest.bytes}",
             _quote(manifest.source)]
    for entry in manifest.entries:
        if entry.directory:
            lines.append("D " + _quote(entry.relative))
        else:
            lines.append(f"F {entry.size} {entry.crc} " + _quote(entry.relative))
    try:
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
    except OSError as error:
        raise EngineError("无法写入备份清单。") from error


def read_manifest(snapshot: Path) -> Manifest:
    snapshot = Path(snapshot)
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise EngineError("备份版本目录不存在。")
    manifest_path = snapshot / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise EngineError("备份清单缺失或不是普通文件。")
    if manifest_path.stat().st_size > 64 * 1024 * 1024:
        raise EngineError("备份清单过大。")
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise EngineError("无法读取备份清单。") from error
    if len(lines) < 3:
        raise EngineError("备份清单被截断。")
    if lines[0] != f"BACKNEST {MANIFEST_VERSION}":
        raise EngineError("备份清单格式无效或版本不受支持。")
    try:
        files, directories, total_bytes = (int(value) for value in lines[1].split())
    except ValueError as error:
        raise EngineError("备份清单总计格式不正确。") from error
    if min(files, directories, total_bytes) < 0 or files + directories > MAX_ENTRIES:
        raise EngineError("备份清单条目数量不合法。")
    source = _unquote(lines[2].strip())
    entries: list[Entry] = []
    seen: dict[str, bool] = {}
    actual_files = 0
    actual_directories = 0
    actual_bytes = 0
    index = 3
    for _ in range(files + directories):
        if index >= len(lines):
            raise EngineError("备份清单被截断。")
        line = lines[index]
        index += 1
        if not line:
            raise EngineError("备份清单条目为空。")
        kind = line[0]
        if kind == "D":
            tokens, quoted = _split_quoted(line, 1)
            del tokens
            entry = Entry(_safe_relative(_unquote(quoted)), True)
            actual_directories += 1
        elif kind == "F":
            tokens, quoted = _split_quoted(line, 3)
            try:
                size, crc = int(tokens[1]), int(tokens[2])
            except ValueError as error:
                raise EngineError("备份清单条目的大小或校验和无效。") from error
            if size < 0 or crc < 0 or crc > 0xFFFFFFFF:
                raise EngineError("备份清单条目的大小或校验和无效。")
            entry = Entry(_safe_relative(_unquote(quoted)), False, size, crc)
            actual_files += 1
            actual_bytes += size
        else:
            raise EngineError("备份清单包含无效的条目类型。")
        if entry.relative in seen:
            raise EngineError("备份清单包含重复条目。")
        seen[entry.relative] = entry.directory
        entries.append(entry)
    if index != len(lines):
        raise EngineError("备份清单包含多余内容。")
    if (actual_files, actual_directories, actual_bytes) != (files, directories, total_bytes):
        raise EngineError("备份清单总计与条目不一致。")
    for entry in entries:
        parent = entry.relative.rsplit("/", 1)[0] if "/" in entry.relative else ""
        if parent and seen.get(parent) is not True:
            raise EngineError("备份清单缺少父目录。")
    entries.sort(key=lambda entry: (not entry.directory, entry.relative))
    return Manifest(source, entries, actual_bytes, actual_files, actual_directories)


def _data_tree(data: Path) -> Iterable[tuple[str, Path, os.stat_result]]:
    def walk(directory: Path, prefix: str):
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise EngineError(f"无法读取备份数据目录：{directory}") from error
        for item in children:
            relative = item.name if not prefix else prefix + "/" + item.name
            info = item.stat(follow_symlinks=False)
            yield relative, Path(item.path), info
            if stat.S_ISDIR(info.st_mode):
                yield from walk(Path(item.path), relative)

    yield from walk(data, "")


def verify(snapshot: Path, *, progress: Callable[[str, int, int, int, int, str], None] | None = None,
           cancel=None) -> Manifest:
    snapshot = _resolve_existing(snapshot)
    _no_links(snapshot)
    manifest = read_manifest(snapshot)
    data = snapshot / DATA_NAME
    if data.is_symlink() or not data.is_dir():
        raise EngineError("备份数据目录缺失。")
    expected = {entry.relative: entry for entry in manifest.entries}
    seen: set[str] = set()
    bytes_done = 0
    files_done = 0
    for relative, path, info in _data_tree(data):
        if cancel is not None and cancel.is_set():
            raise EngineError("操作已取消。")
        if stat.S_ISLNK(info.st_mode):
            raise EngineError("备份数据中不允许符号链接。")
        entry = expected.get(relative)
        if entry is None:
            raise EngineError(f"备份中包含清单外的文件：{relative}")
        if entry.directory:
            if not stat.S_ISDIR(info.st_mode):
                raise EngineError(f"备份条目的类型不一致：{relative}")
        else:
            if not stat.S_ISREG(info.st_mode):
                raise EngineError(f"备份条目的类型不一致：{relative}")
            size, crc = _checksum(path, cancel=cancel)
            if size != entry.size or crc != entry.crc:
                raise EngineError(f"备份内容校验失败：{relative}")
            bytes_done += size
            files_done += 1
            if progress:
                progress("verifying", bytes_done, manifest.bytes, files_done, manifest.files, relative)
        seen.add(relative)
    if len(seen) != len(expected):
        raise EngineError("备份中有文件或目录缺失。")
    return manifest


def _unique_id() -> str:
    return f"{time.time_ns() // 1000}-{_secrets.token_hex(4)}"


def latest_snapshot(root: Path) -> Path | None:
    root = Path(root)
    if not root.is_dir():
        return None
    candidates = [item for item in root.iterdir()
                  if item.is_dir() and not item.is_symlink() and item.name.startswith("snapshot-")]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item.stat().st_mtime_ns, item.name))
    return candidates[-1]


def _result(path: Path, manifest: Manifest, operation: str) -> dict:
    return {
        "operation": operation,
        "path": _text(path),
        "source": manifest.source,
        "files": manifest.files,
        "directories": manifest.directories,
        "bytes": manifest.bytes,
        "verified": True,
        "completedAt": datetime.now(timezone.utc).isoformat(),
    }


def backup(source: Path, root: Path, *, incremental: bool = False,
           progress: Callable[[str, int, int, int, int, str], None] | None = None,
           cancel=None) -> dict:
    source = _resolve_existing(source)
    root = _resolve_target(root)
    _no_links(source)
    _no_links(root)
    if not source.is_dir():
        raise EngineError("源目录不存在。")
    _disjoint(source, root)
    manifest = scan(source)
    base_snapshot = latest_snapshot(root) if incremental else None
    base: dict[str, Entry] = {}
    if base_snapshot is not None:
        try:
            base = {entry.relative: entry for entry in read_manifest(base_snapshot).entries if not entry.directory}
        except EngineError:
            base = {}
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise EngineError(f"无法创建备份位置：{root}") from error
    identifier = _unique_id()
    stage = root / (".partial-" + identifier)
    final = root / ("snapshot-" + identifier)
    try:
        stage.mkdir()
    except OSError as error:
        raise EngineError("无法创建临时备份目录。") from error
    try:
        data = stage / DATA_NAME
        data.mkdir()
        processed = 0
        done = 0
        for entry in manifest.entries:
            if cancel is not None and cancel.is_set():
                raise EngineError("操作已取消。")
            destination = data.joinpath(*entry.relative.split("/"))
            if entry.directory:
                destination.mkdir()
                continue
            file_source = source.joinpath(*entry.relative.split("/"))
            if progress:
                progress("copying", processed, manifest.bytes, done, manifest.files, entry.relative)
            baseline = base.get(entry.relative)
            linked = False
            if baseline is not None and baseline.size == entry.size and base_snapshot is not None:
                size, crc = _checksum(file_source, cancel=cancel)
                if size == baseline.size and crc == baseline.crc:
                    base_file = base_snapshot / DATA_NAME / Path(*entry.relative.split("/"))
                    try:
                        if base_file.is_file() and not base_file.is_symlink() and base_file.stat().st_size == entry.size:
                            os.link(base_file, destination)
                            entry.crc = crc
                            linked = True
                    except OSError:
                        linked = False
            if not linked:
                entry.crc = _copy_checked(file_source, destination, entry, cancel=cancel,
                                         progress=progress, total=manifest.bytes, processed=processed,
                                         done=done, total_files=manifest.files)
            processed += entry.size
            done += 1
            if progress:
                progress("copying", processed, manifest.bytes, done, manifest.files, entry.relative)
        after = scan(source)
        if [(_entry.relative, _entry.directory) for _entry in after.entries] != \
           [(_entry.relative, _entry.directory) for _entry in manifest.entries]:
            raise EngineError("源目录在备份过程中发生变化。")
        for before_entry, after_entry in zip(manifest.entries, after.entries):
            if not before_entry.directory and (before_entry.size != after_entry.size
                                               or before_entry.mtime_ns != after_entry.mtime_ns):
                raise EngineError("源目录在备份过程中发生变化。")
        write_manifest(stage / MANIFEST_NAME, manifest)
        verify(stage, progress=progress, cancel=cancel)
        os.replace(stage, final)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    result = _result(final, manifest, "backup")
    return result


def restore(snapshot: Path, destination: Path, *,
            progress: Callable[[str, int, int, int, int, str], None] | None = None,
            cancel=None) -> dict:
    snapshot = _resolve_existing(snapshot)
    destination = _resolve_target(destination)
    _no_links(snapshot)
    _no_links(destination)
    _disjoint(snapshot, destination)
    if destination.exists() and not (destination.is_dir() and not any(destination.iterdir())):
        raise EngineError("还原目标必须是新目录或空目录。")
    manifest = verify(snapshot, progress=progress, cancel=cancel)
    if not destination.parent.is_dir():
        raise EngineError("还原目标的上级目录不存在。")
    stage = destination.parent / (".backnest-restore-" + _unique_id())
    removed_target = False
    try:
        stage.mkdir()
    except OSError as error:
        raise EngineError("无法创建临时还原目录。") from error
    try:
        processed = 0
        done = 0
        for entry in manifest.entries:
            if cancel is not None and cancel.is_set():
                raise EngineError("操作已取消。")
            target = stage.joinpath(*entry.relative.split("/"))
            if entry.directory:
                target.mkdir()
                continue
            source = snapshot / DATA_NAME / Path(*entry.relative.split("/"))
            crc = _copy_checked(source, target, entry, cancel=cancel, progress=progress,
                                total=manifest.bytes, processed=processed,
                                done=done, total_files=manifest.files)
            if crc != entry.crc:
                raise EngineError(f"备份在还原过程中发生变化：{entry.relative}")
            processed += entry.size
            done += 1
            if progress:
                progress("copying", processed, manifest.bytes, done, manifest.files, entry.relative)
        _no_links(destination)
        if destination.exists():
            if destination.is_symlink() or any(destination.iterdir()):
                raise EngineError("还原目标已发生变化，不会覆盖已有文件。")
            destination.rmdir()
            removed_target = True
        os.replace(stage, destination)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        if removed_target and not destination.exists():
            try:
                destination.mkdir()
            except OSError:
                pass
        raise
    return _result(destination, manifest, "restore")


def incremental_preview(source: Path, root: Path) -> dict:
    source = _resolve_existing(source)
    root = _resolve_target(root)
    _disjoint(source, root)
    manifest = scan(source)
    latest = latest_snapshot(root)
    base: dict[str, Entry] = {}
    base_id = ""
    if latest is not None:
        base_id = latest.name
        base = {entry.relative: entry for entry in read_manifest(latest).entries}
    current = {entry.relative: entry for entry in manifest.entries}
    added = [entry for relative, entry in current.items() if relative not in base]
    deleted = [entry for relative, entry in base.items() if relative not in current]
    changed: list[Entry] = []
    for relative, entry in current.items():
        old = base.get(relative)
        if old is None:
            continue
        if entry.directory or old.directory:
            if entry.directory != old.directory:
                changed.append(entry)
            continue
        if entry.size != old.size:
            changed.append(entry)
            continue
        size, crc = _checksum(source.joinpath(*relative.split("/")))
        if size != old.size or crc != old.crc:
            changed.append(entry)
    def metadata(entry: Entry) -> dict:
        return {"path": entry.relative, "kind": "directory" if entry.directory else "file",
                "bytes": entry.size}
    return {
        "added": [metadata(entry) for entry in added],
        "changed": [metadata(entry) for entry in changed],
        "deleted": [metadata(entry) for entry in deleted],
        "baseSnapshotId": base_id,
        "files": manifest.files,
        "bytes": manifest.bytes,
    }
