# Managed by codex-deepseek-subagents
import argparse
import copy
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


TASK_TYPES = {"analysis", "execution", "review"}
AGENT_KINDS = {"codex_main", "deepseek_worker"}
DEFAULT_TASK_STORE = {"tasks": []}
try:
    DEFAULT_PORT = int("__PORT__")
except ValueError:
    DEFAULT_PORT = 4000


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
    payload = {"ts": utc_now(), **entry}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


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


def classify_error(message):
    text = str(message)
    if any(token in text for token in ("DEEPSEEK_API_KEY", "401", "403", "Unauthorized", "Invalid proxy authorization")):
        return "api_key_missing_or_invalid"
    if any(token in text for token in ("Connection refused", "actively refused", "No connection could be made", "Proxy is not running")):
        return "proxy_not_running"
    if any(token in text for token in ("Address already in use", "Only one usage of each socket address", "port")):
        return "port_in_use"
    if any(token in text for token in ("timed out", "Temporary failure", "Name or service not known", "SSL", "TLS", "urlopen error")):
        return "network_or_api_error"
    return "unknown_error"


class RuntimeState:
    def __init__(self, project_root, log_path, port, user_config_path, task_store_path):
        self.project_root = Path(project_root).resolve()
        self.log_path = str(Path(log_path))
        self.port = port
        self.user_config_path = Path(user_config_path)
        self.task_store_path = Path(task_store_path)
        self.config = self.load_user_config()
        self.agent_index = build_agent_index(self.config)
        self.task_store = self.load_task_store()

    def load_user_config(self):
        if not self.user_config_path.exists():
            raise RuntimeError(f"Missing user config: {self.user_config_path}")
        with self.user_config_path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
        return validate_user_config(config)

    def load_task_store(self):
        if not self.task_store_path.exists():
            self.task_store_path.parent.mkdir(parents=True, exist_ok=True)
            self.task_store_path.write_text(json.dumps(DEFAULT_TASK_STORE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return {"tasks": []}
        with self.task_store_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise RuntimeError(f"Invalid task store format: {self.task_store_path}")
        return data

    def save_task_store(self):
        self.task_store_path.parent.mkdir(parents=True, exist_ok=True)
        self.task_store_path.write_text(json.dumps(self.task_store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def find_task(self, task_id):
        for task in self.task_store["tasks"]:
            if task["task_id"] == task_id:
                return task
        return None

    def health(self):
        return {
            "ok": True,
            "service": "deepseek-scheduler",
            "port": self.port,
            "project_root": str(self.project_root),
            "user_config_path": str(self.user_config_path),
            "task_store_path": str(self.task_store_path),
            "agents": len([agent for agent in self.config["connected_agents"] if agent.get("enabled", True)]),
            "tasks": len(self.task_store["tasks"]),
        }

    def create_task(self, payload):
        task_type = str(payload.get("type") or "").strip()
        if task_type not in TASK_TYPES:
            raise ValueError("type must be one of: analysis, execution, review")

        description = str(payload.get("description") or "").strip()
        if not description:
            raise ValueError("description is required")

        assigned_agent = payload.get("assigned_agent") or default_agent_for_type(task_type, self.config)
        if assigned_agent not in self.agent_index:
            raise ValueError(f"assigned_agent is not registered or enabled: {assigned_agent}")

        task_id = str(payload.get("task_id") or f"task_{uuid4().hex}")
        if self.find_task(task_id):
            raise ValueError(f"task_id already exists: {task_id}")

        attempt = int(payload.get("attempt") or 0)
        task = {
            "task_id": task_id,
            "type": task_type,
            "description": description,
            "inputs": copy.deepcopy(payload.get("inputs") or []),
            "allowed_paths": copy.deepcopy(payload.get("allowed_paths") or []),
            "approval_scope": normalize_approval_scope(payload.get("approval_scope")),
            "assigned_agent": assigned_agent,
            "status": initial_status_for_agent(self.agent_index[assigned_agent]),
            "attempt": attempt,
            "parent_task_id": payload.get("parent_task_id"),
            "result": None,
            "usage": {},
            "timestamps": {
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "approved_at": None,
                "started_at": None,
                "completed_at": None,
                "failed_at": None,
            },
        }
        self.task_store["tasks"].append(task)
        self.save_task_store()
        append_log(self.log_path, {
            "kind": "task_event",
            "event": "created",
            "task_id": task["task_id"],
            "task_type": task["type"],
            "assigned_agent": task["assigned_agent"],
            "status": task["status"],
        })
        return task

    def approve_task(self, task_id, payload):
        task = self.find_task(task_id)
        if not task:
            raise KeyError(task_id)
        if task["assigned_agent"] not in self.agent_index:
            raise ValueError(f"assigned_agent is not registered: {task['assigned_agent']}")
        agent = self.agent_index[task["assigned_agent"]]
        if agent["kind"] != "deepseek_worker":
            raise ValueError("Only deepseek_worker tasks require scheduler approval dispatch.")
        if task["status"] not in {"awaiting_approval", "failed"}:
            raise ValueError(f"Task status does not allow approval: {task['status']}")

        approval_token = str(payload.get("approval_token") or "").strip()
        if not approval_token:
            raise ValueError("approval_token is required")

        scope = task.get("approval_scope") or {}
        scope["approved"] = True
        scope["approval_token_present"] = True
        scope["approval_note"] = payload.get("approval_note") or scope.get("approval_note") or ""
        task["approval_scope"] = scope
        task["timestamps"]["approved_at"] = utc_now()
        task["timestamps"]["updated_at"] = utc_now()
        append_log(self.log_path, {
            "kind": "task_event",
            "event": "approved",
            "task_id": task["task_id"],
            "task_type": task["type"],
            "assigned_agent": task["assigned_agent"],
        })
        self.dispatch_execution_task(task)
        self.save_task_store()
        return task

    def retry_task(self, task_id, payload):
        original = self.find_task(task_id)
        if not original:
            raise KeyError(task_id)
        if original["status"] not in {"failed", "waiting_for_codex"}:
            raise ValueError(f"Task status does not allow retry: {original['status']}")

        retry_payload = {
            "type": payload.get("type") or original["type"],
            "description": payload.get("description") or original["description"],
            "inputs": copy.deepcopy(payload.get("inputs") or original.get("inputs") or []),
            "allowed_paths": copy.deepcopy(payload.get("allowed_paths") or original.get("allowed_paths") or []),
            "approval_scope": copy.deepcopy(payload.get("approval_scope") or original.get("approval_scope") or {}),
            "assigned_agent": payload.get("assigned_agent") or original["assigned_agent"],
            "parent_task_id": original["task_id"],
            "attempt": int(original.get("attempt") or 0) + 1,
        }
        retry_task = self.create_task(retry_payload)
        append_log(self.log_path, {
            "kind": "task_event",
            "event": "retry_created",
            "task_id": retry_task["task_id"],
            "parent_task_id": original["task_id"],
            "attempt": retry_task["attempt"],
        })
        return retry_task

    def dispatch_execution_task(self, task):
        task["status"] = "running"
        task["timestamps"]["started_at"] = utc_now()
        task["timestamps"]["updated_at"] = utc_now()
        agent = self.agent_index[task["assigned_agent"]]
        mode = str((agent.get("defaults") or {}).get("mode") or "pro-thinking")
        try:
            result = invoke_deepseek_chat(task, mode=mode)
            task["result"] = {
                "content": result["content"],
                "model": result["model"],
                "model_label": result["model_label"],
                "finish_reason": result.get("finish_reason"),
                "reasoning_content_discarded": True,
            }
            task["usage"] = {
                "prompt_tokens": result.get("prompt_tokens"),
                "completion_tokens": result.get("completion_tokens"),
                "reasoning_tokens": result.get("reasoning_tokens"),
                "total_tokens": result.get("total_tokens"),
            }
            task["status"] = "success"
            task["timestamps"]["completed_at"] = utc_now()
            task["timestamps"]["updated_at"] = utc_now()
            append_log(self.log_path, {
                "kind": "task_event",
                "event": "completed",
                "task_id": task["task_id"],
                "task_type": task["type"],
                "assigned_agent": task["assigned_agent"],
                "status": task["status"],
                "prompt_tokens": result.get("prompt_tokens"),
                "completion_tokens": result.get("completion_tokens"),
                "reasoning_tokens": result.get("reasoning_tokens"),
                "total_tokens": result.get("total_tokens"),
                "model": result.get("model"),
                "model_label": result.get("model_label"),
            })
        except Exception as exc:
            task["status"] = "failed"
            task["result"] = {
                "error": str(exc),
                "error_category": classify_error(exc),
            }
            task["timestamps"]["failed_at"] = utc_now()
            task["timestamps"]["updated_at"] = utc_now()
            append_log(self.log_path, {
                "kind": "task_event",
                "event": "failed",
                "task_id": task["task_id"],
                "task_type": task["type"],
                "assigned_agent": task["assigned_agent"],
                "status": task["status"],
                "error": str(exc),
                "error_category": classify_error(exc),
            })


def normalize_approval_scope(scope):
    scope = copy.deepcopy(scope or {})
    if not isinstance(scope, dict):
        raise ValueError("approval_scope must be an object")
    normalized = {
        "summary": str(scope.get("summary") or ""),
        "files": copy.deepcopy(scope.get("files") or []),
        "exploration": str(scope.get("exploration") or "listed paths only"),
        "approved": bool(scope.get("approved") or False),
        "approval_note": str(scope.get("approval_note") or ""),
    }
    if scope.get("approval_token_present"):
        normalized["approval_token_present"] = True
    return normalized


def validate_user_config(config):
    if not isinstance(config, dict):
        raise ValueError("user_config.json must be a JSON object")
    if "deepseek_api_key" in config:
        raise ValueError("user_config.json must not contain deepseek_api_key")

    runtime = config.get("runtime") or {}
    if not isinstance(runtime, dict):
        raise ValueError("runtime must be an object")
    runtime.setdefault("port", DEFAULT_PORT)
    runtime.setdefault("log_level", "info")
    if not isinstance(runtime["port"], int):
        raise ValueError("runtime.port must be an integer")
    if not isinstance(runtime["log_level"], str):
        raise ValueError("runtime.log_level must be a string")

    connected_agents = config.get("connected_agents")
    if not isinstance(connected_agents, list) or not connected_agents:
        raise ValueError("connected_agents must be a non-empty array")

    normalized_agents = []
    for index, agent in enumerate(connected_agents):
        if not isinstance(agent, dict):
            raise ValueError(f"connected_agents[{index}] must be an object")
        kind = str(agent.get("kind") or "").strip()
        if kind not in AGENT_KINDS:
            raise ValueError(f"connected_agents[{index}].kind must be codex_main or deepseek_worker")
        name = str(agent.get("name") or "").strip()
        endpoint = str(agent.get("endpoint") or "").strip()
        if not name:
            raise ValueError(f"connected_agents[{index}].name is required")
        if not endpoint:
            raise ValueError(f"connected_agents[{index}].endpoint is required")
        capabilities = agent.get("capabilities") or []
        if not isinstance(capabilities, list):
            raise ValueError(f"connected_agents[{index}].capabilities must be an array")
        defaults = agent.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise ValueError(f"connected_agents[{index}].defaults must be an object")
        normalized_agents.append({
            "name": name,
            "kind": kind,
            "endpoint": endpoint,
            "enabled": bool(agent.get("enabled", True)),
            "capabilities": capabilities,
            "defaults": defaults,
        })

    defaults = config.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be an object")
    defaults.setdefault("execution_agent", next((agent["name"] for agent in normalized_agents if agent["kind"] == "deepseek_worker"), "DeepSeek Worker"))
    defaults.setdefault("review_agent", next((agent["name"] for agent in normalized_agents if agent["kind"] == "codex_main"), "Codex Main"))

    normalized = {
        "runtime": runtime,
        "connected_agents": normalized_agents,
        "defaults": defaults,
    }
    return normalized


def build_agent_index(config):
    index = {}
    for agent in config["connected_agents"]:
        if agent.get("enabled", True):
            index[agent["name"]] = agent
    return index


def default_agent_for_type(task_type, config):
    defaults = config["defaults"]
    if task_type in {"analysis", "review"}:
        return defaults["review_agent"]
    return defaults["execution_agent"]


def initial_status_for_agent(agent):
    if agent["kind"] == "codex_main":
        return "waiting_for_codex"
    return "awaiting_approval"


def build_task_prompt(task):
    lines = [
        "You are the DeepSeek execution worker.",
        f"Task type: {task['type']}",
        f"Description: {task['description']}",
    ]
    if task.get("allowed_paths"):
        lines.append("Allowed paths:")
        for path in task["allowed_paths"]:
            lines.append(f"- {path}")
    if task.get("approval_scope"):
        scope = task["approval_scope"]
        if scope.get("summary"):
            lines.append(f"Approved summary: {scope['summary']}")
        if scope.get("exploration"):
            lines.append(f"Approved exploration: {scope['exploration']}")
        if scope.get("files"):
            lines.append("Approved files:")
            for path in scope["files"]:
                lines.append(f"- {path}")
    if task.get("inputs"):
        lines.append("Inputs:")
        lines.append(json.dumps(task["inputs"], ensure_ascii=False, indent=2))
    lines.append("Return the execution result as plain text or JSON. Do not include hidden reasoning.")
    return "\n".join(lines)


def mode_to_model_spec(selected_mode):
    if selected_mode == "pro-thinking":
        return {
            "model": os.environ.get("DEEPSEEK_OPENAI_MODEL") or "__MODEL_SH__",
            "thinking": {"type": "enabled", "reasoning_effort": "high"},
        }
    if selected_mode == "flash-thinking":
        return {
            "model": os.environ.get("DEEPSEEK_OPENAI_FAST_MODEL") or os.environ.get("DEEPSEEK_OPENAI_MODEL") or "__FAST_MODEL_SH__",
            "thinking": {"type": "enabled", "reasoning_effort": "high"},
        }
    if selected_mode == "flash":
        return {
            "model": os.environ.get("DEEPSEEK_OPENAI_FAST_MODEL") or os.environ.get("DEEPSEEK_OPENAI_MODEL") or "__FAST_MODEL_SH__",
            "thinking": {"type": "disabled"},
        }
    return {
        "model": os.environ.get("DEEPSEEK_OPENAI_MODEL") or "__MODEL_SH__",
        "thinking": {"type": "disabled"} if selected_mode == "pro" else {"type": "enabled", "reasoning_effort": "high"},
    }


def invoke_deepseek_chat(task, mode):
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY is not set.")
    spec = mode_to_model_spec(mode)
    model = spec["model"]
    thinking = spec["thinking"]
    model_label = f"{model}(thinking)" if thinking["type"] == "enabled" else model
    body = {
        "model": model,
        "messages": [{"role": "user", "content": build_task_prompt(task)}],
        "thinking": thinking,
        "max_tokens": 2048,
        "stream": False,
    }
    base_url = os.environ.get("DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as upstream:
        data = json.loads(upstream.read().decode("utf-8"))
    message = data["choices"][0]["message"]
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "content": message.get("content"),
        "model": data.get("model"),
        "model_label": model_label,
        "finish_reason": data["choices"][0].get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def build_handler(state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DeepSeekScheduler/2.0"

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            try:
                self._do_get()
            except Exception as exc:
                append_log(state.log_path, {"kind": "runtime_error", "path": self.path, "error": str(exc)})
                write_json(self, 500, {"error": {"message": str(exc)}})

        def do_POST(self):
            try:
                self._do_post()
            except KeyError:
                write_json(self, 404, {"error": {"message": "Task not found"}})
            except ValueError as exc:
                write_json(self, 400, {"error": {"message": str(exc)}})
            except Exception as exc:
                append_log(state.log_path, {"kind": "runtime_error", "path": self.path, "error": str(exc)})
                write_json(self, 500, {"error": {"message": str(exc)}})

        def read_payload(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

        def _do_get(self):
            if self.path in {"/health", "/healthz"}:
                write_json(self, 200, state.health())
                return
            if self.path == "/v1/agents":
                write_json(self, 200, {
                    "data": list(state.agent_index.values()),
                    "defaults": state.config["defaults"],
                })
                return
            if self.path.startswith("/v1/tasks/"):
                task_id = self.path.split("/v1/tasks/", 1)[1]
                task = state.find_task(task_id)
                if not task:
                    raise KeyError(task_id)
                write_json(self, 200, task)
                return
            write_json(self, 404, {"error": {"message": "Not found"}})

        def _do_post(self):
            if self.path == "/v1/responses":
                return self.handle_responses()
            if self.path == "/v1/tasks":
                payload = self.read_payload()
                task = state.create_task(payload)
                write_json(self, 201, task)
                return
            if self.path.endswith("/approve"):
                task_id = self.path.rsplit("/", 2)[1]
                task = state.approve_task(task_id, self.read_payload())
                write_json(self, 200, task)
                return
            if self.path.endswith("/retry"):
                task_id = self.path.rsplit("/", 2)[1]
                task = state.retry_task(task_id, self.read_payload())
                write_json(self, 201, task)
                return
            write_json(self, 404, {"error": {"message": "Not found"}})

        def handle_responses(self):
            expected_key = os.environ.get("DEEPSEEK_PROXY_API_KEY")
            if expected_key and self.headers.get("Authorization") != f"Bearer {expected_key}":
                write_json(self, 401, {"error": {"message": "Invalid proxy authorization."}})
                return

            payload = self.read_payload()
            unsupported = []
            if payload.get("stream"):
                unsupported.append("stream=true")
            if payload.get("tools"):
                unsupported.append("tools")
            if payload.get("tool_choice"):
                unsupported.append("tool_choice")
            if unsupported:
                write_json(self, 400, {
                    "error": {
                        "message": (
                            "This scheduler smoke-test Responses endpoint does not implement: "
                            + ", ".join(unsupported)
                            + ". Use stream=false, no tools, or replace it with a production Responses-compatible proxy."
                        )
                    }
                })
                return

            model = str(payload.get("model") or os.environ.get("DEEPSEEK_OPENAI_MODEL") or "__MODEL_SH__")
            messages = response_input_to_messages(payload.get("input"))
            if not messages:
                messages = [{"role": "user", "content": "Respond with exactly: ok"}]

            effort = str((payload.get("metadata") or {}).get("deepseek_reasoning_effort") or os.environ.get("DEEPSEEK_THINKING_DEFAULT") or "__THINKING_DEFAULT_SH__")
            thinking = {"type": "disabled"} if effort in {"disabled", "none", "low-cost"} else {"type": "enabled", "reasoning_effort": effort}
            model_label = f"{model}(thinking)" if thinking["type"] == "enabled" else model
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
                append_log(state.log_path, {
                    "kind": "responses_error",
                    "path": self.path,
                    "upstream_error": str(exc),
                    "upstream_body": body,
                    "model": model,
                    "model_label": model_label,
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

            append_log(state.log_path, {
                "kind": "responses_usage",
                "path": self.path,
                "model": model,
                "model_label": model_label,
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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log-path", default=".codex/deepseek-proxy.log.jsonl")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--user-config", default="user_config.json")
    parser.add_argument("--task-store", default=".codex/runtime/task_queue.json")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    os.chdir(project_root)
    state = RuntimeState(
        project_root=project_root,
        log_path=str(Path(args.log_path)),
        port=args.port,
        user_config_path=(project_root / args.user_config),
        task_store_path=(project_root / args.task_store),
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), build_handler(state))
    print(f"DeepSeek scheduler listening on http://127.0.0.1:{args.port}/")
    print(f"Health: http://127.0.0.1:{args.port}/healthz")
    print(f"Log: {args.log_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
