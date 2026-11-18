"""For every test: the Platform Radar's scores file (app/radar.py) answered from
tests/fixtures/scores.json, as raw.githubusercontent.com served it from main on
2026-11-16, never fetched from GitHub. And the environments' queries, from a recording."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import environments, radar


class GitHub:
    """Answers the scores file's URL with `body`, or `status` with no body. A test changes
    either."""

    def __init__(self) -> None:
        self.body: Any = json.loads((Path(__file__).parent / "fixtures/scores.json").read_text())
        self.status = 200
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="404: Not Found")
        if isinstance(self.body, str):
            return httpx.Response(200, text=self.body)
        return httpx.Response(200, json=self.body)


@pytest.fixture(autouse=True)
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[GitHub]:
    github = GitHub()
    monkeypatch.setattr(radar, "transport", httpx.MockTransport(github))
    monkeypatch.setattr(radar, "_cache", None)
    yield github


class Prometheus:
    """The Overview's environments (app/environments.py): its two queries answered from
    tests/fixtures/environments.json, recorded from the laptop cluster on 2026-11-17. A
    test changes `bodies`, or names a query (images, applications) in `down`."""

    def __init__(self) -> None:
        recording = json.loads((Path(__file__).parent / "fixtures/environments.json").read_text())
        self.bodies: dict[str, Any] = recording
        self.down: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        name = {environments.IMAGES: "images", environments.APPLICATIONS: "applications"}[query]
        if name in self.down:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=self.bodies[name])


@pytest.fixture(autouse=True)
def env_prometheus(monkeypatch: pytest.MonkeyPatch) -> Iterator[Prometheus]:
    prometheus = Prometheus()
    monkeypatch.setattr(environments, "transport", httpx.MockTransport(prometheus))
    monkeypatch.setattr(environments, "_cache", None)
    yield prometheus
