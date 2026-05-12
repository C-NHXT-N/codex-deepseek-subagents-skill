#!/usr/bin/env bash
# Managed by codex-deepseek-subagents
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/deepseek.local.env.sh"

python3 - <<'PY'
import json
import os
import urllib.request

def call(thinking_type, effort=None, max_tokens=256):
    thinking = {"type": "disabled"} if thinking_type == "disabled" else {"type": "enabled", "reasoning_effort": effort or "high"}
    body = {
        "model": os.environ["DEEPSEEK_OPENAI_MODEL"],
        "messages": [{"role": "user", "content": "Which number is larger, 9.11 or 9.8? Reply with only the larger number."}],
        "thinking": thinking,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        os.environ["DEEPSEEK_OPENAI_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"], "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        data = json.loads(res.read().decode())
    msg = data["choices"][0]["message"]
    usage = data.get("usage", {})
    return {
        "thinking_type": thinking_type,
        "reasoning_effort": effort,
        "model": data.get("model"),
        "finish_reason": data["choices"][0].get("finish_reason"),
        "content": msg.get("content"),
        "has_reasoning_content": bool(msg.get("reasoning_content")),
        "reasoning_chars": len(msg.get("reasoning_content") or ""),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }

print(json.dumps([
    call("disabled"),
    call("enabled", "high", 1024),
], ensure_ascii=False, indent=2))
PY
