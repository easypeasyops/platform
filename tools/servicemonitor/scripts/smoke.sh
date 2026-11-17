#!/usr/bin/env bash
# Black-box smoke test of the built image, run the way the platform's service chart runs
# it: as UID 10001 on a read-only root filesystem. It answers the probes, serves metrics on
# their own port and stops cleanly on SIGTERM.
#
#   IMAGE=servicemonitor:dev scripts/smoke.sh
set -euo pipefail

IMAGE="${IMAGE:-servicemonitor:dev}"
NAME="servicemonitor-smoke-$$"
fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok   $*"; }
cleanup() {
  status=$?
  [[ $status -eq 0 ]] || { echo "--- container logs ---"; docker logs "$NAME" 2>&1 | tail -50 || true; }
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

docker run -d --name "$NAME" --read-only --cap-drop ALL \
  -p 127.0.0.1:8000:8000 -p 127.0.0.1:9100:9100 "$IMAGE" >/dev/null

code=""
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/healthz || true)
  [[ "$code" == 200 ]] && break
  sleep 1
done
[[ "$code" == 200 ]] || fail "/healthz returned ${code:-nothing} after 30 s"
pass "/healthz 200 on a read-only root filesystem"

uid=$(docker exec "$NAME" id -u)
[[ "$uid" == 10001 ]] || fail "the app runs as uid $uid (the chart runs it as 10001)"
pass "runs as uid 10001"

code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 http://127.0.0.1:8000/readyz)
[[ "$code" == 200 ]] || fail "/readyz returned $code (want 200)"
pass "/readyz 200"

# The page renders from the templates in the image. Nothing here answers for the
# Kubernetes API or Prometheus, so it says so instead of showing monitors.
page=$(curl -s -m 30 -w '\n%{http_code}' http://127.0.0.1:8000/monitors)
[[ "${page##*$'\n'}" == 200 ]] || fail "/monitors returned ${page##*$'\n'} (want 200)"
grep -q 'has not answered yet' <<<"$page" || fail "/monitors is not the Monitors tab"
pass "/monitors renders, with no source answering"

# Metrics on their own port, with the probe counted by route.
metrics=$(curl -s http://127.0.0.1:9100/metrics) # captured: grep -q would SIGPIPE curl
grep -q 'http_requests_total{method="GET",route="/readyz",status="200"}' <<<"$metrics" \
  || fail "no request metrics on :9100"
pass "metrics on :9100"

# SIGTERM (what Kubernetes sends) stops it cleanly: Python is PID 1, not behind a shell.
docker stop -t 25 "$NAME" >/dev/null
code=$(docker inspect -f '{{.State.ExitCode}}' "$NAME")
[[ "$code" == 0 ]] || fail "exited $code on SIGTERM (want 0)"
pass "clean exit on SIGTERM"

echo "smoke test passed"
