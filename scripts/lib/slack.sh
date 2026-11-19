# shellcheck shell=bash
# Slack's bot token: where it is kept on the laptop and where the cluster reads it. Sourced
# by scripts/cluster.sh (loads it on every `up`) and scripts/load-slack.sh (saves it once),
# so the names live in one place (PT-103). Another alert channel's token would follow the
# same pattern: a Keychain item, copied into a store Secret by `up`.
SLACK_KEYCHAIN_SERVICE=easypeasyops-slack-bot-token # macOS Keychain, the login keychain
SLACK_STORE_SECRET=alertmanager-slack               # in namespace secret-store
SLACK_TOKEN_KEY=bot-token                           # its key there

# Copy the token from Keychain into the store Secret, in the cluster of kube context $1.
# Nothing to do where there is no Keychain (Linux, CI) or no token in it: the store keeps
# what it has, so a token loaded into the store by hand (Linux) still counts. The token goes
# through a pipe, never an argument or a file, and is never printed. Applied server-side
# under its own field manager, so the Secret's other keys stay.
slack_token_to_store() {
  local context=$1 token
  command -v security >/dev/null || return 0
  token=$(security find-generic-password -s "$SLACK_KEYCHAIN_SERVICE" -w 2>/dev/null) || return 0
  # Argo CD creates the namespace (the secret-store Application), but a new cluster loads the
  # token before that sync, so `up` can switch Slack on in the same run. Same label as
  # Argo CD's managedNamespaceMetadata.
  kubectl --context "$context" create namespace secret-store --dry-run=client -o yaml \
    | kubectl --context "$context" label --local -f - -o yaml \
      pod-security.kubernetes.io/enforce=restricted \
    | kubectl --context "$context" apply --server-side --field-manager=slack-bot-token -f - >/dev/null
  printf %s "$token" \
    | kubectl --context "$context" -n secret-store create secret generic "$SLACK_STORE_SECRET" \
      --from-file="$SLACK_TOKEN_KEY=/dev/stdin" --dry-run=client -o yaml \
    | kubectl --context "$context" apply --server-side --field-manager=slack-bot-token -f - >/dev/null
  echo "slack: bot token copied from Keychain into secret-store/$SLACK_STORE_SECRET"
}

# Prints "found" when the store Secret has the token, in the cluster of kube context $1. A
# failed lookup fails (under set -e, the caller stops) rather than reading as "no token".
slack_token_in_store() {
  kubectl --context "$1" -n secret-store get secret "$SLACK_STORE_SECRET" --ignore-not-found \
    -o go-template="{{if index .data \"$SLACK_TOKEN_KEY\"}}found{{end}}"
}

# What `up` says, then exits non-zero, when Slack is on but has no token (PT-97).
slack_token_missing() {
  echo "FAIL: Slack is on, but there is no Slack bot token in Keychain or the store," >&2
  echo "      so alerts stay in Alertmanager. The rest of the cluster is up. Either:" >&2
  echo "      - load the token (RUNBOOK, Slack alerts) and run scripts/cluster.sh up again, or" >&2
  echo "      - run SLACK=0 scripts/cluster.sh up, to go without Slack" >&2
}
