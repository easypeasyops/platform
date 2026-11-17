# service chart: changes

Released as `oci://ghcr.io/easypeasyops/charts/service`, tag `service/vX.Y.Z`; apps pin a
release by digest (`apps/<app>/envs/<env>.yaml`). Semantic versioning from 1.0.0: a change
an app must react to (a value removed or renamed, a default that changes what renders for
an app that doesn't set it) is a major version; a new optional value is a minor one; a fix
that changes no render an app relies on is a patch. Each release's digest is in its GitHub
Release.

## 1.7.1

The SLO dashboard says how many requests each error budget is made of (PT-70). On low
traffic a budget is a ratio of a handful of requests: aipostit's prod had 25 in the window
and 4 slow ones, and its latency budget read -1501% (PT-62). A new panel between "Error
budget remaining" and "Budget looks back", "Requests in the budget", shows each SLO's valid
requests in the window and how many were bad. It reads `slo:sli_events:increase30d`, a new
recording rule from `scripts/gen_slo_rules.py` (bad = `slo:sli_error:ratio_rate30d` times
it). Nothing for an app to set: only the slo.json render differs from 1.7.0. An app's panel
shows "No data" until it regenerates its SLO rules with a platform at or after this change
(its next `platform.lock` move); the rest of the dashboard is unchanged.

## 1.7.0

A values schema (`values.schema.json`, PT-73): a value the chart doesn't know now fails the
render, naming it, where before Helm ignored it and rendered the chart without it. A typo
such as `autoscalling:` or `gateway.hostname:` used to pass every check and quietly turn a
feature off; now `helm template`, `helm lint`, CI and Argo CD's sync all refuse it:

    at '/gateway': additional properties 'hostname' not allowed

What it refuses: an unknown key in any object the chart defines (`app`, `image`,
`autoscaling`, `database` and `database.cluster`, `redis`, `secretEnv`, `migrations`,
`gateway`, `canary` and its `analysis`, `monitoring`, `networkPolicy`, each `workers` and
`cronJobs` entry, each `env` entry and `canary.analysis.latencyChecks` entry), and a value
of the wrong type there (a string where a number or a list goes, an `image.digest` that
isn't `sha256:` and 64 hex digits). What it leaves open: `config` (an app's own settings go
there too) and the Kubernetes objects passed through as they are (`resources`, `affinity`,
`tolerations`, network policy peers, `autoscaling.behavior`, `database.cluster.bootstrap`):
kubeconform checks those once rendered.

A minor version: every app's values on the cluster (aipostit staging and prod, deploybot
ops, servicemonitor ops) and app-template's render with it unchanged, and every golden is
byte for byte the same. An app whose values carry a key the chart never read must remove it.

## 1.6.0

Two new optional values, for an app that shows what Prometheus watches (PT-52, PT-53; the
ServiceMonitor page first). Additive: nothing renders differently from 1.5.0 for an app that
sets neither (every existing golden is byte for byte the same).

- **`gateway.sectionName`** (default empty): the one listener of the Gateway the HTTPRoute
  attaches to, as its parent's `sectionName`. Empty, as before: no `sectionName`, so every
  listener that admits the namespace, which for an app's namespaces is `http`. An internal
  tool sets `tools`. Needs a platform whose `tools` listener names the app's namespace
  (`deploy/kind/gateway.yaml`): from any other namespace the route is not `Accepted` and
  nothing serves the hostname.

- **`rbac.clusterRead`** (default `false`): a ServiceAccount `<release>` the app's pods run
  as, with its token mounted, and a ClusterRole and ClusterRoleBinding
  `<namespace>-cluster-read` giving it `get`, `list` and `watch` on `servicemonitors`,
  `podmonitors`, `probes` and `prometheusrules` (monitoring.coreos.com) and on `namespaces`.
  Nothing else: no Secrets, no writes. Workers, scheduled jobs and the migration Job keep
  running without a token.
- **Only for an app the platform allows.** The two cluster-scoped objects are applied only
  if the app's AppProject (the platform's `apps/<app>/project.yaml`) lists them by name; for
  any other app Argo CD refuses the sync, so setting the value changes nothing in the
  cluster. The platform's `scripts/check-apps.py` names the apps that may ask.
- **With it, the app's NetworkPolicy lets its pods reach the API server**: one more egress
  rule, to `networkPolicy.apiServerTo` on TCP `networkPolicy.apiServerPort`. The defaults are
  the private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and `6443`: the API
  server is on a node's network, which no pod selector matches, and the chart can't know a
  node's address. Not the internet and no other port; it does admit any private address on
  6443. Narrowing it for a cluster is the platform's to do; an app sets neither value. Only the app's own policy gets the rule (the pods with the
  token, and the migration Job, which shares their labels): workers' and scheduled jobs'
  policies don't.

## 1.5.0

