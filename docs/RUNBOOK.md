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
