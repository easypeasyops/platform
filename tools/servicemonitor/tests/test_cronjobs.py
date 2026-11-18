"""CronJobs on time or not, over app/cronjobs.py's QUERIES as Prometheus answered them on
the laptop cluster (tests/fixtures/cronjobs.json, 2026-11-17 21:35 PST): aipostit-prod's
four and monitoring's provenance-canary, none failed. Two of aipostit-prod's daily ones
really were late: they last succeeded on 2026-11-16 and the laptop slept through the next
two runs' starting deadline. The other cases are that recording with one CronJob's series
edited here; those that read monitoring's tile clear its one alert first."""

import json
from datetime import datetime
from typing import Any

import pytest

from app import cronjobs, status
from tests import test_status
from tests.test_overview import page, text_of, tiles
from tests.test_status import FIXTURES, Cluster, read

cluster = test_status.cluster  # the recordings, behind both APIs, for every test here
NOW = 1794980147.9  # when Prometheus answered the recording
SYNC = ("aipostit-prod", "aipostit-sync-buffer")  # hourly, last success 7 min before
CANARY = ("monitoring", "provenance-canary")  # every 15 min, last success 5 min before
MONITORING = "monitoring {} Platform · 5/5 up · pods 7/7 ready · {} · 0 alerts"


@pytest.fixture(autouse=True)
def recording(cluster: Cluster) -> None:
    cluster.bodies["cronjobs"] = json.loads((FIXTURES / "cronjobs.json").read_text())


