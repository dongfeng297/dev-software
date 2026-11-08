"""End-to-end HTTP tests for the P4/P5/P6 extension ports."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server import DemoServer
from support import WorkspaceTemporaryDirectory


class FeatureApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = WorkspaceTemporaryDirectory(prefix="feature-state-")
        cls.server = DemoServer(0, state_dir=cls.state.name)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.server.scheduler.stop()  # Tests drive the scheduler explicitly.

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.state.cleanup()

    def setUp(self):
        self.temp = WorkspaceTemporaryDirectory(prefix="feature-case-")
        self.root = Path(self.temp.name)
        self.source = self.root / "源 目录"
        self.source.mkdir()
        (self.source / "空目录").mkdir()
        (self.source / "空.txt").touch()
        (self.source / "中文 文件.txt").write_text("hello 世界\n", encoding="utf-8")
        (self.source / "sub").mkdir()
        (self.source / "sub" / "data.bin").write_bytes(os.urandom(150_000))

    def tearDown(self):
        self.temp.cleanup()

    def request(self, path, body=None, method=None):
        headers = {"X-BackNest-Token": self.server.token}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=60) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def ok(self, path, body=None, method=None):
        code, data = self.request(path, body, method)
        self.assertEqual(code, 200, (path, data))
        return data

    def accepted(self, path, body):
        code, data = self.request(path, body, "POST")
        self.assertEqual(code, 202, (path, data))
        return data["id"]

    def wait(self, job_id, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            code, job = self.request("/api/v1/jobs/" + job_id)
            self.assertEqual(code, 200)
            if job["state"] != "running":
                return job
            time.sleep(0.02)
        self.fail("job timeout: " + job_id)

    def test_algorithms_report_the_real_implementations(self):
        archive = self.ok("/api/v1/archive/algorithms")
        compression = self.ok("/api/v1/compression/algorithms")
        encryption = self.ok("/api/v1/encryption/algorithms")
        self.assertEqual(archive[0]["id"], "bkn-archive-v1")
        self.assertTrue(archive[0]["selfImplemented"])
        self.assertEqual(compression[0]["id"], "lzss-v1")
        self.assertTrue(compression[0]["selfImplemented"])
        self.assertEqual(encryption[0]["id"], "aes-256-gcm")
        self.assertFalse(encryption[0]["selfImplemented"])

    def test_archive_pack_list_and_unpack(self):
        archive = self.root / "bundle.bkn"
        job = self.wait(self.accepted("/api/v1/archive/pack", {
            "source": str(self.source), "destination": str(archive),
            "options": {"algorithm": "bkn-archive-v1"}}))
        self.assertEqual(job["state"], "completed", job)
        entries = self.ok("/api/v1/archive/list", {"snapshot": str(archive)}, "POST")
        self.assertEqual(len(entries), 5)
        self.assertTrue(any(entry["path"] == "sub/data.bin" for entry in entries))
        destination = self.root / "unpacked"
        job = self.wait(self.accepted("/api/v1/archive/unpack", {
            "source": str(archive), "destination": str(destination),
            "options": {"algorithm": "bkn-archive-v1"}}))
        self.assertEqual(job["state"], "completed", job)
        self.assertEqual((destination / "中文 文件.txt").read_bytes(),
                         (self.source / "中文 文件.txt").read_bytes())

    def test_archive_corruption_and_bad_options_fail(self):
        archive = self.root / "bundle.bkn"
        self.wait(self.accepted("/api/v1/archive/pack", {
            "source": str(self.source), "destination": str(archive),
            "options": {"algorithm": "bkn-archive-v1"}}))
        data = bytearray(archive.read_bytes())
        data[-1] ^= 1
        bad = self.root / "bad.bkn"
        bad.write_bytes(data)
        job = self.wait(self.accepted("/api/v1/archive/unpack", {
            "source": str(bad), "destination": str(self.root / "bad-out"),
            "options": {"algorithm": "bkn-archive-v1"}}))
        self.assertEqual(job["state"], "failed")
        self.assertFalse((self.root / "bad-out").exists())
        code, error = self.request("/api/v1/archive/pack", {
            "source": str(self.source), "destination": str(self.root / "x.bkn"),
            "options": {"algorithm": "zip"}}, "POST")
        self.assertEqual(code, 400, error)

    def test_compression_round_trip_and_output_limit(self):
        source = self.root / "text.txt"
        source.write_text(("BackNest " * 5000), encoding="utf-8")
        compressed = self.root / "text.lz"
        job = self.wait(self.accepted("/api/v1/compression/compress", {
            "source": str(source), "destination": str(compressed),
            "options": {"algorithm": "lzss-v1", "blockBytes": 32768}}))
        self.assertEqual(job["state"], "completed", job)
        self.assertLess(compressed.stat().st_size, source.stat().st_size)
        decompressed = self.root / "text.out"
        job = self.wait(self.accepted("/api/v1/compression/decompress", {
            "source": str(compressed), "destination": str(decompressed),
            "options": {"algorithm": "lzss-v1"}}))
        self.assertEqual(decompressed.read_bytes(), source.read_bytes())
        limited = self.root / "limited"
        job = self.wait(self.accepted("/api/v1/compression/decompress", {
            "source": str(compressed), "destination": str(limited),
            "options": {"algorithm": "lzss-v1", "maxOutputBytes": 10}}))
        self.assertEqual(job["state"], "failed")
        self.assertFalse(limited.exists())

    def test_encryption_correct_wrong_password_and_tamper(self):
        plain = self.root / "secret.txt"
        plain.write_text("机密内容\n" * 1000, encoding="utf-8")
        secret = self.ok("/api/v1/secrets", {"value": "correct horse", "purpose": "encryption"}, "POST")
        encrypted = self.root / "secret.enc"
        job = self.wait(self.accepted("/api/v1/encryption/encrypt", {
            "source": str(plain), "destination": str(encrypted),
            "options": {"algorithm": "aes-256-gcm"}, "secretRef": secret["secretRef"]}))
        self.assertEqual(job["state"], "completed", job)
        decrypted = self.root / "secret.out"
        job = self.wait(self.accepted("/api/v1/encryption/decrypt", {
            "source": str(encrypted), "destination": str(decrypted),
            "options": {"algorithm": "aes-256-gcm"}, "secretRef": secret["secretRef"]}))
        self.assertEqual(job["state"], "completed", job)
        self.assertEqual(decrypted.read_bytes(), plain.read_bytes())
        wrong = self.ok("/api/v1/secrets", {"value": "wrong", "purpose": "encryption"}, "POST")
        bad = self.root / "secret.bad"
        job = self.wait(self.accepted("/api/v1/encryption/decrypt", {
            "source": str(encrypted), "destination": str(bad),
            "options": {"algorithm": "aes-256-gcm"}, "secretRef": wrong["secretRef"]}))
        self.assertEqual(job["state"], "failed")
        self.assertNotIn("correct horse", job.get("error", ""))
        self.assertFalse(bad.exists())
        self.ok("/api/v1/secrets/" + secret["secretRef"], method="DELETE")
        self.ok("/api/v1/secrets/" + wrong["secretRef"], method="DELETE")
        code, error = self.request("/api/v1/secrets/" + secret["secretRef"], method="DELETE")
        self.assertEqual(code, 404, error)
        tampered = bytearray(encrypted.read_bytes())
        tampered[40] ^= 1
        encrypted.write_bytes(tampered)
        fresh = self.ok("/api/v1/secrets", {"value": "correct horse", "purpose": "encryption"}, "POST")
        job = self.wait(self.accepted("/api/v1/encryption/decrypt", {
            "source": str(encrypted), "destination": str(self.root / "tampered.out"),
            "options": {"algorithm": "aes-256-gcm"}, "secretRef": fresh["secretRef"]}))
        self.assertEqual(job["state"], "failed")
        self.ok("/api/v1/secrets/" + fresh["secretRef"], method="DELETE")

    def test_incremental_backup_preview_verify_restore(self):
        backups = self.root / "backups"
        job = self.wait(self.accepted("/api/v1/backups", {
            "source": str(self.source), "destination": str(backups), "options": {"mode": "full"}}))
        first = job["result"]["path"]
        (self.source / "中文 文件.txt").write_text("changed", encoding="utf-8")
        (self.source / "new.txt").write_text("new", encoding="utf-8")
        (self.source / "空.txt").unlink()
        preview = self.ok("/api/v1/incremental/preview", {
            "source": str(self.source), "destination": str(backups)}, "POST")
        self.assertEqual([item["path"] for item in preview["added"]], ["new.txt"])
        self.assertEqual([item["path"] for item in preview["changed"]], ["中文 文件.txt"])
        self.assertEqual([item["path"] for item in preview["deleted"]], ["空.txt"])
        job = self.wait(self.accepted("/api/v1/backups", {
            "source": str(self.source), "destination": str(backups), "options": {"mode": "incremental"}}))
        second = job["result"]["path"]
        for snapshot in (first, second):
            job = self.wait(self.accepted("/api/v1/snapshots/verify", {"snapshot": snapshot}))
            self.assertTrue(job["result"]["verified"])
        destination = self.root / "restored"
        job = self.wait(self.accepted("/api/v1/restores", {"snapshot": second, "destination": str(destination)}))
        self.assertEqual((destination / "new.txt").read_text(), "new")

    def test_tasks_schedules_and_scheduler_trigger(self):
        task = self.ok("/api/v1/tasks", {
            "name": "自动任务", "source": str(self.source),
            "destination": str(self.root / "auto"), "options": {"mode": "incremental"}}, "POST")
        self.assertEqual(task["state"], "ready")
        schedule = self.ok("/api/v1/schedules", {
            "taskId": task["id"], "timezone": "Asia/Shanghai", "expression": "*/5 * * * *",
            "enabled": True, "missedRunPolicy": "skip"}, "POST")
        self.assertTrue(schedule["nextRunAt"].endswith("+08:00"))
        runs = self.ok("/api/v1/schedules/" + schedule["id"] + "/next-runs")
        self.assertEqual(len(runs), 3)
        # Force the next occurrence slightly into the past, then drive one scheduler tick.
        schedule = self.server.store.get_schedule(schedule["id"])
        schedule["nextRunAt"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        self.server.store.put_schedule(schedule)
        self.server.scheduler.tick()
        end = time.time() + 20
        while time.time() < end:
            snapshots = self.ok("/api/v1/snapshots")
            if any((self.root / "auto").as_posix() in item["path"] for item in snapshots):
                break
            time.sleep(0.05)
        else:
            self.fail("scheduled backup did not run")
        updated = self.ok("/api/v1/tasks")
        self.assertEqual(next(item for item in updated if item["id"] == task["id"])["state"], "completed")
        self.ok("/api/v1/schedules/" + schedule["id"], method="DELETE")
        self.ok("/api/v1/tasks/" + task["id"], method="DELETE")
        code, _ = self.request("/api/v1/tasks/" + task["id"], method="DELETE")
        self.assertEqual(code, 404)

    def test_watcher_triggers_incremental_backup(self):
        task = self.ok("/api/v1/tasks", {
            "name": "实时任务", "source": str(self.source),
            "destination": str(self.root / "realtime"), "options": {"mode": "incremental"}}, "POST")
        status = self.ok("/api/v1/watchers", {
            "taskId": task["id"], "source": str(self.source), "debounceMs": 250}, "POST")
        self.assertEqual(status["state"], "running")
        time.sleep(0.5)
        (self.source / "live.txt").write_text("live change", encoding="utf-8")
        end = time.time() + 20
        while time.time() < end:
            snapshots = self.ok("/api/v1/snapshots")
            if any((self.root / "realtime").as_posix() in item["path"] for item in snapshots):
                break
            time.sleep(0.1)
        else:
            self.fail("watcher did not trigger a backup")
        self.ok("/api/v1/watchers/" + status["id"], method="DELETE")
        code, _ = self.request("/api/v1/watchers/" + status["id"], method="DELETE")
        self.assertEqual(code, 404)

    def test_settings_snapshots_activity_and_overview(self):
        settings = self.ok("/api/v1/settings")
        self.assertEqual(settings["keepVersions"], 0)
        updated = self.ok("/api/v1/settings", {"keepVersions": 3, "notifications": True}, "PATCH")
        self.assertEqual(updated["keepVersions"], 3)
        job = self.wait(self.accepted("/api/v1/backups", {
            "source": str(self.source), "destination": str(self.root / "indexed"),
            "options": {"mode": "full"}}))
        snapshot = job["result"]["path"]
        record = self.ok("/api/v1/snapshots", {"snapshot": snapshot}, "POST")
        self.assertTrue(record["verified"])
        records = self.ok("/api/v1/snapshots")
        self.assertTrue(any(Path(item["path"]) == Path(snapshot) for item in records))
        activity = self.ok("/api/v1/activity")
        self.assertTrue(activity)
        export = self.ok("/api/v1/activity/export", {}, "POST")
        self.assertEqual(export["format"], "json")
        overview = self.ok("/api/v1/workspace/overview")
        self.assertGreaterEqual(overview["snapshots"], 1)
        self.ok("/api/v1/snapshots/" + record["id"], method="DELETE")
        self.ok("/api/v1/settings", {"keepVersions": 0, "notifications": False}, "PATCH")

    def test_optional_version_retention_prunes_only_indexed_versions(self):
        self.ok("/api/v1/settings", {"keepVersions": 1}, "PATCH")
        destination = self.root / "retained"
        first = self.wait(self.accepted("/api/v1/backups", {
            "source": str(self.source), "destination": str(destination), "options": {"mode": "full"}}))
        (self.source / "next.txt").write_text("next", encoding="utf-8")
        second = self.wait(self.accepted("/api/v1/backups", {
            "source": str(self.source), "destination": str(destination), "options": {"mode": "full"}}))
        self.assertFalse(Path(first["result"]["path"]).exists())
        self.assertTrue(Path(second["result"]["path"]).is_dir())
        self.assertEqual(second["state"], "completed")
        self.ok("/api/v1/settings", {"keepVersions": 0}, "PATCH")

    def test_invalid_extension_inputs_are_400(self):
        cases = [
            ("/api/v1/incremental/preview", {"source": str(self.root / "missing"), "destination": str(self.root / "x")}, "POST"),
            ("/api/v1/archive/pack", {"source": str(self.source), "destination": "relative.bkn",
                                      "options": {"algorithm": "bkn-archive-v1"}}, "POST"),
            ("/api/v1/encryption/encrypt", {"source": str(self.source), "destination": str(self.root / "x.enc"),
                                            "options": {"algorithm": "aes-256-gcm"}, "secretRef": "secret-missing"}, "POST"),
            ("/api/v1/schedules", {"taskId": "missing", "timezone": "UTC", "expression": "* * * * *"}, "POST"),
            ("/api/v1/watchers", {"taskId": "missing", "source": str(self.source)}, "POST"),
        ]
        for path, body, method in cases:
            with self.subTest(path=path):
                code, error = self.request(path, body, method)
                self.assertEqual(code, 400, error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
