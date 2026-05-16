#!/usr/bin/env bash
# Managed by codex-deepseek-subagents
set -euo pipefail

COMMAND="${1:-doctor}"
if [[ $# -gt 0 ]]; then shift; fi

PROJECT_ROOT="$(pwd)"
API_KEY=""
MODEL="deepseek-v4-pro"
FAST_MODEL="deepseek-v4-flash"
BASE_URL="https://api.deepseek.com"
ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
PORT="4000"
PORT_EXPLICIT=0
THINKING_DEFAULT="disabled"
DRY_RUN=0
NO_BACKUP=0
FORCE=0
REMOVE_SKILL=0
OUT_FILE=""
MODE="pro-thinking"
THINKING_VIEW="hidden"
PROMPT=""
PROMPT_FILE=""
MAX_TOKENS="2048"
MANAGED_MARKER="# Managed by codex-deepseek-subagents"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_ROOT="$SKILL_ROOT/templates"
SCHEDULER_ROOT="$SKILL_ROOT/scheduler"

usage() {
  cat <<'EOF'
Usage: deepseek-codex.sh <command> [options]

Commands:
  install, update, uninstall, doctor, delegate, analyze,
  start-runtime, stop-runtime, test-runtime,
  usage, redact, export-shareable

Compatibility aliases:
  start-proxy, stop-proxy, test-proxy, desktop-doctor

Options:
  --project-root PATH
  --api-key KEY
  --model MODEL
  --fast-model MODEL
  --base-url URL
  --anthropic-base-url URL
  --port PORT
  --thinking-default disabled|high|max
  --mode pro-thinking|flash-thinking|pro|flash
  --thinking-view hidden|summary|raw
  --prompt TEXT
  --prompt-file PATH
  --max-tokens N
  --dry-run
  --no-backup
  --force
  --remove-skill
  --out-file PATH
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-root|-ProjectRoot) PROJECT_ROOT="$2"; shift 2 ;;
    --api-key|-ApiKey) API_KEY="$2"; shift 2 ;;
    --model|-Model) MODEL="$2"; shift 2 ;;
    --fast-model|-FastModel) FAST_MODEL="$2"; shift 2 ;;
    --base-url|-BaseUrl) BASE_URL="$2"; shift 2 ;;
    --anthropic-base-url|-AnthropicBaseUrl) ANTHROPIC_BASE_URL="$2"; shift 2 ;;
    --port|-Port) PORT="$2"; PORT_EXPLICIT=1; shift 2 ;;
    --thinking-default|-ThinkingDefault) THINKING_DEFAULT="$2"; shift 2 ;;
    --mode|-Mode) MODE="$2"; shift 2 ;;
    --thinking-view|-ThinkingView) THINKING_VIEW="$2"; shift 2 ;;
    --prompt|-Prompt) PROMPT="$2"; shift 2 ;;
    --prompt-file|-PromptFile) PROMPT_FILE="$2"; shift 2 ;;
    --max-tokens|-MaxTokens) MAX_TOKENS="$2"; shift 2 ;;
    --dry-run|-DryRun) DRY_RUN=1; shift ;;
    --no-backup|-NoBackup) NO_BACKUP=1; shift ;;
    --force|-Force) FORCE=1; shift ;;
    --remove-skill|-RemoveSkill) REMOVE_SKILL=1; shift ;;
    --out-file|-OutFile) OUT_FILE="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd)"

case "$THINKING_VIEW" in
  hidden|summary|raw) ;;
  *) echo "Invalid thinking view: $THINKING_VIEW. Use hidden, summary, or raw." >&2; exit 2 ;;
esac

