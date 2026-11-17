# servicemonitor

A page that shows, at a glance, whether Prometheus sees every app and platform tool on the
cluster: every ServiceMonitor, PodMonitor and Probe, its scrape targets, alerts and SLO
error budget (PT-51). For now it serves the Monitors tab (`/monitors`, rendered on the
server from `app/templates/`; `?q=` and `?show=problems|apps|platform` filter it;
`/apps/<app>` is a link to one app's monitors), and `/api/monitors`: every monitor joined
with its scrape targets, as JSON.

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

`/api/monitors` reads the Kubernetes API (`KUBERNETES_URL`, by default the cluster's own,
with the pod's ServiceAccount) and Prometheus (`PROMETHEUS_URL`, by default
`http://kps-prometheus.monitoring.svc:9090`). Against the laptop cluster, from outside it:

```bash
kubectl --context kind-shortly proxy &   # 127.0.0.1:8001, read with your own access
KUBERNETES_URL=http://127.0.0.1:8001 \
PROMETHEUS_URL=http://127.0.0.1:8001/api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy \
  uv run uvicorn app.main:app --port 8000
```

The page links each monitor to Grafana (dashboards, and the logs in Explore), Prometheus's
alerts and its Argo CD Application, at `GRAFANA_LINK_URL`, `PROMETHEUS_LINK_URL` and
`ARGOCD_LINK_URL`: where a browser reaches them, by default the laptop's front door
(`http://grafana.localhost:8090`, `http://prometheus.localhost:8090`,
`http://argocd.localhost:8090`).

## Release and deploy

Bump `version` in `pyproject.toml` (a PR), then Emrah tags the merge commit
`servicemonitor/vX.Y.Z` and pushes the tag: `.github/workflows/release.yml` publishes
`ghcr.io/easypeasyops/platform/servicemonitor` and puts its digest in the release's notes.
Set it as `image.digest` in `deploy/envs/ops.yaml` (a PR): Argo CD
(`apps/servicemonitor/envs/ops.yaml`) deploys what is on `main`.

## SLO burn-rate alerts

The SLO alerts' runbook link points here until the app has a runbook of its own. An alert
names the SLO (`slo` label) and the namespace; it fires when the error budget is being
spent faster than the objective allows. Start from the platform's SLO dashboard in
Grafana (traffic, errors and latency by route), then the app's logs.
