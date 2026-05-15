# Managed by codex-deepseek-subagents
import argparse
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


TASK_TYPES = {"analysis", "execution", "review"}
AGENT_KINDS = {"codex_main", "deepseek_worker"}
DEFAULT_TASK_STORE = {"tasks": []}
SUPPORTED_NATIVE_TOOLS = (
    "repo_list_files",
    "repo_read_file",
    "repo_search_text",
    "repo_apply_patch",
    "repo_write_file",
    "repo_delete_file",
)
DEFAULT_ALLOWED_TOOLS = [
    "repo_list_files",
    "repo_read_file",
    "repo_search_text",
    "repo_apply_patch",
    "repo_write_file",
]
DEFAULT_READ_EXTENSIONS = [
    "",
    ".py",
    ".md",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".sh",
    ".ps1",
    ".go",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".css",
    ".html",
    ".rs",
    ".java",
]
DEFAULT_WRITE_EXTENSIONS = [
    ".py",
    ".md",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".sh",
    ".ps1",
    ".go",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".css",
    ".html",
    ".rs",
    ".java",
]
DEFAULT_MAX_TOOL_STEPS = 12
try:
    DEFAULT_PORT = int("__PORT__")
except ValueError:
    DEFAULT_PORT = 4000


class PolicyError(RuntimeError):
    pass


class TaskConflictError(RuntimeError):
    pass


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
    if isinstance(message, PolicyError):
        return "failed_policy"
    if isinstance(message, TaskConflictError):
        return "task_conflict"
    return "unknown_error"


def normalize_rel_path(path_value):
    path = str(path_value or "").replace("\\", "/").strip()
    path = path.lstrip("./")
    while "//" in path:
        path = path.replace("//", "/")
    return path.rstrip("/") or "."


def normalize_extensions(values, field_name):
    if values is None:
        return None
    if not isinstance(values, list) or not values:
        raise ValueError(f"{field_name} must be a non-empty array")
    normalized = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must contain strings")
        entry = item.strip()
        if entry and not entry.startswith("."):
            raise ValueError(f"{field_name} entries must start with '.' or be an empty string")
        normalized.append(entry)
    return normalized


def normalize_allowed_tools(values):
    if values is None:
        return None
    if not isinstance(values, list) or not values:
        raise ValueError("allowed_tools must be a non-empty array")
    normalized = []
    for tool_name in values:
        name = str(tool_name or "").strip()
        if name not in SUPPORTED_NATIVE_TOOLS:
            raise ValueError(f"Unsupported tool in allowed_tools: {name}")
        if name not in normalized:
            normalized.append(name)
    return normalized


def normalize_tool_policy(policy=None, base=None):
    base_policy = copy.deepcopy(base or {})
    incoming = copy.deepcopy(policy or {})
    if not isinstance(incoming, dict):
        raise ValueError("tool_policy must be an object")

    merged = {
        "allowed_tools": copy.deepcopy(base_policy.get("allowed_tools") or DEFAULT_ALLOWED_TOOLS),
        "allowed_paths": copy.deepcopy(base_policy.get("allowed_paths") or []),
        "read_extensions": copy.deepcopy(base_policy.get("read_extensions") or DEFAULT_READ_EXTENSIONS),
        "write_extensions": copy.deepcopy(base_policy.get("write_extensions") or DEFAULT_WRITE_EXTENSIONS),
        "max_file_read_bytes": int(base_policy.get("max_file_read_bytes") or 262144),
        "max_search_results": int(base_policy.get("max_search_results") or 50),
        "max_tool_steps": int(base_policy.get("max_tool_steps") or DEFAULT_MAX_TOOL_STEPS),
        "allow_full_rewrite": bool(base_policy.get("allow_full_rewrite") or False),
        "allow_delete": bool(base_policy.get("allow_delete") or False),
    }

    if "allowed_tools" in incoming:
        merged["allowed_tools"] = incoming["allowed_tools"]
    if "allowed_paths" in incoming:
        merged["allowed_paths"] = incoming["allowed_paths"]
    if "read_extensions" in incoming:
        merged["read_extensions"] = incoming["read_extensions"]
    if "write_extensions" in incoming:
        merged["write_extensions"] = incoming["write_extensions"]
    if "max_file_read_bytes" in incoming:
        merged["max_file_read_bytes"] = incoming["max_file_read_bytes"]
    if "max_search_results" in incoming:
        merged["max_search_results"] = incoming["max_search_results"]
    if "max_tool_steps" in incoming:
        merged["max_tool_steps"] = incoming["max_tool_steps"]
    if "allow_full_rewrite" in incoming:
        merged["allow_full_rewrite"] = incoming["allow_full_rewrite"]
    if "allow_delete" in incoming:
        merged["allow_delete"] = incoming["allow_delete"]

    allowed_tools = normalize_allowed_tools(merged.get("allowed_tools")) or copy.deepcopy(DEFAULT_ALLOWED_TOOLS)
    if "repo_delete_file" in allowed_tools:
        merged["allow_delete"] = True
    if merged["allow_delete"] and "repo_delete_file" not in allowed_tools:
        allowed_tools.append("repo_delete_file")
    merged["allowed_tools"] = allowed_tools

    allowed_paths = merged.get("allowed_paths") or []
    if not isinstance(allowed_paths, list):
        raise ValueError("allowed_paths must be an array")
    merged["allowed_paths"] = [normalize_rel_path(path) for path in allowed_paths if str(path or "").strip()]

    merged["read_extensions"] = normalize_extensions(merged.get("read_extensions"), "read_extensions") or copy.deepcopy(DEFAULT_READ_EXTENSIONS)
    merged["write_extensions"] = normalize_extensions(merged.get("write_extensions"), "write_extensions") or copy.deepcopy(DEFAULT_WRITE_EXTENSIONS)
    merged["max_file_read_bytes"] = int(merged["max_file_read_bytes"])
    merged["max_search_results"] = int(merged["max_search_results"])
    merged["max_tool_steps"] = int(merged["max_tool_steps"])
    merged["allow_full_rewrite"] = bool(merged["allow_full_rewrite"])
    merged["allow_delete"] = bool(merged["allow_delete"])

    if merged["max_file_read_bytes"] <= 0:
        raise ValueError("max_file_read_bytes must be positive")
    if merged["max_search_results"] <= 0:
        raise ValueError("max_search_results must be positive")
    if merged["max_tool_steps"] <= 0:
        raise ValueError("max_tool_steps must be positive")

    return merged