step() {
  printf '[codex-deepseek-subagents] %s\n' "$*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

project_path() {
  printf '%s/%s\n' "$PROJECT_ROOT" "$1"
}

sq() {
  printf "%s" "$1" | sed "s/'/'\\\\''/g"
}

tomlq() {
  printf "%s" "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

expand_template() {
  local template="$1"
  sed \
    -e "s|__API_KEY_SH__|$(sq "$API_KEY")|g" \
    -e "s|__BASE_URL_SH__|$(sq "$BASE_URL")|g" \
    -e "s|__ANTHROPIC_BASE_URL_SH__|$(sq "$ANTHROPIC_BASE_URL")|g" \
    -e "s|__MODEL_SH__|$(sq "$MODEL")|g" \
    -e "s|__FAST_MODEL_SH__|$(sq "$FAST_MODEL")|g" \
    -e "s|__THINKING_DEFAULT_SH__|$(sq "$THINKING_DEFAULT")|g" \
    -e "s|__MODEL_TOML__|$(tomlq "$MODEL")|g" \
    -e "s|__PORT__|$PORT|g" \
    "$TEMPLATE_ROOT/$template"
}

expand_scheduler_source() {
  local relative="$1"
  sed \
    -e "s|__API_KEY_SH__|$(sq "$API_KEY")|g" \
    -e "s|__BASE_URL_SH__|$(sq "$BASE_URL")|g" \
    -e "s|__ANTHROPIC_BASE_URL_SH__|$(sq "$ANTHROPIC_BASE_URL")|g" \
    -e "s|__MODEL_SH__|$(sq "$MODEL")|g" \
    -e "s|__FAST_MODEL_SH__|$(sq "$FAST_MODEL")|g" \
    -e "s|__THINKING_DEFAULT_SH__|$(sq "$THINKING_DEFAULT")|g" \
    -e "s|__PORT__|$PORT|g" \
    "$SCHEDULER_ROOT/$relative"
}

is_managed_file() {
  [[ -f "$1" ]] && head -n 3 "$1" 2>/dev/null | grep -Fq "Managed by codex-deepseek-subagents"
}

ensure_dir() {
  if [[ "$DRY_RUN" == "1" ]]; then
    step "Would ensure directory: $1"
  else
    mkdir -p "$1"
  fi
}

backup_file() {
  local file="$1"
  [[ "$NO_BACKUP" == "1" || ! -e "$file" ]] && return 0
  case "$(basename "$file")" in
    *.local.*) step "Skipping backup for local secret file: $file"; return 0 ;;
  esac
  local backup_root
  backup_root="$(project_path ".codex/backups")"
  local backup_path="$backup_root/$(date +%Y%m%d-%H%M%S)-$(basename "$file")"
  if [[ "$DRY_RUN" == "1" ]]; then
    step "Would backup $file -> $backup_path"
  else
    mkdir -p "$backup_root"
    cp -f "$file" "$backup_path"
  fi
}

write_managed_file() {
  local path="$1"
  local content="$2"
  local secret="${3:-0}"
  if [[ -e "$path" && "$FORCE" != "1" ]] && ! is_managed_file "$path"; then
    echo "Refusing to overwrite non-managed file: $path. Re-run with --force after reviewing it." >&2
    exit 1
  fi
  [[ -e "$path" ]] && backup_file "$path"
  ensure_dir "$(dirname "$path")"
  if [[ "$DRY_RUN" == "1" ]]; then
    if [[ "$secret" == "1" ]]; then
      step "Would write secret managed file: $path"
    else
      step "Would write managed file: $path"
    fi
  else
    printf '%s\n' "$content" > "$path"
    case "$path" in
      *.sh|*.py) chmod +x "$path" ;;
    esac
  fi
}

