# Platform runbook

How to operate the platform's own parts. An app's runbook is in its repo; the rest of the
platform's runbook is still in links (README, Docs) and moves here part by part. Alert
`runbook_url`s that point into this file name a heading below (`tests/test_alerting.py`
checks they exist).

## Argo CD alerts

Tickets, never pages: a stuck Argo CD doesn't break what already runs, it stops new changes
from landing. They cover every app's Applications (`<app>-<env>`, label `name`) and the
platform's own. Rules: `deploy/argocd/platform/files/argocd-rules.yaml`.

| Alert | Means | First look |
| --- | --- | --- |
| `ArgoCDAppOutOfSync` | An Application has differed from git for 15 minutes (auto-sync normally takes about a minute) | `kubectl -n argocd get applications`; `kubectl -n argocd get application <name> -o jsonpath='{.status.conditions}'` |
| `ArgoCDAppUnhealthy` | Degraded, Missing or Unknown for 10 minutes (a canary between steps is Suspended or Progressing, never this) | `kubectl -n argocd get application <name> -o jsonpath='{range .status.resources[?(@.health.status!="Healthy")]}{.kind}/{.namespace}/{.name}: {.health.message}{"\n"}{end}'` |
| `ArgoCDSyncFailed` | A sync was refused in the last 15 minutes (admission policy, invalid manifest, failed hook), even if a retry worked | `kubectl -n argocd get application <name> -o jsonpath='{.status.operationState.message}'` |
| `ArgoCDDown` | No application controller scraped for 10 minutes: nothing syncs, nothing self-heals, and the three above are blind | `kubectl -n argocd get pods`; `kubectl -n argocd logs statefulset/argocd-application-controller --tail=50` |

Then the app's own runbook for its Application (aipostit: "API health check fails"), or,
for a platform Application, the table of platform sync failures in links'
`apps/shortly/docs/RUNBOOK.md` (Argo CD alerts).

## Slack alerts

Alertmanager posts to Slack through incoming webhooks, one per channel. A webhook URL is a
secret (anyone holding it can post to the channel): it lives only in the store, as the
Secret `alertmanager-slack` in `secret-store`, never in git, chat or a ticket. The
ExternalSecret in `deploy/secrets/monitoring.yaml` copies it into `monitoring`, where
Alertmanager mounts it. Keys, one per channel:

| Key | Channel |
| --- | --- |
| `aipostit-prod-webhook-url` | #aipostit-alerts-prod |
| `aipostit-staging-webhook-url` | #aipostit-alerts-staging |

**Load or replace the webhooks** (before the ExternalSecret first syncs, or Alertmanager
can't start). Make each webhook in Slack (api.slack.com/apps: the app, **Incoming
Webhooks**, **Add New Webhook**, pick the channel), then:

```sh
umask 077
cat > /tmp/alertmanager-slack.env   # paste these two lines with the real URLs, then Ctrl-D
# aipostit-prod-webhook-url=https://hooks.slack.com/services/...
# aipostit-staging-webhook-url=https://hooks.slack.com/services/...
kubectl -n secret-store create secret generic alertmanager-slack \
  --from-env-file=/tmp/alertmanager-slack.env --dry-run=client -o yaml \
  | kubectl apply --server-side --field-manager=alertmanager-slack -f -
rm /tmp/alertmanager-slack.env
# Key names only, never values:
kubectl -n secret-store get secret alertmanager-slack -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}'
```

Write no quotes around the values. Every key must be in the file: a key left out is
removed. A replaced URL reaches Alertmanager within the hour (the ExternalSecret's
refresh); check with `kubectl -n monitoring get externalsecret alertmanager-slack` (Ready
`True`). If a webhook leaks, delete it in Slack first, then load the new one.

