#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

config_path="${CC_PROXY_CONFIG:-${repo_root}/proxy/config.claude-code.toml.example}"
host="${CC_PROXY_HOST:-127.0.0.1}"
port="${CC_PROXY_PORT:-8787}"

if [[ ! -f "${config_path}" ]]; then
  printf 'CC proxy config not found: %s\n' "${config_path}" >&2
  exit 1
fi

cd "${repo_root}"
export CC_PROXY_CONFIG="${config_path}"

exec .venv/bin/python -m uvicorn proxy.app:create_app --factory --host "${host}" --port "${port}"
