#!/usr/bin/env bash
# One-time HTTPS upgrade for the existing TANEM Selectel publisher.
set -euo pipefail
if [[ "$(id -u)" != 0 ]]; then echo 'Run in Selectel root console'; exit 1; fi
revision="$1"
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Pass an inspected full Git commit SHA'; exit 1
fi
command -v curl >/dev/null || { echo 'curl is required'; exit 1; }
temp="$(mktemp -d)"
trap 'rm -rf "$temp"' EXIT
base="https://raw.githubusercontent.com/TanemTech/test-01/$revision/deploy/https"
for file in receiver.py agent.py enable.py; do
  curl --fail --silent --show-error --location --max-time 20 "$base/$file" -o "$temp/$file"
  test -s "$temp/$file"
done
python3 "$temp/enable.py" "$temp/receiver.py" "$temp/agent.py"
