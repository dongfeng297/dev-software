"""Small JSON repository for backend-owned tasks, schedules and activity.

It intentionally stores *configuration* only (never passwords or secretRef
values).  The file lives under `.backnest/` which is git-ignored.  Writes are
atomic: a unique temporary file is fsynced and then replaced.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import threading
from typing import Any

STORE_VERSION = 1
MAX_ACTIVITY = 500


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return prefix + "-" + secrets.token_hex(6)


def empty_state() -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "tasks": {},
        "schedules": {},
        "settings": {"destination": "", "defaultOptions": {}, "keepVersions": 0, "notifications": False},
        "snapshots": [],
        "activity": [],
    }


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = empty_state()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
                raise ValueError("unsupported store version")
            for key, default in empty_state().items():
                raw.setdefault(key, default)
            self.data = raw
        except (OSError, ValueError, json.JSONDecodeError) as error:
            # Keep a copy of the damaged file instead of silently deleting it.
            try:
                self.path.replace(self.path.with_suffix(self.path.suffix + ".corrupt"))
            except OSError:
                pass
            self.data = empty_state()
            self.data["loadError"] = str(error)

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".partial-" + secrets.token_hex(4))
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    json.dump(self.data, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except Exception:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise

    # tasks ---------------------------------------------------------------
    def list_tasks(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(list(self.data["tasks"].values()))

    def get_task(self, task_id: str) -> dict | None:
        with self.lock:
            record = self.data["tasks"].get(task_id)
            return copy.deepcopy(record) if record is not None else None

    def put_task(self, record: dict) -> dict:
        with self.lock:
            self.data["tasks"][record["id"]] = record
            self.save()
            return record

    def delete_task(self, task_id: str) -> bool:
        with self.lock:
            existed = self.data["tasks"].pop(task_id, None) is not None
            for schedule_id in [key for key, value in self.data["schedules"].items() if value.get("taskId") == task_id]:
                self.data["schedules"].pop(schedule_id, None)
            if existed:
                self.save()
            return existed

    # schedules -----------------------------------------------------------
    def list_schedules(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(list(self.data["schedules"].values()))

    def get_schedule(self, schedule_id: str) -> dict | None:
        with self.lock:
            record = self.data["schedules"].get(schedule_id)
            return copy.deepcopy(record) if record is not None else None

    def put_schedule(self, record: dict) -> dict:
        with self.lock:
            self.data["schedules"][record["id"]] = record
            self.save()
            return record

    def delete_schedule(self, schedule_id: str) -> bool:
        with self.lock:
            existed = self.data["schedules"].pop(schedule_id, None) is not None
            if existed:
                self.save()
            return existed

    # settings ------------------------------------------------------------
    def get_settings(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data["settings"])

    def put_settings(self, values: dict) -> dict:
        with self.lock:
            self.data["settings"].update(values)
            self.save()
            return copy.deepcopy(self.data["settings"])

    # snapshots -----------------------------------------------------------
    def list_snapshots(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(self.data["snapshots"])

    def register_snapshot(self, record: dict) -> dict:
        with self.lock:
            self.data["snapshots"] = [item for item in self.data["snapshots"] if item.get("path") != record.get("path")]
            self.data["snapshots"].append(record)
            self.save()
            return record

    def delete_snapshot_index(self, snapshot_id: str) -> bool:
        with self.lock:
            before = len(self.data["snapshots"])
            self.data["snapshots"] = [item for item in self.data["snapshots"]
                                      if item.get("id") != snapshot_id and item.get("path") != snapshot_id]
            changed = len(self.data["snapshots"]) != before
            if changed:
                self.save()
            return changed

    # activity ------------------------------------------------------------
    def add_activity(self, *, title: str, detail: str = "", level: str = "info",
                     task_id: str = "", job_id: str = "") -> dict:
        record = {"id": new_id("log"), "taskId": task_id, "jobId": job_id, "time": now_iso(),
                  "level": level, "title": title, "detail": detail}
        with self.lock:
            self.data["activity"].insert(0, record)
            del self.data["activity"][MAX_ACTIVITY:]
            self.save()
            return record

    def list_activity(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(self.data["activity"])

    # overview ------------------------------------------------------------
    def overview(self) -> dict:
        with self.lock:
            snapshots = self.data["snapshots"]
            activity = self.data["activity"]
            return {
                "tasks": len(self.data["tasks"]),
                "snapshots": len(snapshots),
                "backedUpFiles": sum(int(item.get("files", 0)) for item in snapshots),
                "lastActivity": activity[0] if activity else None,
            }
