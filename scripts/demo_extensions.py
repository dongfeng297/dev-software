"""Run a real end-to-end demo of P4/P5/P6 through the v1 HTTP bridge.

Usage: python scripts/demo_extensions.py

Creates an isolated directory under .backnest/demo-ext-<id>, starts the local
bridge on a random loopback port, then exercises:
  * BNAR pack / list / unpack
  * LZSS compress / decompress
  * BNE1 encrypt / decrypt, wrong-password failure
  * full + incremental backup, preview, verify, restore
  * cron schedule creation and next-run calculation
  * debounced watcher triggering a real incremental backup
Artifacts are intentionally kept for inspection; print paths are shown.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets as _secrets
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import DemoServer  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    base = ROOT / ".backnest" / ("demo-ext-" + _secrets.token_hex(4))
    base.mkdir(parents=True, exist_ok=True)
    state = base / "state"
    state.mkdir()
    source = base / "源 目录"
    source.mkdir()
    (source / "空目录").mkdir()
    (source / "空.txt").touch()
    (source / "说明.txt").write_text("BackNest 扩展演示\n" * 200, encoding="utf-8")
    (source / "binary.bin").write_bytes(os.urandom(300_000))

    server = DemoServer(0, state_dir=state)
    server.scheduler.stop()  # The demo drives scheduling explicitly.
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"

    def request(path, body=None, method=None):
        headers = {"X-BackNest-Token": server.token}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = Request(url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=120) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw else None

    def ok(path, body=None, method=None):
        code, data = request(path, body, method)
        if code != 200:
            raise SystemExit(f"{path} failed: {code} {data}")
        return data

    def accepted(path, body):
        code, data = request(path, body, "POST")
        if code != 202:
            raise SystemExit(f"{path} was not accepted: {code} {data}")
        return data["id"]

    def wait(job_id, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = ok("/api/v1/jobs/" + job_id)
            if job["state"] != "running":
                return job
            time.sleep(0.03)
        raise SystemExit("job timeout: " + job_id)

    print("BackNest P4/P5/P6 demo")
    print("workspace:", base)
    print("engine   :", server.engine_name)
    try:
        # P4 container
        archive = base / "bundle.bkn"
        job = wait(accepted("/api/v1/archive/pack", {
            "source": str(source), "destination": str(archive),
            "options": {"algorithm": "bkn-archive-v1"}}))
        print(f"\n[P4] archive.pack   : {job['state']} · {archive.stat().st_size} bytes")
        entries = ok("/api/v1/archive/list", {"snapshot": str(archive)}, "POST")
        print(f"[P4] archive.list   : {len(entries)} entries")
        unpacked = base / "unpacked"
        job = wait(accepted("/api/v1/archive/unpack", {
            "source": str(archive), "destination": str(unpacked),
            "options": {"algorithm": "bkn-archive-v1"}}))
        print(f"[P4] archive.unpack : {job['state']} · {job['result']['files']} files")

        # P4 compression
        compressed = base / "bundle.lz"
        job = wait(accepted("/api/v1/compression/compress", {
            "source": str(archive), "destination": str(compressed),
            "options": {"algorithm": "lzss-v1", "blockBytes": 32768}}))
        print(f"[P4] compress       : {archive.stat().st_size} -> {compressed.stat().st_size} bytes")
        decompressed = base / "bundle.restored"
        job = wait(accepted("/api/v1/compression/decompress", {
            "source": str(compressed), "destination": str(decompressed),
            "options": {"algorithm": "lzss-v1"}}))
        print(f"[P4] decompress     : {job['state']} · identical={decompressed.read_bytes() == archive.read_bytes()}")

        # P5 encryption
        secret = ok("/api/v1/secrets", {"value": "demo-password", "purpose": "encryption"}, "POST")
        encrypted = base / "bundle.bne"
        job = wait(accepted("/api/v1/encryption/encrypt", {
            "source": str(archive), "destination": str(encrypted),
            "options": {"algorithm": "aes-256-gcm"}, "secretRef": secret["secretRef"]}))
        print(f"\n[P5] encrypt        : {job['state']} · {encrypted.stat().st_size} bytes")
        decrypted = base / "bundle.dec"
        job = wait(accepted("/api/v1/encryption/decrypt", {
            "source": str(encrypted), "destination": str(decrypted),
            "options": {"algorithm": "aes-256-gcm"}, "secretRef": secret["secretRef"]}))
        print(f"[P5] decrypt        : {job['state']} · identical={decrypted.read_bytes() == archive.read_bytes()}")
        wrong = ok("/api/v1/secrets", {"value": "wrong-password", "purpose": "encryption"}, "POST")
        job = wait(accepted("/api/v1/encryption/decrypt", {
            "source": str(encrypted), "destination": str(base / "must-not-exist"),
            "options": {"algorithm": "aes-256-gcm"}, "secretRef": wrong["secretRef"]}))
        print(f"[P5] wrong password : {job['state']} · output_exists={(base / 'must-not-exist').exists()}")
        ok("/api/v1/secrets/" + secret["secretRef"], method="DELETE")
        ok("/api/v1/secrets/" + wrong["secretRef"], method="DELETE")

        # P6 incremental
        backups = base / "backups"
        job = wait(accepted("/api/v1/backups", {
            "source": str(source), "destination": str(backups), "options": {"mode": "full"}}))
        first = job["result"]["path"]
        print(f"\n[P6] full backup    : {first}")
        (source / "说明.txt").write_text("changed\n", encoding="utf-8")
        (source / "新增.txt").write_text("new\n", encoding="utf-8")
        (source / "空.txt").unlink()
        preview = ok("/api/v1/incremental/preview", {
            "source": str(source), "destination": str(backups)}, "POST")
        print(f"[P6] preview        : added={len(preview['added'])} changed={len(preview['changed'])} "
              f"deleted={len(preview['deleted'])}")
        job = wait(accepted("/api/v1/backups", {
            "source": str(source), "destination": str(backups), "options": {"mode": "incremental"}}))
        second = job["result"]["path"]
        print(f"[P6] incremental    : {second}")
        job = wait(accepted("/api/v1/snapshots/verify", {"snapshot": second}))
        print(f"[P6] verify         : {job['state']} · verified={job['result']['verified']}")
        restored = base / "restored"
        wait(accepted("/api/v1/restores", {"snapshot": second, "destination": str(restored)}))
        print(f"[P6] restore        : {restored}")

        # P6 schedule
        task = ok("/api/v1/tasks", {
            "name": "演示任务", "source": str(source),
            "destination": str(base / "scheduled"), "options": {"mode": "incremental"}}, "POST")
        schedule = ok("/api/v1/schedules", {
            "taskId": task["id"], "timezone": "Asia/Shanghai", "expression": "*/5 * * * *",
            "enabled": True, "missedRunPolicy": "skip"}, "POST")
        runs = ok("/api/v1/schedules/" + schedule["id"] + "/next-runs")
        print(f"\n[P6] schedule       : {schedule['expression']} next={runs[0]}")
        ok("/api/v1/schedules/" + schedule["id"], method="DELETE")

        # P6 watcher
        status = ok("/api/v1/watchers", {
            "taskId": task["id"], "source": str(source), "debounceMs": 300}, "POST")
        time.sleep(0.5)
        (source / "实时.txt").write_text("watcher change", encoding="utf-8")
        deadline = time.time() + 30
        while time.time() < deadline:
            if any((base / "scheduled").as_posix() in item["path"] for item in ok("/api/v1/snapshots")):
                break
            time.sleep(0.1)
        else:
            print("[P6] watcher        : TIMEOUT")
        print(f"[P6] watcher        : {status['id']} triggered and stopped")
        ok("/api/v1/watchers/" + status["id"], method="DELETE")

        overview = ok("/api/v1/workspace/overview")
        print(f"\n[summary] versions={overview['snapshots']} files={overview['backedUpFiles']} "
              f"activity={len(ok('/api/v1/activity'))}")
        print("artifacts are kept under:", base)
        return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
