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
  node-exporter, VictoriaLogs and the log collector, Kyverno, the backup bucket.
- **Wave 1:** the Kyverno policies, and the alerts on Argo CD, on the log pipeline, on
  External Secrets and on cert-manager.

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
| `monitoring` or `backups` Degraded on an ExternalSecret | It can't read the store: the value isn't there yet (a generator runs within seconds; the Slack webhooks are loaded by hand, and deleted from the store after `cluster.sh up` saw them: Slack alerts), or the store isn't Ready (`secret-store`, below) | `kubectl -n monitoring get externalsecret -o wide`; `kubectl -n monitoring get externalsecret <name> -o jsonpath='{.status.conditions}'` (or `-n backups … seaweedfs-s3`) |
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

## Kyverno alerts

Warnings, to the platform's channel. Kyverno runs one admission pod and the provenance
policy fails closed, so Kyverno down blocks every new Pod in the apps' namespaces: a deploy,
a scale-up, the replacement for a pod that crashed. What already runs keeps running. Rules:
`deploy/argocd/platform/files/kyverno-rules.yaml`.

| Alert | Means | First look |
| --- | --- | --- |
| `KyvernoAdmissionControllerDown` | The admission controller hasn't been scraped for 5 minutes: no Pod can be created in an app's namespace | `kubectl -n kyverno get pods`; `kubectl -n kyverno describe pod -l app.kubernetes.io/component=admission-controller` (OOMKilled? its limit is in `deploy/kind/kyverno-values.yaml`); `kubectl -n kyverno logs deploy/kyverno-admission-controller --tail=50` (add `--previous` after a crash) |
| `KyvernoReportsControllerDown` | The reports controller hasn't been scraped for 15 minutes: PolicyReports go stale (the allowlist's Audit results), admission still works | `kubectl -n kyverno get pods`; `kubectl -n kyverno logs deploy/kyverno-reports-controller --tail=50` |
| `KyvernoProvenanceCheckErrors` | Kyverno is up but the provenance policy couldn't run for a request in `resource_namespace` (it errored, it didn't refuse): that Pod or rollout was refused. Usually `ghcr.io` unreachable from Kyverno; in one namespace only, maybe a digest that doesn't exist | `kubectl -n <resource_namespace> get events --sort-by=.lastTimestamp \| grep -i -E 'denied\|webhook' \| tail`; `kubectl -n kyverno logs deploy/kyverno-admission-controller --tail=50 \| grep -i -E 'imagedata\|error'`; `scripts/verify-image.sh <ref>` from the laptop |
| `KyvernoWebhookCallsFailing` | The API server got no answer from the provenance webhook (a 30 s timeout, or Kyverno unreachable) and refused the request: no Pod can be created in an app's namespace. Fires beside `KyvernoAdmissionControllerDown` when the pod is down, and before it | As `KyvernoAdmissionControllerDown`; with the pod Ready, it is slow: `kubectl -n kyverno top pod`, and its log for registry or Rekor timeouts |