def series(cluster: Cluster, key: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = cluster.bodies["cronjobs"][key]["data"]["result"]
    return found


def answer(cluster: Cluster, key: str, cronjob: tuple[str, str], value: float | None) -> None:
    """Edit the recording: `key`'s query answers `value` for the CronJob (None: no series)."""
    namespace, name = cronjob
    result = series(cluster, key)
    result[:] = [s for s in result if (s["metric"]["namespace"], s["metric"]["cronjob"]) != cronjob]
    if value is not None:
        metric = {"namespace": namespace, "cronjob": name}
        result.append({"metric": metric, "value": [NOW, str(value)]})


def job(
    cluster: Cluster, cronjob: tuple[str, str], name: str, created: float, failed: bool
) -> None:
    """Edit the recording: the CronJob owns one more Job, which failed or not."""
    namespace, owner = cronjob
    metric = {"namespace": namespace, "job_name": name}
    series(cluster, "jobs").append(
        {"metric": {**metric, "owner_name": owner}, "value": [NOW, str(created)]}
    )
    if failed:
        series(cluster, "failed").append({"metric": metric, "value": [NOW, "1"]})


def states(namespace: str) -> dict[str, str]:
    return {c["name"]: c["state"] for c in read()["cronjobs"][namespace]}


def test_the_recording_two_on_time_and_two_late() -> None:
    model = read()["cronjobs"]
    assert set(model) == {"aipostit-prod", "monitoring"}  # aipostit-staging has none
    assert {c["name"]: (c["state"], c["last"], c["next"]) for c in model["aipostit-prod"]} == {
        "aipostit-generate-weekly": ("on time", "47 h ago", "in 5 d"),
        "aipostit-purge-invites": ("late", "2 d ago", "missed, due 25 h ago"),
        "aipostit-purge-photos": ("late", "2 d ago", "missed, due 25 h ago"),
        "aipostit-sync-buffer": ("on time", "7 min ago", "in 39 min"),
    }
    overview = tiles(page())
    assert overview["aipostit-prod"][::2] == (
        "warn",
        "aipostit-prod Alert App · 2/2 up · pods 4/4 ready · cron 2/4 on time · 1 alert"
        " · SLO -1501%",
    )
    assert "cron" not in overview["aipostit-staging"][2]
    assert "cron 1/1 on time" in overview["monitoring"][2]
    shown = text_of(page("/monitors?q=aipostit-prod"))
    assert "aipostit-purge-invites late 2 d ago missed, due 25 h ago 45 3 * * *" in shown
    assert "CronJob" not in text_of(page("/monitors?q=aipostit-staging"))


def test_on_time_within_two_intervals(cluster: Cluster) -> None:
    for name in ("aipostit-purge-invites", "aipostit-purge-photos"):
        answer(cluster, "success", ("aipostit-prod", name), NOW - 47 * 3600)  # 2 days: 48 h
    assert set(states("aipostit-prod").values()) == {"on time"}
    assert "cron 4/4 on time" in tiles(page())["aipostit-prod"][2]


def test_late_past_two_intervals(cluster: Cluster) -> None:
    answer(cluster, "success", SYNC, NOW - 2 * 3600 - 60)
    assert states("aipostit-prod")["aipostit-sync-buffer"] == "late"


def test_the_latest_job_failed_turns_the_tile_alert_until_a_newer_one_succeeds(
    cluster: Cluster,
) -> None:
    cluster.alerts.clear()
    job(cluster, CANARY, "provenance-canary-manual", NOW - 60, failed=True)
    (canary,) = read()["cronjobs"]["monitoring"]
    assert (canary["state"], canary["tone"], canary["failed"]) == (
        "failed",
        "down",
        "provenance-canary-manual",
    )
    assert tiles(page())["monitoring"][::2] == (
        "warn",
        MONITORING.format("Alert", "cron 0/1 on time"),
    )
    assert "latest Job provenance-canary-manual failed" in text_of(page("/monitors?q=monitoring"))

    status._cache = None  # the next refresh
    job(cluster, CANARY, "provenance-canary-29916345", NOW - 30, failed=False)
    assert tiles(page())["monitoring"][::2] == (
        "ok",
        MONITORING.format("Healthy", "cron 1/1 on time"),
    )


def test_an_old_failure_outside_the_window_does_not_stick(cluster: Cluster) -> None:
    """The latest Job, created 3 h ago, failed: two hourly intervals have passed since."""
    answer(cluster, "success", SYNC, NOW - 3600)
    job(cluster, SYNC, "aipostit-sync-buffer-old", NOW - 3 * 3600, failed=True)
    assert states("aipostit-prod")["aipostit-sync-buffer"] == "on time"


def test_never_run_is_unknown_not_on_time(cluster: Cluster) -> None:
    cluster.alerts.clear()
    answer(cluster, "success", CANARY, None)
    assert states("monitoring") == {"provenance-canary": "never run"}
    assert tiles(page())["monitoring"][::2] == (
        "unknown",
        MONITORING.format("Unknown", "cron 0/1 on time"),
    )


def test_a_suspended_one_is_not_counted_or_a_problem(cluster: Cluster) -> None:
    cluster.alerts.clear()
    answer(cluster, "suspend", CANARY, 1)
    answer(cluster, "success", CANARY, NOW - 86400)
    (canary,) = read()["cronjobs"]["monitoring"]
    assert (canary["state"], canary["next"]) == ("suspended", "—")
    assert tiles(page())["monitoring"][::2] == (
        "ok",
        MONITORING.format("Healthy", "cron 1 suspended"),
    )


def test_the_query_failing_leaves_the_cronjobs_unknown(cluster: Cluster) -> None:
    cluster.down = {"cronjobs"}
    model = read()
    assert model["cronjobs"] is None
    assert model["sources"]["cronjobs"]["stale"]
    text = page()
    assert "Prometheus's query API has not answered yet: the CronJobs are unknown." in text
    assert tiles(text)["argocd"][::2] == (
        "unknown",
        "argocd Unknown Platform · 6/6 up · pods 5/5 ready · cron unknown · 0 alerts",
    )
    assert "CronJobs unknown" in page("/monitors?q=aipostit-prod")


@pytest.mark.parametrize(
    ("schedule", "zone", "state"),
    [
        ("0 0 31 2 *", "Etc/UTC", "schedule not understood"),  # never fires
        ("*/15 * * * *", "Mars/Olympus_Mons", "schedule not understood"),
        ("*/15 * * * *", "America/Los_Angeles", "on time"),
    ],
)
def test_a_schedule_or_zone_it_cannot_read_is_unknown(
    cluster: Cluster, schedule: str, zone: str, state: str
) -> None:
    answer(cluster, "success", CANARY, NOW - 30)
    info = series(cluster, "info")[0]
    assert (info["metric"]["namespace"], info["metric"]["cronjob"]) == CANARY
    info["metric"] |= {"schedule": schedule, "timezone": zone}
    (canary,) = read()["cronjobs"]["monitoring"]
    assert (canary["state"], canary["last"]) == (state, "30 s ago")


@pytest.mark.parametrize(
    ("schedule", "before", "previous"),
    [
        ("15 * * * *", "2026-11-18 07:15", "2026-11-18 06:15"),
        ("*/15 * * * *", "2026-11-18 05:45", "2026-11-18 05:30"),
        ("0 6 * * 1", "2026-11-23 06:00", "2026-11-16 06:00"),  # Mondays
        ("0 6 * * MON-FRI", "2026-11-23 06:00", "2026-11-20 06:00"),  # Monday: Friday's
        ("0 0 1,15 * 0", "2026-11-15 00:00", "2026-11-08 00:00"),  # a day or a Sunday
        ("@daily", "2026-11-18 00:00", "2026-11-17 00:00"),
        ("0 0 31 2 *", "2026-11-18 00:00", None),  # never
        ("not a schedule", "2026-11-18 00:00", None),
        ("61 * * * *", "2026-11-18 00:00", None),
        ("0 6 * * FOO", "2026-11-18 00:00", None),
    ],
)
def test_the_run_before(schedule: str, before: str, previous: str | None) -> None:
    found = cronjobs.previous_run(schedule, datetime.fromisoformat(before))
    assert found == (previous and datetime.fromisoformat(previous))
