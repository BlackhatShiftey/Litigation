#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 /path/to/seam_suite-VERSION-py3-none-any.whl [user@tailscale-host]" >&2
  exit 2
fi

wheel=$1
target=${2:-Ex0@100.126.138.102}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ ! -f $wheel || $wheel != *.whl ]]; then
  echo "wheel must be an existing .whl file" >&2
  exit 2
fi
if [[ ! $target =~ ^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$ ]]; then
  echo "invalid SSH target" >&2
  exit 2
fi

wheel_name=$(basename -- "$wheel")
if [[ ! $wheel_name =~ ^seam_suite-[A-Za-z0-9_.+-]+-py3-none-any\.whl$ ]]; then
  echo "wheel name is not a seam-suite universal wheel" >&2
  exit 2
fi

ssh_options=(-o BatchMode=yes -o ConnectTimeout=8)
ssh "${ssh_options[@]}" "$target" \
  'set -eu; umask 077; mkdir -p "$HOME/.local/share/litigation-seam/artifacts" "$HOME/.local/share/litigation-seam/state"; if [ ! -x "$HOME/.local/share/litigation-seam/.venv/bin/python" ]; then python3 -m venv "$HOME/.local/share/litigation-seam/.venv"; fi'

scp -q "${ssh_options[@]}" "$wheel" "$target:.local/share/litigation-seam/artifacts/$wheel_name"
scp -q "${ssh_options[@]}" "$script_dir/remote_helper.py" "$target:.local/share/litigation-seam/remote_helper.py"
scp -q "${ssh_options[@]}" "$script_dir/remote-helper" "$target:.local/share/litigation-seam/remote-helper"

ssh "${ssh_options[@]}" "$target" sh -s -- "$wheel_name" <<'REMOTE'
set -eu
umask 077
wheel="$HOME/.local/share/litigation-seam/artifacts/$1"
test -f "$wheel"
"$HOME/.local/share/litigation-seam/.venv/bin/python" -m pip install "$wheel"
chmod 700 "$HOME/.local/share/litigation-seam/remote_helper.py" "$HOME/.local/share/litigation-seam/remote-helper"
"$HOME/.local/share/litigation-seam/remote-helper" <<'JSON'
{"protocol":"litigation-seam/1","operation":"status"}
JSON
REMOTE
