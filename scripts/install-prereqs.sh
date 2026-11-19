#!/usr/bin/env bash
# Install what it takes to bring an app from app-template to prod, on macOS with Homebrew:
#   - scripts/new-app.py (create, staging, prod): git, gh, uv
#   - the laptop cluster those environments run on (scripts/cluster.sh): Docker Desktop,
#     kind, kubectl, helm (4 or newer)
# Idempotent: a tool already on PATH is left as it is (no upgrade), except a Helm older
# than 4. Docker Desktop and the gh login are only checked: they need you.
#
#   scripts/install-prereqs.sh
set -euo pipefail

command -v brew >/dev/null || {
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
}

# command on PATH -> Homebrew formula
for pair in git:git gh:gh uv:uv kind:kind kubectl:kubernetes-cli helm:helm; do
  cmd=${pair%%:*} formula=${pair#*:}
  if command -v "$cmd" >/dev/null; then
    echo "ok: $cmd"
  else
    echo "installing $formula"
    brew install "$formula"
  fi
done

helm_major=$(helm version --template '{{.Version}}' | sed -E 's/^v([0-9]+).*/\1/')
if ((helm_major < 4)); then
  echo "upgrading helm (cluster.sh needs Helm 4 or newer)"
  brew upgrade helm
fi

todo=()
if ! command -v docker >/dev/null; then
  todo+=("Install Docker Desktop (https://www.docker.com/products/docker-desktop/) and start it")
elif ! docker info >/dev/null 2>&1; then
  todo+=("Start Docker Desktop")
fi
# new-app.py pushes the template's .github/workflows/, which needs the workflow scope.
if ! gh auth status >/dev/null 2>&1; then
  todo+=("Log in to GitHub: gh auth login -s workflow")
elif ! gh auth status 2>&1 | grep -q "'workflow'"; then
  todo+=("Add the workflow scope: gh auth refresh -s workflow")
fi

if ((${#todo[@]})); then
  echo
  echo "Left for you:"
  printf '  - %s\n' "${todo[@]}"
  exit 1
fi
echo "All set: scripts/cluster.sh up, then scripts/new-app.py create <app> --lang <lang>."
