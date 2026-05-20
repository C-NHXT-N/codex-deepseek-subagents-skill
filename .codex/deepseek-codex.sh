#!/usr/bin/env bash
# Managed by codex-deepseek-subagents
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/runtime/deepseek_runtime.py" "$@"