**Where alerts go** (`deploy/kind/monitoring-values.yaml`, checked by
`tests/test_alerting.py`). aipostit's rules (severity `page` or `ticket`) go to its
environment's channel: `aipostit-prod` to #aipostit-alerts-prod, `aipostit-staging` to
#aipostit-alerts-staging. Argo CD's alerts on its Applications go by the Application's name:
`aipostit-staging`'s to staging, any other to prod. Argo CD's alerts on `deploybot-ops`
(aipostit's release bot, hotel_social_posts `apps/deploybot/`) go to #aipostit-alerts-prod
too, as tickets: it has no rules of its own, and a crash loop, a pod never Ready or a failed
sync shows there as `ArgoCDAppUnhealthy` or `ArgoCDSyncFailed`, titled "in deploybot-ops". Grouped by alert and environment, repeated
every 4 hours while firing, and a message when it resolves. A page is red and, in prod,
notifies the channel (`@channel`); a ticket is yellow and doesn't. While a page fires in an
environment, its tickets there are held back (inhibited). The stack's own warning and
critical alerts on the same pods stay on `null`: aipostit's rules cover them.

**Test the route end to end** (posts to the real channels: say so there first). An alert sent
by hand, which resolves itself after its end time:

```sh
amtool --alertmanager.url=http://alertmanager.localhost:8090 alert add AipostitTest \
  namespace=aipostit-staging severity=ticket \
  --annotation=summary="Test alert, please ignore" --end="$(date -u -v+5M +%Y-%m-%dT%H:%M:%SZ)"
```

It reaches #aipostit-alerts-staging after `group_wait` (30 s), and a resolved message about
5 minutes after the end time. `namespace=aipostit-prod severity=page` tests the prod channel
and its `@channel`. Nothing arrives: `kubectl -n monitoring logs alertmanager-kps-alertmanager-0
-c alertmanager | grep -i slack` (a 404 or `invalid_token` is a revoked webhook: load a new
one, above).

**Silence** a known alert while you work on it: Alertmanager's UI
(http://alertmanager.localhost:8090, New Silence) or `amtool silence add alertname=<name>
namespace=<ns> --duration=2h --comment="<why>"`.

## Prometheus data

Prometheus keeps 10 days (`retention: 10d`, capped at 12 GB by `retentionSize`) on its own
15Gi volume, `prometheus-kps-prometheus-db-prometheus-kps-prometheus-0` in `monitoring`
(kind's local-path storage, under the node's disk), so restarts and upgrades keep it.
Settings: `deploy/kind/monitoring-values.yaml`.

- How far back it goes: `min(prometheus_tsdb_lowest_timestamp)` in Prometheus, in ms.
- How much it holds: `prometheus_tsdb_storage_blocks_bytes`. About 0.5 GB a day at today's
  load; past 12 GB the oldest blocks go first, whatever their age.
- Deleting the PVC (or the kind cluster) loses the history; Prometheus starts empty.

## Logs

Every pod's log lines are kept in VictoriaLogs (single node, `victoria-logs-0` in
`monitoring`, the platform's `logs` Application), so they outlive the pod that wrote them.
The collector (vlagent, the DaemonSet `log-collector` in the namespace of that name, the
platform's `log-collector` Application) reads each container's log file on the node and
sends the lines within seconds. Grafana's data source is a change of its own: until then,
ask VictoriaLogs directly (below).

**What a line looks like there.** It is indexed by where it came from, its stream:
`kubernetes.pod_namespace`, `kubernetes.pod_name`, `kubernetes.container_name`. A JSON line
(aipostit's) is taken apart: `msg` becomes `_msg`, `ts` becomes `_time`, and every other
key (`request_id`, `route`, `status`, `level`...) is a field to search by. Any other line is
kept whole as `_msg`. Never make a per-request value a stream field
(`deploy/kind/log-collector-values.yaml`).

**Asking it.** `kubectl -n monitoring port-forward svc/victoria-logs 9428`, then its own UI
at http://localhost:9428/select/vmui, or:

```sh
q() { curl -s localhost:9428/select/logsql/query --data-urlencode "query=$1"; }
# Lines per namespace over the last hour: is everything arriving?
q '_time:1h | stats by (kubernetes.pod_namespace) count() lines'
# Everything one aipostit request logged (its X-Request-ID)
q '{kubernetes.pod_namespace="aipostit-prod"} request_id:="<id>"'
```

**The store.** It keeps 30 days (`retentionPeriod: 30d`), or less if the logs outgrow 4 GiB
first (`retentionDiskSpaceUsage`: the oldest day goes, whatever its age), on its own 5Gi
volume, `server-volume-victoria-logs-0` in `monitoring` (kind's local-path storage, under
the node's disk), so restarts and upgrades keep it. Settings:
`deploy/kind/victoria-logs-values.yaml`. It listens on `victoria-logs.monitoring.svc:9428`
and has no login of its own, so a NetworkPolicy lets only the collector's namespace,
Grafana and Prometheus connect: don't give it a route on the Gateway.

- Is it up: the `logs` Application is Synced and Healthy, and in Prometheus
  `up{job="victoria-logs"}` is 1.
- How much it holds: `sum(vl_data_size_bytes)` in Prometheus.
- Deleting the PVC (or the kind cluster) loses the logs; VictoriaLogs starts empty.

**The collector.** `log-collector` is the one namespace at Pod Security `privileged`,
because reading the node's files takes a hostPath volume; nothing else belongs in it. The
pod runs as root (the kubelet's log files are root's alone) with no capabilities, the
node's `/var/log` read-only, and one directory it may write: the node's
`/var/lib/vl-collector`, where it notes how far it has read each file and buffers lines
(up to 1 GiB) while VictoriaLogs is away, so that replacing either pod neither skips nor
re-sends lines. Settings: `deploy/kind/log-collector-values.yaml`.

- Is it sending: `rate(vl_rows_ingested_total[5m])` in Prometheus is above 0 (Argo CD alone
  logs every few seconds), and the per-namespace count above lists every namespace.
- Nothing arrives: `kubectl -n log-collector get pods` and `logs ds/log-collector` (a refused
  connection is the store down or its NetworkPolicy; "permission denied" is the security
  context).
- A pod deleted before its lines were read loses them: the kubelet removes its files with it.
  Recreating the kind node loses the reading positions with the rest of its disk.
