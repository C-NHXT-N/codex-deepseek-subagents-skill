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

usage() {
  cat <<'EOF'
Usage: deepseek-codex.sh <command> [options]

Commands:
  install, update, uninstall, doctor, desktop-doctor, delegate,
  start-proxy, stop-proxy, test-proxy, usage, redact, export-shareable

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
    --port|-Port) PORT="$2"; shift 2 ;;
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

is_managed_file() {
  [[ -f "$1" ]] && head -n 3 "$1" 2>/dev/null | grep -Fxq "$MANAGED_MARKER"
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

add_gitignore_rules() {
  local gitignore
  gitignore="$(project_path ".gitignore")"
  local rules=(".codex/*.local.*" ".codex/deepseek-proxy.log.jsonl" ".codex/backups/" ".codex/deepseek-proxy.pid" ".codex/deepseek-proxy.stdout.log" ".codex/deepseek-proxy.stderr.log")
  local missing=()
  for rule in "${rules[@]}"; do
    if [[ ! -f "$gitignore" ]] || ! grep -Fxq "$rule" "$gitignore"; then
      missing+=("$rule")
    fi
  done
  [[ "${#missing[@]}" == "0" ]] && { step ".gitignore already contains DeepSeek local rules."; return; }
  if [[ "$DRY_RUN" == "1" ]]; then
    step "Would append .gitignore rules: ${missing[*]}"
    return
  fi
  [[ -f "$gitignore" ]] && backup_file "$gitignore"
  {
    printf '\n# Local Codex DeepSeek secrets and logs\n'
    printf '%s\n' "${missing[@]}"
  } >> "$gitignore"
}

install_or_update() {
  local is_update="$1"
  require_command python3
  if [[ -z "$API_KEY" && "$is_update" == "0" ]]; then
    echo "install requires --api-key. The key is written only to .codex/deepseek.local.env.*." >&2
    exit 1
  fi
  if [[ -z "$API_KEY" && "$is_update" == "1" ]]; then
    local existing
    existing="$(project_path ".codex/deepseek.local.env.sh")"
    if [[ -f "$existing" ]]; then
      API_KEY="$(sed -n "s/^export DEEPSEEK_API_KEY='\(.*\)'$/\1/p" "$existing" | head -n 1)"
    fi
    [[ -z "$API_KEY" ]] && { echo "update requires --api-key when no existing managed key can be reused." >&2; exit 1; }
  fi
  write_managed_file "$(project_path ".codex/config.toml")" "$(expand_template "config.toml.tpl")"
  write_managed_file "$(project_path ".codex/agents/deepseek-worker.toml")" "$(expand_template "deepseek-worker.toml.tpl")"
  write_managed_file "$(project_path ".codex/deepseek.local.env.sh")" "$(expand_template "deepseek.local.env.sh.tpl")" 1
  write_managed_file "$(project_path ".codex/deepseek.local.env.ps1")" "$(powershell_template_or_comment)" 1
  write_managed_file "$(project_path ".codex/deepseek_responses_shim.py")" "$(expand_template "deepseek_responses_shim.py.tpl")"
  write_managed_file "$(project_path ".codex/test-deepseek-direct.sh")" "$(expand_template "test-deepseek-direct.sh.tpl")"
  write_managed_file "$(project_path ".codex/test-responses-proxy.sh")" "$(expand_template "test-responses-proxy.sh.tpl")"
  add_gitignore_rules
  step "$([[ "$is_update" == "1" ]] && echo Update || echo Install) complete."
}

powershell_template_or_comment() {
  # Keep Windows files available even when installing from bash.
  API_KEY_PS="$API_KEY" BASE_URL_PS="$BASE_URL" ANTHROPIC_BASE_URL_PS="$ANTHROPIC_BASE_URL" MODEL_PS="$MODEL" FAST_MODEL_PS="$FAST_MODEL" THINKING_DEFAULT_PS="$THINKING_DEFAULT" PORT_PS="$PORT" python3 - "$TEMPLATE_ROOT/deepseek.local.env.ps1.tpl" <<'PY'
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
    ".codex/config.toml"
    ".codex/agents/deepseek-worker.toml"
    ".codex/deepseek.local.env.sh"
    ".codex/deepseek.local.env.ps1"
    ".codex/deepseek_responses_shim.py"
    ".codex/test-deepseek-direct.sh"
    ".codex/test-responses-proxy.sh"
  )
  for rel in "${paths[@]}"; do remove_managed_path "$(project_path "$rel")"; done
  for rel in ".codex/deepseek-proxy.log.jsonl" ".codex/deepseek-proxy.pid" ".codex/deepseek-proxy.stdout.log" ".codex/deepseek-proxy.stderr.log"; do
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

doctor() {
  import_env
  require_command python3
  (cd "$PROJECT_ROOT" && python3 - <<'PY'
import json, os, urllib.request, urllib.error
checks = {
    "config_exists": os.path.exists(".codex/config.toml"),
    "worker_exists": os.path.exists(".codex/agents/deepseek-worker.toml"),
    "env_exists": os.path.exists(".codex/deepseek.local.env.sh"),
    "env_ignored": ".codex/*.local.*" in open(".gitignore", encoding="utf-8").read() if os.path.exists(".gitignore") else False,
}
try:
    body = {"model": os.environ["DEEPSEEK_OPENAI_MODEL"], "messages": [{"role":"user","content":"Reply with exactly: direct-ok"}], "thinking": {"type":"disabled"}, "max_tokens": 32, "stream": False}
    req = urllib.request.Request(os.environ["DEEPSEEK_OPENAI_BASE_URL"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(), headers={"Authorization":"Bearer "+os.environ["DEEPSEEK_API_KEY"],"Content-Type":"application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as res:
        data = json.loads(res.read().decode())
    checks["direct_api"] = {"ok": "direct-ok" in str(data["choices"][0]["message"].get("content")), "total_tokens": data.get("usage", {}).get("total_tokens")}
except Exception as exc:
    checks["direct_api_error"] = str(exc)
try:
    urllib.request.urlopen("http://127.0.0.1:" + os.environ["DEEPSEEK_PROXY_BASE_URL"].split(":")[-1].split("/")[0] + "/health", timeout=2)
    checks["proxy_health"] = {"ok": True}
except Exception:
    checks["proxy_health_error"] = "Proxy is not running. Run start-proxy."
checks["desktop_native_subagent"] = {
    "configured_agent": "deepseek_worker",
    "worker_config_exists": checks["worker_exists"],
    "registry_status": "not_verifiable_from_script",
    "note": "If Codex Desktop returns 'agent type is currently not available', use the delegate fallback. Skills cannot force Desktop to render a native subagent card.",
    "fallback_command": "delegate --mode pro-thinking --prompt <task>",
}
print(json.dumps(checks, ensure_ascii=False, indent=2))
PY
)
}

delegate() {
  import_env
  require_command python3
  if [[ -z "$PROMPT" && -z "$PROMPT_FILE" ]]; then
    echo "delegate requires --prompt or --prompt-file. It never reads repository files automatically." >&2
    exit 1
  fi
  if [[ -n "$PROMPT" && -n "$PROMPT_FILE" ]]; then
    echo "Use either --prompt or --prompt-file, not both." >&2
    exit 1
  fi
  MODE_ENV="$MODE" THINKING_VIEW_ENV="$THINKING_VIEW" PROMPT_ENV="$PROMPT" PROMPT_FILE_ENV="$PROMPT_FILE" MAX_TOKENS_ENV="$MAX_TOKENS" python3 - <<'PY'
import json
import os
import urllib.request

mode = os.environ["MODE_ENV"]
thinking_view = os.environ["THINKING_VIEW_ENV"]
prompt = os.environ.get("PROMPT_ENV") or ""
prompt_file = os.environ.get("PROMPT_FILE_ENV") or ""
if prompt_file:
    with open(prompt_file, encoding="utf-8") as handle:
        prompt = handle.read()
if thinking_view == "summary":
    prompt = prompt + "\n\nAt the end of the final answer, add a short section titled 'Reasoning summary'. Summarize only the key decision factors. Do not reveal or restate hidden chain-of-thought or raw reasoning content."

model_map = {
    "pro-thinking": (os.environ["DEEPSEEK_OPENAI_MODEL"], {"type": "enabled", "reasoning_effort": "high"}),
    "flash-thinking": (os.environ["DEEPSEEK_OPENAI_FAST_MODEL"], {"type": "enabled", "reasoning_effort": "high"}),
    "pro": (os.environ["DEEPSEEK_OPENAI_MODEL"], {"type": "disabled"}),
    "flash": (os.environ["DEEPSEEK_OPENAI_FAST_MODEL"], {"type": "disabled"}),
}
if mode not in model_map:
    raise SystemExit(f"Unknown mode: {mode}")
model, thinking = model_map[mode]
model_label = f"{model}(thinking)" if thinking["type"] == "enabled" else model
body = {
    "model": model,
    "messages": [{"role": "user", "content": prompt}],
    "thinking": thinking,
    "max_tokens": int(os.environ["MAX_TOKENS_ENV"]),
    "stream": False,
}
req = urllib.request.Request(
    os.environ["DEEPSEEK_OPENAI_BASE_URL"].rstrip("/") + "/chat/completions",
    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"], "Content-Type": "application/json; charset=utf-8"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=300) as res:
    data = json.loads(res.read().decode())
message = data["choices"][0]["message"]
usage = data.get("usage") or {}
details = usage.get("completion_tokens_details") or {}
output = {
    "ok": True,
    "mode": mode,
    "model": data.get("model"),
    "model_label": model_label,
    "thinking_type": thinking["type"],
    "reasoning_effort": thinking.get("reasoning_effort"),
    "thinking_view": thinking_view,
    "prompt_chars_sent": len(prompt),
    "prompt_tokens": usage.get("prompt_tokens"),
    "completion_tokens": usage.get("completion_tokens"),
    "reasoning_tokens": details.get("reasoning_tokens"),
    "total_tokens": usage.get("total_tokens"),
    "reasoning_content_discarded": thinking_view != "raw",
    "content": message.get("content"),
}
if thinking_view == "raw":
    output["reasoning_content"] = message.get("reasoning_content")
print(json.dumps(output, ensure_ascii=False, indent=2))
PY
}

start_proxy() {
  import_env
  require_command python3
  local pid_file
  pid_file="$(project_path ".codex/deepseek-proxy.pid")"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    if python3 - <<PY >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:$PORT/health", timeout=2)
PY
    then
      step "Proxy already running with PID $(cat "$pid_file")"
      return
    fi
    step "Found stale proxy PID $(cat "$pid_file") without health response; restarting."
    if [[ "$DRY_RUN" != "1" ]]; then
      kill "$(cat "$pid_file")" 2>/dev/null || true
      rm -f "$pid_file"
    fi
  fi
  if [[ "$DRY_RUN" == "1" ]]; then step "Would start Python proxy on port $PORT"; return; fi
  (cd "$PROJECT_ROOT" && nohup python3 .codex/deepseek_responses_shim.py --port "$PORT" --log-path .codex/deepseek-proxy.log.jsonl >.codex/deepseek-proxy.stdout.log 2>.codex/deepseek-proxy.stderr.log & echo $! > .codex/deepseek-proxy.pid)
  step "Started proxy PID $(cat "$pid_file") on port $PORT"
}

stop_proxy() {
  local pid_file
  pid_file="$(project_path ".codex/deepseek-proxy.pid")"
  [[ -f "$pid_file" ]] || { step "No proxy pid file found."; return; }
  local pid
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    if [[ "$DRY_RUN" == "1" ]]; then step "Would stop proxy PID $pid"; else kill "$pid"; step "Stopped proxy PID $pid"; fi
  fi
  [[ "$DRY_RUN" == "1" ]] || rm -f "$pid_file"
}

test_proxy() {
  import_env
  require_command python3
  (cd "$PROJECT_ROOT" && ./.codex/test-responses-proxy.sh)
}

show_usage() {
  require_command python3
  local log
  log="$(project_path ".codex/deepseek-proxy.log.jsonl")"
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
  if grep -RInE 'sk-[A-Za-z0-9]{12,}' "$PROJECT_ROOT" --exclude='*.local.*' --exclude='deepseek-proxy.log.jsonl' --exclude-dir='.git' --exclude-dir='backups' >/tmp/codex-deepseek-redact.txt; then
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
  (cd "$SKILL_ROOT" && zip -qr "$destination" SKILL.md agents scripts templates -x '*.local.env.sh' '*.local.env.ps1' '*/deepseek-proxy.log.jsonl' '*/backups/*')
  step "Exported shareable skill zip: $destination"
}

case "$COMMAND" in
  install) install_or_update 0 ;;
  update) install_or_update 1 ;;
  uninstall) uninstall_project ;;
  doctor) doctor ;;
  desktop-doctor) doctor ;;
  delegate) delegate ;;
  start-proxy) start_proxy ;;
  stop-proxy) stop_proxy ;;
  test-proxy) test_proxy ;;
  usage) show_usage ;;
  redact) redact_check ;;
  export-shareable) export_shareable ;;
  *) echo "Unknown command: $COMMAND" >&2; usage; exit 2 ;;
esac
