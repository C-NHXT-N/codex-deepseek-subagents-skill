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
  "user_config.json"
  ".codex/config.toml"
  ".codex/agents/deepseek-worker.toml"
  ".codex/deepseek.local.env.sh"
  ".codex/deepseek.local.env.ps1"
  ".codex/runtime/deepseek_scheduler.py"
  ".codex/runtime/deepseek_runtime.py"
  ".codex/runtime/deepseek_client.py"
  ".codex/runtime/events.py"
  ".codex/runtime/render.py"
  ".codex/runtime/patch_preview.py"
  ".codex/runtime/tool_protocol.py"
  ".codex/runtime/usage.py"
  ".codex/runtime/doctor.py"
  ".codex/runtime/tui.py"
  ".codex/runtime/task_queue.json"
  ".codex/runtime/sessions.json"
  ".codex/runtime/events.log.jsonl"
  ".codex/test-runtime.sh"
  ".codex/test-runtime.ps1"
  ".codex/deepseek-codex.cmd"
  ".codex/deepseek-codex.sh"
)

for rel in "${required[@]}"; do
  test -f "$tmp/$rel"
done

grep -q -- '--thinking-view' "$script"
grep -q -- '--patch-view' "$script"
grep -q 'tui()' "$script"
grep -q 'runtime_cli tui' "$script"
grep -q 'show_patch()' "$script"
grep -q 'approve_patch()' "$script"

cat > "$tmp/.codex/test-responses-proxy.sh" <<'EOF'
#!/usr/bin/env bash
# Managed by codex-deepseek-subagents
echo legacy
EOF
cat > "$tmp/.codex/deepseek_responses_shim.py" <<'EOF'
# Managed by codex-deepseek-subagents
print("legacy")
EOF
: > "$tmp/.codex/deepseek-proxy.log.jsonl"

bash "$script" update \
  --project-root "$tmp"

for rel in \
  ".codex/test-responses-proxy.sh" \
  ".codex/deepseek_responses_shim.py" \
  ".codex/deepseek-proxy.log.jsonl"; do
  test ! -e "$tmp/$rel"
done

python3 - <<'PY' "$tmp/user_config.json"
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert "deepseek_api_key" not in data
assert data["defaults"]["execution_agent"] == "DeepSeek Worker"
assert len(data["connected_agents"]) == 2
assert data["tool_calling"]["mode"] == "native"
PY

grep -q "127.0.0.1:5001" "$tmp/.codex/config.toml"
grep -q "127.0.0.1:5001" "$tmp/.codex/deepseek.local.env.sh"
grep -q "127.0.0.1:5001" "$tmp/.codex/deepseek.local.env.ps1"

bash "$script" uninstall \
  --project-root "$tmp" \
  --dry-run

mkdir -p "$tmp_conflict/.codex"
cat > "$tmp_conflict/.codex/test-runtime.ps1" <<'EOF'
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
