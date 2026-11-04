#!/usr/bin/env bash
# Load an app's keys into the platform's secret store: the Secret <app> in namespace
# secret-store, which the app's own ClusterSecretStore (deploy/secrets/app-stores.yaml) hands
# to its namespaces, where the service chart's `secretEnv` copies it into <release>-env.
#
#   scripts/load-app-env.sh postitglobal ../hotel_social_posts/apps/postitglobal/.env
#
# From a dotenv file (KEY=value lines; # comments and blank lines skipped; quotes are kept
# as part of the value, so write none). Every key in the file replaces what the Secret held:
# a key removed from the file is removed from the store. Prints key names, never values.
# The app's pods read their env at start: restart them to pick up a changed key
# (kubectl -n <app>-<env> rollout restart deployment).
set -euo pipefail

STORE_NS=secret-store
STORES="$(cd "$(dirname "$0")/.." && pwd)/deploy/secrets/app-stores.yaml"

die() { echo "load-app-env: $*" >&2; exit 1; }

[[ $# -eq 2 ]] || die "usage: $0 <app> <dotenv file>"
app=$1 file=$2
[[ -f $file ]] || die "no such file: $file"
grep -qE "^  name: ${app}-reader$" "$STORES" || die "$app has no store in ${STORES#"$PWD"/}"

# Server-side apply: no last-applied-configuration annotation, which would hold a copy of
# every value; this script's field manager owns the keys, so dropped keys go.
kubectl -n "$STORE_NS" create secret generic "$app" --from-env-file="$file" \
    --dry-run=client -o yaml \
  | kubectl apply --server-side --field-manager=load-app-env --force-conflicts -f - >/dev/null

# shellcheck disable=SC2016 # a go-template, not shell
keys=$(kubectl -n "$STORE_NS" get secret "$app" -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}')
echo "$STORE_NS/$app holds: $keys"
