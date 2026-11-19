#!/usr/bin/env bash
# Set up the Slack app Alertmanager posts as, once (PT-103). Slack lets no script install an
# app or read its token, so two steps stay in the browser; this script does the rest:
#   1. copies the app's manifest (deploy/slack/app-manifest.yaml) to the clipboard and opens
#      Slack's "create app" page: you paste it, then Install and Allow;
#   2. asks for the bot token (not echoed) and hands it to scripts/load-slack.sh, which checks
#      it with Slack, saves it in Keychain and copies it into the cluster if one is up.
#
#   scripts/setup-slack-app.sh
#
# Run it again to replace the token (after revoking a leaked one, say): skip step 1 then.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ -t 0 ]] || { echo "setup-slack-app: run it in a terminal: it asks for the token" >&2; exit 1; }

pbcopy <"$ROOT/deploy/slack/app-manifest.yaml"
open "https://api.slack.com/apps?new_app=1"
cat <<'EOF'
1. In the browser (the app's manifest is on your clipboard):
   From an app manifest > pick the workspace > Next > YAML tab > select all, paste (Cmd-V)
   > Next > Create > Install to Workspace > Allow.
   Already have the app? Paste the manifest on its App Manifest page instead, Save, then
   Reinstall to Workspace if Slack asks.
2. On the app's OAuth & Permissions page, copy the Bot User OAuth Token (xoxb-...).
EOF
read -rsp "Paste the token here and press Enter (not shown): " token
echo
printf '%s\n' "$token" | "$ROOT/scripts/load-slack.sh"
