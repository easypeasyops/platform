# dbcanary

A Postgres the platform owns (PT-106), so the service chart's `database.cluster` and its
backups are proven on the cluster whether or not an app uses them. It is deployed as an
app, `dbcanary-ops`, with a released service chart (`apps/dbcanary/`), so it goes through
the same namespace label, policies, bucket store and network rules an app's database does.

- **The database:** `dbcanary-db`, one instance, archiving WAL continuously and taking a
  base backup every hour to `s3://cnpg-backups/dbcanary-ops/dbcanary-db` (7 days kept).
- **The writer:** the CronJob `dbcanary-write` inserts a row into `beats` every 15 minutes,
  so there is always fresh WAL to archive and something to look for after a restore.
- **No image of its own:** the writer runs `psql` from the database's own Postgres image,
  which the app policy admits. The chart's Deployment is kept at no pods.

It is what the platform's backup checks, alerts and restore drills target
(`scripts/restore-db.sh dbcanary ops`). Don't remove it: docs/RUNBOOK.md, The database
canary.

Values: `deploy/service/values.yaml`, `deploy/envs/ops.yaml`. CI renders them with the
released chart (ci.yml, tools-deploy).