normalized_user_config() {
  local existing="$1"
  local template_file
  template_file="$(mktemp)"
  expand_template "user_config.json.tpl" > "$template_file"
  local status=0
  python3 - "$template_file" "$existing" "$SCHEDULER_ROOT/deepseek_scheduler.py" <<'PY' || status=$?
import importlib.util
import json
import sys
from pathlib import Path

template_path = Path(sys.argv[1])
existing_path = Path(sys.argv[2])
scheduler_path = Path(sys.argv[3])

template = json.loads(template_path.read_text(encoding="utf-8"))
existing = {}
if existing_path.exists():
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8-sig"))
    except Exception:
        existing = {}

spec = importlib.util.spec_from_file_location("deepseek_scheduler", scheduler_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
normalized = module.normalize_user_config_for_write(existing, template)
print(json.dumps(normalized, ensure_ascii=False, indent=2))
PY
  rm -f "$template_file"
  return "$status"
}

write_user_config() {
  local path="$1"
  [[ -e "$path" ]] && backup_file "$path"
  ensure_dir "$(dirname "$path")"
  if [[ "$DRY_RUN" == "1" ]]; then
    step "Would normalize user config: $path"
  else
    local content
    content="$(normalized_user_config "$path")"
    printf '%s\n' "$content" > "$path"
  fi
}

initialize_runtime_state_files() {
  local task_store session_store
  task_store="$(project_path ".codex/runtime/task_queue.json")"
  session_store="$(project_path ".codex/runtime/sessions.json")"
  if [[ ! -e "$task_store" || "$FORCE" == "1" ]]; then
    ensure_dir "$(dirname "$task_store")"
    if [[ "$DRY_RUN" == "1" ]]; then
      step "Would initialize runtime state file: $task_store"
    else
      printf '{\n  "tasks": []\n}\n' > "$task_store"
    fi
  fi
  if [[ ! -e "$session_store" || "$FORCE" == "1" ]]; then
    ensure_dir "$(dirname "$session_store")"
    if [[ "$DRY_RUN" == "1" ]]; then
      step "Would initialize runtime state file: $session_store"
    else
      printf '{\n  "sessions": []\n}\n' > "$session_store"
    fi
  fi
  local rel path
  for rel in ".codex/runtime/events.log.jsonl" ".codex/runtime/stdout.log" ".codex/runtime/stderr.log"; do
    path="$(project_path "$rel")"
    if [[ ! -e "$path" || "$FORCE" == "1" ]]; then
      ensure_dir "$(dirname "$path")"
      if [[ "$DRY_RUN" == "1" ]]; then
        step "Would initialize runtime state file: $path"
      else
        : > "$path"
      fi
    fi
  done
}

cleanup_legacy_artifacts() {
  local rel path
  for rel in \
    ".codex/deepseek-responses-shim.ps1" \
    ".codex/deepseek_responses_shim.py" \
    ".codex/test-deepseek-direct.ps1" \
    ".codex/test-deepseek-direct.sh" \
    ".codex/test-responses-proxy.ps1" \
    ".codex/test-responses-proxy.sh"; do
    path="$(project_path "$rel")"
    [[ -e "$path" ]] || continue
    if is_managed_file "$path"; then
      remove_managed_path "$path"
    else
      step "Skipping non-managed legacy file: $path"
    fi
  done
  for rel in ".codex/deepseek-proxy.log.jsonl" ".codex/deepseek-proxy.pid" ".codex/deepseek-proxy.stdout.log" ".codex/deepseek-proxy.stderr.log"; do
    path="$(project_path "$rel")"
    [[ -e "$path" ]] || continue
    if [[ "$DRY_RUN" == "1" ]]; then
      step "Would remove runtime file: $path"
    else
      rm -f "$path"
      step "Removed runtime file: $path"
    fi
  done
}

add_gitignore_rules() {
  local gitignore
  gitignore="$(project_path ".gitignore")"
  local rules=(".codex/*.local.*" ".codex/runtime/task_queue.json" ".codex/runtime/sessions.json" ".codex/runtime/events.log.jsonl" ".codex/runtime/runtime.pid" ".codex/runtime/stdout.log" ".codex/runtime/stderr.log" ".codex/test-runtime.ps1" ".codex/test-runtime.sh" ".codex/backups/" "__pycache__/" "*.py[cod]")
  local legacy_rules=(
    ".codex/deepseek-responses-shim.ps1"
    ".codex/deepseek_responses_shim.py"
    ".codex/test-deepseek-direct.ps1"
    ".codex/test-deepseek-direct.sh"
    ".codex/test-responses-proxy.ps1"
    ".codex/test-responses-proxy.sh"
    ".codex/deepseek-proxy.log.jsonl"
    ".codex/deepseek-proxy.pid"
    ".codex/deepseek-proxy.stdout.log"
    ".codex/deepseek-proxy.stderr.log"
  )
  local tmp filtered_changed=0 missing=()
  tmp="$(mktemp)"
  if [[ -f "$gitignore" ]]; then
    cp "$gitignore" "$tmp"
    for legacy in "${legacy_rules[@]}"; do
      if grep -Fxq "$legacy" "$tmp"; then
        filtered_changed=1
        grep -Fvx "$legacy" "$tmp" > "$tmp.next" || true
        mv "$tmp.next" "$tmp"
      fi
    done
  fi
  for rule in "${rules[@]}"; do
    if [[ ! -f "$tmp" ]] || ! grep -Fxq "$rule" "$tmp"; then
      missing+=("$rule")
    fi
  done
  if [[ "${#missing[@]}" == "0" && "$filtered_changed" == "0" ]]; then
    rm -f "$tmp"
    step ".gitignore already contains current DeepSeek local rules."
    return
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    rm -f "$tmp"
    step "Would refresh .gitignore DeepSeek local rules."
    return
  fi
  [[ -f "$gitignore" ]] && backup_file "$gitignore"
  if [[ ! -f "$tmp" ]]; then
    : > "$tmp"
  fi
  if [[ -s "$tmp" && "$(tail -c 1 "$tmp" 2>/dev/null || true)" != "" ]]; then
    printf '\n' >> "$tmp"
  fi
  if ! grep -Fxq "# Local Codex DeepSeek secrets and logs" "$tmp"; then
    printf '# Local Codex DeepSeek secrets and logs\n' >> "$tmp"
  fi
  for rule in "${rules[@]}"; do
    if ! grep -Fxq "$rule" "$tmp"; then
      printf '%s\n' "$rule" >> "$tmp"
    fi
  done
  mv "$tmp" "$gitignore"
}

install_or_update() {
  local is_update="$1"
  require_command python3
  if [[ -z "$API_KEY" && "$is_update" == "0" ]]; then
    echo "install requires --api-key. The key is written only to .codex/deepseek.local.env.*." >&2
    exit 1
  fi
  if [[ "$is_update" == "1" ]]; then
    sync_port_from_user_config
  fi
  if [[ -z "$API_KEY" && "$is_update" == "1" ]]; then
    local existing
    existing="$(project_path ".codex/deepseek.local.env.sh")"
    reuse_api_key_from_managed_env "$existing" || true
    [[ -z "$API_KEY" ]] && { echo "update requires --api-key when no existing managed key can be reused. Pass --api-key explicitly." >&2; exit 1; }
  fi
  write_user_config "$(project_path "user_config.json")"
  write_managed_file "$(project_path ".codex/config.toml")" "$(expand_template "config.toml.tpl")"
  write_managed_file "$(project_path ".codex/agents/deepseek-worker.toml")" "$(expand_template "deepseek-worker.toml.tpl")"
  write_managed_file "$(project_path ".codex/deepseek.local.env.sh")" "$(expand_template "deepseek.local.env.sh.tpl")" 1
  write_managed_file "$(project_path ".codex/deepseek.local.env.ps1")" "$(powershell_template_or_comment)" 1
  write_managed_file "$(project_path ".codex/runtime/deepseek_scheduler.py")" "$(expand_scheduler_source "deepseek_scheduler.py")"
  write_managed_file "$(project_path ".codex/runtime/deepseek_runtime.py")" "$(expand_scheduler_source "deepseek_runtime.py")"
  write_managed_file "$(project_path ".codex/test-runtime.sh")" "$(expand_template "test-runtime.sh.tpl")"
  write_managed_file "$(project_path ".codex/test-runtime.ps1")" "$(powershell_runtime_test_template_or_comment)"
  write_managed_file "$(project_path ".codex/deepseek-codex.cmd")" "$(powershell_expand_template "deepseek-codex.cmd.tpl")"
  write_managed_file "$(project_path ".codex/deepseek-codex.sh")" "$(expand_template "deepseek-codex.sh.tpl")"
  initialize_runtime_state_files
  cleanup_legacy_artifacts
  add_gitignore_rules
  step "$([[ "$is_update" == "1" ]] && echo Update || echo Install) complete."
  step "Post-install check: keep only one codex-deepseek-subagents skill under CODEX_HOME/skills, then run doctor, start-runtime, and test-runtime."
}

powershell_template_or_comment() {
  # Keep Windows files available even when installing from bash.
  powershell_expand_template "deepseek.local.env.ps1.tpl"
}

powershell_expand_template() {
  local template="$1"
  API_KEY_PS="$API_KEY" BASE_URL_PS="$BASE_URL" ANTHROPIC_BASE_URL_PS="$ANTHROPIC_BASE_URL" MODEL_PS="$MODEL" FAST_MODEL_PS="$FAST_MODEL" THINKING_DEFAULT_PS="$THINKING_DEFAULT" PORT_PS="$PORT" python3 - "$TEMPLATE_ROOT/$template" <<'PY'
import os, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
replacements = {
    "__API_KEY_PS__": os.environ["API_KEY_PS"].replace("\\", "\\\\").replace("'", "''"),
    "__BASE_URL_PS__": os.environ["BASE_URL_PS"].replace("\\", "\\\\").replace("'", "''"),
    "__ANTHROPIC_BASE_URL_PS__": os.environ["ANTHROPIC_BASE_URL_PS"].replace("\\", "\\\\").replace("'", "''"),
    "__MODEL_PS__": os.environ["MODEL_PS"].replace("\\", "\\\\").replace("'", "''"),
    "__FAST_MODEL_PS__": os.environ["FAST_MODEL_PS"].replace("\\", "\\\\").replace("'", "''"),
    "__THINKING_DEFAULT_PS__": os.environ["THINKING_DEFAULT_PS"].replace("\\", "\\\\").replace("'", "''"),
    "__PORT__": os.environ["PORT_PS"],
}
for key, value in replacements.items():
    text = text.replace(key, value)
print(text)
PY
}

powershell_runtime_test_template_or_comment() {
  powershell_expand_template "test-runtime.ps1.tpl"
}

remove_managed_path() {
  local path="$1"
  [[ ! -e "$path" ]] && return
  if ! is_managed_file "$path"; then
    step "Skipping non-managed file: $path"
    return
  fi
  backup_file "$path"
  if [[ "$DRY_RUN" == "1" ]]; then
    step "Would remove managed file: $path"
  else
    rm -f "$path"
    step "Removed: $path"
  fi
}

uninstall_project() {
  local paths=(
    "user_config.json"
    ".codex/config.toml"
    ".codex/agents/deepseek-worker.toml"
    ".codex/deepseek.local.env.sh"
    ".codex/deepseek.local.env.ps1"
    ".codex/runtime/deepseek_scheduler.py"
    ".codex/runtime/deepseek_runtime.py"
    ".codex/test-runtime.sh"
    ".codex/test-runtime.ps1"
    ".codex/deepseek-codex.cmd"
    ".codex/deepseek-codex.sh"
  )
  for rel in "${paths[@]}"; do remove_managed_path "$(project_path "$rel")"; done
  for rel in ".codex/deepseek-proxy.log.jsonl" ".codex/deepseek-proxy.pid" ".codex/deepseek-proxy.stdout.log" ".codex/deepseek-proxy.stderr.log" ".codex/runtime/task_queue.json" ".codex/runtime/sessions.json" ".codex/runtime/events.log.jsonl" ".codex/runtime/runtime.pid" ".codex/runtime/stdout.log" ".codex/runtime/stderr.log"; do
    local path
    path="$(project_path "$rel")"
    [[ -e "$path" ]] || continue
    if [[ "$DRY_RUN" == "1" ]]; then step "Would remove $path"; else rm -f "$path"; step "Removed: $path"; fi
  done
  if [[ "$REMOVE_SKILL" == "1" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then step "Would remove skill folder: $SKILL_ROOT"; else rm -rf "$SKILL_ROOT"; step "Removed skill folder: $SKILL_ROOT"; fi
  fi
}

import_env() {
  local env_file
  env_file="$(project_path ".codex/deepseek.local.env.sh")"
  [[ -f "$env_file" ]] || { echo "Missing local env file: $env_file. Run install first." >&2; exit 1; }
  # shellcheck disable=SC1090
  source "$env_file"
}

sync_port_from_env() {
  if [[ "$PORT_EXPLICIT" == "1" ]]; then
    return
  fi

  if [[ -n "${DEEPSEEK_PROXY_BASE_URL:-}" ]]; then
    local parsed
    parsed="$(
      DEEPSEEK_PROXY_BASE_URL="$DEEPSEEK_PROXY_BASE_URL" python3 - <<'PY'
import os
from urllib.parse import urlparse

url = os.environ.get("DEEPSEEK_PROXY_BASE_URL", "")
parsed = urlparse(url)
print(parsed.port or "")
PY
    )"
    if [[ -n "$parsed" ]]; then
      PORT="$parsed"
    fi
  fi
}

sync_port_from_user_config() {
  if [[ "$PORT_EXPLICIT" == "1" ]]; then
    return
  fi
  local config_path
  config_path="$(project_path "user_config.json")"
  [[ -f "$config_path" ]] || return
  local parsed
  parsed="$(
    python3 - "$config_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
except Exception:
    print("")
else:
    print((data.get("runtime") or {}).get("port") or "")
PY
  )"
  if [[ -n "$parsed" ]]; then
    PORT="$parsed"
  fi
}

runtime_cli() {
  require_command python3
  local runtime_cli_file="$SCHEDULER_ROOT/deepseek_runtime.py"
  local args=("$runtime_cli_file" "$1" "--project-root" "$PROJECT_ROOT")
  shift
  if [[ "$PORT_EXPLICIT" == "1" ]]; then
    args+=("--port" "$PORT")
  fi
  python3 "${args[@]}" "$@"
}

reuse_api_key_from_managed_env() {
  local existing="$1"
  if [[ ! -f "$existing" ]] || ! is_managed_file "$existing"; then
    return 1
  fi

  local reused
  reused="$(
    bash -c 'source "$1"; printf "%s" "${DEEPSEEK_API_KEY:-}"' _ "$existing"
  )" || return 1

  if [[ -z "$reused" ]]; then
    return 1
  fi

  API_KEY="$reused"
}

