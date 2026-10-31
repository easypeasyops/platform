# platform

[![CI](https://github.com/easypeasyops/platform/actions/workflows/ci.yml/badge.svg)](https://github.com/easypeasyops/platform/actions/workflows/ci.yml)

How every easypeasyops app is built, released, admitted and run. An app team changes only
its own code, values and the release each environment runs; this repo owns everything else.

## Status: moving in (Phase 3)

The files here came from [easypeasyops/links](https://github.com/easypeasyops/links)
(formerly `statsguy/shortly`) with their history, as plan step 3.1. Since 3.4 this repo is
the only copy: Argo CD syncs the platform from here, the laptop cluster is started from a
checkout here, and links runs it at the commit its `platform.lock` names.

| Step | Change | State |
| --- | --- | --- |
| 3.1 | Platform files copied here with history | done |
| 3.2 | A generic `service` chart, released to GHCR as a signed OCI chart | done: 0.1.0 |
| 3.3 | An ApplicationSet over `apps/*`; `apps/shortly/` points at links | done |
| 3.4 | links drops what moved and pins this repo's workflows and chart | in progress |
| 3.5 | Releases per app (`shortly/vX.Y.Z`, `ghcr.io/easypeasyops/links/shortly`) | |

## What's here

| Path | What |
| --- | --- |
| `apps/` | Every app's environments: `apps/applicationset.yaml` generates one Argo CD Application per `apps/<app>/envs/<env>.yaml` (the service chart pinned by digest, values from the app's repo); `apps/<app>/project.yaml` is the app's AppProject. Applied by the `apps` root (`deploy/argocd/apps-{root,project}.yaml`) |
| `charts/service/` | The chart every app deploys with (3.2, not used by anything yet): Deployment or canary Rollout, HPA, PDB, probes, network policies, monitoring. `tests/<app>/` holds an app's values and the golden render CI checks |
| `.github/workflows/reusable-*.yml` | Reusable workflows (`workflow_call`) an app's CI calls: Python check, image build/scan/smoke test |
| `deploy/argocd/platform/` | The platform chart (app-of-apps): Envoy Gateway, Argo Rollouts, metrics-server, kube-prometheus-stack, Kyverno and its policies, the backup bucket |
| `deploy/argocd/platform-{root,project}.yaml` | The root Application and the AppProject it runs in |
| `deploy/policies/` | Kyverno admission policies: allowed registries, release provenance |
| `scripts/verify-image.sh` | The deploy gate: an image's release provenance, by the same rule as the provenance policy. An app's deploy script runs it for every registry image |
| `tests/` | pytest: what a schema can't check (pinning, namespaces, ports, policy verdicts through the Kyverno CLI, the deploy gate) |
| `deploy/kind/` | The kind cluster config and the values and manifests the platform installs on it |
| `deploy/platform/metrics-server/` | The vendored metrics-server release, checksum-pinned |
| `scripts/gen_slo_rules.py` | Generates an app's SLO recording rules and burn-rate alerts from its SLO spec, as a values file for the service chart (`--values`) or a plain rule file for promtool. Apps run it with `uv run --script` from their repo root |
| `scripts/cluster.sh`, `scripts/lib/` | Bootstraps the local cluster: Argo CD, then the platform root |

`scripts/cluster.sh` runs from this repo: it bootstraps Argo CD, applies the platform root
(which syncs the platform from this repo) and the apps root.

## CI

`ci.yml` validates every manifest against its schema (the platform chart rendered for
amd64, arm64 and with optional components off), checks the vendored metrics-server
checksum, renders the service chart for every app fixture and diffs it against the
golden files (`scripts/chart-golden.sh`), tests the SLO generator
(`scripts/test-slo-generator.sh`), runs `tests/` with pytest (`uv run pytest`, through
`reusable-python-check.yml`, the same workflow apps call) and runs shellcheck and actionlint.
`ci` is the one required check. The kind end-to-end test runs in links, against the commit links pins.

## Onboarding an app

One folder, reviewed here: `apps/<app>/project.yaml` (the app's repo and its `<app>-<env>`
namespaces) and `apps/<app>/envs/<env>.yaml` per environment:

```yaml
app: <app>
env: staging
chart: { version: 0.1.0, digest: sha256:… } # a released, verified service chart
values:
  repoURL: https://github.com/easypeasyops/<repo>.git
  revision: main
  files: [deploy/service/values.yaml, deploy/envs/staging.yaml] # layered in order
```

CI (`apps` job, `scripts/check-apps.py`) checks each env file the way Argo CD will use it: the
project allows it, the digest is a release of that version whose provenance verifies, and the
chart renders with the app's values fetched from its repo. Move a chart pin one environment at
a time, staging first.

## Releasing the service chart

1. A PR bumps `version` in `charts/service/Chart.yaml` (semver: a change an app must react
   to is a major version, from 1.0.0 on).
2. After it merges, tag the merge commit: `git tag service/vX.Y.Z <sha> && git push origin service/vX.Y.Z`.
3. `release-chart.yml` checks the tag (it must match `Chart.yaml`, be on `main`, pass the
   goldens, and be a version not yet published), pushes the chart to
   `oci://ghcr.io/easypeasyops/charts/service`, pulls it back by digest, attests its
   provenance (Sigstore, keyless), verifies the attestation and creates a GitHub Release whose
   notes hold the digest. Apps pin that digest.

A published version is never replaced: a fix is a new version. CI rehearses the release on
every PR against a throwaway registry (`chart release rehearsal`).

## Docs

The design and runbook still live in links ([DESIGN](https://github.com/easypeasyops/links/blob/main/docs/DESIGN.md),
[RUNBOOK](https://github.com/easypeasyops/links/blob/main/docs/RUNBOOK.md)) and move here
with the platform's parts of them.
