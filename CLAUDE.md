# Working on the platform

## This repo is an API

App repos call the reusable workflows and (from 3.2) the `service` chart, pinned by this
repo's commit SHA or the chart's digest. What an app provides and what it gets in return is
the app contract (platform plan): treat it like an API.

- A change an app must react to (a workflow input renamed or made required, a chart value
  removed, a stricter policy) is a breaking change: expand first (accept both), migrate the
  apps, then contract. Never in one PR.
- Never rewrite `main`'s history: apps pin its commit SHAs.
- Admission policies decide what runs in every app's namespaces. A policy change says in the
  PR which images it newly admits or refuses.

## Until links drops these files (plan step 3.4)

links' copies are the live ones: Argo CD syncs the platform from links, and `cluster.sh`
runs from a links checkout. A fix to a file that exists in both repos lands in both, and
the two PRs link each other. `deploy/policies/` and links' `scripts/verify-image.sh`
enforce the same provenance rules; links' `tests/test_admission_policies.py` checks they
agree.

## Checks

CI (`ci.yml`): kubeconform on every manifest and on the platform chart rendered three ways,
the metrics-server checksum, shellcheck (`-x`, entry points only) and actionlint. Run the
same locally before pushing when a change touches them. `ci` is the one required check:
add every new job to the gate's `needs`.

The service chart's behaviour is its golden renders (`charts/service/tests/<app>/<env>.golden.yaml`).
A refactor leaves them byte for byte the same; an intended change regenerates them
(`UPDATE=1 scripts/chart-golden.sh`) in the same PR, and the diff is the review. Golden
files are generated: they don't count toward a PR's size.

`scripts/gen_slo_rules.py` is part of the contract: its output's bytes end up in every app's
goldens and PrometheusRules. Its dependencies are pinned in the script (PEP 723); a change
to its output, or a PyYAML bump, regenerates every fixture's `slo-rules.values.yaml` in the
same PR (`scripts/test-slo-generator.sh` fails on a stale one).

`deploy/platform/metrics-server/components.yaml` is vendored byte for byte; never edit it.
To bump it, replace it with the upstream release and update `SHA256SUMS` in the same PR.

## apps/

`apps/<app>/envs/<env>.yaml` is a deploy of the platform's side of an app: a changed chart
digest re-renders that environment on merge. Move a pin only to a released digest (the
release notes have it), staging before prod, one PR per environment. Never edit an app's
values from here: they live in the app's repo. `scripts/check-apps.py` runs in CI; run it
locally (`uv run --script scripts/check-apps.py --out <dir>`) before pushing a change to
`apps/`.

## Releasing the chart

A release is a PR that bumps `charts/service/Chart.yaml`'s `version`, then a
`service/vX.Y.Z` tag on its merge commit, pushed by Emrah (the session can't push tags).
`scripts/release-chart.sh` holds the logic `release-chart.yml` runs; change them together.
A published version is immutable: never re-tag, never delete and re-push; a fix is a new
version.

## Pull requests opened by Claude

- **No tool attribution in PR descriptions** (Emrah, 2026-10-29): no "Generated with/by
  Claude Code" line, no claude.ai session link, no `---` separator for one. The description
  ends with its last point. (The session's GitHub connection may append its own footer when
  it writes a PR; that one is outside the session's control.)
- **One problem per PR, small enough to review in one sitting**: aim for about 200 changed
  lines (a sweet spot, not a hard limit). Vendored or generated files don't count, but say
  so in the description.
- A bigger change goes in as a stack: each PR leaves `main` working and CI green on its own
  (dormant pieces first, then the switch that uses them), and its description says where it
  sits in the stack.
- A refactor and a behaviour change never share a PR.
- Docs-only PRs: turn on auto-merge (squash) when opening them. Code, CI, chart and policy
  PRs: never auto-merge.
- Squash merges only: a PR's commits become one commit on `main`.
