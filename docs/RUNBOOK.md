# Platform runbook

How to operate the platform's own parts. An app's runbook is in its repo. Alert
`runbook_url`s that point into this file name a heading below (`tests/test_alerting.py`
checks they exist).

## Local cluster operations

The cluster is one kind cluster, `shortly` (context `kind-shortly`), and it is disposable:
`scripts/cluster.sh up`, from a checkout of this repo, rebuilds it from nothing in a few
minutes. It needs Docker Desktop running, and `brew install kind kubectl helm` (Helm 4 or
newer).

| Task | Command |
| --- | --- |
| Create or repair (idempotent) | `scripts/cluster.sh up` |
| Tear down everything | `scripts/cluster.sh down` |
| After the laptop slept (CronJobs late, gaps in Grafana) | `scripts/cluster.sh wake`. To stop it happening while plugged in: `caffeinate -s` in a spare terminal |
| Try a platform change before merging it | Push the branch, then `PLATFORM_REVISION=$(git rev-parse HEAD) scripts/cluster.sh up`; `scripts/cluster.sh up` (back to `main`) when done |
| Without the monitoring stack and the logs (saves about 1 GB of RAM) | `MONITORING=0 scripts/cluster.sh up`, on a fresh cluster: switching it off later removes the Applications but leaves their resources running, by design |
| Without Kyverno and the admission policies | `POLICIES=0 scripts/cluster.sh up` |
| Without the backup bucket (the databases can't archive) | `BACKUPS=0 scripts/cluster.sh up` |
| Without the apps' Applications (an app's e2e deploys itself) | `ARGOCD_APPS=0 scripts/cluster.sh up` |
| Without the front door (ports 8080 and 8090 left free) | `FRONT_DOOR=0 scripts/cluster.sh up`; then port-forward by hand, e.g. `kubectl -n monitoring port-forward svc/grafana 3000:80` |
| Platform state | `kubectl -n argocd get applications -l app.kubernetes.io/part-of=platform`, or the Argo CD UI |
| Every app's state | `kubectl -n argocd get applications` (`<app>-<env>`) |
| Argo CD UI | http://argocd.localhost:8090, user `admin`; password: `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' \| base64 -d` |
| Grafana | http://grafana.localhost:8090, user `admin`; password: `kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' \| base64 -d` |
| Prometheus UI (targets, rules, alerts) | http://prometheus.localhost:8090 |
| Alertmanager UI (routing, silences) | http://alertmanager.localhost:8090 |
| Admission policies and their state | `kubectl get imagevalidatingpolicy,validatingpolicy`; reports: `kubectl get policyreport -A` |

**What `cluster.sh up` does.** It installs Argo CD with Helm (pinned by version and digest,
values in `deploy/kind/argocd-values.yaml`), the one component not deployed from git. It
applies the platform's AppProject and its root Application (`deploy/argocd/platform-root.yaml`,
with the node's architecture and `PLATFORM_REVISION`, default `main`) and waits until every
platform Application is Synced and Healthy, the Gateway is programmed and the policies are
ready. Then it starts the front door and applies the apps root, whose ApplicationSet makes
one Application per `apps/<app>/envs/<env>.yaml`. It makes no secret and no app namespace:
the secrets come from the store (Platform secrets, below), and each app's Application
creates its namespace. From then on git decides what runs: a merged change reaches the
cluster within a minute.

Argo CD installs the platform from `deploy/argocd/platform/` in sync waves:

- **Wave -2:** the platform's AppProject (`platform-project`): what the children may deploy.
- **Wave -1:** External Secrets, cert-manager.
- **Wave 0:** the secret store, CloudNativePG and its Barman Cloud plugin, metrics-server,
  Envoy Gateway, the Gateway and the tool routes, Argo Rollouts, kube-prometheus-stack,
  VictoriaLogs and the log collector, Kyverno, the backup bucket.
- **Wave 1:** the Kyverno policies, and the alerts on Argo CD.

On a fresh cluster the waves overlap: a child that has never synced doesn't hold its wave,
and what orders them is each child's sync retry (every 5 s, doubling to 30 s).

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

Then the tables below, and the app's own runbook for its Application (aipostit: "API
health check fails").

### An Application that won't sync

```sh
kubectl -n argocd get applications
kubectl -n argocd get application <name> -o jsonpath='{.status.sync.revision}{"\n"}{range .status.conditions[*]}{.type}: {.message}{"\n"}{end}{.status.operationState.phase}: {.status.operationState.message}{"\n"}'
kubectl -n argocd get pods
kubectl -n argocd logs deploy/argocd-repo-server --tail=50
```

