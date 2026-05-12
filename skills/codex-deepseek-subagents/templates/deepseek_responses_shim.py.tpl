# Managed by codex-deepseek-subagents
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


def response_input_to_messages(response_input):
    if isinstance(response_input, str):
        return [{"role": "user", "content": response_input}] if response_input.strip() else []

    messages = []
    items = response_input if isinstance(response_input, list) else [response_input]
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role and content is not None:
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("text"):
                        parts.append(str(part["text"]))
                if parts:
                    messages.append({"role": role, "content": "\n".join(parts)})
        elif item.get("type") == "input_text" and item.get("text"):
            messages.append({"role": "user", "content": str(item["text"])})
        elif item.get("type") == "message" and item.get("role") and item.get("content"):
            messages.append({"role": str(item["role"]), "content": str(item["content"])})
    return messages


def write_json(handler, status, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def append_log(log_path, entry):
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def build_handler(log_path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DeepSeekResponsesShim/1.0"

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path == "/health":
                write_json(self, 200, {"ok": True, "service": "deepseek-responses-shim"})
                return
            write_json(self, 404, {"error": {"message": "Not found"}})

        def do_POST(self):
            try:
                self._do_post()
            except Exception as exc:
                append_log(log_path, {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "path": self.path,
                    "error": str(exc),
                })
                write_json(self, 500, {"error": {"message": str(exc)}})

        def _do_post(self):
            expected_key = os.environ.get("DEEPSEEK_PROXY_API_KEY")
            if expected_key and self.headers.get("Authorization") != f"Bearer {expected_key}":
                write_json(self, 401, {"error": {"message": "Invalid proxy authorization."}})
                return
            if self.path != "/v1/responses":
                write_json(self, 404, {"error": {"message": "Only POST /v1/responses is implemented by this smoke-test shim."}})
                return

            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            model = str(payload.get("model") or os.environ.get("DEEPSEEK_OPENAI_MODEL") or "__MODEL_SH__")
            messages = response_input_to_messages(payload.get("input"))
            if not messages:
                messages = [{"role": "user", "content": "Respond with exactly: ok"}]

            effort = str((payload.get("metadata") or {}).get("deepseek_reasoning_effort") or os.environ.get("DEEPSEEK_THINKING_DEFAULT") or "__THINKING_DEFAULT_SH__")
            thinking = {"type": "disabled"} if effort in {"disabled", "none", "low-cost"} else {"type": "enabled", "reasoning_effort": effort}
            chat_body = {
                "model": model,
                "messages": messages,
                "thinking": thinking,
                "max_tokens": int(payload.get("max_output_tokens") or 512),
                "stream": False,
            }

            api_key = os.environ.get("DEEPSEEK_API_KEY")
            if not api_key:
                raise RuntimeError("DEEPSEEK_API_KEY is not set. Run: source .codex/deepseek.local.env.sh")
            base_url = os.environ.get("DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/")
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(chat_body).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as upstream:
                    chat_response = json.loads(upstream.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                append_log(log_path, {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "path": self.path,
                    "upstream_error": str(exc),
                    "upstream_body": body,
                    "model": model,
                    "thinking_type": thinking["type"],
                    "message_count": len(messages),
                    "request_input_chars": sum(len(m.get("content", "")) for m in messages),
                })
                raise RuntimeError(str(exc)) from exc

            message = chat_response["choices"][0]["message"]
            content = str(message.get("content") or "")
            reasoning_content = str(message.get("reasoning_content") or "")
            usage = chat_response.get("usage") or {}
            details = usage.get("completion_tokens_details") or {}
            reasoning_tokens = details.get("reasoning_tokens")

            append_log(log_path, {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "path": self.path,
                "model": model,
                "thinking_type": thinking["type"],
                "reasoning_effort": thinking.get("reasoning_effort"),
                "request_input_chars": sum(len(m.get("content", "")) for m in messages),
                "message_count": len(messages),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": reasoning_tokens,
                "reasoning_chars_discarded": len(reasoning_content),
                "total_tokens": usage.get("total_tokens"),
            })

            response = {
                "id": chat_response.get("id") or f"resp_{uuid4().hex}",
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "error": None,
                "model": model,
                "output": [{
                    "id": f"msg_{uuid4().hex}",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content, "annotations": []}],
                }],
                "output_text": content,
                "usage": {
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "reasoning_tokens": reasoning_tokens,
                },
            }
            write_json(self, 200, response)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=__PORT__)
    parser.add_argument("--log-path", default=".codex/deepseek-proxy.log.jsonl")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), build_handler(args.log_path))
    print(f"DeepSeek Responses shim listening on http://127.0.0.1:{args.port}/")
    print(f"Log: {args.log_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()

