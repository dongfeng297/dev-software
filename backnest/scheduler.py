"""Cron-style schedules with explicit timezone and missed-run behaviour.

The schedule engine is intentionally small but real: five-field cron (`minute
hour day-of-month month day-of-week`), lists/ranges/steps, and either a named
IANA zone (when `tzdata`/`zoneinfo` is available) or a fixed offset.  On a
Windows Python without the `tzdata` package, common no-DST zones such as
Asia/Shanghai are mapped to fixed offsets; other names fail with a clear error
instead of silently running in the wrong zone.

Schedules run only while this process is alive.  A missed occurrence is either
skipped or run once on restart, according to `missedRunPolicy`.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
import re
import threading
import time
from typing import Callable

from .store import Store, now_iso

_CRON_FIELDS = [
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
]

# IANA names that never observe DST.  Used only when zoneinfo data is absent.
_FIXED_ZONES = {
    "asia/shanghai": 8, "asia/hong_kong": 8, "asia/macau": 8, "asia/taipei": 8,
    "asia/singapore": 8, "asia/kuala_lumpur": 8, "asia/manila": 8, "asia/brunei": 8,
    "asia/tokyo": 9, "asia/seoul": 9, "asia/pyongyang": 9,
    "asia/bangkok": 7, "asia/jakarta": 7, "asia/ho_chi_minh": 7,
    "asia/kolkata": 5.5, "asia/calcutta": 5.5, "asia/dhaka": 6, "asia/karachi": 5,
    "asia/dubai": 4, "asia/riyadh": 3, "asia/tehran": 3.5,
    "australia/brisbane": 10, "australia/perth": 8, "australia/darwin": 9.5,
    "pacific/auckland": 12, "utc": 0, "etc/utc": 0, "gmt": 0, "z": 0,
}


class ScheduleError(ValueError):
    """Invalid cron expression, timezone or schedule state."""


def _fixed_offset(hours: float) -> tzinfo:
    return timezone(timedelta(hours=hours))


def resolve_timezone(name: str) -> tzinfo:
    name = (name or "").strip()
    if not name or name.lower() in {"local", "system"}:
        return datetime.now().astimezone().tzinfo or timezone.utc
    offset = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", name)
    if offset:
        sign = -1 if offset.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(offset.group(2)), minutes=int(offset.group(3))))
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        pass
    fixed = _FIXED_ZONES.get(name.lower())
    if fixed is not None:
        return _fixed_offset(fixed)
    raise ScheduleError("当前环境没有该时区数据；请使用 UTC、local 或 +08:00 形式的固定偏移（可安装 tzdata 支持 IANA 名称）。")


def _parse_field(text: str, low: int, high: int, label: str) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ScheduleError(f"{label} 字段包含空项。")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) <= 0:
                raise ScheduleError(f"{label} 字段的步长无效。")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ScheduleError(f"{label} 字段的范围无效。")
            start, end = int(start_text), int(end_text)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ScheduleError(f"{label} 字段包含无法识别的值。")
        if start < low or end > high or start > end:
            raise ScheduleError(f"{label} 字段超出范围 {low}-{high}。")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    def __init__(self, expression: str):
        fields = expression.split()
        if len(fields) != 5:
            raise ScheduleError("定时表达式必须包含 5 个字段：分 时 日 月 周。")
        self.expression = " ".join(fields)
        self.minutes = _parse_field(fields[0], 0, 59, "分钟")
        self.hours = _parse_field(fields[1], 0, 23, "小时")
        self.days = _parse_field(fields[2], 1, 31, "日期")
        self.months = _parse_field(fields[3], 1, 12, "月份")
        weekdays = _parse_field(fields[4], 0, 7, "星期")
        self.weekdays = {0 if value == 7 else value for value in weekdays}
        self.day_restricted = fields[2] not in {"*"}
        self.weekday_restricted = fields[4] not in {"*"}

    def day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom = day.day in self.days
        dow = (day.weekday() + 1) % 7 in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return dom or dow
        return dom and dow

    def next_after(self, after: datetime, zone: tzinfo) -> datetime:
        local = after.astimezone(zone)
        start = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.date()
        for _ in range(367):
            if self.day_matches(day):
                for hour in sorted(self.hours):
                    if day == start.date() and hour < start.hour:
                        continue
                    for minute in sorted(self.minutes):
                        candidate = datetime.combine(day, dtime(hour, minute), tzinfo=zone)
                        if candidate > after:
                            return candidate
            day += timedelta(days=1)
        raise ScheduleError("定时表达式在未来一年内没有可执行时间。")


def next_run(expression: str, timezone_name: str = "UTC", after: datetime | None = None) -> datetime:
    zone = resolve_timezone(timezone_name)
    cron = Cron(expression)
    return cron.next_after(after or datetime.now(timezone.utc), zone)


def next_runs(expression: str, timezone_name: str = "UTC", count: int = 3,
              after: datetime | None = None) -> list[str]:
    zone = resolve_timezone(timezone_name)
    cron = Cron(expression)
    moment = after or datetime.now(timezone.utc)
    values = []
    for _ in range(max(1, min(count, 20))):
        moment = cron.next_after(moment, zone)
        values.append(moment.isoformat())
    return values


class Scheduler:
    def __init__(self, store: Store, trigger: Callable[[str, str], bool], interval: float = 5.0):
        self.store = store
        self.trigger = trigger
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="backnest-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:  # never let the scheduler thread die silently
                if str(error) != self._last_error:
                    self._last_error = str(error)
                    try:
                        self.store.add_activity(title="定时调度异常", detail=str(error), level="error")
                    except Exception:
                        pass
            self._stop.wait(self.interval)

    def _schedule_next(self, schedule: dict, after: datetime) -> str:
        return next_run(schedule.get("expression", ""), schedule.get("timezone", "UTC"), after).isoformat()

    def tick(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        summary = {"checked": 0, "triggered": 0, "skipped": 0, "missed": 0}
        for schedule in self.store.list_schedules():
            if not schedule.get("enabled"):
                continue
            summary["checked"] += 1
            try:
                next_at = schedule.get("nextRunAt")
                if not next_at:
                    schedule["nextRunAt"] = self._schedule_next(schedule, now)
                    self.store.put_schedule(schedule)
                    continue
                due = datetime.fromisoformat(next_at)
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
            except (ValueError, ScheduleError) as error:
                schedule["enabled"] = False
                schedule["lastError"] = str(error)
                self.store.put_schedule(schedule)
                self.store.add_activity(title="定时计划已停用", detail=f"{schedule.get('id')}: {error}", level="error",
                                        task_id=schedule.get("taskId", ""))
                continue
            if now < due:
                continue
            missed = (now - due).total_seconds() > max(90.0, self.interval * 3)
            if missed and schedule.get("missedRunPolicy", "skip") == "skip":
                summary["missed"] += 1
                self.store.add_activity(title="跳过错过的定时备份", detail=f"{schedule.get('id')} · 计划时间 {next_at}",
                                        level="warning", task_id=schedule.get("taskId", ""))
            else:
                if missed:
                    summary["missed"] += 1
                started = False
                try:
                    started = bool(self.trigger(schedule.get("taskId", ""), schedule.get("id", "")))
                except Exception as error:
                    self.store.add_activity(title="定时备份启动失败", detail=str(error), level="error",
                                            task_id=schedule.get("taskId", ""))
                if started:
                    summary["triggered"] += 1
                else:
                    summary["skipped"] += 1
                    self.store.add_activity(title="定时备份已跳过", detail="已有备份正在运行或任务不可用。",
                                            level="warning", task_id=schedule.get("taskId", ""))
            schedule["lastRunAt"] = now_iso()
            try:
                schedule["nextRunAt"] = self._schedule_next(schedule, now)
            except ScheduleError as error:
                schedule["enabled"] = False
                schedule["lastError"] = str(error)
            self.store.put_schedule(schedule)
        return summary

    def next_runs(self, schedule: dict, count: int = 3) -> list[str]:
        return next_runs(schedule.get("expression", ""), schedule.get("timezone", "UTC"), count)