An app's Application (`<app>-<env>`):

| What you see | Cause | Fix |
| --- | --- | --- |
| Synced, but to an older revision | Not polled yet (every 60 s), or the repo server can't reach GitHub | Wait a minute; `kubectl -n argocd annotate application <name> argocd.argoproj.io/refresh=normal --overwrite` forces a check; then the repo server's logs |
| `ComparisonError` | The service chart doesn't render with the app's values (a typo, a bad value), or the env file here names a digest or a file that doesn't exist | The message names the template or the file; fix it in a PR. Nothing changed in the cluster. CI here (`apps` job) renders every env file, so the break is usually in the app's repo |
| `<app>-<env>` missing | The ApplicationSet isn't running, or can't read `apps/` | `kubectl -n argocd get applicationset apps -o jsonpath='{.status.conditions}'` |
| Sync failed, a webhook denied the request | An admission policy refused a Pod | Admission policy refuses pods, below |
| OutOfSync, auto-sync not running | Someone turned auto-sync off (break glass, below) and didn't turn it back on | After checking git says what should run: `kubectl -n argocd delete application <name>`; the ApplicationSet recreates it from git |
| OutOfSync on the HTTPRoute or the `<app>-stable`/`<app>-canary` Services only | The Application lost its `ignoreDifferences` for what Argo Rollouts owns (an edited Application) | The ApplicationSet restores it within seconds; if not, delete the Application to have it recreated |
| Synced, Progressing or Degraded | The app's own pods, a canary or a hook | The app's runbook |

A platform Application (`kubectl -n argocd get applications -l app.kubernetes.io/part-of=platform`):

| What you see | Cause | Fix |
| --- | --- | --- |
| `kyverno-policies` fails with a webhook error | Kyverno's admission webhook isn't serving yet | It retries for about 9 minutes; if it still fails, `kubectl -n kyverno get pods` |
| `monitoring` or `backups` Degraded on an ExternalSecret | It can't read the store: the value isn't there yet (a generator runs within seconds; the Slack webhooks are loaded by hand: Slack alerts), or the store isn't Ready (`secret-store`, below) | `kubectl -n monitoring get externalsecret -o wide`; `kubectl -n monitoring get externalsecret <name> -o jsonpath='{.status.conditions}'` (or `-n backups … seaweedfs-s3`) |
| `barman-cloud` stays Progressing or Degraded | Its Certificates aren't issued: cert-manager isn't running, or its webhook refused the Issuer before it was serving (the sync retry catches up) | `kubectl -n cnpg-system get certificates,issuers`; `kubectl -n cert-manager logs deploy/cert-manager --tail=30` |
| An app's `<release>-db-backup` ExternalSecret isn't Ready | The `backup-bucket` store refuses it: the namespace lacks the app label its conditions select, or the store's reader can't get `seaweedfs-s3` | `kubectl get clustersecretstore backup-bucket -o jsonpath='{.status.conditions}'`; `kubectl -n <ns> describe externalsecret <release>-db-backup` |
| `secret-store` stays Degraded | A ClusterSecretStore can't read its namespace: External Secrets isn't running, or its token request for the store's reader was refused | `kubectl get clustersecretstore`; `kubectl get clustersecretstore platform -o jsonpath='{.status.conditions}'`; `kubectl -n external-secrets logs deploy/external-secrets --tail=30` |
| `kyverno-policies` (wave 1) never appears | A wave-0 Application isn't Healthy, so the root waits | Fix that one first: `kubectl -n argocd get application platform -o jsonpath='{.status.operationState.message}'` |
| OutOfSync on a webhook's `caBundle` or an aggregated ClusterRole | A field another controller writes, missing from the child's `ignoreDifferences` | Add it in `deploy/argocd/platform/templates/applications.yaml` (a PR) |
| `ComparisonError` on a chart | The registry is unreachable, or a digest in `deploy/argocd/platform/values.yaml` doesn't exist | The repo server's logs; `uv run pytest -m integration tests/test_platform.py` checks every pin against its registry |

### Break glass: turn auto-sync off

Only when the cluster must run something git doesn't say yet (git or GitHub unreachable
mid-incident). Argo CD reverts a hand edit within seconds, so turn auto-sync off first; say
so in the incident channel, and turn it back on, from git, as soon as you can.

```sh
kubectl -n argocd patch application <name> --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'
```