def normalize_approval_scope(scope, tool_policy=None):
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
    if tool_policy:
        normalized["allowed_tools"] = copy.deepcopy(tool_policy.get("allowed_tools") or [])
        normalized["allowed_paths"] = copy.deepcopy(tool_policy.get("allowed_paths") or [])
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
    defaults.setdefault(
        "execution_agent",
        next((agent["name"] for agent in normalized_agents if agent["kind"] == "deepseek_worker"), "DeepSeek Worker"),
    )
    defaults.setdefault(
        "review_agent",
        next((agent["name"] for agent in normalized_agents if agent["kind"] == "codex_main"), "Codex Main"),
    )
    defaults["tool_policy"] = normalize_tool_policy(defaults.get("tool_policy") or {
        "allowed_tools": defaults.get("default_allowed_tools") or DEFAULT_ALLOWED_TOOLS,
        "allowed_paths": defaults.get("allowed_paths") or [],
        "read_extensions": defaults.get("read_extensions") or DEFAULT_READ_EXTENSIONS,
        "write_extensions": defaults.get("write_extensions") or DEFAULT_WRITE_EXTENSIONS,
        "max_file_read_bytes": defaults.get("max_file_read_bytes") or 262144,
        "max_search_results": defaults.get("max_search_results") or 50,
        "max_tool_steps": defaults.get("max_tool_steps") or DEFAULT_MAX_TOOL_STEPS,
    })

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


def runtime_capabilities():
    return {
        "text_delegate_ready": True,
        "native_tool_agent_ready": True,
        "responses_smoke_test": True,
        "responses_tool_calling": True,
        "supported_tools": list(SUPPORTED_NATIVE_TOOLS),
        "stream_supported": False,
        "shell_supported": False,
        "unsupported_responses_features": ["stream=true"],
        "native_tool_agent_note": "Execution tasks can run approved local repository tools through the scheduler. Shell command execution is intentionally disabled in v1.",
    }


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
    if task.get("tool_policy") and task["execution_mode"] == "native_tools":
        policy = task["tool_policy"]
        lines.append("Approved tool policy:")
        lines.append(json.dumps({
            "allowed_tools": policy["allowed_tools"],
            "allowed_paths": policy["allowed_paths"],
            "read_extensions": policy["read_extensions"],
            "write_extensions": policy["write_extensions"],
            "allow_full_rewrite": policy["allow_full_rewrite"],
            "allow_delete": policy["allow_delete"],
        }, ensure_ascii=False, indent=2))
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


def invoke_deepseek_messages(messages, mode, max_tokens):
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY is not set.")
    spec = mode_to_model_spec(mode)
    model = spec["model"]
    thinking = spec["thinking"]
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
    with urllib.request.urlopen(req, timeout=300) as upstream:
        data = json.loads(upstream.read().decode("utf-8"))
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


