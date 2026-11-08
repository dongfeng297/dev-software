"""Loopback API integration tests against the real C++ executable."""
import json
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


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = DemoServer(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = WorkspaceTemporaryDirectory(prefix="backnest-api-test-")
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "源文件"
        self.source.mkdir()
        (self.source / "文件.txt").write_text("通过 API 调用 C++\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        options = {"X-BackNest-Token": self.server.token, **(headers or {})}
        data = None
        if body is not None:
            options["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = Request(self.base + path, data=data, headers=options)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def wait(self, job_id):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            status, job = self.request("/api/jobs/" + job_id)
            self.assertEqual(status, 200)
            if job["state"] != "running":
                return job
            time.sleep(.01)
        self.fail("Job did not complete")

    def test_api_backup_verify_and_restore_real_files(self):
        status, session = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertIn(session["engine"], {"C++17", "Python"})
        status, response = self.request("/api/backup", {"source": str(self.source), "destination": str(self.root / "backups")})
        self.assertEqual(status, 202)
        job = self.wait(response["id"])
        self.assertEqual(job["state"], "completed", job)
        snapshot = job["result"]["path"]
        self.assertTrue(Path(snapshot).is_dir())
        status, response = self.request("/api/verify", {"snapshot": snapshot})
        self.assertEqual(status, 202)
        self.assertTrue(self.wait(response["id"])["result"]["verified"])
        target = self.root / "restored"
        status, response = self.request("/api/restore", {"snapshot": snapshot, "destination": str(target)})
        self.assertEqual(status, 202)
        restored = self.wait(response["id"])
        self.assertEqual(restored["state"], "completed", restored)
        self.assertEqual((target / "文件.txt").read_bytes(), (self.source / "文件.txt").read_bytes())

    def test_foreign_origin_bad_token_and_rebinding_host_rejected(self):
        status, _ = self.request("/api/backup", {}, {"X-BackNest-Token": "wrong"})
        self.assertEqual(status, 403)
        status, _ = self.request("/api/session", headers={"Origin": "https://example.com"})
        self.assertEqual(status, 403)
        status, _ = self.request("/api/session", headers={"Host": "attacker.invalid"})
        self.assertEqual(status, 403)

    def test_invalid_paths_and_busy_engine_rejected(self):
        for body in [[], {}, {"source": "relative", "destination": str(self.root)}, {"source": str(self.source), "destination": "\0"}]:
            with self.subTest(body=body):
                status, _ = self.request("/api/backup", body)
                self.assertEqual(status, 400)
        self.server.engine_lock.acquire()
        try:
            status, _ = self.request("/api/backup", {"source": str(self.source), "destination": str(self.root / "backup")})
            self.assertEqual(status, 400)
        finally:
            self.server.engine_lock.release()

    def test_arbitrary_project_files_not_served(self):
        for path in ["/server.py", "/.git/config", "/../README.md"]:
            status, _ = self.request(path)
            self.assertEqual(status, 404)

    def test_failed_restore_is_not_reported_as_success(self):
        target = self.root / "target"
        target.mkdir()
        (target / "important.txt").write_text("keep")
        status, response = self.request("/api/restore", {"snapshot": str(self.root / "missing"), "destination": str(target)})
        self.assertEqual(status, 202)
        job = self.wait(response["id"])
        self.assertEqual(job["state"], "failed")
        self.assertNotIn("result", job)
        self.assertEqual((target / "important.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
