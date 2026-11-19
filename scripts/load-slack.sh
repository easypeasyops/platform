#!/usr/bin/env bash
# Save the Slack app's bot token in macOS Keychain, once (PT-103). From then on every
# `scripts/cluster.sh up` copies it into the platform's secret store, so a new cluster gets
# Slack with no other step. If the laptop's cluster is up, it is copied there right away.
#
#   scripts/load-slack.sh                  # asks for the token, without echoing it
#   pbpaste | scripts/load-slack.sh        # or from stdin (scripts/setup-slack-app.sh does)
#
# The token is a bot token (xoxb-...) of a Slack app with the chat:write scope, from the
# app's OAuth & Permissions page (docs/RUNBOOK.md, Slack alerts). Running it again replaces
# the token. It never prints the token, and never puts it in an argument or a file.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/slack.sh
source "$ROOT/scripts/lib/slack.sh"
CLUSTER=shortly # scripts/cluster.sh's

die() { echo "load-slack: $*" >&2; exit 1; }

command -v security >/dev/null || die "needs macOS Keychain (security); on Linux, load the store by hand (docs/RUNBOOK.md, Slack alerts)"
if [[ -t 0 ]]; then
  read -rsp "Slack bot token (xoxb-...): " token
  echo
else
  read -r token || [[ -n "${token:-}" ]]
fi
# Also keeps the line below safe: security -i splits its input on spaces and quotes.
[[ "$token" =~ ^xoxb-[A-Za-z0-9-]+$ ]] || die "that isn't a bot token (xoxb- and letters, digits, dashes)"

# Ask Slack whose token it is before saving it (auth.test). The header goes through stdin
# (-H @-), so the token isn't in curl's arguments either.
who=$(printf 'Authorization: Bearer %s\n' "$token" \
  | curl -sS --max-time 10 -H @- https://slack.com/api/auth.test \
  | python3 -c 'import json, sys; r = json.load(sys.stdin)
print("bot " + r["user"] + " in " + r["team"] if r.get("ok") else "error " + r.get("error", "?"))') \
  || die "couldn't reach Slack to check the token"
[[ "$who" != error* ]] || die "Slack refused the token: ${who#error }"
echo "load-slack: Slack says this is the token of $who"

# security -i reads the command from stdin: the token isn't in any process's arguments.
# -U replaces an existing item.
printf 'add-generic-password -U -a %s -s %s -w %s\n' "$USER" "$SLACK_KEYCHAIN_SERVICE" "$token" \
  | security -i >/dev/null
security find-generic-password -s "$SLACK_KEYCHAIN_SERVICE" >/dev/null \
  || die "the token didn't reach Keychain"
echo "load-slack: token saved in Keychain ($SLACK_KEYCHAIN_SERVICE)"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  slack_token_to_store "kind-$CLUSTER"
else
  echo "load-slack: no cluster up; the next scripts/cluster.sh up copies it into the store"
fi
