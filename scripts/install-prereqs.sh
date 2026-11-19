#!/usr/bin/env bash
# Install what it takes to bring an app from app-template to prod, with Homebrew (macOS or
# Linux):
#   - scripts/new-app.py (create, staging, prod): git, gh, uv
#   - the laptop cluster those environments run on (scripts/cluster.sh): kind, kubectl,
#     helm (4 or newer), and Docker (Docker Desktop on macOS, Docker Engine on Linux)
# Idempotent: a tool already on PATH is left as it is (no upgrade), except a Helm older
# than 4. Docker and the gh login are only checked: they need you.
#
#   scripts/install-prereqs.sh
set -euo pipefail

os=$(uname -s)
# Homebrew on Linux lives outside PATH until its shellenv is loaded.
if ! command -v brew >/dev/null && [[ -x /home/linuxbrew/.linuxbrew/bin/brew ]]; then
  eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"
fi
command -v brew >/dev/null || {
  echo "Homebrew is required (macOS or Linux): https://brew.sh" >&2
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
if [[ $os == Linux ]]; then
  if ! command -v docker >/dev/null; then
    todo+=("Install Docker Engine (https://docs.docker.com/engine/install/) and start it")
  elif ! docker info >/dev/null 2>&1; then
    todo+=("Start Docker (sudo systemctl start docker), and if it still refuses you, join the docker group: sudo usermod -aG docker \"$USER\", then log in again")
  fi
elif ! command -v docker >/dev/null; then
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
