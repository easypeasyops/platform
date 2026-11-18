"""Each namespace's CronJobs, as kube-state-metrics sees them in Prometheus (QUERIES, read
by app/status.py like the replicas): whether each ran on time and whether its latest Job
failed. No Kubernetes API call, so no RBAC of its own.

A CronJob is, in this order: suspended (by intent: not a problem, not counted); failed,
when its latest Job (the newest one it owns) has failed pods and no succeeded one and was
created within two schedule intervals, so a failure kept in the Job history stops counting
once the schedule has moved on (a CronJob that then didn't run again is late instead);
never run, while it has no successful run (unknown, never on time); late, when its last
success is older than two schedule intervals; else on time.

The interval is the gap between kube-state-metrics' next run and the run before it, which
this module works out from the schedule, in the CronJob's time zone. "Now" is when
Prometheus evaluated the queries, so a recorded answer always reads the same.
"""

import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

QUERIES = {
    "info": "kube_cronjob_info",  # schedule, timezone
    "success": "kube_cronjob_status_last_successful_time",
    "next": "kube_cronjob_next_schedule_time",
    "suspend": "kube_cronjob_spec_suspend",
    # Every Job a CronJob owns, with its owner's name; and those that failed.
    "jobs": "kube_job_created * on (namespace, job_name) group_left (owner_name)"
    ' kube_job_owner{owner_kind="CronJob"}',
    "failed": "kube_job_status_failed > 0"
    " unless on (namespace, job_name) kube_job_status_succeeded > 0",
}
MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
NAMES = {
    name: number
    for names in ("jan feb mar apr may jun jul aug sep oct nov dec", "sun mon tue wed thu fri sat")
    for number, name in enumerate(names.split(), start=1 if names.startswith("jan") else 0)
}
RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))  # minute, hour, day, month, weekday


def _field(text: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in re.sub(r"[a-z]{3}", lambda m: str(NAMES[m[0]]), text.lower()).split(","):
        span, _, step = part.partition("/")
        if span in ("*", "?"):
            start, end = low, high
        elif "-" in span:
            start, end = (int(n) for n in span.split("-"))
        else:
            start = int(span)
            end = high if step else start
        values.update(range(start, end + 1, int(step or 1)))
    if not values or min(values) < low or max(values) > high:
        raise ValueError(text)
    return values


def previous_run(schedule: str, before: datetime) -> datetime | None:
    """The last time `schedule` fired before `before` (a wall-clock time in the CronJob's
    zone), within a year; None for a schedule this can't read."""
    try:
        fields = MACROS.get(schedule.strip(), schedule).split()
        if len(fields) != 5:
            return None
        minutes, hours, days, months, weekdays = (
            _field(f, *r) for f, r in zip(fields, RANGES, strict=True)
        )
    except ValueError, KeyError:
        return None
    weekdays = {d % 7 for d in weekdays}  # 7 is Sunday too
    # Day and weekday both restricted: either may match (cron's rule).
    either = not fields[2].startswith("*") and not fields[4].startswith("*")
    day = before.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(367):
        by_day, by_weekday = day.day in days, (day.weekday() + 1) % 7 in weekdays
        if day.month in months and ((by_day or by_weekday) if either else by_day and by_weekday):
            for hour in sorted(hours, reverse=True):
                for minute in sorted(minutes, reverse=True):
                    if (run := day.replace(hour=hour, minute=minute)) < before:
                        return run
        day -= timedelta(days=1)
    return None


def _zone(name: str) -> tzinfo | None:
    """A CronJob's time zone: kind's controllers run in UTC ("local"). None: one the image
    has no data for."""
    if name in ("local", "UTC", "Etc/UTC"):
        return UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return None


def _span(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= 2 * size:
            return f"{seconds // size} {unit}"
    return f"{seconds} s"


def _by_cronjob(answer: dict[str, Any]) -> dict[tuple[str, str], Any]:
    return {(s["metric"]["namespace"], s["metric"]["cronjob"]): s for s in answer["result"]}


def shape(answers: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per namespace with a CronJob, each one's row, in name order. `answers` are
    the `data` of Prometheus's answers to QUERIES, by key."""
    values = {
        key: {k: float(s["value"][1]) for k, s in _by_cronjob(answers[key]).items()}
        for key in ("success", "next", "suspend")
    }
    failed = {
        (s["metric"]["namespace"], s["metric"]["job_name"]) for s in answers["failed"]["result"]
    }
    latest: dict[tuple[str, str], tuple[float, str]] = {}  # (created, Job) per CronJob
    for s in answers["jobs"]["result"]:
        owner = (s["metric"]["namespace"], s["metric"]["owner_name"])
        latest[owner] = max(
            latest.get(owner, (0, "")), (float(s["value"][1]), s["metric"]["job_name"])
        )

    found: dict[str, list[dict[str, Any]]] = {}
    for key, series in sorted(_by_cronjob(answers["info"]).items()):
        namespace, name = key
        now = float(series["value"][0])
        schedule, zone = series["metric"]["schedule"], series["metric"].get("timezone", "local")
        success, upcoming = values["success"].get(key), values["next"].get(key)
        interval = None
        if upcoming is not None and (tz := _zone(zone)):
            nxt = datetime.fromtimestamp(upcoming, tz)
            before = previous_run(schedule, nxt.replace(tzinfo=None))
            if before is not None:
                interval = upcoming - before.replace(tzinfo=tz).timestamp()
        window = 2 * interval if interval else float("inf")
        created, job = latest.get(key, (0, ""))
        if values["suspend"].get(key):
            state, tone = "suspended", "unknown"
        elif (namespace, job) in failed and now - created <= window:
            state, tone = "failed", "down"
        elif not success:  # absent, or 0: no successful run yet
            state, tone = "never run", "unknown"
        elif interval is None:
            state, tone = "schedule not understood", "unknown"  # or its time zone
        elif now - success > window:
            state, tone = "late", "warn"
        else:
            state, tone = "on time", "ok"
        next_run = "—"
        if upcoming and state != "suspended":
            # The run after the last one scheduled: in the past when that one was missed.
            next_run = (
                f"in {_span(upcoming - now)}"
                if upcoming > now
                else f"missed, due {_span(now - upcoming)} ago"
            )
        found.setdefault(namespace, []).append(
            {
                "name": name,
                "schedule": schedule,
                "state": state,
                "tone": tone,
                "failed": job if state == "failed" else None,  # the latest Job's name
                "last": f"{_span(now - success)} ago" if success else "never",
                "next": next_run,
            }
        )
    return found