A pod that is there and not Ready, or in a crash loop: propose
`kubectl -n kyverno rollout restart deploy/kyverno-admission-controller` (or
`deploy/kyverno-reports-controller`). No pod at all: the `kyverno` Application
(`kubectl -n argocd get application kyverno`; An Application that won't sync, above). If
pods must start before Kyverno is back, the break glass is in Admission policy refuses
pods, above. Either `…Down` alert with its pod running and Ready is the scrape: Prometheus's Targets
page (http://prometheus.localhost:8090/targets), `kyverno-svc-metrics` and
`kyverno-reports-controller-metrics`.

Neither of the last two tells Sigstore (Rekor, TUF) unreachable from an unreleased image:
Kyverno counts a signature it couldn't check as a refusal (`result="fail"`). A release
digest refused with `our images must be a release digest…` is that case: check
Kyverno's log, and `scripts/verify-image.sh <ref>`, which passes for a release.

## The front door

One Gateway, `shortly` in namespace `gateway` (Envoy Gateway, `deploy/kind/gateway.yaml`),
routes by hostname on two listeners: `http` for the apps (the laptop's port 8080, e.g.
http://aipostit.localhost:8080; each app's values name its hostnames) and `tools` for
Argo CD, Grafana, Prometheus and Alertmanager (port 8090). Two small forwarder containers
beside the kind node, `shortly-front-door-8080` and `-8090`, carry the laptop's ports to the
Gateway's node ports; they restart with Docker, so there is no port-forward to keep alive.
The `tools` listener has no login of its own: the forwarders listen on 127.0.0.1 only.

The Gateway is the user path; probes and Prometheus go around it. So a broken front door
looks like **traffic dropping**, not errors: no alert fires on the pods. The proxies' own
metrics show it (the dashboard below); alerts watch the proxies and the controller
([the front door is down](#the-front-door-is-down)) and the routes' status
([a route with no backend](#a-route-with-no-backend)).

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

**Dashboard:** http://grafana.localhost:8090/d/front-door (Front door). Per route (an
HTTPRoute, so an app and its environment): request rate, 5xx share, latency and healthy
backends, for the requests that reached a backend. Per listener (`http-10080` is `http`,
the users' port 8080; `http-8081` is `tools`): every answer, and the ones the proxy gave
itself, which no app's metrics show: 5xx for a route with no backend, 404 for a hostname
no route matches. A route with no backend has no Envoy series of its own: it drops off
"Healthy backends", and is listed under "Routes with no backend", from its status.

Prometheus scrapes the proxies (job `envoy`) and the controller (job `envoy-gateway`):
http://prometheus.localhost:8090/targets. The proxies count every request they answer,
the ones that never reached an app included; a route's series (`envoy_cluster_*`) carry
`route_namespace` and `route`, the HTTPRoute's namespace and name.

### The front door is down

To #platform-alerts. Rules: `deploy/argocd/platform/files/front-door-rules.yaml`. With
the pod gone the stack's `TargetDown` has no target left to be down, so these read
`absent(up{job=...} == 1)`.

| Alert | Means | First look |
| --- | --- | --- |
| `FrontDoorDown` (critical) | No proxy (job `envoy`) scraped for 3 minutes: every app and tool is unreachable from outside | `kubectl -n envoy-gateway-system get deploy,pods -l app.kubernetes.io/component=proxy`; `kubectl -n envoy-gateway-system describe pod -l app.kubernetes.io/component=proxy` (OOMKilled? its limit is the EnvoyProxy's, `deploy/kind/gateway.yaml`); `kubectl -n gateway get gateway shortly` |
| `FrontDoorControllerDown` (warning) | No controller (job `envoy-gateway`) scraped for 10 minutes: what runs keeps serving, but no Gateway or route change reaches the proxies, and a proxy that goes is not replaced | `kubectl -n envoy-gateway-system get pods`; `kubectl -n envoy-gateway-system logs deploy/envoy-gateway --tail=50` (add `--previous` after a crash) |

The controller makes the proxy's Deployment: a proxy Deployment that is gone with the
controller up comes back within seconds, so `FrontDoorDown` with no proxy Deployment
means the controller is down too (fix that first) or the Gateway isn't `Programmed` (the
table above). No controller pod at all: the `envoy-gateway` Application
(`kubectl -n argocd get application envoy-gateway`; An Application that won't sync,
above). Either alert with its pod running and Ready is the scrape: Prometheus's Targets
page (http://prometheus.localhost:8090/targets), `envoy` and `envoy-gateway`.

### A route with no backend

`FrontDoorRouteWithoutBackend` (a ticket, to #platform-alerts): for 5 minutes an HTTPRoute
has had no backend to send to. Envoy Gateway programs a route whose Service has no ready
pod to answer 503, and one whose Service doesn't exist 500: the proxy answers **itself**.
The requests never reach the app: its own metrics and alerts stay quiet, which is why this
one is the platform's, an app's route included (the app's channel doesn't get it: its own
alerts on its pods say why they are gone).

The message names the route, `<namespace>/<name>`, and the reason: `EndpointsNotFound` (no
ready pod) or `BackendNotFound` (no Service). It needs no traffic: Envoy counts these
answers per listener only, so the alert reads the route's status instead, which
kube-state-metrics exports as `gateway_httproute_status_condition{route_namespace, route,
type, reason}` (1 for True, 0 for False; `deploy/kind/monitoring-values.yaml`). The same
from the cluster:

```sh
# Routes with no backend to send to (EndpointsNotFound: no ready pod; BackendNotFound: no Service)
kubectl get httproute -A -o json | jq -r '.items[]
  | select(any(.status.parents[]?.conditions[]?;
      (.type == "BackendsAvailable" or .type == "ResolvedRefs") and .status == "False"))
  | "\(.metadata.namespace)/\(.metadata.name)"'
kubectl -n <ns> get deploy,rollout,pods        # none Ready: why? (crash loop, scaled to 0, a failed release)
kubectl -n <ns> get endpointslices             # the Service's ready addresses
```

On the dashboard the route is under "Routes with no backend" and has dropped off "Healthy
backends"; "Answered by the proxy itself" shows whether anyone asked for it, and since
when. Then it is the app's incident (its runbook): the pods have to come back. For an
Argo CD or monitoring route (`tools`), it is the platform's own pod. The alert resolves
once the route's status says it has a backend again. No alert and no series at all:
kube-state-metrics isn't exporting them (`kubectl -n monitoring logs deploy/kube-state-metrics`).

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
ExternalSecret in `deploy/secrets/alertmanager-slack.yaml` copies it into `monitoring`, where
Alertmanager mounts it. Keys, one per channel:

| Key | Channel |
| --- | --- |
| `aipostit-prod-webhook-url` | #aipostit-alerts-prod |
| `aipostit-staging-webhook-url` | #aipostit-alerts-staging |
| `platform-webhook-url` | #platform-alerts |

**A cluster without the webhooks runs without Slack.** `scripts/cluster.sh up` looks for
the Secret in the store and sets the platform's `components.slack` from it: where it is
missing (a fresh cluster, an app's CI), the ExternalSecret and Alertmanager's mount of its
Secret are left out, `up` says so, and alerts stay in Alertmanager. So on a new cluster:
`up`, load the webhooks (below), `up` again. Deleting the Secret from the store and running
`up` switches Slack off the same way.

**Load or replace the webhooks.** Make each webhook in Slack (api.slack.com/apps: the app,
**Incoming Webhooks**, **Add New Webhook**, pick the channel), then:

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
tickets: yellow, never `@channel`. So does the front door's `FrontDoorRouteWithoutBackend`
(The front door, above), whichever app's route it is about: its message names the route. So do the stack's own `warning` and `critical` alerts (a
crash loop, a volume filling up, a target down, a failed rule evaluation) on the platform's
namespaces, or on none (`KubeAPIDown`): titled "in <namespace>", red when critical, with the
alert's description and the upstream runbook. The namespaces are listed in the route; a new
platform namespace is added there (`tests/test_alerting.py` fails until it is). The stack's
`info` alerts and `Watchdog` stay on `null`.

**The links under a firing message** (`links-slack.tmpl`, both channels), after the
runbook: **Query**, the alert's expression in Prometheus at the moment it fired; **Logs**,
Explore on VictoriaLogs with its namespace's lines (`exported_namespace` or
`route_namespace` where it has one), from an hour before it fired to half an hour after
(Argo CD's alerts, which name only an Application: Argo CD's lines naming it);
**Dashboard**, where the rule has a `dashboard_url` annotation, a path under Grafana's
address (the front door's: `/d/front-door`; the node alerts: Node Exporter / Nodes);
**Silence**, a new silence filled in with the group's labels. A resolved message keeps only
the runbook. They open on the laptop only (`*.localhost:8090`).

**Test the route end to end** (posts to the real channels: say so there first). An alert sent
by hand, which resolves itself after its end time:

```sh
amtool --alertmanager.url=http://alertmanager.localhost:8090 alert add AipostitTest \
  namespace=aipostit-staging severity=ticket \
  --annotation=summary="Test alert, please ignore" --end="$(date -u -v+5M +%Y-%m-%dT%H:%M:%SZ)" \
  --generator-url='http://prometheus.localhost:8090/graph?g0.expr=up%7Bnamespace%3D%22aipostit-staging%22%7D&g0.tab=1'
```

It reaches #aipostit-alerts-staging after `group_wait` (30 s), and a resolved message about
5 minutes after the end time. Its links open `up{namespace="aipostit-staging"}` at the
moment it fired, aipostit-staging's logs around then, and a silence for it. `namespace=aipostit-prod severity=page` tests the prod channel
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

## External Secrets alerts

Warnings, to the platform's channel whichever app the ExternalSecret belongs to: a failed
sync breaks nothing that runs, the Secret keeps its last value. Label `exported_namespace`
is the ExternalSecret's namespace (`namespace` is the controller's). Rules:
`deploy/argocd/platform/files/external-secrets-rules.yaml`.

| Alert | Means | First look |
| --- | --- | --- |
| `ExternalSecretNotSyncing` | An ExternalSecret has been not Ready for 15 minutes: its Secret is stale, or was never made | `kubectl get externalsecret -A`; `kubectl -n <ns> describe externalsecret <name>` (the events name the store or the key it can't read) |
| `ExternalSecretsDown` | No controller scraped for 10 minutes: nothing is refreshed or created, and the alert above is blind | `kubectl -n external-secrets get pods`; `kubectl -n external-secrets logs deploy/external-secrets --tail=50` |

| The ExternalSecret says | Cause | Fix |
| --- | --- | --- |
| The store Secret or one of its keys doesn't exist | Never loaded, or loaded without that key | Platform secrets, above: `scripts/load-app-env.sh` for an app's, Slack alerts for `alertmanager-slack` |
| The store isn't ready | `kubectl get clustersecretstore` shows which; `kubectl describe clustersecretstore <name>` why (its ServiceAccount or Role in `secret-store`) | `deploy/secrets/`, through a PR |
| Nothing wrong, after a fix | The next try is up to an hour away | `kubectl -n <ns> annotate externalsecret <name> force-sync=$(date +%s) --overwrite` |

`ExternalSecretsDown` with the pod running is the scrape: Prometheus's Targets page
(http://prometheus.localhost:8090/targets), `external-secrets-metrics`.

## cert-manager alerts

Warnings, to the platform's channel. The only certificates are the two the Barman Cloud
plugin and the CloudNativePG operator talk over (`barman-cloud-client` and
`barman-cloud-server` in `cnpg-system`): 90 days, renewed 15 days before the end, so a
healthy one never has less than 15 days left. A renewal that fails breaks nothing until the
certificate expires; then backups stop. Label `exported_namespace` is the Certificate's
namespace (`namespace` is the controller's). Rules:
`deploy/argocd/platform/files/cert-manager-rules.yaml`.

| Alert | Means | First look |
| --- | --- | --- |
| `CertificateExpiresSoon` | A certificate has less than 7 days left: its renewal has been failing for 8 | `kubectl get certificate -A`; `kubectl -n <ns> describe certificate <name>` (the events name the CertificateRequest); `kubectl -n <ns> get certificaterequest` |
| `CertificateNotReady` | A Certificate has been not Ready for 15 minutes: never issued, or expired | The same; `kubectl -n <ns> get issuer` (Ready?) |
| `CertManagerDown` | No controller scraped for 10 minutes: nothing is issued or renewed, and the alerts above are blind | `kubectl -n cert-manager get pods`; `kubectl -n cert-manager logs deploy/cert-manager --tail=50` |

| The Certificate says | Cause | Fix |
| --- | --- | --- |
| Its Issuer isn't found or isn't Ready | The plugin's Issuer was refused while cert-manager's webhook wasn't serving, or was deleted | The `barman-cloud` Application makes it: `kubectl -n argocd annotate application barman-cloud argocd.argoproj.io/refresh=normal --overwrite` if its retries gave up |
| A CertificateRequest failed | `kubectl -n <ns> describe certificaterequest <name>` says why; cert-manager waits before it tries again (an hour, doubling) | Fix the cause, then wait for the next try, or `cmctl renew <name> -n <ns>` (cert-manager's CLI: `brew install cmctl`) to issue now |
| Ready again after it had expired, and backups still fail | The plugin or the operator still holds the old certificate | `kubectl -n cnpg-system rollout restart deploy/plugin-barman-cloud deploy/cloudnative-pg` |

`CertManagerDown` with the pod running is the scrape: Prometheus's Targets page
(http://prometheus.localhost:8090/targets), `cert-manager`.

The controller's restart count climbs with the laptop's sleeps (about 20 a day), by design:
its `/livez` fails once the clock has jumped more than 5 minutes in all since it started,
short sleeps added up (`clockHealth` in
`kubectl -n cert-manager logs deploy/cert-manager --previous`), so the kubelet restarts it
and its renewal timers start again from the real time. So it restarts while the laptop is
in use too, when idle sleeps of a minute or two have added up. A restart takes seconds, far
less than `CertManagerDown` waits. The liveness probe stays on: without it a renewal would come late
by as long as the laptop had slept. A restart with no `clockHealth` line in the previous
log is another kind: last state `Unknown`, exit code 255, is the node having restarted
(Docker or the cluster stopped); `OOMKilled` or `Error` is a fault.

## Prometheus data

Prometheus keeps 10 days (`retention: 10d`, capped at 12 GB by `retentionSize`) on its own
15Gi volume, `prometheus-kps-prometheus-db-prometheus-kps-prometheus-0` in `monitoring`
(kind's local-path storage, under the node's disk), so restarts and upgrades keep it.
Settings: `deploy/kind/monitoring-values.yaml`.

- How far back it goes: `min(prometheus_tsdb_lowest_timestamp)` in Prometheus, in ms.
- How much it holds: `prometheus_tsdb_storage_blocks_bytes`. About 0.5 GB a day at today's
  load; past 12 GB the oldest blocks go first, whatever their age.
- Deleting the PVC (or the kind cluster) loses the history; Prometheus starts empty.
- **An error budget looks back only this far.** The SLO rules' 30-day window
  (`slo:sli_error:ratio_rate30d`, the SLO dashboard's "Error budget remaining") is a rate
  over the data there is: today the last 10 days at most, less after a cap or a new volume.
  An incident older than that no longer counts against the budget. Before trusting a
  budget, check how far back the data goes:
  `(time() - min(prometheus_tsdb_lowest_timestamp) / 1000) / 86400`, in days.

## Prometheus misses rule evaluations

`PrometheusMissingRuleEvaluations` (warning, #platform-alerts) is the platform's own rule
(`deploy/argocd/platform/files/prometheus-rules.yaml`), not the stack's, which is turned off
in `deploy/kind/monitoring-values.yaml`. The stack's fires when the laptop sleeps (PT-15):
the cluster's VM is frozen, the clock jumps on wake, and every rule group counts the sleep
as missed evaluations. Ours fires only when a group misses evaluations for 15 minutes while
Prometheus is scraped without a hole, and fewer evaluations ran than its interval asks for
(a group loaded since the last wake counts a miss now and then with none skipped). So it
means a real one: alerts, the SLO ones included, are evaluated late.

- Which group, and how long it takes against its interval:
  `prometheus_rule_group_last_duration_seconds / prometheus_rule_group_interval_seconds`
  (near or above 1 is the group to look at). Shorten its queries or give it a longer interval.
- Every group at once, none of them slow: Prometheus is short of CPU. See what the node is
  busy with (`kubectl top pods -A --sort-by=cpu`); its request is under `prometheus` in the
  values file.
- Was it a sleep after all? `count_over_time(up{job="kps-prometheus"}[5m])` under 20 around
  that time is a hole in the scrapes; `pmset -g log | grep -E "Sleep|Wake"` on the Mac shows
  the sleeps.

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
Connections, Data sources, VictoriaLogs, **Test**. If Test fails, the store is down or its
NetworkPolicy no longer admits Grafana's pods.

Its plugin, `victoriametrics-logs-datasource`, is kept on a volume of its own
(`grafana-plugins` in `monitoring`, mounted at Grafana's plugin directory), so Grafana
starts with it whether the laptop is online or not. Grafana downloads it from grafana.com
only when the volume doesn't hold the pinned version: on a new cluster, after the PVC was
deleted, and after a bump of `grafana.plugins`. Offline, that one start waits for the
network (`kubectl -n monitoring logs deploy/grafana -c grafana | grep -i plugin`).

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

**The collector.** `log-collector` is one of the two namespaces at Pod Security `privileged`
(the other is `node-exporter`, Node metrics below),
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

### Log alerts

Nothing fails when logs stop arriving, so rules watch the pipeline's own metrics
(`deploy/argocd/platform/files/logs-rules.yaml`, the PrometheusRule `log-pipeline` in
`argocd`, beside Argo CD's). Warnings, except the buffer: what it drops is gone. They carry
the namespace of the part they are about, `log-collector` or `monitoring`, which is what
routes them to #platform-alerts (Slack alerts, above).

| Alert | Means | First look |
| --- | --- | --- |
| `LogsNotArriving` | VictoriaLogs took in no line for 15 minutes, or isn't scraped. Nothing is lost yet: the collector buffers | `kubectl -n monitoring get pods victoria-logs-0`; `up{job="victoria-logs"}` in Prometheus; then the collector, as below |
| `LogCollectorNotSending` | A collector's sends have failed for 10 minutes (a restart of the store is a few minutes of this) | `kubectl -n log-collector logs <pod> --tail=50`: a refused connection is the store down or its NetworkPolicy |
| `LogCollectorBufferNearlyFull` (critical) | Over 80% of the 1 GiB buffer (`maxDiskUsagePerURL`) is unsent lines: at the cap the oldest are dropped | Why it isn't sending (the two above); how full: `vlagent_remotewrite_pending_data_bytes`; dropped already: `vlagent_remotewrite_packets_dropped_total` |
| `LogCollectorPodMissing` | A node has had no ready collector pod for 15 minutes: its pods' logs aren't read, and a pod deleted meanwhile takes its lines with it | `kubectl -n log-collector get pods -o wide`; `kubectl -n log-collector describe ds log-collector` ("permission denied" in the pod's logs is the security context) |
| `LogStoreNearItsSizeCap` | The store holds over 80% of `retentionDiskSpaceUsage` (4 GiB) for 30 minutes. The volume won't fill: at the cap the oldest day goes, so the logs go back less than 30 days | Who writes the most: `_time:1d \| stats by (kubernetes.pod_namespace) sum_len(_msg) bytes \| sort by (bytes desc)`. Then quieten that app, or raise the cap and the volume together (`deploy/kind/victoria-logs-values.yaml`) |

## Node metrics

node-exporter (the DaemonSet `node-exporter` in the namespace of that name, the platform's
`node-exporter` Application) reports the kind node's CPU, memory and disk to Prometheus as
`node_*`, job `node-exporter`. The node's disk is the one everything is kept on:
Prometheus's volume, the logs' volume and the backup bucket are directories under the
node's `/var` (`node_filesystem_avail_bytes{mountpoint="/var"}`), which is the Docker VM's
disk.

`node-exporter` is the other namespace at Pod Security `privileged`: it reads the node's
`/proc`, `/sys` and `/` through hostPath volumes, all read-only, and nothing else belongs
in it. The pod is not root, has no capabilities, no token for the API and no host network,
so its NetworkPolicy holds: only Prometheus may connect, and it may connect to nothing.
Because it is on the pod network, `node_network_*` describes the pod's own interface, not
the node's. Settings: `deploy/kind/node-exporter-values.yaml`.

- Is it up: the `node-exporter` Application is Synced and Healthy, and in Prometheus
  `up{job="node-exporter"}` is 1.
- The target is down with `context deadline exceeded`: its NetworkPolicy no longer admits
  Prometheus's pods (Network policies, above). `kubectl -n node-exporter get pods` shows a
  pod refused at admission as a DaemonSet with none (`describe ds/node-exporter`).

The stack's node rules and dashboards read these (`defaultRules.rules.node*` and
`nodeExporter.forceDeployDashboards`, `deploy/kind/monitoring-values.yaml`): in Grafana,
**Node Exporter / Nodes** and the two **USE Method** dashboards. Their warning and critical
alerts (`Node...`) go to #platform-alerts.

### The node's disk is filling up

`NodeFilesystemSpaceFillingUp` (under 15% free and, at the last 6 hours' pace, none left
within a day: warning; under 10% and 4 hours: critical) and `NodeFilesystemAlmostOutOfSpace`
(under 5% free: warning; under 3%: critical). It names `mountpoint="/var"`, once: the disk
everything shares (the files Docker binds into the node from it, `/etc/hostname`, `/etc/hosts`
and `/etc/resolv.conf`, aren't reported). Act on the warning: the kubelet starts evicting
pods, prod's too, when the disk it keeps images and pods on is nearly full, and Postgres
stops when it can't write.

```sh
# Free space on the node's disk, as a share of it
#   node_filesystem_avail_bytes{mountpoint="/var"} / node_filesystem_size_bytes{mountpoint="/var"}
# What the volumes hold, biggest first (Prometheus, the logs, the bucket, the databases)
#   sort_desc(kubelet_volume_stats_used_bytes)
# The rest, on the node: images and containers, the pods' log files, the volumes' directories
docker exec shortly-control-plane du -xsh /var/lib/containerd /var/log /var/local-path-provisioner
# The same disk holds everything else Docker has on the laptop
docker system df
```

- **A volume grew.** Prometheus and VictoriaLogs cap themselves (`retentionSize`,
  `retentionDiskSpaceUsage`: Prometheus data and Logs, above): lower the cap in a PR. The
  bucket and the databases don't: The backup bucket, below, and the app's runbook.
- **Images.** Every release leaves its image on the node. Propose
  `docker exec shortly-control-plane crictl rmi --prune` (removes the images no container
  uses; a rollback to one pulls it again).
- **Something else of Docker's** (`docker system df`: other images, build cache, volumes):
  that is Emrah's to prune, or the VM's disk to grow (Docker Desktop, Settings, Resources).
- A `tmpfs` mount point (`/run`, `/tmp`) is the node's memory, not its disk: look at what
  writes there instead.

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

## CloudNativePG alerts

Warnings, to the platform's channel whichever app the database belongs to. Neither breaks
what runs: the databases serve queries without the operator, and a missing backup is missed
on the day of a restore. In `DatabaseBackupTooOld`, label `exported_namespace` is the
database's namespace and `cluster` its Cluster (`<release>-db`). Rules:
`deploy/argocd/platform/files/cloudnative-pg-rules.yaml`.

| Alert | Means | First look |
| --- | --- | --- |
| `CloudNativePGOperatorDown` | The operator hasn't been scraped for 10 minutes: no failover, no scheduled backup, no change to a Cluster | `kubectl -n cnpg-system get pods`; `kubectl -n cnpg-system logs deploy/cloudnative-pg --tail=50` (add `--previous` after a crash) |
| `DatabaseBackupTooOld` | A database's last successful base backup is more than 24 hours old, or it never had one | `kubectl -n <ns> get backups.postgresql.cnpg.io --sort-by=.metadata.creationTimestamp \| tail -5` (the last ones' phase); `kubectl -n <ns> describe backups.postgresql.cnpg.io <name>` (why it failed); `kubectl -n <ns> get scheduledbackup,objectstore` |

| What you find | Cause | Fix |
| --- | --- | --- |
| The operator's pod is there, not Ready or in a crash loop | Its logs say; OOMKilled shows in `kubectl -n cnpg-system describe pod` (its limit is in `deploy/kind/cloudnative-pg-values.yaml`) | Propose `kubectl -n cnpg-system rollout restart deploy/cloudnative-pg` |
| No operator pod at all | The `cloudnative-pg` Application | `kubectl -n argocd get application cloudnative-pg`; An Application that won't sync, above |
| No Backup objects since a certain time | The operator was down, or the laptop slept | The ScheduledBackup catches up at its next run; the alert clears with the next backup |
| Backups there, phase `failed` | The bucket, its keys, or the plugin: the Backup's description names it | The backup bucket, above; `kubectl -n cnpg-system logs deploy/plugin-barman-cloud --tail=50`; an expired certificate between the two is cert-manager alerts, above |
| Backups `completed`, and the alert stays | The ObjectStore's status isn't updated, which is what the metric reads | `kubectl -n <ns> get objectstore <release>-db -o jsonpath='{.status}'`; the plugin's sidecar in the instance: `kubectl -n <ns> logs <release>-db-1 -c plugin-barman-cloud --tail=50` |

To take a backup now rather than wait for the schedule, propose
`kubectl cnpg backup <release>-db -n <ns> --method plugin --plugin-name barman-cloud.cloudnative-pg.io`
(the `cnpg` kubectl plugin). `CloudNativePGOperatorDown` with the pod running and Ready is
the scrape: Prometheus's Targets page (http://prometheus.localhost:8090/targets),
`cloudnative-pg`. A database is watched only while Prometheus scrapes its instances
(`<ns>/<release>-db` on the same page): that needs `monitoring.enabled` in the app's values,
and an app whose database isn't scraped gets no backup alert.
