# service chart: changes

Released as `oci://ghcr.io/easypeasyops/charts/service`, tag `service/vX.Y.Z`; apps pin a
release by digest (`apps/<app>/envs/<env>.yaml`). Semantic versioning from 1.0.0: a change
an app must react to (a value removed or renamed, a default that changes what renders for
an app that doesn't set it) is a major version; a new optional value is a minor one; a fix
that changes no render an app relies on is a patch. Each release's digest is in its GitHub
Release.

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
