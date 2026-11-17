"""servicemonitor: the Monitors tab (`/monitors`, and `/apps/<app>` for one app's monitors),
with what the platform's service chart expects of an app:

* port 8000: the app's routes, `/healthz` (liveness: the process runs) and `/readyz`
  (readiness: it can serve);
* port METRICS_PORT (9100): `/metrics` for Prometheus, on a port of its own so the public
  port carries none of it. `http_requests_total` and `http_request_duration_seconds` are
  the names the platform's SLO rules, canary analysis and dashboard read.

`python -m app.main`.
"""

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import Counter, Histogram, start_http_server
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import monitors, pages

REQUESTS = Counter("http_requests_total", "Requests served", ["method", "route", "status"])
# The default buckets; an SLO's latency threshold must be one of their edges
# (deploy/prometheus/slos.yaml).
LATENCY = Histogram("http_request_duration_seconds", "Time to answer", ["method", "route"])

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# No OpenAPI document or docs pages: the routes are pages for people, not an API.
api = FastAPI(title="servicemonitor", openapi_url=None)


@api.get("/", response_class=RedirectResponse)
async def index() -> str:
    return "/monitors"  # until the Overview tab (PT-57) is served here


@api.get("/monitors", response_class=HTMLResponse)
@api.get("/apps/{app}", response_class=HTMLResponse)
async def monitors_tab(
    request: Request, q: str = "", show: str = "all", app: str | None = None
) -> HTMLResponse:
    """`q` filters by app, namespace or monitor name; `show` is all, problems, apps or
    platform. Both are in the URL, so a filtered page can be shared."""
    view = pages.view(await monitors.read(), request.url.path, q, show, app, datetime.now(UTC))
    return templates.TemplateResponse(request, "monitors.html", view)


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
            # The matched route, which the router leaves in the scope.
            route = getattr(scope.get("route"), "path", "unmatched")
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
