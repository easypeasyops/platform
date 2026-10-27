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

`deploy/platform/metrics-server/components.yaml` is vendored byte for byte; never edit it.
To bump it, replace it with the upstream release and update `SHA256SUMS` in the same PR.

## Pull requests opened by Claude

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
