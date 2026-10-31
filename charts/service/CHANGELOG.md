# service chart: changes

Released as `oci://ghcr.io/easypeasyops/charts/service`, tag `service/vX.Y.Z`; apps pin a
release by digest (`apps/<app>/envs/<env>.yaml`). Semantic versioning from 1.0.0: a change
an app must react to (a value removed or renamed, a default that changes what renders for
an app that doesn't set it) is a major version; a new optional value is a minor one; a fix
that changes no render an app relies on is a patch. Each release's digest is in its GitHub
Release.

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
