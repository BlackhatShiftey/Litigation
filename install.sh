#!/usr/bin/env bash
# Litigation installer: project venv + dependencies, then the first-run
# auth choice (subscriptions vs API keys) happens in the browser at /setup.
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_dir"

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# Put the terminal client on PATH as `litigation`.
chmod +x litigation_cli.py
mkdir -p "${HOME}/.local/bin"
ln -sf "${repo_dir}/litigation_cli.py" "${HOME}/.local/bin/litigation"
case ":${PATH}:" in
  *":${HOME}/.local/bin:"*) ;;
  *) echo "note: add ~/.local/bin to your PATH to run 'litigation' directly" ;;
esac

cat <<'EOF'
Litigation installed.

Next:
  1. ./start.sh
  2. Open http://127.0.0.1:8766 — a first-run settings page covers
     subscription logins, API keys, and local models (Ollama / LM Studio).
     You pick subscription, API, and local participants per conversation
     whenever you start a chat.

Terminal client (same device, or point LITIGATION_URL at the server):
  litigation rooms
  litigation send <room-id> "address the court"
  litigation models

Headless settings instead?  With the server running:
  litigation setup --set OPENAI_API_KEY
  litigation setup --local-model ollama=llama3.1
  litigation setup --done
EOF

if [[ "${1:-}" == "--run" ]]; then
  exec ./start.sh
fi