def invoke_deepseek_chat(task, mode):
    return invoke_deepseek_messages(
        [{"role": "user", "content": build_task_prompt(task)}],
        mode=mode,
        max_tokens=2048,
    )


def parse_jsonish_object(content):
    text = str(content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_tool_loop_response(content):
    parsed = parse_jsonish_object(content)
    if not parsed:
        return {"type": "final", "content": str(content or "").strip()}
    response_type = str(parsed.get("type") or "").strip()
    if response_type == "final":
        return {"type": "final", "content": str(parsed.get("content") or "")}
    if response_type == "tool_call":
        tool_name = str(parsed.get("tool_name") or parsed.get("name") or "").strip()
        arguments = parsed.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("tool_call.arguments must be an object")
        return {"type": "tool_call", "tool_name": tool_name, "arguments": arguments}
    if parsed.get("tool_name") or parsed.get("name"):
        arguments = parsed.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("tool_call.arguments must be an object")
        return {
            "type": "tool_call",
            "tool_name": str(parsed.get("tool_name") or parsed.get("name")),
            "arguments": arguments,
        }
    return {"type": "final", "content": str(parsed.get("content") or str(content or "").strip())}


def coerce_path_argument(arguments, key="path"):
    value = arguments.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    return normalize_rel_path(value)


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
        with self.user_config_path.open("r", encoding="utf-8-sig") as handle:
            config = json.load(handle)
        return validate_user_config(config)

    def load_task_store(self):
        if not self.task_store_path.exists():
            self.task_store_path.parent.mkdir(parents=True, exist_ok=True)
            self.task_store_path.write_text(json.dumps(DEFAULT_TASK_STORE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return {"tasks": []}
        with self.task_store_path.open("r", encoding="utf-8-sig") as handle:
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
            "capabilities": runtime_capabilities(),
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

        tool_policy = normalize_tool_policy(payload.get("tool_policy") or {}, self.config["defaults"].get("tool_policy"))
        execution_mode = "native_tools" if task_type == "execution" and payload.get("tool_policy") else "text_delegate"
        if payload.get("execution_mode"):
            requested_mode = str(payload["execution_mode"]).strip()
            if requested_mode not in {"text_delegate", "native_tools"}:
                raise ValueError("execution_mode must be text_delegate or native_tools")
            execution_mode = requested_mode
        if execution_mode == "text_delegate":
            tool_policy = normalize_tool_policy({}, self.config["defaults"].get("tool_policy"))

        approval_scope = normalize_approval_scope(payload.get("approval_scope"), tool_policy if execution_mode == "native_tools" else None)
        allowed_paths = copy.deepcopy(payload.get("allowed_paths") or tool_policy.get("allowed_paths") or [])
        attempt = int(payload.get("attempt") or 0)
        task = {
            "task_id": task_id,
            "type": task_type,
            "description": description,
            "inputs": copy.deepcopy(payload.get("inputs") or []),
            "allowed_paths": copy.deepcopy(allowed_paths),
            "approval_scope": approval_scope,
            "tool_policy": tool_policy,
            "execution_mode": execution_mode,
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
            "execution_mode": task["execution_mode"],
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

        if payload.get("tool_policy") is not None:
            task["tool_policy"] = normalize_tool_policy(payload.get("tool_policy") or {}, self.config["defaults"].get("tool_policy"))

        scope = task.get("approval_scope") or {}
        scope["approved"] = True
        scope["approval_token_present"] = True
        scope["approval_note"] = payload.get("approval_note") or scope.get("approval_note") or ""
        if task["execution_mode"] == "native_tools":
            scope["allowed_tools"] = copy.deepcopy(task["tool_policy"]["allowed_tools"])
            scope["allowed_paths"] = copy.deepcopy(task["tool_policy"]["allowed_paths"])
        task["approval_scope"] = scope
        task["timestamps"]["approved_at"] = utc_now()
        task["timestamps"]["updated_at"] = utc_now()
        append_log(self.log_path, {
            "kind": "task_event",
            "event": "approved",
            "task_id": task["task_id"],
            "task_type": task["type"],
            "assigned_agent": task["assigned_agent"],
            "execution_mode": task["execution_mode"],
        })

        if task["execution_mode"] == "native_tools":
            task["status"] = "approved"
            self.save_task_store()
            return task

        self.save_task_store()
        self.dispatch_execution_task(task)
        self.save_task_store()
        return task

    def retry_task(self, task_id, payload):
        original = self.find_task(task_id)
        if not original:
            raise KeyError(task_id)
        if original["status"] not in {"failed", "failed_policy", "waiting_for_codex", "success"}:
            raise ValueError(f"Task status does not allow retry: {original['status']}")

        retry_payload = {
            "type": payload.get("type") or original["type"],
            "description": payload.get("description") or original["description"],
            "inputs": copy.deepcopy(payload.get("inputs") or original.get("inputs") or []),
            "allowed_paths": copy.deepcopy(payload.get("allowed_paths") or original.get("allowed_paths") or []),
            "approval_scope": copy.deepcopy(payload.get("approval_scope") or original.get("approval_scope") or {}),
            "tool_policy": copy.deepcopy(payload.get("tool_policy") or original.get("tool_policy") or {}),
            "execution_mode": payload.get("execution_mode") or original.get("execution_mode") or "text_delegate",
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
        self.save_task_store()
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
                "execution_mode": task["execution_mode"],
                "prompt_tokens": result.get("prompt_tokens"),
                "completion_tokens": result.get("completion_tokens"),
                "reasoning_tokens": result.get("reasoning_tokens"),
                "total_tokens": result.get("total_tokens"),
                "model": result.get("model"),
                "model_label": result.get("model_label"),
            })
            self.save_task_store()
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
                "execution_mode": task["execution_mode"],
                "error": str(exc),
                "error_category": classify_error(exc),
            })
            self.save_task_store()

    def require_task_for_native_tools(self, task_id):
        if not task_id:
            raise ValueError("metadata.scheduler_task_id is required for tools mode")
        task = self.find_task(task_id)
        if not task:
            raise KeyError(task_id)
        if task["type"] != "execution":
            raise ValueError("scheduler_task_id must reference an execution task")
        if task["execution_mode"] != "native_tools":
            raise ValueError("scheduler_task_id is not configured for native tool execution")
        if not task.get("approval_scope", {}).get("approved"):
            raise PolicyError("Execution task has not been approved")
        if task["status"] not in {"approved", "running", "success"}:
            raise TaskConflictError(f"Task status does not allow native tool execution: {task['status']}")
        return task

    def begin_native_tool_session(self, task):
        if task["status"] == "success":
            raise TaskConflictError("Task already completed successfully")
        task["status"] = "running"
        if not task["timestamps"]["started_at"]:
            task["timestamps"]["started_at"] = utc_now()
        task["timestamps"]["updated_at"] = utc_now()
        self.save_task_store()

    def fail_native_tool_session(self, task, exc):
        task["status"] = "failed_policy" if isinstance(exc, PolicyError) else "failed"
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
            "execution_mode": task["execution_mode"],
            "error": str(exc),
            "error_category": classify_error(exc),
        })
        self.save_task_store()

    def complete_native_tool_session(self, task, result, usage, tool_steps):
        task["status"] = "success"
        task["result"] = {
            "content": result["content"],
            "model": result["model"],
            "model_label": result["model_label"],
            "finish_reason": result.get("finish_reason"),
            "reasoning_content_discarded": True,
            "tool_steps": tool_steps,
        }
        task["usage"] = usage
        task["timestamps"]["completed_at"] = utc_now()
        task["timestamps"]["updated_at"] = utc_now()
        append_log(self.log_path, {
            "kind": "task_event",
            "event": "completed",
            "task_id": task["task_id"],
            "task_type": task["type"],
            "assigned_agent": task["assigned_agent"],
            "status": task["status"],
            "execution_mode": task["execution_mode"],
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "model": result.get("model"),
            "model_label": result.get("model_label"),
            "tool_step_count": len(tool_steps),
        })
        self.save_task_store()

    def allowed_tool_names_for_response(self, payload_tools, tool_policy):
        requested = []
        for entry in payload_tools or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "function":
                function_def = entry.get("function") if isinstance(entry.get("function"), dict) else entry
                name = str(function_def.get("name") or "").strip()
            else:
                name = str(entry.get("name") or "").strip()
            if name:
                requested.append(name)
        allowed = [name for name in requested if name in tool_policy["allowed_tools"]]
        if not allowed:
            raise ValueError("No supported scheduler native tools were requested for this task")
        return allowed

    def effective_tool_policy(self, tool_policy):
        return normalize_tool_policy(tool_policy or {}, self.config["defaults"].get("tool_policy"))

    def resolve_repo_path(self, rel_path):
        normalized = normalize_rel_path(rel_path)
        resolved = (self.project_root / normalized).resolve(strict=False)
        try:
            resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise PolicyError(f"Path escapes project_root: {normalized}") from exc
        return normalized, resolved

    def path_within_allowed(self, rel_path, allowed_paths):
        if not allowed_paths:
            return False
        for allowed in allowed_paths:
            base = normalize_rel_path(allowed)
            if base == ".":
                return True
            if rel_path == base or rel_path.startswith(base + "/"):
                return True
        return False

    def ensure_path_allowed(self, rel_path, tool_policy, mode):
        _, _ = self.resolve_repo_path(rel_path)
        if not self.path_within_allowed(rel_path, tool_policy["allowed_paths"]):
            raise PolicyError(f"Path is outside approved scope: {rel_path}")
        ext = Path(rel_path).suffix
        allowed_exts = tool_policy["read_extensions"] if mode == "read" else tool_policy["write_extensions"]
        if ext not in allowed_exts:
            raise PolicyError(f"{mode} extension is not allowed for: {rel_path}")

    def read_file(self, rel_path, tool_policy):
        tool_policy = self.effective_tool_policy(tool_policy)
        self.ensure_path_allowed(rel_path, tool_policy, "read")
        _, resolved = self.resolve_repo_path(rel_path)
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(rel_path)
        size = resolved.stat().st_size
        if size > tool_policy["max_file_read_bytes"]:
            raise PolicyError(f"File exceeds max_file_read_bytes: {rel_path}")
        return resolved.read_text(encoding="utf-8")

    def list_files(self, directory, tool_policy):
        tool_policy = self.effective_tool_policy(tool_policy)
        rel_dir = normalize_rel_path(directory or ".")
        if rel_dir != ".":
            self.ensure_path_allowed(rel_dir, tool_policy, "read")
        _, resolved = self.resolve_repo_path(rel_dir)
        if not resolved.exists():
            raise FileNotFoundError(rel_dir)
        results = []
        for path in resolved.rglob("*"):
            if path.is_file():
                rel = normalize_rel_path(path.relative_to(self.project_root).as_posix())
                if self.path_within_allowed(rel, tool_policy["allowed_paths"]):
                    results.append(rel)
            if len(results) >= tool_policy["max_search_results"]:
                break
        return results

    def search_text(self, query, tool_policy):
        tool_policy = self.effective_tool_policy(tool_policy)
        if not str(query or "").strip():
            raise ValueError("query is required")
        matches = []
        seen = set()
        search_roots = tool_policy["allowed_paths"] or ["."]
        for root in search_roots:
            _, resolved_root = self.resolve_repo_path(root)
            if resolved_root.is_file():
                paths = [resolved_root]
            elif resolved_root.exists():
                paths = [path for path in resolved_root.rglob("*") if path.is_file()]
            else:
                continue
            for path in paths:
                rel = normalize_rel_path(path.relative_to(self.project_root).as_posix())
                if rel in seen:
                    continue
                seen.add(rel)
                try:
                    self.ensure_path_allowed(rel, tool_policy, "read")
                    content = self.read_file(rel, tool_policy)
                except Exception:
                    continue
                for line_number, line in enumerate(content.splitlines(), start=1):
                    if query in line:
                        matches.append({
                            "path": rel,
                            "line_number": line_number,
                            "line": line,
                        })
                        if len(matches) >= tool_policy["max_search_results"]:
                            return matches
        return matches

    def write_file(self, rel_path, content, tool_policy, create_only=False):
        tool_policy = self.effective_tool_policy(tool_policy)
        self.ensure_path_allowed(rel_path, tool_policy, "write")
        _, resolved = self.resolve_repo_path(rel_path)
        if resolved.exists() and create_only:
            raise PolicyError(f"File already exists and create_only is enforced: {rel_path}")
        if resolved.exists() and not tool_policy["allow_full_rewrite"]:
            raise PolicyError(f"Full file rewrite is not approved for: {rel_path}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(str(content), encoding="utf-8")
        return {"path": rel_path, "bytes_written": len(str(content).encode("utf-8"))}

    def delete_file(self, rel_path, tool_policy):
        tool_policy = self.effective_tool_policy(tool_policy)
        if not tool_policy["allow_delete"] or "repo_delete_file" not in tool_policy["allowed_tools"]:
            raise PolicyError("Delete is not approved for this task")
        self.ensure_path_allowed(rel_path, tool_policy, "write")
        _, resolved = self.resolve_repo_path(rel_path)
        if not resolved.exists():
            raise FileNotFoundError(rel_path)
        resolved.unlink()
        return {"path": rel_path, "deleted": True}

    def apply_patch(self, patch_text, tool_policy):
        tool_policy = self.effective_tool_policy(tool_policy)
        files = parse_unified_diff(str(patch_text or ""))
        if not files:
            raise ValueError("patch is empty or invalid")
        results = []
        for file_patch in files:
            rel_path = normalize_rel_path(file_patch["path"])
            self.ensure_path_allowed(rel_path, tool_policy, "write")
            _, resolved = self.resolve_repo_path(rel_path)
            original = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
            if not resolved.exists() and file_patch["old_path"] is not None:
                raise FileNotFoundError(rel_path)
            updated = apply_unified_patch_to_text(original, file_patch["hunks"])
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(updated, encoding="utf-8")
            results.append({"path": rel_path, "bytes_written": len(updated.encode("utf-8"))})
        return {"updated_files": results}

    def execute_native_tool(self, task, tool_name, arguments):
        tool_policy = self.effective_tool_policy(task["tool_policy"])
        if tool_name not in tool_policy["allowed_tools"]:
            raise PolicyError(f"Tool is not approved for this task: {tool_name}")
        if tool_name == "repo_list_files":
            return self.list_files(arguments.get("directory") or ".", tool_policy)
        if tool_name == "repo_read_file":
            return self.read_file(coerce_path_argument(arguments), tool_policy)
        if tool_name == "repo_search_text":
            return self.search_text(arguments.get("query"), tool_policy)
        if tool_name == "repo_apply_patch":
            return self.apply_patch(arguments.get("patch"), tool_policy)
        if tool_name == "repo_write_file":
            rel_path = coerce_path_argument(arguments)
            create_only = bool(arguments.get("create_only", True))
            return self.write_file(rel_path, arguments.get("content") or "", tool_policy, create_only=create_only)
        if tool_name == "repo_delete_file":
            return self.delete_file(coerce_path_argument(arguments), tool_policy)
        raise ValueError(f"Unsupported native tool: {tool_name}")


def strip_diff_path(token):
    value = str(token or "").strip().split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    return normalize_rel_path(value)


def parse_unified_diff(patch_text):
    lines = patch_text.splitlines()
    files = []
    index = 0
    current = None
    current_hunk = None
    hunk_pattern = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- "):
            old_path = strip_diff_path(line[4:])
            index += 1
            if index >= len(lines) or not lines[index].startswith("+++ "):
                raise ValueError("Unified diff is missing +++ header")
            new_path = strip_diff_path(lines[index][4:])
            path = new_path or old_path
            current = {"path": path, "old_path": old_path, "new_path": new_path, "hunks": []}
            files.append(current)
            current_hunk = None
            index += 1
            continue
        match = hunk_pattern.match(line)
        if match:
            if current is None:
                raise ValueError("Unified diff hunk appeared before file header")
            current_hunk = {"start": int(match.group(2)), "lines": []}
            current["hunks"].append(current_hunk)
            index += 1
            continue
        if current_hunk is not None:
            if line.startswith(("\\ No newline",)):
                index += 1
                continue
            if line[:1] in {" ", "+", "-"}:
                current_hunk["lines"].append(line)
                index += 1
                continue
        index += 1
    return files


def apply_unified_patch_to_text(original_text, hunks):
    newline = "\r\n" if "\r\n" in original_text else "\n"
    had_trailing_newline = original_text.endswith("\n") or original_text.endswith("\r\n")
    original_lines = original_text.splitlines()
    cursor = 0
    result = []
    for hunk in hunks:
        start = max(hunk["start"] - 1, 0)
        if start < cursor:
            raise ValueError("Overlapping hunks are not supported")
        result.extend(original_lines[cursor:start])
        cursor = start
        for line in hunk["lines"]:
            prefix = line[:1]
            value = line[1:]
            if prefix == " ":
                if cursor >= len(original_lines) or original_lines[cursor] != value:
                    raise ValueError("Patch context mismatch")
                result.append(original_lines[cursor])
                cursor += 1
            elif prefix == "-":
                if cursor >= len(original_lines) or original_lines[cursor] != value:
                    raise ValueError("Patch removal mismatch")
                cursor += 1
            elif prefix == "+":
                result.append(value)
        continue
    result.extend(original_lines[cursor:])
    updated = newline.join(result)
    if result and had_trailing_newline:
        updated += newline
    return updated


def build_native_tool_messages(task, user_messages, allowed_tool_names):
    tool_descriptions = {
        "repo_list_files": "List files under an approved directory. Arguments: {\"directory\": \".\"}",
        "repo_read_file": "Read one approved file. Arguments: {\"path\": \"src/app.py\"}",
        "repo_search_text": "Search approved files for exact text. Arguments: {\"query\": \"needle\"}",
        "repo_apply_patch": "Apply a unified diff patch to approved writable files. Arguments: {\"patch\": \"--- a/file\\n+++ b/file\\n@@ ...\"}",
        "repo_write_file": "Create a new approved file, or rewrite one file only when explicitly approved. Arguments: {\"path\": \"notes.txt\", \"content\": \"...\", \"create_only\": true}",
        "repo_delete_file": "Delete an approved file only when delete is explicitly approved. Arguments: {\"path\": \"old.txt\"}",
    }
    system_lines = [
        "You are the DeepSeek native repository worker.",
        "You must operate only through approved tools and approved paths.",
        "Reply with JSON only.",
        "When you need a tool, output:",
        "{\"type\":\"tool_call\",\"tool_name\":\"repo_read_file\",\"arguments\":{\"path\":\"...\"}}",
        "When you are done, output:",
        "{\"type\":\"final\",\"content\":\"...\"}",
        "Never emit hidden reasoning. Never request shell access.",
        f"Task description: {task['description']}",
        f"Approved paths: {json.dumps(task['tool_policy']['allowed_paths'], ensure_ascii=False)}",
        f"Approved tools: {json.dumps(allowed_tool_names, ensure_ascii=False)}",
    ]
    for tool_name in allowed_tool_names:
        system_lines.append(f"{tool_name}: {tool_descriptions[tool_name]}")
    messages = [{"role": "system", "content": "\n".join(system_lines)}]
    messages.extend(user_messages or [{"role": "user", "content": build_task_prompt(task)}])
    return messages


def build_response_output(content):
    return [{
        "id": f"msg_{uuid4().hex}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": content, "annotations": []}],
    }]


