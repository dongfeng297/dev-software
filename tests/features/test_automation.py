"""Tests for cron schedules, debounced watchers and short-lived secrets."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backnest.scheduler import Cron, ScheduleError, Scheduler, next_run, next_runs, resolve_timezone
from backnest.secrets import SecretError, SecretStore
from backnest.store import Store, new_id, now_iso
from backnest.watcher import WatcherManager
from support import WorkspaceTemporaryDirectory


class CronTests(unittest.TestCase):
    def test_next_run_steps_and_lists(self):
        moment = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(Cron("*/5 * * * *").next_after(moment, timezone.utc).isoformat(),
                         "2026-01-01T00:05:00+00:00")
        self.assertEqual(Cron("10,20 3 * * *").next_after(moment, timezone.utc).isoformat(),
                         "2026-01-01T03:10:00+00:00")
        values = next_runs("0 9 * * 1-5", "UTC", 3, moment)
        self.assertEqual(len(values), 3)
        self.assertTrue(all(value.endswith("+00:00") for value in values))

    def test_fixed_offset_timezone_fallback_for_windows(self):
        moment = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
        zone = resolve_timezone("Asia/Shanghai")
        self.assertEqual(Cron("*/15 * * * *").next_after(moment, zone).isoformat(),
                         "2026-01-01T14:15:00+08:00")
        self.assertEqual(resolve_timezone("+05:30").utcoffset(None), timedelta(hours=5, minutes=30))

    def test_invalid_expressions_and_sunday_alias(self):
        for expression in ["", "* * *", "61 * * * *", "*/0 * * * *", "* 24 * * *", "a b c d e"]:
            with self.subTest(expression=expression):
                with self.assertRaises(ScheduleError):
                    Cron(expression)
        cron = Cron("0 0 * * 7")
        self.assertIn(0, cron.weekdays)
        self.assertIsNotNone(next_run("0 0 * * 7", "UTC"))


class SecretTests(unittest.TestCase):
    def test_create_get_revoke_and_purpose(self):
        store = SecretStore(ttl_seconds=60)
        created = store.create("correct horse battery staple")
        self.assertTrue(created["secretRef"].startswith("secret-"))
        self.assertNotIn("correct horse", str(created))
        self.assertEqual(store.get(created["secretRef"]), b"correct horse battery staple")
        with self.assertRaises(SecretError):
            store.get(created["secretRef"], purpose="remote_auth")
        self.assertTrue(store.revoke(created["secretRef"]))
        with self.assertRaises(SecretError):
            store.get(created["secretRef"])
        self.assertFalse(store.revoke(created["secretRef"]))

    def test_expiry_is_enforced(self):
        store = SecretStore(ttl_seconds=60)
        created = store.create("password")
        reference = created["secretRef"]
        value, _expires, purpose = store._values[reference]
        store._values[reference] = (value, datetime.now(timezone.utc) - timedelta(seconds=1), purpose)
        with self.assertRaises(SecretError):
            store.get(reference)

    def test_invalid_values_rejected(self):
        store = SecretStore()
        for value in ["", "x" * 5000]:
            with self.assertRaises(SecretError):
                store.create(value)
        with self.assertRaises(SecretError):
            store.create("x", purpose="unknown")


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemporaryDirectory(prefix="automation-")
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state" / "backend.json")
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "file.txt").write_text("one", encoding="utf-8")
        self.destination = self.root / "backups"

    def tearDown(self):
        self.temp.cleanup()

    def task(self, destination=None):
        task = {"id": new_id("task"), "name": "t", "source": str(self.source),
                "destination": str(destination or self.destination), "options": {},
                "state": "ready", "lastRunAt": ""}
        self.store.put_task(task)
        return task

    def test_scheduler_triggers_once_and_advances_next_run(self):
        task = self.task()
        calls = []
        scheduler = Scheduler(self.store, lambda task_id, reason: calls.append(task_id) or True)
        schedule = {"id": new_id("schedule"), "taskId": task["id"], "timezone": "UTC",
                    "expression": "* * * * *", "enabled": True, "missedRunPolicy": "skip",
                    "nextRunAt": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                    "lastRunAt": ""}
        self.store.put_schedule(schedule)
        summary = scheduler.tick(datetime.now(timezone.utc))
        self.assertEqual(summary["triggered"], 1)
        self.assertEqual(calls, [task["id"]])
        stored = self.store.get_schedule(schedule["id"])
        self.assertGreater(datetime.fromisoformat(stored["nextRunAt"]), datetime.now(timezone.utc))
        # A second immediate tick must not re-enter the same occurrence.
        scheduler.tick(datetime.now(timezone.utc))
        self.assertEqual(len(calls), 1)

    def test_missed_run_policy_skip_and_run_once(self):
        task = self.task()
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        for policy, expected in [("skip", 0), ("run_once", 1)]:
            calls = []
            schedule = {"id": new_id("schedule"), "taskId": task["id"], "timezone": "UTC",
                        "expression": "0 * * * *", "enabled": True, "missedRunPolicy": policy,
                        "nextRunAt": stale, "lastRunAt": ""}
            self.store.put_schedule(schedule)
            Scheduler(self.store, lambda task_id, reason: calls.append(task_id) or True).tick()
            self.assertEqual(len(calls), expected, policy)
            self.store.delete_schedule(schedule["id"])

    def test_scheduler_skips_when_trigger_is_busy(self):
        task = self.task()
        schedule = {"id": new_id("schedule"), "taskId": task["id"], "timezone": "UTC",
                    "expression": "* * * * *", "enabled": True, "missedRunPolicy": "skip",
                    "nextRunAt": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                    "lastRunAt": ""}
        self.store.put_schedule(schedule)
        summary = Scheduler(self.store, lambda task_id, reason: False).tick()
        self.assertEqual(summary["skipped"], 1)
        self.assertTrue(any("跳过" in item["title"] for item in self.store.list_activity()))

    def test_scheduler_disables_invalid_schedule_instead_of_looping(self):
        task = self.task()
        schedule = {"id": new_id("schedule"), "taskId": task["id"], "timezone": "UTC",
                    "expression": "not a cron", "enabled": True, "missedRunPolicy": "skip",
                    "nextRunAt": now_iso(), "lastRunAt": ""}
        self.store.put_schedule(schedule)
        Scheduler(self.store, lambda task_id, reason: True).tick()
        stored = self.store.get_schedule(schedule["id"])
        self.assertFalse(stored["enabled"])
        self.assertIn("lastError", stored)

    def test_watcher_debounces_and_triggers(self):
        task = self.task()
        happened = threading.Event()
        calls = []

        def trigger(task_id, reason):
            calls.append((task_id, reason))
            happened.set()
            return True

        manager = WatcherManager(self.store, trigger)
        status = manager.start({"taskId": task["id"], "source": str(self.source), "debounceMs": 200})
        self.assertEqual(status["state"], "running")
        time.sleep(0.4)
        (self.source / "file.txt").write_text("two", encoding="utf-8")
        self.assertTrue(happened.wait(5), "watcher did not trigger")
        self.assertEqual(calls[0][0], task["id"])
        time.sleep(2.0)
        self.assertEqual(len(calls), 1, "watcher must not re-trigger without a new change")
        self.assertTrue(manager.get(status["id"]))
        self.assertTrue(manager.stop(status["id"]))
        self.assertIsNone(manager.get(status["id"]))
        manager.stop_all()

    def test_watcher_excludes_destination_tree(self):
        nested = self.source / "backups"
        nested.mkdir()
        task = self.task(destination=nested)
        happened = threading.Event()
        manager = WatcherManager(self.store, lambda task_id, reason: happened.set() or True)
        manager.start({"taskId": task["id"], "source": str(self.source), "debounceMs": 200})
        time.sleep(0.4)
        (nested / "snapshot").write_text("backup output", encoding="utf-8")
        self.assertFalse(happened.wait(1.2), "backup destination should not trigger the watcher")
        manager.stop_all()


if __name__ == "__main__":
    unittest.main(verbosity=2)
