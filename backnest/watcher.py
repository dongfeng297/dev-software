"""Debounced polling watcher for near-real-time backups.

A polling watcher is used instead of OS notifications so the same logic works
on every platform without extra dependencies.  Changes are compared as a tree
signature (relative path + size + mtime + kind).  After a change settles for
`debounceMs`, the trigger callback starts an incremental backup through the
same job executor as manual backups.  The destination and explicit excluded
paths are never scanned, so a backup cannot trigger itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Callable

from .store import Store, now_iso


class WatcherError(ValueError):
    pass


def _signature(root: Path, excluded: list[Path], stop: threading.Event) -> tuple:
    if root.is_symlink() or not root.is_dir():
        raise WatcherError("监听源目录不存在或不是普通目录。")
    items: list[tuple[str, int, int, bool]] = []

    def excluded_path(path: Path) -> bool:
        for item in excluded:
            try:
                if path == item or path.is_relative_to(item):
                    return True
            except ValueError:
                continue
        return False

    def walk(directory: Path, prefix: str) -> None:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise WatcherError(f"无法读取监听目录：{directory}") from error
        for item in children:
            if stop.is_set():
                return
            path = Path(item.path)
            if excluded_path(path):
                continue
            relative = item.name if not prefix else prefix + "/" + item.name
            info = item.stat(follow_symlinks=False)
            if item.is_symlink() or os.path.islink(item.path):
                raise WatcherError("实时监听不支持符号链接。")
            is_dir = os.path.isdir(item.path)
            items.append((relative, info.st_size, info.st_mtime_ns, is_dir))
            if is_dir:
                walk(path, relative)
            elif not os.path.isfile(item.path):
                raise WatcherError("实时监听不支持特殊文件。")

    walk(root, "")
    return tuple(items)


class _Watcher:
    def __init__(self, identifier: str, request: dict, trigger: Callable[[str, str], bool]):
        self.id = identifier
        self.task_id = request["taskId"]
        self.source = Path(request["source"])
        self.debounce_ms = int(request.get("debounceMs", 500))
        self.excluded = [Path(item).expanduser().resolve(strict=False) for item in request.get("excludedPaths", [])]
        self.trigger = trigger
        self.state = "running"
        self.error = ""
        self.last_event_at = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="backnest-watch-" + identifier, daemon=True)
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)
        with self._lock:
            self.state = "stopped"

    def status(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "taskId": self.task_id,
                "source": str(self.source),
                "state": self.state,
                "lastEventAt": self.last_event_at,
                "error": self.error,
            }

    def _set_state(self, state: str, error: str = "") -> None:
        with self._lock:
            self.state = state
            self.error = error
            self.last_event_at = now_iso()

    def _run(self) -> None:
        try:
            baseline = _signature(self.source, self.excluded, self._stop)
        except Exception as error:
            self._set_state("failed", str(error))
            return
        last_change = 0.0
        last_attempt = 0.0
        pending = False
        interval = max(0.2, min(1.0, self.debounce_ms / 2000))
        while not self._stop.wait(interval):
            try:
                current = _signature(self.source, self.excluded, self._stop)
            except Exception as error:
                self._set_state("failed", str(error))
                return
            now = time.monotonic()
            if current != baseline:
                baseline = current
                last_change = now
                pending = True
                self._set_state("running")
            if not pending:
                continue
            quiet = (now - last_change) * 1000.0
            if quiet < self.debounce_ms:
                continue
            if now - last_attempt < max(1.0, self.debounce_ms / 1000.0):
                continue
            last_attempt = now
            try:
                started = self.trigger(self.task_id, self.id)
            except Exception as error:
                self._set_state("failed", str(error))
                return
            if started:
                # The backup reads a stable-enough snapshot.  Any change that
                # happens after this point is detected on the next poll because
                # it will differ from the new baseline.
                pending = False
                last_change = 0.0
                try:
                    baseline = _signature(self.source, self.excluded, self._stop)
                except Exception:
                    pass
                self._set_state("running")
            else:
                self._set_state("running", "备份正在运行，稍后重试。")


class WatcherManager:
    def __init__(self, store: Store, trigger: Callable[[str, str], bool]):
        self.store = store
        self.trigger = trigger
        self._watchers: dict[str, _Watcher] = {}
        self._lock = threading.Lock()

    def start(self, request: dict) -> dict:
        source = Path(request["source"]).expanduser()
        if not source.is_absolute():
            raise WatcherError("监听源必须是绝对路径。")
        source = source.resolve(strict=True)
        if not source.is_dir():
            raise WatcherError("监听源必须是目录。")
        task = self.store.get_task(request["taskId"])
        excluded = list(request.get("excludedPaths", []))
        if task:
            excluded.append(task["destination"])
        identifier = "watch-" + secrets.token_hex(6)
        watcher = _Watcher(identifier, {**request, "source": str(source), "excludedPaths": excluded}, self.trigger)
        with self._lock:
            self._watchers[identifier] = watcher
        watcher.start()
        return watcher.status()

    def get(self, watch_id: str) -> dict | None:
        with self._lock:
            watcher = self._watchers.get(watch_id)
        return watcher.status() if watcher else None

    def stop(self, watch_id: str) -> bool:
        with self._lock:
            watcher = self._watchers.pop(watch_id, None)
        if not watcher:
            return False
        watcher.stop()
        return True

    def stop_all(self) -> None:
        with self._lock:
            watchers = list(self._watchers.values())
            self._watchers.clear()
        for watcher in watchers:
            watcher.stop()
