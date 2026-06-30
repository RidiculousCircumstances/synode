#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
unit_src="${script_dir}/ollama.service"
unit_dst="/etc/systemd/system/ollama.service"

if [[ ! -x /home/rd/.local/bin/ollama ]]; then
  echo "Missing executable: /home/rd/.local/bin/ollama" >&2
  exit 1
fi

if [[ ! -d /home/rd/.ollama/models ]]; then
  echo "Missing model store: /home/rd/.ollama/models" >&2
  exit 1
fi

install -m 0644 "${unit_src}" "${unit_dst}"
systemctl daemon-reload
systemctl enable --now ollama.service
systemctl --no-pager --full status ollama.service