doctor() {
  runtime_cli doctor
}

delegate() {
  local args=(delegate --mode "$MODE" --max-tokens "$MAX_TOKENS")
  [[ -n "$PROMPT" ]] && args+=(--prompt "$PROMPT")
  [[ -n "$PROMPT_FILE" ]] && args+=(--prompt-file "$PROMPT_FILE")
  [[ "$THINKING_VIEW" == "raw" ]] && args+=(--verbose)
  runtime_cli "${args[@]}"
}

analyze() {
  local args=(analyze --mode "$MODE" --max-tokens "$MAX_TOKENS" --yes)
  [[ -n "$PROMPT" ]] && args+=(--prompt "$PROMPT")
  runtime_cli "${args[@]}"
}

start_proxy() {
  runtime_cli start-runtime
}

stop_proxy() {
  runtime_cli stop-runtime
}

test_proxy() {
  runtime_cli test-runtime
}

test_runtime() {
  runtime_cli test-runtime
}

show_usage() {
  require_command python3
  local log
  log="$(project_path ".codex/runtime/events.log.jsonl")"
  [[ -f "$log" ]] || { step "No usage log found: $log"; return; }
  python3 - "$log" <<'PY'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
def total(key): return sum((row.get(key) or 0) for row in rows)
groups = {}
for row in rows:
    if not row.get("total_tokens"):
        continue
    label = row.get("model_label") or (str(row.get("model")) + "(thinking)" if row.get("thinking_type") == "enabled" else row.get("model"))
    groups.setdefault(label, []).append(row)
print(json.dumps({
    "requests": sum(1 for row in rows if row.get("total_tokens")),
    "prompt_tokens": total("prompt_tokens"),
    "completion_tokens": total("completion_tokens"),
    "reasoning_tokens": total("reasoning_tokens"),
    "total_tokens": total("total_tokens"),
    "by_model_label": [{
        "model_label": label,
        "requests": len(items),
        "prompt_tokens": sum((row.get("prompt_tokens") or 0) for row in items),
        "completion_tokens": sum((row.get("completion_tokens") or 0) for row in items),
        "reasoning_tokens": sum((row.get("reasoning_tokens") or 0) for row in items),
        "total_tokens": sum((row.get("total_tokens") or 0) for row in items),
    } for label, items in sorted(groups.items())],
}, ensure_ascii=False, separators=(",", ":")))
PY
}

