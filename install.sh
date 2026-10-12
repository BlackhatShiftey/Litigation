#!/usr/bin/env bash
# Litigation installer: project venv + dependencies, then the first-run
# auth choice (subscriptions vs API keys) happens in the browser at /setup.
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_dir"

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

cat <<'EOF'
Litigation installed.

Next:
  1. ./start.sh
  2. Open http://127.0.0.1:8766 — you'll be taken to /setup to choose
     subscription logins or API keys (flippable any time from the top bar).

Headless instead?  With the server running:
  python3 litigation_cli.py setup --mode subscription
  python3 litigation_cli.py setup --set ZAI_API_KEY
EOF

if [[ "${1:-}" == "--run" ]]; then
  exec ./start.sh
fi
