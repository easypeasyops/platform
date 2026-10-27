#!/usr/bin/env bash
# Tests for scripts/gen_slo_rules.py, the SLO rule generator apps run (with uv):
#   1. Every chart fixture's generated slo-rules.values.yaml is current: regenerated from the
#      fixture's slos.yaml, run from the fixture's directory as an app runs it from its root.
#   2. Any app: a made-up spec gives a rule file and a values file holding the same rules,
#      with the app's runbook link, and the service chart renders the values file as the
#      app's <release>-slo PrometheusRule.
#
#   scripts/test-slo-generator.sh    # needs uv and helm
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GEN="$ROOT/scripts/gen_slo_rules.py"
gen() { uv run --quiet --script "$GEN" "$@"; }

echo "== fixtures are current"
for out in "$ROOT"/charts/service/tests/*/slo-rules.values.yaml; do
  [[ -f "$out" ]] || continue
  dir=$(dirname "$out")
  mapfile -t specs < <(cd "$dir" && find . -name slos.yaml -not -path './.*' | sort)
  [[ ${#specs[@]} == 1 ]] || { echo "::error::${dir#"$ROOT"/}: want one slos.yaml, found ${#specs[@]}"; exit 1; }
  (cd "$dir" && gen --spec "${specs[0]#./}" --values --out slo-rules.values.yaml --check)
  echo "ok ${out#"$ROOT"/}"
done

echo "== any app"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/app/deploy"
cat >"$tmp/app/deploy/slos.yaml" <<'EOF'
window: 30d
runbook_url: https://example.test/pager/RUNBOOK.md#slo
slos:
  - name: ack-availability
    description: POST /ack answers without a server error
    objective: 0.999
    sli: { type: availability, selector: 'method="POST", route="/ack"' }
EOF
(cd "$tmp/app" && gen --spec deploy/slos.yaml --out rules/slo.yaml >/dev/null &&
  gen --spec deploy/slos.yaml --values --out deploy/slo-rules.values.yaml >/dev/null)
head -1 "$tmp/app/rules/slo.yaml" | grep -q 'from deploy/slos.yaml\.' ||
  { echo "::error::the header doesn't name the spec by its path from the app's root"; exit 1; }

# The values file holds the rule file's text, byte for byte; the rules carry the app's runbook.
uv run --quiet --no-project --with pyyaml==6.0.3 python - "$tmp/app" <<'EOF'
import sys, yaml
app = sys.argv[1]
rules = open(f"{app}/rules/slo.yaml").read()
values = yaml.safe_load(open(f"{app}/deploy/slo-rules.values.yaml"))
assert values == {"monitoring": {"rules": {"slo": rules}}}, "values file != rule file"
alerts = [r for g in yaml.safe_load(rules)["groups"] for r in g["rules"] if "alert" in r]
assert alerts and {a["annotations"]["runbook_url"] for a in alerts} == {
    "https://example.test/pager/RUNBOOK.md#slo"
}, "alerts or runbook_url wrong"
EOF

# The chart renders it as the app's PrometheusRule.
helm template pager "$ROOT/charts/service" --set app.name=pager \
  --set image.repository=ghcr.io/easypeasyops/oncall/pager --set image.tag=t \
  --set monitoring.enabled=true --set monitoring.clusterWide=true \
  --api-versions monitoring.coreos.com/v1/PrometheusRule \
  --values "$tmp/app/deploy/slo-rules.values.yaml" >"$tmp/render.yaml"
uv run --quiet --no-project --with pyyaml==6.0.3 python - "$tmp/render.yaml" <<'EOF'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
rules = [d for d in docs if d["kind"] == "PrometheusRule"]
assert [r["metadata"]["name"] for r in rules] == ["pager-slo"], rules
assert rules[0]["spec"]["groups"][0]["name"] == "slo-ack-availability"
EOF
echo "ok: rule file, values file and the chart's PrometheusRule agree"
