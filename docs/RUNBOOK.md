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

Then the app's own runbook for its Application (shortly: "A merged change didn't deploy";
aipostit: "API health check fails"), or, for a platform Application, shortly's runbook's
table of platform sync failures (links `apps/shortly/docs/RUNBOOK.md`, Argo CD alerts).

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
