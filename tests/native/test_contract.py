"""Requirements-to-port coverage and real v1 HTTP boundary checks."""
import json
from pathlib import Path
import re
import sys
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from api_contract import OPERATIONS, REQUIREMENTS, SCHEMAS, match_operation, openapi, validate
from server import DemoServer
from scripts.generate_api_contract import outputs
from support import WorkspaceTemporaryDirectory


class ContractTests(unittest.TestCase):
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

    def request(self, method, path, body=None, authorized=True):
        headers = {"X-BackNest-Token": self.server.token} if authorized else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, method=method, data=data, headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)

    def wait(self, job_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            code, job = self.request("GET", "/api/v1/jobs/" + job_id)
            self.assertEqual(code, 200)
            validate(job, SCHEMAS["Job"])
            if job["state"] != "running":
                return job
            time.sleep(.01)
        self.fail("Job timeout")

    def test_all_requirements_have_explicit_backend_ports(self):
        requirements = {r["id"] for r in REQUIREMENTS}
        self.assertEqual(requirements, {op["requirement"] for op in OPERATIONS})
        self.assertEqual(len({op["id"] for op in OPERATIONS}), len(OPERATIONS))
        self.assertEqual(set(SCHEMAS["FilterSpec"]["properties"]), {"paths", "types", "names", "time", "size", "users"})
        for op in OPERATIONS:
            self.assertTrue(op["path"].startswith("/api/v1/"))
            self.assertIn(op["response"], SCHEMAS)
            if op["request"]:
                self.assertIn(op["request"], SCHEMAS)

    def test_generated_openapi_frontend_and_audit_are_current(self):
        for path, expected in outputs().items():
            self.assertEqual(path.read_text(encoding="utf-8"), expected, str(path))
        def refs(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    self.assertIn(value["$ref"].rsplit("/", 1)[-1], SCHEMAS)
                for child in value.values():
                    refs(child)
            elif isinstance(value, list):
                for child in value:
                    refs(child)
        refs(openapi())

    def test_all_reserved_routes_return_501_and_create_no_job(self):
        before = set(self.server.jobs)
        for op in OPERATIONS:
            if op["implemented"]:
                continue
            path = re.sub(r"\{[^}]+\}", "test-id", op["path"])
            with self.subTest(operation=op["id"]):
                code, result = self.request(op["method"], path, {} if op["method"] in {"POST", "PATCH"} else None)
                self.assertEqual(code, 501, result)
                self.assertEqual(result["code"], "NOT_IMPLEMENTED")
                self.assertEqual(result["operationId"], op["id"])
                validate(result, SCHEMAS["Error"])
        self.assertEqual(before, set(self.server.jobs))

    def test_capabilities_never_advertise_unimplemented_algorithms(self):
        code, result = self.request("GET", "/api/v1/capabilities")
        self.assertEqual(code, 200)
        self.assertEqual(result["operations"], OPERATIONS)
        self.assertTrue(result["algorithms"]["compression"])
        self.assertTrue(result["algorithms"]["archive"])
        self.assertTrue(result["algorithms"]["encryption"])
        self.assertTrue(result["algorithms"]["archive"][0]["selfImplemented"])
        self.assertTrue(result["algorithms"]["compression"][0]["selfImplemented"])
        self.assertFalse(result["algorithms"]["encryption"][0]["selfImplemented"])
        self.assertEqual(result["algorithmPolicy"]["compression"], "self_implemented_only")
        self.assertEqual(result["algorithmPolicy"]["encryption"], "libraries_allowed")
        self.assertIn("backend_json", result["persistence"])
        self.assertFalse(result["automation"]["runs_after_process_exit"])

    def test_requests_cannot_silently_enable_extensions(self):
        with WorkspaceTemporaryDirectory(prefix="contract-") as temp:
            root = Path(temp).resolve()
            source = root / "source"
            source.mkdir()
            for options in [{"compression": {"algorithm": "huffman-v1"}},
                            {"archive": {"algorithm": "bkn-container-v1"}}, {"encryption": {"algorithm": "aes-gcm", "secretRef": "temporary"}},
                            {"filters": {"names": ["*.txt"]}}, {"metadata": {"preserve": ["owner"]}},
                            {"fileTypes": {"symlinks": "preserve"}}]:
                for path in ["/api/v1/backups", "/api/backup"]:
                    with self.subTest(options=options, path=path):
                        code, result = self.request("POST", path, {"source": str(source), "destination": str(root / "backup"), "options": options})
                        self.assertEqual(code, 501, result)
                        self.assertFalse((root / "backup").exists())
            code, _ = self.request("POST", "/api/v1/restores", {"snapshot": str(root / "snapshot"), "destination": str(root / "out"), "conflictPolicy": "overwrite"})
            self.assertEqual(code, 501)
            self.assertFalse((root / "out").exists())

    def test_v1_real_backup_and_restore_use_documented_dtos(self):
        with WorkspaceTemporaryDirectory(prefix="contract-roundtrip-") as temp:
            root = Path(temp).resolve()
            source = root / "源目录"
            source.mkdir()
            (source / "中文.txt").write_text("接口验证", encoding="utf-8")
            code, accepted = self.request("POST", "/api/v1/backups", {"source": str(source), "destination": str(root / "backups"), "options": {"mode": "full"}})
            self.assertEqual(code, 202)
            validate(accepted, SCHEMAS["JobAccepted"])
            result = self.wait(accepted["id"])
            self.assertEqual(result["state"], "completed", result)
            code, accepted = self.request("POST", "/api/v1/restores", {"snapshot": result["result"]["path"], "destination": str(root / "restored"), "conflictPolicy": "require_empty"})
            self.assertEqual(code, 202)
            self.assertEqual(self.wait(accepted["id"])["state"], "completed")
            self.assertEqual((root / "restored" / "中文.txt").read_bytes(), (source / "中文.txt").read_bytes())

    def test_contract_routes_require_auth_and_reject_wrong_methods_or_fields(self):
        code, result = self.request("GET", "/api/v1/capabilities", authorized=False)
        self.assertEqual(code, 403)
        validate(result, SCHEMAS["Error"])
        code, _ = self.request("POST", "/api/v1/capabilities", {})
        self.assertEqual(code, 405)
        self.assertIsNone(match_operation("GET", "/api/v1/openapiXjson")[0])
        code, _ = self.request("POST", "/api/v1/backups", {"source": "/source", "destination": "/backup", "unknown": True})
        self.assertEqual(code, 400)

    def test_persisted_task_dto_rejects_ephemeral_secrets(self):
        task = {"name": "task", "source": "/source", "destination": "/backup",
                "options": {"encryption": {"enabled": True, "algorithm": "reviewed-cipher", "secretRef": "must-not-persist"}}}
        with self.assertRaises(ValueError):
            validate(task, SCHEMAS["Task"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
