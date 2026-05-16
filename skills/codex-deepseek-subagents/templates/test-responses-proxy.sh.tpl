#!/usr/bin/env bash
# Managed by codex-deepseek-subagents
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/deepseek.local.env.sh"

python3 - <<'PY'
import json
import os
import urllib.request

body = {
    "model": os.environ["DEEPSEEK_OPENAI_MODEL"],
    "input": [{"role": "user", "content": 'Return exactly this JSON and nothing else: {"status":"proxy-ok"}'}],
    "metadata": {"deepseek_reasoning_effort": "disabled"},
    "max_output_tokens": 64,
}
req = urllib.request.Request(
    os.environ["DEEPSEEK_PROXY_BASE_URL"].rstrip("/") + "/responses",
    data=json.dumps(body).encode(),
    headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_PROXY_API_KEY"], "Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=120) as res:
    data = json.loads(res.read().decode())

print(json.dumps({
    "id": data.get("id"),
    "status": data.get("status"),
    "model": data.get("model"),
    "model_label": data.get("model_label"),
    "output_text": data.get("output_text"),
    "contains_proxy_ok": "proxy-ok" in str(data.get("output_text")),
    "input_tokens": (data.get("usage") or {}).get("input_tokens"),
    "output_tokens": (data.get("usage") or {}).get("output_tokens"),
    "reasoning_tokens": (data.get("usage") or {}).get("reasoning_tokens"),
    "total_tokens": (data.get("usage") or {}).get("total_tokens"),
}, ensure_ascii=False, separators=(",", ":")))
PY
