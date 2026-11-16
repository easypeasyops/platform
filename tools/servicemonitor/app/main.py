"""servicemonitor: a placeholder page for now, with what the platform's service chart
expects of an app:

* port 8000: the app's routes, `/healthz` (liveness: the process runs) and `/readyz`
  (readiness: it can serve);
* port METRICS_PORT (9100): `/metrics` for Prometheus, on a port of its own so the public
  port carries none of it. `http_requests_total` and `http_request_duration_seconds` are
  the names the platform's SLO rules, canary analysis and dashboard read.

`python -m app.main`.
"""

import os
import time
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.routing import APIRoute
from prometheus_client import Counter, Histogram, start_http_server
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import monitors

REQUESTS = Counter("http_requests_total", "Requests served", ["method", "route", "status"])
# The default buckets; an SLO's latency threshold must be one of their edges
# (deploy/prometheus/slos.yaml).
LATENCY = Histogram("http_request_duration_seconds", "Time to answer", ["method", "route"])

PLACEHOLDER = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>servicemonitor</title></head>
<body>
<h1>servicemonitor</h1>
<p>Placeholder. The overview and the monitors table come next.</p>
</body>
</html>
"""

# No OpenAPI document or docs pages: the routes are pages for people, not an API.
api = FastAPI(title="servicemonitor", openapi_url=None)


@api.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PLACEHOLDER


@api.get("/api/monitors")
async def api_monitors() -> dict[str, Any]:
    return await monitors.read()


@api.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@api.get("/readyz", response_class=PlainTextResponse)
async def readyz() -> str:
    # Answer 503 here while something the app can't serve without (Prometheus, the
    # Kubernetes API) is unreachable: the pod then gets no traffic, and is not restarted.
    return "ok"


_PATHS = frozenset(route.path for route in api.routes if isinstance(route, APIRoute))


class Metrics:
    """Counts and times every request, labelled by route, never by raw path: one series per
    ID or per scanner's guess would grow without bound. A route with a path parameter is
    labelled by its template (`/items/{id}`)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500  # what the client sees if the app raises before responding
        start = time.perf_counter()

        async def send_and_note(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_note)
        finally:
            route = scope["path"] if scope["path"] in _PATHS else "unmatched"
            REQUESTS.labels(scope["method"], route, str(status)).inc()
            LATENCY.labels(scope["method"], route).observe(time.perf_counter() - start)


app = Metrics(api)


def main() -> None:  # pragma: no cover  # scripts/smoke.sh runs it, in the image
    start_http_server(int(os.environ.get("METRICS_PORT", "9100")))
    # The kubelet and the Service reach the pod's IP. uvicorn handles SIGTERM itself: it
    # finishes the requests in hand, then exits 0.
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")  # noqa: S104


if __name__ == "__main__":  # pragma: no cover
    main()
