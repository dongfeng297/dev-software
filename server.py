"""Local demo bridge for BackNest.

File operations are performed by the C++ executable when a C++17 compiler is
available; otherwise a compatible Python engine (`backnest.engine`) is used so
the demo still runs.  Archive/compression/encryption algorithms are implemented
in the `backnest` package (crypto uses the OS crypto library on Windows).

Run: python3 server.py --port 4173
Only binds loopback. This development bridge is not an internet-facing service.
"""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

from api_contract import OPERATIONS, SCHEMAS, capabilities, match_operation, openapi, unsupported_option, validate
from backnest import container, crypto, engine
from backnest import compress as compression
from backnest.scheduler import ScheduleError, Scheduler, next_run, next_runs
from backnest.secrets import SecretError, SecretStore
from backnest.store import Store, new_id, now_iso
from backnest.watcher import WatcherError, WatcherManager

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from build import build, ROOT  # noqa: E402

STATIC_FILES = {"index.html", "app.js", "state.js", "native.js", "styles.css", "backend-api.js", "workspace-store.js"}
MAX_JOBS = 100


def _absolute(value: str, label: str = "路径") -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label}必须使用绝对路径，或点击“浏览”选择本机目录。")
    return path


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, state_dir=None):
        self.binary = None
        self.build_error = ""
        try:
            self.binary = build()
        except Exception as error:  # A missing compiler must not hide the feature endpoints.
            self.build_error = str(error)
        self.engine_name = "C++17" if self.binary else "Python"
        self.force_python = bool(os.environ.get("BACKNEST_FORCE_PYTHON"))
        self.state_dir = Path(state_dir) if state_dir else ROOT / ".backnest"
        self.store = Store(self.state_dir / "backend.json")
        self.secrets = SecretStore()
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.engine_lock = threading.Lock()
        self._closing = False
        self.watchers = WatcherManager(self.store, self._trigger_task)
        self.scheduler = Scheduler(self.store, self._trigger_task)
        super().__init__(("127.0.0.1", port), Handler)
        self.scheduler.start()

    # ---------------------------------------------------------------- jobs
    def _new_job(self, operation, task_id=""):
        job_id = uuid.uuid4().hex
        with self.jobs_lock:
            self.jobs[job_id] = {
                "id": job_id, "operation": operation, "state": "running", "phase": "starting",
                "files": 0, "totalFiles": 0, "bytes": 0, "totalBytes": 0, "path": "",
            }
        return job_id

    def _update_job(self, job_id, **fields):
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if job:
                job.update(fields)

    def _retain_jobs(self):
        with self.jobs_lock:
            for old in list(self.jobs)[:-MAX_JOBS]:
                if self.jobs[old]["state"] != "running":
                    del self.jobs[old]

    def start_python_job(self, operation, runner, *, on_success=None, on_failure=None, task_id=""):
        if not self.engine_lock.acquire(blocking=False):
            raise ValueError("已有备份或文件操作正在运行，请等待完成。")
        job_id = self._new_job(operation, task_id)
        cancel = threading.Event()

        def work():
            result = None
            failure = None
            try:
                result = runner(
                    lambda **fields: self._update_job(job_id, **fields),
                    cancel,
                )
                result.setdefault("completedAt", now_iso())
                if on_success:
                    try:
                        on_success(result, job_id, task_id)
                    except Exception as error:
                        self.store.add_activity(title="结果登记失败", detail=str(error), level="warning", task_id=task_id)
            except Exception as error:
                failure = str(error) or error.__class__.__name__
            finally:
                # Release before exposing a terminal state so a client that sees
                # "completed"/"failed" can immediately start the next operation.
                self.engine_lock.release()
            if failure is None:
                self._update_job(job_id, state="completed", phase="completed", result=result)
            else:
                self._update_job(job_id, state="failed", error=failure)
                if on_failure:
                    try:
                        on_failure(failure, job_id, task_id)
                    except Exception:
                        pass
            self._retain_jobs()

        threading.Thread(target=work, name="backnest-job-" + job_id, daemon=True).start()
        return job_id

    def start_native_job(self, operation, arguments, *, on_success=None, on_failure=None, task_id=""):
        if not self.binary:
            raise ValueError("C++ 引擎未构建，无法执行本机操作。")
        if not self.engine_lock.acquire(blocking=False):
            raise ValueError("已有备份或文件操作正在运行，请等待完成。")
        job_id = self._new_job(operation, task_id)
        threading.Thread(target=self._run_native_job,
                         args=(job_id, operation, arguments, on_success, on_failure, task_id),
                         name="backnest-native-" + job_id, daemon=True).start()
        return job_id

    def _run_native_job(self, job_id, operation, arguments, on_success, on_failure, task_id):
        process = None
        result = None
        failure = None
        try:
            process = subprocess.Popen([str(self.binary), operation] + arguments, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "progress":
                    self._update_job(job_id, **{k: v for k, v in event.items() if k != "event"})
                elif event.get("event") == "result":
                    result = {k: v for k, v in event.items() if k != "event"}
                elif event.get("event") == "error":
                    failure = event.get("message", "本机引擎执行失败。")
            code = process.wait()
            if code or failure or not result:
                raise RuntimeError(failure or "本机引擎未返回成功结果。")
            result["completedAt"] = now_iso()
            if on_success:
                try:
                    on_success(result, job_id, task_id)
                except Exception as error:
                    self.store.add_activity(title="结果登记失败", detail=str(error), level="warning", task_id=task_id)
        except Exception as error:
            failure = str(error) or error.__class__.__name__
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        finally:
            if process and process.stdout:
                process.stdout.close()
            # Release before exposing a terminal state (see start_python_job).
            self.engine_lock.release()
        if failure is None and result is not None:
            self._update_job(job_id, state="completed", phase="completed", result=result)
        else:
            message = failure or "本机引擎未返回成功结果。"
            self._update_job(job_id, state="failed", error=message)
            if on_failure:
                try:
                    on_failure(message, job_id, task_id)
                except Exception:
                    pass
        self._retain_jobs()

    # ------------------------------------------------------------- automation
    def _register_snapshot(self, result, task_id="", remote_id=""):
        path = Path(result.get("path", ""))
        record = {
            "id": path.name or new_id("snapshot"),
            "taskId": task_id,
            "remoteId": remote_id,
            "path": path.as_posix(),
            "createdAt": result.get("completedAt", now_iso()),
            "files": int(result.get("files", 0)),
            "bytes": int(result.get("bytes", 0)),
            "verified": bool(result.get("verified", False)),
        }
        self.store.register_snapshot(record)
        return record

    def _prune_versions(self, destination: Path):
        """Apply the optional backend retention preference.

        Only snapshots recorded by this backend under the same destination are
        considered; unregistered folders are never touched.  keepVersions=0
        means "keep every version".
        """
        try:
            keep = int((self.store.get_settings() or {}).get("keepVersions", 0) or 0)
        except (TypeError, ValueError):
            return
        if keep <= 0:
            return
        root = Path(destination).expanduser().resolve(strict=False)
        records = []
        for record in self.store.list_snapshots():
            path = Path(record.get("path", "")).expanduser().resolve(strict=False)
            if path.parent == root and path.name.startswith("snapshot-"):
                records.append((record, path))
        if len(records) <= keep:
            return
        records.sort(key=lambda item: (item[0].get("createdAt", ""), item[1].name))
        for record, path in records[:-keep]:
            if path == root or path.name == "." or path.is_symlink() or not path.is_dir():
                continue
            try:
                shutil.rmtree(path)
                self.store.delete_snapshot_index(record["id"])
                self.store.add_activity(title="已按版本策略清理旧版本",
                                        detail=f"保留最新 {keep} 个版本：{path.name}", level="info",
                                        task_id=record.get("taskId", ""))
            except OSError as error:
                self.store.add_activity(title="清理旧版本失败", detail=str(error), level="warning",
                                        task_id=record.get("taskId", ""))

    def _task_success(self, result, job_id, task_id):
        record = self._register_snapshot(result, task_id)
        self._prune_versions(Path(result["path"]).parent)
        task = self.store.get_task(task_id)
        if task:
            task["state"] = "completed"
            task["lastRunAt"] = now_iso()
            self.store.put_task(task)
        self.store.add_activity(
            title="自动备份完成",
            detail=f"{record['files']} 个文件 · {record['bytes']} 字节 · {record['path']}",
            level="success", task_id=task_id, job_id=job_id,
        )

    def _task_failure(self, message, job_id, task_id):
        task = self.store.get_task(task_id)
        if task:
            task["state"] = "failed"
            task["lastError"] = message
            self.store.put_task(task)
        self.store.add_activity(title="自动备份失败", detail=message, level="error",
                                task_id=task_id, job_id=job_id)

    def _trigger_task(self, task_id, reason):
        """Start one backup job for a schedule/watch. Never re-enters."""
        if self._closing:
            return False
        task = self.store.get_task(task_id)
        if not task:
            self.store.add_activity(title="自动备份跳过", detail="任务不存在或已删除。", level="warning", task_id=task_id)
            return False
        options = task.get("options") or {}
        incremental = reason.startswith("watch") or options.get("mode", "full") == "incremental"
        source = Path(task["source"]).expanduser()
        destination = Path(task["destination"]).expanduser()

        def runner(update, cancel):
            def report(phase, done, total, files, total_files, path):
                update(phase=phase, bytes=done, totalBytes=total, files=files, totalFiles=total_files, path=path)

            return engine.backup(source, destination, incremental=incremental, progress=report, cancel=cancel)

        try:
            job_id = self.start_python_job("backup", runner, on_success=self._task_success,
                                           on_failure=self._task_failure, task_id=task_id)
        except ValueError:
            self.store.add_activity(title="自动备份已跳过", detail="已有备份正在运行。", level="warning", task_id=task_id)
            return False
        task["state"] = "running"
        self.store.put_task(task)
        self.store.add_activity(title="自动备份已启动", detail=reason, level="info", task_id=task_id, job_id=job_id)
        return True

    # ------------------------------------------------------------------ close
    def server_close(self):
        self._closing = True
        try:
            self.watchers.stop_all()
        finally:
            self.scheduler.stop()
            super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "BackNestLocal/0.3"

    def log_message(self, fmt, *args):
        # Do not echo tokens or local file paths into access logs.
        pass

    def send_data(self, status, data, content_type="application/json; charset=utf-8"):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def local_request(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            self.contract_error(403, "只允许本地访问。", "FORBIDDEN")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in hosts}:
            self.contract_error(403, "拒绝跨来源请求。", "FORBIDDEN")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.contract_error(403, "拒绝跨站请求。", "FORBIDDEN")
            return False
        return True

    def authorized(self):
        if not secrets.compare_digest(self.headers.get("X-BackNest-Token", ""), self.server.token):
            self.contract_error(403, "本地会话已过期，请刷新页面。", "FORBIDDEN")
            return False
        return True

    def contract_error(self, status, message, code, operation_id=None, capability=None):
        result = {"error": message, "code": code}
        if operation_id:
            result["operationId"] = operation_id
        if capability:
            result["capability"] = capability
        self.send_data(status, result)

    def read_body(self, required=True):
        length = int(self.headers.get("Content-Length", "0"))
        if not required and not length:
            return {}
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise ValueError("请求必须使用 JSON。")
        if not 0 < length <= 65536:
            raise ValueError("请求大小不合法。")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("请求必须是 JSON 对象。")
        return body

    def send_directories(self, query):
        value = query.get("path", [str(ROOT)])[0]
        directory = Path(value).expanduser().resolve(strict=True)
        if not directory.is_dir():
            raise ValueError("请选择目录。")
        items = [{"name": child.name, "path": str(child)} for child in directory.iterdir()
                 if not child.is_symlink() and child.is_dir()]
        items.sort(key=lambda item: item["name"].casefold())
        self.send_data(200, {"path": str(directory), "parent": str(directory.parent), "directories": items})

    # ------------------------------------------------------------- operations
    def handle_contract(self, method):
        url = urlsplit(self.path)
        if not url.path.startswith("/api/v1/"):
            return False
        if not self.authorized():
            return True
        operation, parameters = match_operation(method, url.path)
        if not operation:
            known_path = any(match_operation(candidate, url.path)[0] for candidate in {op["method"] for op in OPERATIONS})
            self.contract_error(405 if known_path else 404, "接口不支持该方法。" if known_path else "接口不存在。",
                                "METHOD_NOT_ALLOWED" if known_path else "NOT_FOUND")
            return True
        operation_id = operation["id"]
        if not operation["implemented"]:
            self.contract_error(501, "接口已预留，后端功能尚未实现。", "NOT_IMPLEMENTED", operation_id, operation_id)
            return True
        try:
            if method in {"POST", "PATCH"}:
                body = self.read_body(required=bool(operation["request"]))
            else:
                body = {}
            if operation["request"] and method in {"POST", "PATCH"}:
                validate(body, SCHEMAS[operation["request"]])
            self.dispatch(operation_id, method, body, parameters, url)
        except (ValueError, OSError, engine.EngineError, container.ContainerError,
                compression.CodecError, crypto.CryptoError, ScheduleError, WatcherError, SecretError) as error:
            self.contract_error(400, str(error), "INVALID_ARGUMENT", operation_id)
        except Exception:
            self.contract_error(500, "后端执行异常，请检查本机服务。", "INTERNAL_ERROR", operation_id)
        return True

    def dispatch(self, operation_id, method, body, parameters, url):
        server = self.server
        if operation_id == "system.capabilities":
            return self.send_data(200, capabilities())
        if operation_id == "system.contract":
            return self.send_data(200, openapi())
        if operation_id == "filesystem.directories":
            return self.send_directories(parse_qs(url.query))
        if operation_id == "jobs.get":
            with server.jobs_lock:
                job = server.jobs.get(parameters["jobId"])
            if not job:
                return self.contract_error(404, "运行记录不存在，请检查目标目录。", "NOT_FOUND", operation_id)
            return self.send_data(200, job)

        if operation_id == "backup.start":
            return self.start_backup(body)
        if operation_id == "restore.start":
            return self.start_restore(body)
        if operation_id == "snapshots.verify":
            return self.start_verify(body)

        if operation_id in {"archive.algorithms", "compression.algorithms", "encryption.algorithms"}:
            table = {"archive": container.ALGORITHMS, "compression": compression.ALGORITHMS,
                     "encryption": crypto.ALGORITHMS}[operation_id.split(".", 1)[0]]
            return self.send_data(200, table)
        if operation_id == "archive.pack":
            return self.start_archive_pack(body)
        if operation_id == "archive.unpack":
            return self.start_archive_unpack(body)
        if operation_id == "archive.list":
            entries = container.list_entries(_absolute(body["snapshot"], "归档文件"))
            return self.send_data(200, [container.entry_to_metadata(entry) for entry in entries])
        if operation_id == "compression.compress":
            return self.start_compression(body, decompress=False)
        if operation_id == "compression.decompress":
            return self.start_compression(body, decompress=True)
        if operation_id == "encryption.encrypt":
            return self.start_encryption(body, decrypt=False)
        if operation_id == "encryption.decrypt":
            return self.start_encryption(body, decrypt=True)
        if operation_id == "secrets.create":
            created = server.secrets.create(body["value"], body.get("purpose", "encryption"))
            return self.send_data(200, created)
        if operation_id == "secrets.revoke":
            if not server.secrets.revoke(parameters["secretId"]):
                return self.contract_error(404, "secretRef 不存在或已过期。", "NOT_FOUND", operation_id)
            return self.send_data(200, {"id": parameters["secretId"], "recordDeleted": True, "dataDeleted": False})

        if operation_id == "tasks.list":
            return self.send_data(200, server.store.list_tasks())
        if operation_id == "tasks.create":
            return self.create_task(body)
        if operation_id == "tasks.update":
            return self.update_task(parameters["id"], body)
        if operation_id == "tasks.delete":
            existed = server.store.delete_task(parameters["id"])
            if not existed:
                return self.contract_error(404, "任务不存在。", "NOT_FOUND", operation_id)
            return self.send_data(200, {"id": parameters["id"], "recordDeleted": True, "dataDeleted": False})

        if operation_id == "schedules.list":
            return self.send_data(200, server.store.list_schedules())
        if operation_id == "schedules.create":
            return self.create_schedule(body)
        if operation_id == "schedules.update":
            return self.update_schedule(parameters["id"], body)
        if operation_id == "schedules.delete":
            existed = server.store.delete_schedule(parameters["id"])
            if not existed:
                return self.contract_error(404, "定时计划不存在。", "NOT_FOUND", operation_id)
            return self.send_data(200, {"id": parameters["id"], "recordDeleted": True, "dataDeleted": False})
        if operation_id == "schedules.nextRuns":
            schedule = server.store.get_schedule(parameters["id"])
            if not schedule:
                return self.contract_error(404, "定时计划不存在。", "NOT_FOUND", operation_id)
            return self.send_data(200, next_runs(schedule.get("expression", ""), schedule.get("timezone", "UTC"), 3))

        if operation_id == "watchers.start":
            return self.start_watcher(body)
        if operation_id == "watchers.get":
            status = server.watchers.get(parameters["watchId"])
            if not status:
                return self.contract_error(404, "监听不存在。", "NOT_FOUND", operation_id)
            return self.send_data(200, status)
        if operation_id == "watchers.stop":
            if not server.watchers.stop(parameters["watchId"]):
                return self.contract_error(404, "监听不存在。", "NOT_FOUND", operation_id)
            return self.send_data(200, {"id": parameters["watchId"], "recordDeleted": True, "dataDeleted": False})

        if operation_id == "incremental.preview":
            source = _absolute(body["source"], "源目录")
            destination = _absolute(body["destination"], "备份位置")
            preview = engine.incremental_preview(source, destination)
            preview.pop("files", None)
            preview.pop("bytes", None)
            return self.send_data(200, preview)

        if operation_id == "settings.get":
            return self.send_data(200, server.store.get_settings())
        if operation_id == "settings.update":
            return self.send_data(200, server.store.put_settings(body))

        if operation_id == "snapshots.list":
            return self.send_data(200, server.store.list_snapshots())
        if operation_id == "snapshots.register":
            return self.register_snapshot(body)
        if operation_id == "snapshots.delete":
            existed = server.store.delete_snapshot_index(parameters["snapshotId"])
            if not existed:
                return self.contract_error(404, "版本索引不存在。", "NOT_FOUND", operation_id)
            return self.send_data(200, {"id": parameters["snapshotId"], "recordDeleted": True, "dataDeleted": False})

        if operation_id == "activity.list":
            return self.send_data(200, server.store.list_activity())
        if operation_id == "activity.export":
            return self.send_data(200, {"format": "json", "records": server.store.list_activity()})
        if operation_id == "workspace.overview":
            return self.send_data(200, server.store.overview())

        return self.contract_error(501, "接口已预留，后端功能尚未实现。", "NOT_IMPLEMENTED", operation_id, operation_id)

    # ------------------------------------------------------------ file jobs
    def start_backup(self, body):
        server = self.server
        validate(body, SCHEMAS["BackupRequest"])
        unsupported = unsupported_option("backup.start", body)
        if unsupported:
            return self.contract_error(501, "该选项尚未实现，未执行文件操作。", "NOT_IMPLEMENTED", "backup.start", unsupported)
        source = _absolute(body["source"], "源目录")
        destination = _absolute(body["destination"], "备份位置")
        options = body.get("options") or {}
        incremental = options.get("mode", "full") == "incremental"
        if server.binary and not server.force_python and not incremental:
            job_id = server.start_native_job("backup", [str(source), str(destination)],
                                             on_success=self._backup_success)
        else:
            def runner(update, cancel):
                def report(phase, done, total, files, total_files, path):
                    update(phase=phase, bytes=done, totalBytes=total, files=files, totalFiles=total_files, path=path)

                return engine.backup(source, destination, incremental=incremental, progress=report, cancel=cancel)

            job_id = server.start_python_job("backup", runner, on_success=self._backup_success)
        return self.send_data(202, {"id": job_id})

    def _backup_success(self, result, job_id, task_id):
        record = self.server._register_snapshot(result, task_id)
        self.server._prune_versions(Path(result["path"]).parent)
        self.server.store.add_activity(title="备份完成",
                                       detail=f"{record['files']} 个文件 · {record['bytes']} 字节 · {record['path']}",
                                       level="success", task_id=task_id, job_id=job_id)

    def start_restore(self, body):
        server = self.server
        validate(body, SCHEMAS["RestoreRequest"])
        unsupported = unsupported_option("restore.start", body)
        if unsupported:
            return self.contract_error(501, "该选项尚未实现，未执行文件操作。", "NOT_IMPLEMENTED", "restore.start", unsupported)
        snapshot = _absolute(body["snapshot"], "备份版本")
        destination = _absolute(body["destination"], "还原位置")
        if server.binary and not server.force_python:
            job_id = server.start_native_job("restore", [str(snapshot), str(destination)],
                                             on_success=self._operation_success)
        else:
            def runner(update, cancel):
                def report(phase, done, total, files, total_files, path):
                    update(phase=phase, bytes=done, totalBytes=total, files=files, totalFiles=total_files, path=path)

                return engine.restore(snapshot, destination, progress=report, cancel=cancel)

            job_id = server.start_python_job("restore", runner, on_success=self._operation_success)
        return self.send_data(202, {"id": job_id})

    def start_verify(self, body):
        server = self.server
        validate(body, SCHEMAS["SnapshotRequest"])
        snapshot = _absolute(body["snapshot"], "备份版本")
        if server.binary and not server.force_python:
            job_id = server.start_native_job("verify", [str(snapshot)], on_success=self._operation_success)
        else:
            def runner(update, cancel):
                def report(phase, done, total, files, total_files, path):
                    update(phase=phase, bytes=done, totalBytes=total, files=files, totalFiles=total_files, path=path)

                manifest = engine.verify(snapshot, progress=report, cancel=cancel)
                return {"operation": "verify", "path": str(snapshot), "source": manifest.source,
                        "files": manifest.files, "directories": manifest.directories, "bytes": manifest.bytes,
                        "verified": True, "completedAt": now_iso()}

            job_id = server.start_python_job("verify", runner, on_success=self._operation_success)
        return self.send_data(202, {"id": job_id})

    def _operation_success(self, result, job_id, task_id):
        self.server.store.add_activity(title="文件操作完成",
                                       detail=f"{result.get('operation', '')} · {result.get('path', '')}",
                                       level="success", task_id=task_id, job_id=job_id)

    # -------------------------------------------------------------- codecs
    def _codec_algorithm(self, body, expected):
        options = body.get("options") or {}
        algorithm = options.get("algorithm", expected)
        if algorithm != expected:
            raise ValueError(f"不支持的算法：{algorithm}。当前仅支持 {expected}。")
        return options

    def start_archive_pack(self, body):
        options = self._codec_algorithm(body, container.ALGORITHMS[0]["id"])
        source = _absolute(body["source"], "打包源目录")
        destination = _absolute(body["destination"], "归档文件")

        def runner(update, cancel):
            def report(done, total):
                update(phase="packing", bytes=done, totalBytes=total, path=source.name)

            return container.pack(source, destination, progress=report)

        job_id = self.server.start_python_job("archive.pack", runner, on_success=self._operation_success)
        return self.send_data(202, {"id": job_id})

    def start_archive_unpack(self, body):
        options = self._codec_algorithm(body, container.ALGORITHMS[0]["id"])
        source = _absolute(body["source"], "归档文件")
        destination = _absolute(body["destination"], "解包目标")

        def runner(update, cancel):
            def report(done, total):
                update(phase="unpacking", bytes=done, totalBytes=total, path=destination.name)

            return container.unpack(source, destination, max_output_bytes=int(options.get("maxOutputBytes", 0)),
                                    progress=report)

        job_id = self.server.start_python_job("archive.unpack", runner, on_success=self._operation_success)
        return self.send_data(202, {"id": job_id})

    def start_compression(self, body, decompress):
        options = self._codec_algorithm(body, compression.ALGORITHMS[0]["id"])
        source = _absolute(body["source"], "源文件")
        destination = _absolute(body["destination"], "目标文件")
        block_bytes = int(options.get("blockBytes", 65536))
        max_output = int(options.get("maxOutputBytes", 0))

        def runner(update, cancel):
            def report(done, total):
                update(phase="decompressing" if decompress else "compressing", bytes=done, totalBytes=total)

            if decompress:
                return compression.decompress_file(source, destination, max_output_bytes=max_output, progress=report)
            return compression.compress_file(source, destination, block_bytes=block_bytes, progress=report)

        operation = "compression.decompress" if decompress else "compression.compress"
        job_id = self.server.start_python_job(operation, runner, on_success=self._operation_success)
        return self.send_data(202, {"id": job_id})

    def start_encryption(self, body, decrypt):
        options = self._codec_algorithm(body, crypto.ALGORITHMS[0]["id"])
        source = _absolute(body["source"], "源文件")
        destination = _absolute(body["destination"], "目标文件")
        reference = body.get("secretRef", "")
        if not reference:
            raise ValueError("加密/解密需要短期 secretRef，请先创建 secretRef。")
        password = self.server.secrets.get(reference)
        max_output = int(options.get("maxOutputBytes", 0))

        def runner(update, cancel):
            def report(done, total):
                update(phase="decrypting" if decrypt else "encrypting", bytes=done, totalBytes=total)

            if decrypt:
                return crypto.decrypt_file(source, destination, password, max_output_bytes=max_output, progress=report)
            return crypto.encrypt_file(source, destination, password, progress=report)

        operation = "encryption.decrypt" if decrypt else "encryption.encrypt"
        job_id = self.server.start_python_job(operation, runner, on_success=self._operation_success)
        return self.send_data(202, {"id": job_id})

    # --------------------------------------------------------------- tasks
    def create_task(self, body):
        source = _absolute(body["source"], "源目录")
        destination = _absolute(body["destination"], "备份位置")
        task = {
            "id": new_id("task"),
            "name": body["name"],
            "source": source.as_posix(),
            "destination": destination.as_posix(),
            "options": body.get("options") or {},
            "state": "ready",
            "lastRunAt": "",
        }
        self.server.store.put_task(task)
        self.server.store.add_activity(title="任务已创建", detail=task["name"], level="info", task_id=task["id"])
        return self.send_data(200, task)

    def update_task(self, task_id, body):
        task = self.server.store.get_task(task_id)
        if not task:
            return self.contract_error(404, "任务不存在。", "NOT_FOUND", "tasks.update")
        merged = {**task, "name": body["name"], "source": _absolute(body["source"], "源目录").as_posix(),
                  "destination": _absolute(body["destination"], "备份位置").as_posix(),
                  "options": body.get("options") or {}}
        merged.pop("state", None)
        merged.pop("lastRunAt", None)
        validate({key: merged[key] for key in ("name", "source", "destination", "options")}, SCHEMAS["Task"])
        task.update({"name": merged["name"], "source": merged["source"], "destination": merged["destination"],
                     "options": merged["options"]})
        self.server.store.put_task(task)
        return self.send_data(200, task)

    # ------------------------------------------------------------ schedules
    def _validated_schedule(self, body, schedule_id=None):
        task = self.server.store.get_task(body["taskId"])
        if not task:
            raise ValueError("计划关联的任务不存在。")
        next_at = next_run(body["expression"], body.get("timezone", "UTC"))
        schedule = {
            "id": schedule_id or new_id("schedule"),
            "taskId": body["taskId"],
            "timezone": body.get("timezone", "UTC"),
            "expression": body["expression"],
            "enabled": body.get("enabled", True),
            "missedRunPolicy": body.get("missedRunPolicy", "skip"),
            "nextRunAt": next_at.isoformat(),
            "lastRunAt": "",
        }
        return schedule

    def create_schedule(self, body):
        schedule = self._validated_schedule(body)
        self.server.store.put_schedule(schedule)
        self.server.store.add_activity(title="定时计划已创建",
                                       detail=f"{schedule['expression']} · {schedule['timezone']}",
                                       level="info", task_id=schedule["taskId"])
        return self.send_data(200, schedule)

    def update_schedule(self, schedule_id, body):
        existing = self.server.store.get_schedule(schedule_id)
        if not existing:
            return self.contract_error(404, "定时计划不存在。", "NOT_FOUND", "schedules.update")
        schedule = self._validated_schedule(body, schedule_id)
        schedule["lastRunAt"] = existing.get("lastRunAt", "")
        self.server.store.put_schedule(schedule)
        return self.send_data(200, schedule)

    # ------------------------------------------------------------- watchers
    def start_watcher(self, body):
        if not self.server.store.get_task(body["taskId"]):
            raise ValueError("实时监听关联的任务不存在。")
        status = self.server.watchers.start(body)
        self.server.store.add_activity(title="实时监听已启动", detail=body["source"],
                                       level="info", task_id=body["taskId"])
        return self.send_data(200, status)

    # ------------------------------------------------------------ snapshots
    def register_snapshot(self, body):
        snapshot = _absolute(body["snapshot"], "备份版本")
        manifest = engine.verify(snapshot)
        record = self.server._register_snapshot({
            "path": str(snapshot), "files": manifest.files, "bytes": manifest.bytes,
            "verified": True, "completedAt": now_iso(),
        }, body.get("taskId", ""), body.get("remoteId", ""))
        return self.send_data(200, record)

    # -------------------------------------------------------------- legacy
    def do_GET(self):
        if not self.local_request():
            return
        if self.handle_contract("GET"):
            return
        url = urlsplit(self.path)
        if url.path == "/api/session":
            self.send_data(200, {"token": self.server.token, "engine": self.server.engine_name,
                                 "engineDetail": "C++17" if self.server.binary else "Python compatibility engine",
                                 "version": "0.3", "home": str(Path.home()),
                                 "workspace": str(ROOT), "platform": sys.platform})
            return
        if url.path.startswith("/api/"):
            if not self.authorized():
                return
            if url.path.startswith("/api/jobs/"):
                with self.server.jobs_lock:
                    job = self.server.jobs.get(url.path.rsplit("/", 1)[-1])
                    self.send_data(200 if job else 404, job or {"error": "任务运行记录不存在，请检查目标目录。"})
                return
            if url.path == "/api/directories":
                try:
                    self.send_directories(parse_qs(url.query))
                except (OSError, ValueError) as error:
                    self.send_data(400, {"error": str(error)})
                return
            self.send_data(404, {"error": "接口不存在。"})
            return
        name = url.path.lstrip("/") or "index.html"
        if name not in STATIC_FILES:
            self.send_data(404, {"error": "文件不存在。"})
            return
        path = ROOT / name
        self.send_data(200, path.read_bytes(), mimetypes.guess_type(name)[0] or "application/octet-stream")

    def do_POST(self):
        if not self.local_request():
            return
        if self.handle_contract("POST"):
            return
        if not self.authorized():
            return
        try:
            operation_id = {"/api/backup": "backup.start", "/api/restore": "restore.start",
                            "/api/verify": "snapshots.verify"}.get(self.path)
            if not operation_id:
                self.send_data(404, {"error": "接口不存在。"})
                return
            body = self.read_body()
            if operation_id == "backup.start":
                self.start_backup(body)
            elif operation_id == "restore.start":
                self.start_restore(body)
            else:
                self.start_verify(body)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.send_data(400, {"error": str(error)})
        except Exception as error:
            self.send_data(500, {"error": str(error)})

    def do_PATCH(self):
        if self.local_request() and not self.handle_contract("PATCH"):
            self.contract_error(404, "接口不存在。", "NOT_FOUND")

    def do_DELETE(self):
        if self.local_request() and not self.handle_contract("DELETE"):
            self.contract_error(404, "接口不存在。", "NOT_FOUND")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args()
    server = DemoServer(args.port, args.state_dir)
    print(f"BackNest demo ({server.engine_name}): http://127.0.0.1:{server.server_port}", flush=True)
    if not server.binary:
        print("C++ engine not built; using the compatible Python engine.", flush=True)
        print("Build it with scripts/build.py from a developer shell for native speed.", flush=True)
    print("Backup/restore writes are performed only after an explicit UI action. Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