redact_check() {
  if grep -RInE 'sk-[A-Za-z0-9]{12,}' "$PROJECT_ROOT" --exclude='*.local.*' --exclude='events.log.jsonl' --exclude-dir='.git' --exclude-dir='backups' >/tmp/codex-deepseek-redact.txt; then
    cat /tmp/codex-deepseek-redact.txt
  else
    step "No non-local DeepSeek-looking keys found."
  fi
}

export_shareable() {
  require_command zip
  local destination="${OUT_FILE:-$PROJECT_ROOT/codex-deepseek-subagents.zip}"
  if [[ "$DRY_RUN" == "1" ]]; then step "Would export shareable skill zip to $destination"; return; fi
  rm -f "$destination"
  (cd "$SKILL_ROOT" && zip -qr "$destination" SKILL.md agents scripts templates scheduler -x '*.local.env.sh' '*.local.env.ps1' '*/events.log.jsonl' '*/backups/*' '*/__pycache__/*' '*.pyc')
  step "Exported shareable skill zip: $destination"
}

case "$COMMAND" in
  install) install_or_update 0 ;;
  update) install_or_update 1 ;;
  uninstall) uninstall_project ;;
  doctor) doctor ;;
  desktop-doctor) doctor ;;
  delegate) delegate ;;
  analyze) analyze ;;
  start-proxy) start_proxy ;;
  start-runtime) start_proxy ;;
  stop-proxy) stop_proxy ;;
  stop-runtime) stop_proxy ;;
  test-proxy) test_proxy ;;
  test-runtime) test_runtime ;;
  usage) show_usage ;;
  redact) redact_check ;;
  export-shareable) export_shareable ;;
  *) echo "Unknown command: $COMMAND" >&2; usage; exit 2 ;;
esac