- **An app's Application.** The ApplicationSet leaves the sync policy as you set it. To
  restore it, put what should run in git, then `kubectl -n argocd delete application <name>`:
  it has no finalizer, so the app keeps running, and the ApplicationSet recreates the
  Application from git within seconds.
- **A platform Application.** The root (`platform`) puts a child's spec back, so patch the
  root first, then the child. To restore both: `scripts/cluster.sh up`, which applies the
  root as git has it.

## Admission policy refuses pods

Kyverno checks every Pod in an app's namespaces (the ones labelled
`easypeasyops.github.io/app`) before it is created (`deploy/policies/`). Pods that are
already running are never touched, so a refusal blocks deploys and scale-ups, not traffic.
Find which case it is:

```sh
kubectl -n <ns> get events --sort-by=.lastTimestamp | grep -i -E 'denied|webhook' | tail
kubectl -n kyverno get pods
kubectl get imagevalidatingpolicy,validatingpolicy     # READY must be true
```

| Message | Cause | Fix |
| --- | --- | --- |
| `our images must be a release digest with provenance…` | An unreleased or unattested image: a CI build, a fork, or a per-platform child digest instead of the release's index digest | Deploy the digest from the GitHub Release notes. `scripts/verify-image.sh <ref>` tells you why it fails |
| `image ghcr.io/easypeasyops/…:… does not have a digest` | A tag was used | Use `@sha256:…`; tags are never verified |
| `provenance names a different repository than the image's` | The image was built and signed by another of our repos' release workflows (`ghcr.io/easypeasyops/<repo>/<app>` must come from `easypeasyops/<repo>`) | Treat as suspicious: an image pushed under another repo's name. Find who pushed it |
| `an app image must be ghcr.io/easypeasyops/<repo>/<app>, built from a <app>/vX.Y.Z tag` | An app image built from another app's tag, or a name without the app part | Treat as suspicious unless it's a mistyped name: one app's release published as another |
| `provenance is not from a GitHub Actions workflow build` | The attestation exists but describes a different build | Treat as suspicious: who produced this image? |
| `failed calling webhook "…kyverno…"` or `context deadline exceeded` | Kyverno is down or slow, or it can't reach `ghcr.io` / Sigstore (Rekor, TUF). The policy fails closed, so **every** new Pod in every app namespace, databases included, is refused | `kubectl -n kyverno logs deploy/kyverno-admission-controller --tail=50`; propose `kubectl -n kyverno rollout restart deploy/kyverno-admission-controller` |

**Break glass**, only when Kyverno can't be recovered and pods must start (for example a
node died and prod is below its minimum). It needs cluster-admin. Say so in the incident
channel first:

```sh
# 1. Stop Argo CD putting the policy back: auto-sync off on the root, then on the policies.
kubectl -n argocd patch application platform --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'
kubectl -n argocd patch application kyverno-policies --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'
# 2. Remove the provenance policy (the webhook follows within seconds).
kubectl delete imagevalidatingpolicy require-release-provenance
# 3. If Kyverno itself is down, the webhook still fails closed: remove its configurations.
kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations | grep -i kyverno
kubectl delete validatingwebhookconfiguration <name>      # each kyverno one listed above
# 4. Restore as soon as possible (auto-sync back on; waits for Kyverno and the policies):
scripts/cluster.sh up
```

Afterwards, check that everything running is a release: `scripts/verify-image.sh` on each
image from `kubectl -n <ns> get pods -o jsonpath='{..image}'`.

**Allowlist reports.** `app-allowed-images` is Audit in kind, so nothing is refused: an
image outside the allowlist, or another app's image, runs and is reported.

```sh
kubectl get policyreport -A -o wide                  # FAIL > 0: an image outside the allowlist ran
```

## The front door