def build_handler(state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DeepSeekScheduler/3.0"

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
            except PolicyError as exc:
                write_json(self, 403, {"error": {"message": str(exc)}})
            except TaskConflictError as exc:
                write_json(self, 409, {"error": {"message": str(exc)}})
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
                    "capabilities": runtime_capabilities(),
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

        def authorize_response(self):
            expected_key = os.environ.get("DEEPSEEK_PROXY_API_KEY")
            if expected_key and self.headers.get("Authorization") != f"Bearer {expected_key}":
                write_json(self, 401, {"error": {"message": "Invalid proxy authorization."}})
                return False
            return True

        def handle_responses(self):
            if not self.authorize_response():
                return

            payload = self.read_payload()
            if payload.get("stream"):
                write_json(self, 400, {
                    "error": {
                        "message": "This scheduler Responses endpoint does not implement stream=true. Use synchronous mode only."
                    }
                })
                return

            if payload.get("tools"):
                return self.handle_native_tools_response(payload)
            return self.handle_text_response(payload)

        def handle_text_response(self, payload):
            model = str(payload.get("model") or os.environ.get("DEEPSEEK_OPENAI_MODEL") or "__MODEL_SH__")
            messages = response_input_to_messages(payload.get("input"))
            if not messages:
                messages = [{"role": "user", "content": "Respond with exactly: ok"}]

            effort = str((payload.get("metadata") or {}).get("deepseek_reasoning_effort") or os.environ.get("DEEPSEEK_THINKING_DEFAULT") or "__THINKING_DEFAULT_SH__")
            thinking_mode = "pro" if effort in {"disabled", "none", "low-cost"} else "pro-thinking"
            result = invoke_deepseek_messages(messages, mode=thinking_mode, max_tokens=int(payload.get("max_output_tokens") or 512))
            content = str(result.get("content") or "")
            reasoning_content = str(result.get("reasoning_content") or "")
            append_log(state.log_path, {
                "kind": "responses_usage",
                "path": self.path,
                "model": model,
                "model_label": result["model_label"],
                "thinking_type": "disabled" if thinking_mode == "pro" else "enabled",
                "request_input_chars": sum(len(m.get("content", "")) for m in messages),
                "message_count": len(messages),
                "prompt_tokens": result.get("prompt_tokens"),
                "completion_tokens": result.get("completion_tokens"),
                "reasoning_tokens": result.get("reasoning_tokens"),
                "reasoning_chars_discarded": len(reasoning_content),
                "total_tokens": result.get("total_tokens"),
                "mode": "text_delegate",
            })
            response = {
                "id": f"resp_{uuid4().hex}",
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "error": None,
                "model": model,
                "output": build_response_output(content),
                "output_text": content,
                "usage": {
                    "input_tokens": result.get("prompt_tokens"),
                    "output_tokens": result.get("completion_tokens"),
                    "total_tokens": result.get("total_tokens"),
                    "reasoning_tokens": result.get("reasoning_tokens"),
                },
            }
            write_json(self, 200, response)

        def handle_native_tools_response(self, payload):
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            task_id = metadata.get("scheduler_task_id")
            task = state.require_task_for_native_tools(task_id)
            allowed_tool_names = state.allowed_tool_names_for_response(payload.get("tools"), task["tool_policy"])
            if "shell_command" in allowed_tool_names:
                raise ValueError("shell_command is intentionally disabled in this runtime")

            state.begin_native_tool_session(task)
            model = str(payload.get("model") or os.environ.get("DEEPSEEK_OPENAI_MODEL") or "__MODEL_SH__")
            user_messages = response_input_to_messages(payload.get("input"))
            messages = build_native_tool_messages(task, user_messages, allowed_tool_names)
            agent = state.agent_index[task["assigned_agent"]]
            mode = str((agent.get("defaults") or {}).get("mode") or "pro-thinking")
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
            tool_steps = []

            try:
                for step in range(task["tool_policy"]["max_tool_steps"]):
                    result = invoke_deepseek_messages(messages, mode=mode, max_tokens=int(payload.get("max_output_tokens") or 1024))
                    usage["prompt_tokens"] += int(result.get("prompt_tokens") or 0)
                    usage["completion_tokens"] += int(result.get("completion_tokens") or 0)
                    usage["reasoning_tokens"] += int(result.get("reasoning_tokens") or 0)
                    usage["total_tokens"] += int(result.get("total_tokens") or 0)

                    parsed = parse_tool_loop_response(result.get("content"))
                    if parsed["type"] == "final":
                        append_log(state.log_path, {
                            "kind": "responses_usage",
                            "path": self.path,
                            "model": model,
                            "model_label": result["model_label"],
                            "thinking_type": "enabled" if "thinking" in result["model_label"] else "disabled",
                            "prompt_tokens": usage["prompt_tokens"],
                            "completion_tokens": usage["completion_tokens"],
                            "reasoning_tokens": usage["reasoning_tokens"],
                            "total_tokens": usage["total_tokens"],
                            "mode": "native_tools",
                            "task_id": task["task_id"],
                            "tool_step_count": len(tool_steps),
                        })
                        state.complete_native_tool_session(task, result | {"content": parsed["content"]}, usage, tool_steps)
                        response = {
                            "id": f"resp_{uuid4().hex}",
                            "object": "response",
                            "created_at": int(time.time()),
                            "status": "completed",
                            "error": None,
                            "model": model,
                            "output": build_response_output(parsed["content"]),
                            "output_text": parsed["content"],
                            "usage": {
                                "input_tokens": usage["prompt_tokens"],
                                "output_tokens": usage["completion_tokens"],
                                "total_tokens": usage["total_tokens"],
                                "reasoning_tokens": usage["reasoning_tokens"],
                            },
                        }
                        write_json(self, 200, response)
                        return

                    tool_name = parsed["tool_name"]
                    if tool_name not in allowed_tool_names:
                        raise PolicyError(f"Tool is not allowed by response request: {tool_name}")
                    tool_result = state.execute_native_tool(task, tool_name, parsed["arguments"])
                    step_summary = {
                        "step": step + 1,
                        "tool_name": tool_name,
                        "target": parsed["arguments"].get("path") or parsed["arguments"].get("directory") or parsed["arguments"].get("query") or "",
                    }
                    tool_steps.append(step_summary)
                    append_log(state.log_path, {
                        "kind": "tool_call",
                        "task_id": task["task_id"],
                        "tool_name": tool_name,
                        "target": step_summary["target"],
                        "step": step + 1,
                    })
                    messages.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
                    messages.append({
                        "role": "user",
                        "content": "Tool result:\n" + json.dumps({"tool_name": tool_name, "result": tool_result}, ensure_ascii=False, indent=2),
                    })
                raise TaskConflictError("Native tool loop exceeded max_tool_steps")
            except Exception as exc:
                state.fail_native_tool_session(task, exc)
                raise

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log-path", default=".codex/deepseek-proxy.log.jsonl")
    parser.add_argument("--stdout-log", default="")
    parser.add_argument("--stderr-log", default="")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--user-config", default="user_config.json")
    parser.add_argument("--task-store", default=".codex/runtime/task_queue.json")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    os.chdir(project_root)
    if args.stdout_log:
        stdout_path = Path(args.stdout_log)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = stdout_path.open("a", encoding="utf-8", buffering=1)
    if args.stderr_log:
        stderr_path = Path(args.stderr_log)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        sys.stderr = stderr_path.open("a", encoding="utf-8", buffering=1)
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
