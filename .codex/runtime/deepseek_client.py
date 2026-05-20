# Managed by codex-deepseek-subagents
import copy
import json
import os
import time
import urllib.error
import urllib.request


class DeepSeekAPIError(RuntimeError):
    def __init__(self, status_code, body, url):
        self.status_code = int(status_code)
        self.body = body
        self.url = url
        super().__init__(f"DeepSeek API error {self.status_code} for {self.url}: {self.body}")


def _require_api_key():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set.")
    return api_key


def _normalize_base_url(base_url=None):
    return str(base_url or os.environ.get("DEEPSEEK_OPENAI_BASE_URL") or "https://api.deepseek.com").rstrip("/")


def _model_label(model, thinking):
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        return f"{model}(thinking)"
    return str(model)


def _message_content_to_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(value)


def _parse_usage(usage):
    usage = usage or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
        "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _build_body(
    messages,
    model,
    thinking,
    max_tokens,
    tools=None,
    tool_choice=None,
    response_format=None,
    stream=False,
    stream_options=None,
    user_id=None,
):
    body = {
        "model": model,
        "messages": messages,
        "thinking": thinking if isinstance(thinking, dict) else {"type": "disabled"},
        "max_tokens": max_tokens,
        "stream": bool(stream),
    }
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if response_format is not None:
        body["response_format"] = response_format
    if stream and stream_options is not None:
        body["stream_options"] = stream_options
    if user_id is not None:
        body["user"] = user_id
    return body


def _build_request(body, base_url=None):
    api_key = _require_api_key()
    endpoint = _normalize_base_url(base_url) + "/chat/completions"
    return urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )


def _read_http_error(exc):
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = str(exc)
    return DeepSeekAPIError(getattr(exc, "code", 500), body, getattr(exc, "url", ""))


def _with_retry(request, retry=None, stream=False):
    retry_policy = copy.deepcopy(retry or {})
    max_attempts = int(retry_policy.get("max_attempts") or 1)
    backoff_seconds = float(retry_policy.get("backoff_seconds") or 0)
    last_error = None
    for attempt_index in range(max_attempts):
        try:
            return urllib.request.urlopen(request, timeout=300)
        except urllib.error.HTTPError as exc:
            last_error = _read_http_error(exc)
        except Exception as exc:  # pragma: no cover - generic transport handling
            last_error = exc
        if stream or attempt_index + 1 >= max_attempts:
            break
        time.sleep(backoff_seconds * (2 ** attempt_index))
    raise last_error


def invoke_deepseek_chat_completion(
    messages,
    model,
    thinking,
    max_tokens,
    retry=None,
    tools=None,
    tool_choice=None,
    response_format=None,
    stream=False,
    stream_options=None,
    user_id=None,
    base_url=None,
):
    body = _build_body(
        messages=messages,
        model=model,
        thinking=thinking,
        max_tokens=max_tokens,
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
        stream=stream,
        stream_options=stream_options,
        user_id=user_id,
    )
    request = _build_request(body, base_url=base_url)
    with _with_retry(request, retry=retry, stream=stream) as upstream:
        payload = json.loads(upstream.read().decode("utf-8"))
    choice = ((payload.get("choices") or [{}])[0]) if isinstance(payload, dict) else {}
    message = choice.get("message") or {}
    usage = _parse_usage(payload.get("usage"))
    return {
        "content": _message_content_to_text(message.get("content")),
        "reasoning_content": _message_content_to_text(message.get("reasoning_content")),
        "tool_calls": copy.deepcopy(message.get("tool_calls") or []),
        "model": payload.get("model") or model,
        "model_label": _model_label(payload.get("model") or model, thinking),
        "finish_reason": choice.get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
        "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "raw_message": copy.deepcopy(message),
    }


def invoke_deepseek_messages(messages, model, thinking, max_tokens, retry=None):
    return invoke_deepseek_chat_completion(
        messages=messages,
        model=model,
        thinking=thinking,
        max_tokens=max_tokens,
        retry=retry,
    )


def _yield_sse_events(response):
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            yield "[DONE]"
            return
        yield json.loads(payload)


def stream_deepseek_chat_completion(
    messages,
    model,
    thinking,
    max_tokens,
    retry=None,
    tools=None,
    tool_choice=None,
    response_format=None,
    stream_options=None,
    user_id=None,
    base_url=None,
):
    body = _build_body(
        messages=messages,
        model=model,
        thinking=thinking,
        max_tokens=max_tokens,
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
        stream=True,
        stream_options=stream_options,
        user_id=user_id,
    )
    request = _build_request(body, base_url=base_url)
    with _with_retry(request, retry=retry, stream=True) as upstream:
        for event in _yield_sse_events(upstream):
            if event == "[DONE]":
                yield {"type": "done"}
                return
            if not isinstance(event, dict):
                continue
            if event.get("model"):
                yield {"type": "meta", "model": event.get("model")}
            choices = event.get("choices") or []
            if choices:
                choice = choices[0] or {}
                delta = choice.get("delta") or {}
                reasoning_piece = _message_content_to_text(delta.get("reasoning_content"))
                if reasoning_piece:
                    yield {"type": "reasoning_delta", "text": reasoning_piece}
                content_piece = _message_content_to_text(delta.get("content"))
                if content_piece:
                    yield {"type": "content_delta", "text": content_piece}
                for tool_call in delta.get("tool_calls") or []:
                    function_payload = tool_call.get("function") or {}
                    yield {
                        "type": "tool_call_delta",
                        "index": int(tool_call.get("index") or 0),
                        "id": tool_call.get("id"),
                        "name_delta": _message_content_to_text(function_payload.get("name")),
                        "arguments_delta": _message_content_to_text(function_payload.get("arguments")),
                    }
                if choice.get("finish_reason"):
                    yield {"type": "finish", "finish_reason": choice.get("finish_reason")}
            if event.get("usage"):
                yield {"type": "usage", "usage": _parse_usage(event.get("usage"))}
