#!/usr/bin/env bash
set -euo pipefail

host="${1:-127.0.0.1}"
port="${2:-8787}"
config_path="${3:-proxy/config.claude-code.toml.example}"

cat <<EOF
export CC_PROXY_CONFIG="${config_path}"
export ANTHROPIC_BASE_URL="http://${host}:${port}"
export ANTHROPIC_AUTH_TOKEN="\${ANTHROPIC_AUTH_TOKEN:-}"
export ANTHROPIC_MODEL="\${ANTHROPIC_MODEL:-}"
EOF
