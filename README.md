# platform

[![CI](https://github.com/easypeasyops/platform/actions/workflows/ci.yml/badge.svg)](https://github.com/easypeasyops/platform/actions/workflows/ci.yml)

How every easypeasyops app is built, released, admitted and run. An app team changes only
its own code, values and the release each environment runs; this repo owns everything else.

## Status: moving in (Phase 3)

The files here came from [easypeasyops/links](https://github.com/easypeasyops/links)
(formerly `statsguy/shortly`) with their history, as plan step 3.1. Until links drops them
(3.4), **links' copies are the live ones**: Argo CD syncs the platform from links, and the
laptop cluster is started from a links checkout. A fix to one of these files before then
lands in both repos.

| Step | Change | State |
| --- | --- | --- |
| 3.1 | Platform files copied here with history | done |
| 3.2 | A generic `service` chart, released to GHCR as a signed OCI chart | in progress |
| 3.3 | An ApplicationSet over `apps/*`; `apps/shortly/` points at links | |
| 3.4 | links drops what moved and pins this repo's workflows and chart | |
| 3.5 | Releases per app (`shortly/vX.Y.Z`, `ghcr.io/easypeasyops/links/shortly`) | |

## What's here

| Path | What |
| --- | --- |
| `charts/service/` | The chart every app deploys with (3.2, not used by anything yet): Deployment or canary Rollout, HPA, PDB, probes, network policies, monitoring. `tests/<app>/` holds an app's values and the golden render CI checks |
| `.github/workflows/reusable-*.yml` | Reusable workflows (`workflow_call`) an app's CI calls: Python check, image build/scan/smoke test |
| `deploy/argocd/platform/` | The platform chart (app-of-apps): Envoy Gateway, Argo Rollouts, metrics-server, kube-prometheus-stack, Kyverno and its policies, the backup bucket |
| `deploy/argocd/platform-{root,project}.yaml` | The root Application and the AppProject it runs in |
| `deploy/policies/` | Kyverno admission policies: allowed registries, release provenance |
| `deploy/kind/` | The kind cluster config and the values and manifests the platform installs on it |
| `deploy/platform/metrics-server/` | The vendored metrics-server release, checksum-pinned |
| `scripts/cluster.sh`, `scripts/lib/` | Bootstraps the local cluster: Argo CD, then the platform root |

`scripts/cluster.sh` can't run from this repo yet: it still applies shortly's AppProject
and Applications and reads its chart, which live in links until 3.3.

## CI

`ci.yml` validates every manifest against its schema (the platform chart rendered for
amd64, arm64 and with optional components off), checks the vendored metrics-server
checksum, renders the service chart for every app fixture and diffs it against the
golden files (`scripts/chart-golden.sh`), and runs shellcheck and actionlint. `ci` is the
one required check. The kind end-to-end test of the platform still runs in links.

## Docs

The design and runbook still live in links ([DESIGN](https://github.com/easypeasyops/links/blob/main/docs/DESIGN.md),
[RUNBOOK](https://github.com/easypeasyops/links/blob/main/docs/RUNBOOK.md)) and move here
with the platform's parts of them.