The HTTPRoute names only `gateway.name` as its parent: 1.4.0's second parent, `shortly`, goes
(the platform's front-door rename, 4 of 5). The platform's Gateway is `front-door` since
platform#163, so an app that leaves the defaults is served as before. Nothing for an app to
set; it needs a platform at or after platform#163, where no Gateway is named `shortly`.

## 1.4.0

The platform's Gateway gets its own name, `front-door`, in place of the first app's,
`shortly` (the platform's rename, 2 of 5). Nothing for an app to set: an app that leaves
`gateway.name` and `canary.analysis.syntheticTraffic.url` at their defaults gets both.

- **`gateway.name`** defaults to `front-door`. While it is the default, the HTTPRoute also
  names `shortly` as a parent, so the route is served before and after the platform renames
  its Gateway. The release after the rename drops that parent.
- **`canary.analysis.syntheticTraffic.url`** defaults to
  `http://front-door.envoy-gateway-system.svc`, the platform's alias for the proxies'
  Service since platform#156. Needs a platform at or after that commit: an app's
  `platform.lock` (its kind e2e) older than it gets canary analysis with no traffic.

## 1.3.1

The SLO dashboard says how far back an error budget really looks (PT-27). The rules' window
is 30 days, but `slo:sli_error:ratio_rate30d` is a rate over the days Prometheus has, and
the platform's keeps 10: the budget shown as a month's was those days', and an older
incident showed as budget restored. A new panel beside "Error budget remaining", "Budget
looks back", shows the days of data there are (`prometheus_tsdb_lowest_timestamp`, 30 at
most; green only at 30), and the row's title and the budget's description say so. Nothing
for an app to set or change: only the slo.json render differs from 1.3.0. The rules and
`scripts/gen_slo_rules.py` are unchanged.

## 1.3.0

The SLO dashboard marks its environment's deploys (PT-28): a "Deploys" annotation at the
minute the environment's Argo CD Application (`<app>-<env>`, the dashboard's `namespace`)
synced a new revision, naming the Application and the revision (a commit's short SHA, or
the start of the chart's digest). The same marks as the platform's Front door dashboard
(platform docs/RUNBOOK.md, The front door), read from kube-state-metrics'
`argocd_application_synced_revision(s)_info`, which the platform exports since PT-28's
first PR. Nothing for an app to set or change: only the slo.json render differs from 1.2.1.

## 1.2.1

Three new optional values, for an app with a queue (platform plan 5.4–5.6; postitglobal
first). Additive: nothing renders differently from 1.1.0 for an app that sets none of them
(the goldens of shortly, minimal and notes are byte for byte the same).

- **`workers`** (5.4): long-running processes beside the app, such as a Celery worker. A
  Deployment `<release>-<key>` per entry, from the release image with the entry's `command`
  and the app's env plus its own; `replicas`, `resources`, `terminationGracePeriodSeconds`
  default to 1 and the app's. No Service, no probes; its own NetworkPolicy (no ingress, the
  app's egress). Its pods carry the job labels (`<app.name>-<key>`), so the HPA and the PDB
  don't count them.
- **`redis`** (5.5): with `redis.enabled`, a Redis 8 for the app: a StatefulSet of one
  `<release>-redis` (image pinned by digest, append-only file on a `storage` volume,
  `noeviction` under `maxMemory`), a headless Service, and a NetworkPolicy admitting only
  the chart's own pods (`part-of`) and giving Redis no egress. Every pod of the chart gets
  egress to it; env entries template its URL with `service.redisUrl`. No password: the
  network policy is the boundary.
- **`secretEnv`** (5.6): with `secretEnv.store`, an ExternalSecret `<release>-env` (Argo CD
  wave -3) copying every key of `remoteKey` (default: the app's name) from that
  ClusterSecretStore; env entries read single keys with `secret:`.

## 1.2.0 (never released)

The tag `service/v1.2.0` was pushed on a commit whose `Chart.yaml` still said 1.1.0, so the
release workflow refused it and nothing was published. Release tags can't be moved or
deleted (the `chart-release-tags` ruleset), so 1.2.0's changes were released as 1.2.1.

## 1.1.0

- With `database.cluster` and `monitoring.enabled`: a PodMonitor `<release>-db` scrapes each
  instance's exporter (port 9187), and the database's NetworkPolicy admits
  `networkPolicy.metricsFrom` to it. Prometheus then has CloudNativePG's metrics
  (`cnpg_pg_stat_archiver_*` for WAL archiving) and the Barman Cloud plugin's
  (`barman_cloud_cloudnative_pg_io_last_available_backup_timestamp`,
  `_last_failed_backup_timestamp`, `_first_recoverability_point`), for an app's backup
  alerts. Additive: nothing else renders differently from 1.0.0.

## 1.0.0

The values are a contract from here: shortly runs on this chart in staging and prod.

- **`database.cluster`** (new, off by default; platform plan 4.2): the app's own Postgres on
  the platform's CloudNativePG operator. A `Cluster` `<release>-db` (never pruned or
  deleted by Argo CD), WAL archived continuously and a base backup hourly to the platform's
  bucket through the Barman Cloud plugin (`ObjectStore`, `ScheduledBackup`), the bucket's
  keys from the `backup-bucket` ClusterSecretStore, and network rules both ways. The app
  uses it by pointing `database.existingSecret` / `secretKey` at `<release>-db-app` /
  `uri`. `bootstrap` and `externalClusters` pass through, for an import or a recovery.
- With `database.cluster`, the migration Job runs after the database instead of before
  everything: under Argo CD a Sync hook in wave -1, after the database (waves -3 and -2,
  held until the Cluster is healthy) and before the app (wave 0); under plain Helm a
  `post-install,pre-upgrade` hook.
- Without `database.cluster`, nothing renders differently from 0.1.0 (the goldens of
  shortly and of a minimal app are byte for byte the same).

## 0.1.0

The chart as copied from links' `deploy/helm/shortly` (its 0.10.0), made generic: the app's
name, image, environment, settings and alert rules come in as values (platform plan 3.2).
