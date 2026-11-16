# servicemonitor

A page that shows, at a glance, whether Prometheus sees every app and platform tool on the
cluster: every ServiceMonitor, PodMonitor and Probe, its scrape targets, alerts and SLO
error budget (PT-51). For now it serves a placeholder page.

It lives in the platform repo, and is built, released, admitted and run like any other
app: the image `ghcr.io/easypeasyops/platform/servicemonitor`, deployed with a released
service chart.

## Develop

From `tools/servicemonitor`:

```bash
uv run pytest                 # tests, with coverage
uv run ruff format --check . && uv run ruff check . && uv run mypy app tests
docker build -t servicemonitor:dev . && scripts/smoke.sh
```

## SLO burn-rate alerts

The SLO alerts' runbook link points here until the app has a runbook of its own. An alert
names the SLO (`slo` label) and the namespace; it fires when the error budget is being
spent faster than the objective allows. Start from the platform's SLO dashboard in
Grafana (traffic, errors and latency by route), then the app's logs.
