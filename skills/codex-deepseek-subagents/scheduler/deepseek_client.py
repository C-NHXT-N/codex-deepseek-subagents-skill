import copy
import json
import os
import time
import urllib.request


def invoke_deepseek_messages(messages, model, thinking, max_tokens, retry=None):
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY is not set.")
    model_label = f"{model}(thinking)" if thinking["type"] == "enabled" else model
    body = {
        "model": model,
        "messages": messages,
        "thinking": thinking,
        "max_tokens": max_tokens,
        "stream": False,
    }
    base_url = os.environ.get("DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    retry_policy = copy.deepcopy(retry or {})
    max_attempts = int(retry_policy.get("max_attempts") or 1)
    backoff_seconds = float(retry_policy.get("backoff_seconds") or 0)
    data = None
    last_error = None
    for attempt_index in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=300) as upstream:
                data = json.loads(upstream.read().decode("utf-8"))
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            if attempt_index + 1 >= max_attempts:
                break
            time.sleep(backoff_seconds * (2 ** attempt_index))
    if last_error is not None:
        raise last_error
    message = data["choices"][0]["message"]
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "content": message.get("content"),
        "reasoning_content": message.get("reasoning_content"),
        "model": data.get("model"),
        "model_label": model_label,
        "finish_reason": data["choices"][0].get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
