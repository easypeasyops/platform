"""For every test: the Platform Radar's scores file (app/radar.py) answered from
tests/fixtures/scores.json, as raw.githubusercontent.com served it from main on
2026-11-16, never fetched from GitHub."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import radar


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
