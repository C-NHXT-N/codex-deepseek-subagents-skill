#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

script="skills/codex-deepseek-subagents/scripts/deepseek-codex.sh"

bash -n "$script"

tmp="$(mktemp -d)"
tmp_conflict="$(mktemp -d)"
trap 'rm -rf "$tmp" "$tmp_conflict"' EXIT

bash "$script" install \
  --project-root "$tmp" \
  --api-key "sk-test-placeholder" \
  --port 5001

required=(
  ".codex/config.toml"
  ".codex/agents/deepseek-worker.toml"
  ".codex/deepseek.local.env.sh"
  ".codex/deepseek.local.env.ps1"
  ".codex/deepseek_responses_shim.py"
  ".codex/deepseek-responses-shim.ps1"
  ".codex/test-deepseek-direct.sh"
  ".codex/test-deepseek-direct.ps1"
  ".codex/test-responses-proxy.sh"
  ".codex/test-responses-proxy.ps1"
)

for rel in "${required[@]}"; do
  test -f "$tmp/$rel"
done

grep -q "127.0.0.1:5001" "$tmp/.codex/config.toml"
grep -q "127.0.0.1:5001" "$tmp/.codex/deepseek.local.env.sh"
grep -q "127.0.0.1:5001" "$tmp/.codex/deepseek.local.env.ps1"

bash "$script" uninstall \
  --project-root "$tmp" \
  --dry-run

mkdir -p "$tmp_conflict/.codex"
cat > "$tmp_conflict/.codex/test-deepseek-direct.ps1" <<'EOF'
Write-Host "custom"
EOF

if bash "$script" install \
  --project-root "$tmp_conflict" \
  --api-key "sk-test-placeholder" \
  >/tmp/codex-deepseek-bash-install.log 2>&1; then
  echo "expected install to refuse overwriting a non-managed .ps1 file" >&2
  exit 1
fi

grep -q "Refusing to overwrite non-managed file" /tmp/codex-deepseek-bash-install.log