One Gateway, `shortly` in namespace `gateway` (Envoy Gateway, `deploy/kind/gateway.yaml`),
routes by hostname on two listeners: `http` for the apps (the laptop's port 8080, e.g.
http://aipostit.localhost:8080; each app's values name its hostnames) and `tools` for
Argo CD, Grafana, Prometheus and Alertmanager (port 8090). Two small forwarder containers
beside the kind node, `shortly-front-door-8080` and `-8090`, carry the laptop's ports to the
Gateway's node ports; they restart with Docker, so there is no port-forward to keep alive.
The `tools` listener has no login of its own: the forwarders listen on 127.0.0.1 only.

The Gateway is the user path; probes and Prometheus go around it. So a broken front door
looks like **traffic dropping**, not errors: no alert fires on the pods.

```sh
docker ps --filter name=shortly-front-door                     # two, Up
kubectl -n gateway get gateway shortly                         # PROGRAMMED must be True
kubectl get httproute -A
kubectl -n <ns> get httproute <app> -o jsonpath='{.status.parents[0].conditions}'   # Accepted, ResolvedRefs
kubectl -n envoy-gateway-system get pods                       # envoy-gateway (controller) and the proxy
kubectl -n envoy-gateway-system logs deploy/envoy-gateway --tail=50
```

| What you see | Cause | Fix |
| --- | --- | --- |
| Nothing answers on 8080 or 8090 | A forwarder isn't running: usually its port was taken by an old `kubectl port-forward` | `scripts/cluster.sh up` recreates them, and says which process holds the port; stop that one |
| Gateway not `Programmed` | The controller is down, or the EnvoyProxy/GatewayClass is invalid | The controller's logs; `kubectl get gatewayclass envoy -o yaml` |
| Route not `Accepted`, `NotAllowedByListeners` | Its namespace lacks the `easypeasyops.github.io/app` label the `http` listener selects (a namespace Argo CD hasn't synced since the label was added) | `kubectl get ns -l easypeasyops.github.io/app`. Missing: sync the Application, which labels its namespace (`kubectl -n argocd patch application <app>-<env> --type merge -p '{"operation":{"sync":{}}}'`). The same missing label also takes the namespace out of the admission policies and the `backup-bucket` store |
| Route not `Accepted` otherwise | Its `hostnames` conflict with another route's | One hostname per app and environment |
| `ResolvedRefs: False` | The route points at a Service or port that doesn't exist | `kubectl -n <ns> get svc`; the route's `backendRefs` |
| The Service answers in-cluster, through the Gateway it doesn't | The proxy can't reach the pods: the app's NetworkPolicy doesn't admit `envoy-gateway-system` | The app's `networkPolicy` values |

## Network policies

Every app namespace starts with `default-deny-ingress` (`deploy/app-namespace/`, a source
of each app's Application): a pod receives traffic only if a policy lets it in. The service
chart writes the app's own policies from its values. A blocked connection **times out**
rather than being refused, so it looks like a slow network, not an error; Prometheus shows
the target `down` with `context deadline exceeded`.

```sh
kubectl -n <ns> get networkpolicy
kubectl -n <ns> describe networkpolicy <name>     # what the pods may receive and send
kubectl -n <ns> get pods --show-labels             # policies select by label: check them
```

Don't delete a policy to "see if it helps". Check labels first: almost every case is a
selector that no longer matches, a pod's or a namespace's.

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
| `platform-webhook-url` | #platform-alerts |

**Load or replace the webhooks** (before the ExternalSecret first syncs, or Alertmanager
can't start). Make each webhook in Slack (api.slack.com/apps: the app, **Incoming
Webhooks**, **Add New Webhook**, pick the channel), then:

```sh
umask 077
cat > /tmp/alertmanager-slack.env   # paste these three lines with the real URLs, then Ctrl-D
# aipostit-prod-webhook-url=https://hooks.slack.com/services/...
# aipostit-staging-webhook-url=https://hooks.slack.com/services/...
# platform-webhook-url=https://hooks.slack.com/services/...
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

The platform's own alerts go to #platform-alerts: `ArgoCDDown`, and Argo CD's alerts on the
platform's Applications (projects `platform` and `apps`), titled "on <Application>". All
tickets: yellow, never `@channel`. The stack's warning and critical alerts stay on `null`.

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

## Platform secrets

The source of every platform secret is a Secret in namespace `secret-store`
(`deploy/secrets/`); External Secrets copies each to where it is used, and nothing is in git.

| Store Secret | Made by | Copied to |
| --- | --- | --- |
| `grafana-admin` | A generator, once | `monitoring` (Grafana's admin login) |
| `seaweedfs-s3` | A generator, once | `backups` (the bucket's S3 identity), and each app database's `<release>-db-backup` through the `backup-bucket` store |
| `alertmanager-slack` | By hand (Slack alerts, above) | `monitoring` |
| `<app>` | By hand: `scripts/load-app-env.sh <app> <dotenv file>` | The app's namespaces, as `<release>-env`, through the app's own store |

**Rotate a generated secret** (it leaked). Its source is immutable, so deleting it is the
only way to replace it: the generator makes a new one. Then pull the new value out at once
(otherwise within the hour) and restart what reads it at start.

```sh
# Grafana's admin password
kubectl -n secret-store delete secret grafana-admin
kubectl -n monitoring annotate externalsecret grafana-admin force-sync=$(date +%s) --overwrite
kubectl -n monitoring rollout restart deploy/grafana

# The bucket's keys
kubectl -n secret-store delete secret seaweedfs-s3
kubectl -n backups annotate externalsecret seaweedfs-s3 force-sync=$(date +%s) --overwrite
kubectl -n backups rollout restart statefulset/seaweedfs
kubectl -n <app ns> annotate externalsecret <release>-db-backup force-sync=$(date +%s) --overwrite   # each app database
```

**Replace an app's keys:** edit its dotenv file and run `scripts/load-app-env.sh` again
(every key in the file replaces what the Secret held; it prints key names, never values),
then restart the app's pods, which read their env at start.

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
sends the lines within seconds. Read them in Grafana, or ask VictoriaLogs directly (both
below).

**What a line looks like there.** It is indexed by where it came from, its stream:
`kubernetes.pod_namespace`, `kubernetes.pod_name`, `kubernetes.container_name`. A JSON line
(aipostit's) is taken apart: `msg` becomes `_msg`, `ts` becomes `_time`, and every other
key (`request_id`, `route`, `status`, `level`...) is a field to search by. Any other line is
kept whole as `_msg`. Never make a per-request value a stream field
(`deploy/kind/log-collector-values.yaml`).

**In Grafana.** http://grafana.localhost:8090, Explore, data source **VictoriaLogs**. The
query language is LogsQL (not Loki's LogQL): a stream filter in braces, then fields, then
pipes.

```
{kubernetes.pod_namespace="aipostit-prod"} request_id:="<id>"
{kubernetes.pod_namespace="aipostit-prod"} level:=error
_time:1h | stats by (kubernetes.pod_namespace) count() lines
```

The data source is provisioned (`grafana.additionalDataSources`,
`deploy/kind/monitoring-values.yaml`), so it can't be edited in the UI. Is it healthy:
Connections, Data sources, VictoriaLogs, **Test**. If it is missing or says the plugin is
not found, Grafana started without `victoriametrics-logs-datasource`: it downloads the
plugin from grafana.com at every start of its pod (`kubectl -n monitoring logs
deploy/grafana -c grafana | grep -i plugin`), so restart it once the laptop is online:
propose `kubectl -n monitoring rollout restart deploy/grafana`. If Test fails with the
plugin there, the store is down or its NetworkPolicy no longer admits Grafana's pods.

**Asking it directly.** `kubectl -n monitoring port-forward svc/victoria-logs 9428`, then its own UI
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

## The backup bucket

Where the apps' databases keep their backups: SeaweedFS serving S3, the StatefulSet
`seaweedfs` in namespace `backups` (the platform's `backups` Application,
`deploy/kind/backups.yaml`), on its own 1Gi volume. An app's database (the service chart's
`database.cluster`, a CloudNativePG Cluster) archives WAL continuously and takes base
backups there, to `s3://cnpg-backups/<ns>/<release>-db`, through the Barman Cloud plugin.
Whether an app's backups run, and how to restore one, is in the app's runbook; this is the
bucket's side.

```sh
kubectl -n backups get pods,pvc
kubectl -n backups get externalsecret seaweedfs-s3            # Ready True: it has its S3 identity
kubectl -n cnpg-system get pods                               # the operator and the plugin
```

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| A database's archiving fails with errors on access or credentials | Its `<release>-db-backup` ExternalSecret isn't Ready, or the bucket's keys were rotated and it still holds the old ones | `kubectl -n <ns> describe externalsecret <release>-db-backup`; after a rotation, the force-sync in Platform secrets |
| Errors connecting to `seaweedfs.backups.svc` | The bucket is down, or its NetworkPolicy refuses the pod: it admits only a Cluster's pods (label `cnpg.io/cluster`) from an app namespace or `restore-drill` | `kubectl -n backups get pods`; the pod's and its namespace's labels |
| No new backups at all, on the laptop | The Mac slept, or the operator isn't running | `kubectl -n cnpg-system get pods`; a ScheduledBackup catches up at its next run |
| Archiving fails with "WAL archive not empty" after a Cluster was recreated | A new Cluster won't archive into a folder that holds another one's WAL | Move the old backups aside in the bucket, or delete them if they aren't needed |

Deleting the PVC (or the kind cluster) loses every backup; on a real cluster this is a
bucket in another account, with versioning and object lock.
