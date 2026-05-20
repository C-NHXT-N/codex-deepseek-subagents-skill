# Managed by codex-deepseek-subagents
import argparse
import copy
import hashlib
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

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from deepseek_client import (
    invoke_deepseek_chat_completion as client_invoke_deepseek_chat_completion,
    invoke_deepseek_messages as client_invoke_deepseek_messages,
    stream_deepseek_chat_completion as client_stream_deepseek_chat_completion,
)
from events import (
    append_log,
    emit_event,
    make_event,
    sanitize_event_for_storage,
    utc_now,
    write_sse_event,
    write_sse_headers,
)
from patch_preview import summarize_patch
from tool_protocol import (
    is_invalid_final_response,
    looks_like_failed_tool_protocol,
    parse_tool_loop_response,
    repair_message,
)


TASK_TYPES = {"analysis", "execution", "review"}
AGENT_KINDS = {"codex_main", "deepseek_worker"}
DEFAULT_TASK_STORE = {"tasks": []}
DEFAULT_SESSION_STORE = {"sessions": []}
SUPPORTED_NATIVE_TOOLS = (
    "repo_list_files",
    "repo_read_file",
    "repo_search_text",
    "repo_apply_patch",
    "repo_write_file",
    "repo_delete_file",
)
SUPPORTED_MODES = ("pro-thinking", "flash-thinking", "pro", "flash")
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
DEFAULT_CONTINUATION_TTL_SECONDS = 1800
MAX_CONTINUATION_LIFETIME_SECONDS = 7200
try:
    DEFAULT_PORT = int("4000")
except ValueError:
    DEFAULT_PORT = 4000
DISABLED_THINKING_VALUES = {"disabled", "none", "low-cost", "off", "false", "0"}


class PolicyError(RuntimeError):
    pass


class TaskConflictError(RuntimeError):
    pass


def write_json(handler, status, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


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
    runtime.setdefault("event_transport", "sse")
    retry = runtime.get("retry") or {}
    if not isinstance(retry, dict):
        raise ValueError("runtime.retry must be an object")
    retry.setdefault("max_attempts", 3)
    retry.setdefault("backoff_seconds", 1)
    runtime["retry"] = retry

    ui = config.get("ui") or {}
    if not isinstance(ui, dict):
        raise ValueError("ui must be an object")
    ui.setdefault("default_mode", "stream-cli")
    ui.setdefault("show_reasoning", True)
    ui.setdefault("show_tool_timeline", True)
    ui.setdefault("show_token_usage", True)

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
    defaults.setdefault("verbose", True)
    defaults["tool_policy"] = normalize_tool_policy(defaults.get("tool_policy") or {
        "allowed_tools": defaults.get("default_allowed_tools") or DEFAULT_ALLOWED_TOOLS,
        "allowed_paths": defaults.get("allowed_paths") or [],
        "read_extensions": defaults.get("read_extensions") or DEFAULT_READ_EXTENSIONS,
        "write_extensions": defaults.get("write_extensions") or DEFAULT_WRITE_EXTENSIONS,
        "max_file_read_bytes": defaults.get("max_file_read_bytes") or 262144,
        "max_search_results": defaults.get("max_search_results") or 50,
        "max_tool_steps": defaults.get("max_tool_steps") or DEFAULT_MAX_TOOL_STEPS,
    })

    tool_calling = config.get("tool_calling") or {}
    if not isinstance(tool_calling, dict):
        raise ValueError("tool_calling must be an object")
    tool_calling.setdefault("mode", "native")
    tool_calling.setdefault("fallback_json_protocol", True)
    tool_calling.setdefault("strict", False)

    routing = config.get("routing") or {}
    if not isinstance(routing, dict):
        raise ValueError("routing must be an object")
    routing.setdefault("default", "flash")
    routing.setdefault("analysis", "flash-thinking")
    routing.setdefault("execution", "pro-thinking")
    routing.setdefault("patch_repair", "pro-thinking")
    routing.setdefault("max_effort_after_tool_turns", 2)

    privacy = config.get("privacy") or {}
    if not isinstance(privacy, dict):
        raise ValueError("privacy must be an object")
    privacy.setdefault("persist_raw_reasoning", False)
    privacy.setdefault("persist_full_patch", False)
    privacy.setdefault("persist_assistant_output", False)

    normalized = {
        "runtime": runtime,
        "connected_agents": normalized_agents,
        "defaults": defaults,
        "ui": ui,
        "tool_calling": tool_calling,
        "routing": routing,
        "privacy": privacy,
    }
    return normalized


def normalize_user_config_for_write(existing_config, template_config):
    template = copy.deepcopy(template_config if isinstance(template_config, dict) else {})
    existing = copy.deepcopy(existing_config if isinstance(existing_config, dict) else {})

    merged = {
        "runtime": copy.deepcopy(template.get("runtime") or {}),
        "ui": copy.deepcopy(template.get("ui") or {}),
        "connected_agents": copy.deepcopy(template.get("connected_agents") or []),
        "defaults": copy.deepcopy(template.get("defaults") or {}),
        "tool_calling": copy.deepcopy(template.get("tool_calling") or {}),
        "routing": copy.deepcopy(template.get("routing") or {}),
        "privacy": copy.deepcopy(template.get("privacy") or {}),
    }

    runtime = existing.get("runtime")
    if isinstance(runtime, dict):
        merged["runtime"].update(runtime)
        existing_retry = runtime.get("retry")
        if isinstance(existing_retry, dict):
            merged["runtime"].setdefault("retry", {})
            merged["runtime"]["retry"].update(existing_retry)

    ui = existing.get("ui")
    if isinstance(ui, dict):
        merged["ui"].update(ui)

    connected_agents = existing.get("connected_agents")
    if isinstance(connected_agents, list) and connected_agents:
        merged["connected_agents"] = connected_agents

    defaults = existing.get("defaults")
    if isinstance(defaults, dict):
        for key in ("execution_agent", "review_agent", "verbose"):
            if key in defaults:
                merged["defaults"][key] = defaults[key]
        merged["defaults"].setdefault("tool_policy", {})
        existing_policy = defaults.get("tool_policy")
        if isinstance(existing_policy, dict):
            merged["defaults"]["tool_policy"].update(existing_policy)
        for legacy_key, policy_key in (
            ("allowed_paths", "allowed_paths"),
            ("read_extensions", "read_extensions"),
            ("write_extensions", "write_extensions"),
            ("default_allowed_tools", "allowed_tools"),
            ("max_file_read_bytes", "max_file_read_bytes"),
            ("max_search_results", "max_search_results"),
            ("max_tool_steps", "max_tool_steps"),
            ("allow_full_rewrite", "allow_full_rewrite"),
            ("allow_delete", "allow_delete"),
        ):
            if legacy_key in defaults:
                merged["defaults"]["tool_policy"][policy_key] = defaults[legacy_key]

    for section in ("tool_calling", "routing", "privacy"):
        payload = existing.get(section)
        if isinstance(payload, dict):
            merged[section].update(payload)

    return validate_user_config(merged)


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
        "runtime_ready": True,
        "text_delegate_ready": True,
        "native_tool_agent_ready": True,
        "responses_smoke_test": True,
        "responses_tool_calling": True,
        "supported_tools": list(SUPPORTED_NATIVE_TOOLS),
        "stream_supported": True,
        "shell_supported": False,
        "unsupported_responses_features": [],
        "native_tool_agent_note": "Execution tasks can run approved local repository tools through the scheduler. Shell command execution is intentionally disabled in v1.",
        "interactive_cli_ready": True,
        "reasoning_stream_ready": True,
        "route_display_ready": True,
        "windows_wrapper_ready": True,
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
            "model": os.environ.get("DEEPSEEK_OPENAI_MODEL") or "deepseek-v4-pro",
            "thinking": {"type": "enabled", "reasoning_effort": "high"},
        }
    if selected_mode == "flash-thinking":
        return {
            "model": os.environ.get("DEEPSEEK_OPENAI_FAST_MODEL") or os.environ.get("DEEPSEEK_OPENAI_MODEL") or "deepseek-v4-flash",
            "thinking": {"type": "enabled", "reasoning_effort": "high"},
        }
    if selected_mode == "flash":
        return {
            "model": os.environ.get("DEEPSEEK_OPENAI_FAST_MODEL") or os.environ.get("DEEPSEEK_OPENAI_MODEL") or "deepseek-v4-flash",
            "thinking": {"type": "disabled"},
        }
    return {
        "model": os.environ.get("DEEPSEEK_OPENAI_MODEL") or "deepseek-v4-pro",
        "thinking": {"type": "disabled"} if selected_mode == "pro" else {"type": "enabled", "reasoning_effort": "high"},
    }


def mode_family(selected_mode):
    return "flash" if str(selected_mode or "").startswith("flash") else "pro"


def thinking_enabled_for_mode(selected_mode):
    return str(selected_mode or "").endswith("thinking")


def select_mode(requested_mode=None, requested_model=None, effort=None, default_mode="pro-thinking"):
    mode = str(requested_mode or "").strip()
    if mode in SUPPORTED_MODES:
        return mode

    default_mode = default_mode if default_mode in SUPPORTED_MODES else "pro-thinking"
    base = mode_family(default_mode)
    if requested_model:
        fast_model = os.environ.get("DEEPSEEK_OPENAI_FAST_MODEL") or ""
        requested_model_text = str(requested_model)
        if requested_model_text == fast_model or "flash" in requested_model_text.lower():
            base = "flash"
        else:
            base = "pro"

    if effort is None or str(effort).strip() == "":
        enabled = thinking_enabled_for_mode(default_mode)
    else:
        enabled = str(effort).strip().lower() not in DISABLED_THINKING_VALUES
    return f"{base}-thinking" if enabled else base


def build_route(selected_mode, resolved_model=None):
    spec = mode_to_model_spec(selected_mode)
    model = resolved_model or spec["model"]
    thinking = spec["thinking"]
    display_label = f"{model}(thinking)" if thinking["type"] == "enabled" else model
    return {
        "requested_mode": selected_mode,
        "resolved_model": model,
        "thinking_type": thinking["type"],
        "reasoning_effort": thinking.get("reasoning_effort"),
        "display_label": display_label,
        "model_family": mode_family(selected_mode),
    }


def usage_from_result(result):
    hit = int(result.get("prompt_cache_hit_tokens") or 0)
    miss = int(result.get("prompt_cache_miss_tokens") or 0)
    total_cache = hit + miss
    return {
        "input_tokens": result.get("prompt_tokens"),
        "output_tokens": result.get("completion_tokens"),
        "total_tokens": result.get("total_tokens"),
        "reasoning_tokens": result.get("reasoning_tokens"),
        "prompt_cache_hit_tokens": result.get("prompt_cache_hit_tokens"),
        "prompt_cache_miss_tokens": result.get("prompt_cache_miss_tokens"),
        "cache_hit_ratio": (float(hit) / float(total_cache)) if total_cache else None,
    }


def invoke_deepseek_messages(messages, mode, max_tokens, retry=None):
    spec = mode_to_model_spec(mode)
    return client_invoke_deepseek_messages(messages, spec["model"], spec["thinking"], max_tokens, retry=retry)


def invoke_deepseek_chat_completion(
    messages,
    mode,
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
    spec = mode_to_model_spec(mode)
    return client_invoke_deepseek_chat_completion(
        messages=messages,
        model=spec["model"],
        thinking=spec["thinking"],
        max_tokens=max_tokens,
        retry=retry,
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
        stream=stream,
        stream_options=stream_options,
        user_id=user_id,
        base_url=base_url,
    )


def stream_deepseek_chat_completion(
    messages,
    mode,
    max_tokens,
    retry=None,
    tools=None,
    tool_choice=None,
    response_format=None,
    stream_options=None,
    user_id=None,
    base_url=None,
):
    spec = mode_to_model_spec(mode)
    return client_stream_deepseek_chat_completion(
        messages=messages,
        model=spec["model"],
        thinking=spec["thinking"],
        max_tokens=max_tokens,
        retry=retry,
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
        stream_options=stream_options,
        user_id=user_id,
        base_url=base_url,
    )


def invoke_deepseek_chat(task, mode):
    return invoke_deepseek_messages(
        [{"role": "user", "content": build_task_prompt(task)}],
        mode=mode,
        max_tokens=2048,
    )


def build_official_tool_schemas(allowed_tool_names, strict=False):
    definitions = {
        "repo_list_files": {
            "name": "repo_list_files",
            "description": "List files under an approved directory.",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Approved directory, for example '.' or 'src'.",
                }
            },
            "required": ["directory"],
        },
        "repo_read_file": {
            "name": "repo_read_file",
            "description": "Read one approved file.",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Approved file path.",
                }
            },
            "required": ["path"],
        },
        "repo_search_text": {
            "name": "repo_search_text",
            "description": "Search approved files for exact text.",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Exact text to search for.",
                }
            },
            "required": ["query"],
        },
        "repo_apply_patch": {
            "name": "repo_apply_patch",
            "description": "Request applying a unified diff patch to approved writable files. Runtime must preview and wait for user approval before applying.",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "Unified diff patch.",
                }
            },
            "required": ["patch"],
        },
        "repo_write_file": {
            "name": "repo_write_file",
            "description": "Create a new approved file, or rewrite one file only when explicitly approved.",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Approved file path.",
                },
                "content": {
                    "type": "string",
                    "description": "File content.",
                },
                "create_only": {
                    "type": "boolean",
                    "description": "If true, fail when the file already exists.",
                },
            },
            "required": ["path", "content", "create_only"],
        },
        "repo_delete_file": {
            "name": "repo_delete_file",
            "description": "Delete an approved file only when delete is explicitly approved.",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Approved file path.",
                }
            },
            "required": ["path"],
        },
    }
    schemas = []
    for tool_name in allowed_tool_names:
        definition = definitions.get(tool_name)
        if not definition:
            continue
        function_schema = {
            "name": definition["name"],
            "description": definition["description"],
            "parameters": {
                "type": "object",
                "properties": copy.deepcopy(definition["properties"]),
                "required": list(definition["required"]),
                "additionalProperties": False,
            },
        }
        if strict:
            function_schema["strict"] = True
        schemas.append({"type": "function", "function": function_schema})
    return schemas


def _hash_text(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _update_usage_totals(current, partial):
    current = copy.deepcopy(current or {})
    partial = partial or {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "total_tokens",
    ):
        if partial.get(key) is not None:
            current[key] = int(partial.get(key) or 0)
    return current


def _consume_streamed_chat_completion(
    messages,
    selected_mode,
    max_tokens,
    retry=None,
    tools=None,
    tool_choice=None,
    response_format=None,
    event_sink=None,
    session_id=None,
    task_id=None,
    emit_reasoning=False,
    emit_content=False,
):
    model_name = None
    finish_reason = None
    usage = {}
    reasoning_chunks = []
    content_chunks = []
    tool_calls = {}
    saw_stream_event = False
    for item in stream_deepseek_chat_completion(
        messages=messages,
        mode=selected_mode,
        max_tokens=max_tokens,
        retry=retry,
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
        stream_options={"include_usage": True},
    ):
        saw_stream_event = True
        event_type = item.get("type")
        if event_type == "meta":
            model_name = item.get("model") or model_name
            continue
        if event_type == "finish":
            finish_reason = item.get("finish_reason")
            continue
        if event_type == "usage":
            usage = _update_usage_totals(usage, item.get("usage"))
            continue
        if event_type == "reasoning_delta":
            reasoning_chunks.append(str(item.get("text") or ""))
            if emit_reasoning:
                full_text = "".join(reasoning_chunks)
                emit_event(
                    event_sink,
                    "reasoning.delta",
                    step="reasoning",
                    message=str(item.get("text") or ""),
                    status="reasoning",
                    route=build_route(selected_mode, model_name),
                    data={"chars": len(full_text), "hash": _hash_text(full_text), "reasoning_tokens": usage.get("reasoning_tokens")},
                    session_id=session_id,
                    task_id=task_id,
                )
            continue
        if event_type == "content_delta":
            content_chunks.append(str(item.get("text") or ""))
            if emit_content:
                emit_event(
                    event_sink,
                    "assistant.delta",
                    step="final",
                    message=str(item.get("text") or ""),
                    status="in_progress",
                    route=build_route(selected_mode, model_name),
                    session_id=session_id,
                    task_id=task_id,
                )
            continue
        if event_type == "tool_call_delta":
            index = int(item.get("index") or 0)
            accumulator = tool_calls.setdefault(index, {
                "index": index,
                "id": item.get("id"),
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if item.get("id") and not accumulator.get("id"):
                accumulator["id"] = item.get("id")
            accumulator["function"]["name"] += str(item.get("name_delta") or "")
            accumulator["function"]["arguments"] += str(item.get("arguments_delta") or "")
            emit_event(
                event_sink,
                "tool.call.delta",
                step="tool_call",
                message="Streaming tool call delta received.",
                status="tool_call",
                route=build_route(selected_mode, model_name),
                data={
                    "index": index,
                    "id": accumulator.get("id"),
                    "name_delta": str(item.get("name_delta") or ""),
                    "arguments_delta": str(item.get("arguments_delta") or ""),
                },
                session_id=session_id,
                task_id=task_id,
            )
            continue
    if not saw_stream_event:
        fallback_result = invoke_deepseek_chat_completion(
            messages=messages,
            mode=selected_mode,
            max_tokens=max_tokens,
            retry=retry,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
        )
        if emit_reasoning and fallback_result.get("reasoning_content"):
            emit_event(
                event_sink,
                "reasoning.delta",
                step="reasoning",
                message=str(fallback_result.get("reasoning_content") or ""),
                status="reasoning",
                route=build_route(selected_mode, fallback_result.get("model")),
                data={
                    "chars": len(str(fallback_result.get("reasoning_content") or "")),
                    "hash": _hash_text(str(fallback_result.get("reasoning_content") or "")),
                    "reasoning_tokens": fallback_result.get("reasoning_tokens"),
                },
                session_id=session_id,
                task_id=task_id,
            )
        if emit_content and fallback_result.get("content") and not looks_like_failed_tool_protocol(fallback_result.get("content")):
            emit_event(
                event_sink,
                "assistant.delta",
                step="final",
                message=str(fallback_result.get("content") or ""),
                status="in_progress",
                route=build_route(selected_mode, fallback_result.get("model")),
                session_id=session_id,
                task_id=task_id,
            )
        return fallback_result
    ordered_tool_calls = []
    for index in sorted(tool_calls):
        payload = tool_calls[index]
        ordered_tool_calls.append({
            "id": payload.get("id") or f"toolcall_{uuid4().hex}",
            "type": "function",
            "function": {
                "name": payload["function"]["name"],
                "arguments": payload["function"]["arguments"],
            },
        })
    return {
        "content": "".join(content_chunks),
        "reasoning_content": "".join(reasoning_chunks),
        "tool_calls": ordered_tool_calls,
        "model": model_name or mode_to_model_spec(selected_mode)["model"],
        "model_label": build_route(selected_mode, model_name).get("display_label"),
        "finish_reason": finish_reason,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
        "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "raw_message": {
            "content": "".join(content_chunks),
            "reasoning_content": "".join(reasoning_chunks),
            "tool_calls": ordered_tool_calls,
        },
    }


def _accumulate_usage(accumulator, result):
    accumulator = copy.deepcopy(accumulator or {})
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "total_tokens",
    ):
        accumulator[key] = int(accumulator.get(key) or 0) + int(result.get(key) or 0)
    return accumulator


def run_text_turn(messages, selected_mode, max_tokens, retry=None, event_sink=None, session_id=None, task_id=None):
    initial_route = build_route(selected_mode)
    emit_event(
        event_sink,
        "response.created",
        step="response",
        message="Response created.",
        status="created",
        route=initial_route,
        session_id=session_id,
        task_id=task_id,
    )
    emit_event(
        event_sink,
        "response.in_progress",
        step="response",
        message="Response in progress.",
        status="in_progress",
        route=initial_route,
        session_id=session_id,
        task_id=task_id,
    )
    emit_event(
        event_sink,
        "route.selected",
        step="routing",
        message=f"Model: {initial_route['model_family']} | Thinking: {'ON' if initial_route['thinking_type'] == 'enabled' else 'OFF'}",
        status="routing",
        route=initial_route,
        session_id=session_id,
        task_id=task_id,
    )
    emit_event(
        event_sink,
        "request.sending",
        step="reasoning",
        message="Sending request to DeepSeek.",
        status="reasoning",
        route=initial_route,
        session_id=session_id,
        task_id=task_id,
    )
    emit_event(
        event_sink,
        "reasoning.started",
        step="reasoning",
        message="Thinking active.",
        status="reasoning",
        route=initial_route,
        session_id=session_id,
        task_id=task_id,
    )
    if event_sink:
        result = _consume_streamed_chat_completion(
            messages=messages,
            selected_mode=selected_mode,
            max_tokens=max_tokens,
            retry=retry,
            event_sink=event_sink,
            session_id=session_id,
            task_id=task_id,
            emit_reasoning=True,
            emit_content=True,
        )
    else:
        result = invoke_deepseek_chat_completion(messages=messages, mode=selected_mode, max_tokens=max_tokens, retry=retry)
    route = build_route(selected_mode, result.get("model"))
    reasoning_content = str(result.get("reasoning_content") or "")
    if reasoning_content and not event_sink:
        emit_event(
            event_sink,
            "reasoning.delta",
            step="reasoning",
            message=reasoning_content,
            status="reasoning",
            route=route,
            data={"chars": len(reasoning_content), "hash": _hash_text(reasoning_content), "reasoning_tokens": result.get("reasoning_tokens")},
            session_id=session_id,
            task_id=task_id,
        )
    content = str(result.get("content") or "")
    if not event_sink:
        emit_event(
            event_sink,
            "assistant.delta",
            step="final",
            message=content,
            status="completed",
            route=route,
            session_id=session_id,
            task_id=task_id,
        )
    emit_event(
        event_sink,
        "usage.updated",
        step="final",
        message="Usage updated.",
        status="completed",
        route=route,
        data=usage_from_result(result),
        session_id=session_id,
        task_id=task_id,
    )
    emit_event(
        event_sink,
        "turn.completed",
        step="final",
        message="DeepSeek turn completed.",
        status="completed",
        route=route,
        data={"usage": usage_from_result(result), "content": content},
        session_id=session_id,
        task_id=task_id,
    )
    emit_event(
        event_sink,
        "response.completed",
        step="response",
        message="Response completed.",
        status="completed",
        route=route,
        data={"usage": usage_from_result(result)},
        session_id=session_id,
        task_id=task_id,
    )
    return {
        "content": content,
        "result": result,
        "route": route,
        "usage": usage_from_result(result),
    }


def run_native_tool_turn(
    state,
    task,
    allowed_tool_names,
    user_messages,
    max_output_tokens,
    selected_mode,
    event_sink=None,
    session_id=None,
    response_id=None,
    messages_override=None,
    usage_override=None,
    tool_steps_override=None,
    start_step=0,
    response_started=False,
):
    route = build_route(selected_mode)
    task_id = task["task_id"]
    if not response_started:
        emit_event(event_sink, "response.created", step="response", message="Response created.", status="created", route=route, session_id=session_id, task_id=task_id)
        emit_event(event_sink, "response.in_progress", step="response", message="Response in progress.", status="in_progress", route=route, session_id=session_id, task_id=task_id)
    emit_event(
        event_sink,
        "route.selected",
        step="routing",
        message=f"Model: {route['model_family']} | Thinking: {'ON' if route['thinking_type'] == 'enabled' else 'OFF'}",
        status="routing",
        route=route,
        session_id=session_id,
        task_id=task_id,
    )
    if messages_override is None:
        emit_event(
            event_sink,
            "approval.confirmed",
            step="approval",
            message="Execution scope approved for native repository tools.",
            status="approved",
            route=route,
            data={
                "summary": task.get("approval_scope", {}).get("summary"),
                "allowed_paths": task.get("tool_policy", {}).get("allowed_paths"),
                "allowed_tools": allowed_tool_names,
            },
            session_id=session_id,
            task_id=task_id,
        )
    state.begin_native_tool_session(task)
    messages = copy.deepcopy(messages_override) if messages_override is not None else build_native_tool_messages(task, user_messages, allowed_tool_names)
    usage = copy.deepcopy(usage_override or {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "total_tokens": 0,
    })
    tool_steps = copy.deepcopy(tool_steps_override or [])
    retry_policy = state.config.get("runtime", {}).get("retry")
    strict_tools = bool((state.config.get("tool_calling") or {}).get("strict"))
    allow_json_fallback = bool((state.config.get("tool_calling") or {}).get("fallback_json_protocol", True))
    tool_schemas = build_official_tool_schemas(allowed_tool_names, strict=strict_tools)
    repair_attempts = 0
    tool_protocol = "native"

    try:
        for step in range(int(start_step), task["tool_policy"]["max_tool_steps"]):
            emit_event(event_sink, "step.started", step="reasoning", message=f"DeepSeek reasoning turn {step + 1} started.", status="reasoning", route=route, session_id=session_id, task_id=task_id)
            emit_event(event_sink, "request.sending", step="reasoning", message="Sending request to DeepSeek.", status="reasoning", route=route, session_id=session_id, task_id=task_id)
            emit_event(event_sink, "reasoning.started", step="reasoning", message="Thinking active.", status="reasoning", route=route, session_id=session_id, task_id=task_id)
            if event_sink:
                result = _consume_streamed_chat_completion(
                    messages=messages,
                    selected_mode=selected_mode,
                    max_tokens=max_output_tokens,
                    retry=retry_policy,
                    tools=tool_schemas,
                    tool_choice="auto",
                    event_sink=event_sink,
                    session_id=session_id,
                    task_id=task_id,
                    emit_reasoning=True,
                    emit_content=True,
                )
            else:
                result = invoke_deepseek_chat_completion(
                    messages=messages,
                    mode=selected_mode,
                    max_tokens=max_output_tokens,
                    retry=retry_policy,
                    tools=tool_schemas,
                    tool_choice="auto",
                )
            route = build_route(selected_mode, result.get("model"))
            usage = _accumulate_usage(usage, result)
            if result.get("reasoning_content") and not event_sink:
                emit_event(
                    event_sink,
                    "reasoning.delta",
                    step="reasoning",
                    message=str(result.get("reasoning_content") or ""),
                    status="reasoning",
                    route=route,
                    data={
                        "chars": len(str(result.get("reasoning_content") or "")),
                        "hash": _hash_text(str(result.get("reasoning_content") or "")),
                        "reasoning_tokens": result.get("reasoning_tokens"),
                    },
                    session_id=session_id,
                    task_id=task_id,
                )
            emit_event(
                event_sink,
                "usage.updated",
                step="reasoning",
                message="Usage updated.",
                status="reasoning",
                route=route,
                data=usage_from_result(usage),
                session_id=session_id,
                task_id=task_id,
            )

            assistant_message = {"role": "assistant", "content": str(result.get("content") or "")}
            if result.get("tool_calls"):
                assistant_message["tool_calls"] = copy.deepcopy(result.get("tool_calls") or [])
            if result.get("reasoning_content") and result.get("tool_calls"):
                assistant_message["reasoning_content"] = str(result.get("reasoning_content") or "")

            fallback_parsed = None
            if not result.get("tool_calls"):
                parsed_candidate = parse_tool_loop_response(result.get("content"))
                if parsed_candidate["type"] == "tool_call":
                    if not allow_json_fallback:
                        raise TaskConflictError("Native tool call response missing official tool_calls")
                    tool_protocol = "json_fallback"
                    fallback_parsed = parsed_candidate
                    emit_event(
                        event_sink,
                        "tool.protocol.fallback",
                        step="tool_call",
                        message="Falling back to legacy JSON tool protocol.",
                        status="tool_protocol_fallback",
                        route=route,
                        session_id=session_id,
                        task_id=task_id,
                    )
                elif parsed_candidate["type"] == "final" and is_invalid_final_response(result.get("content")):
                    if repair_attempts >= 1:
                        raise TaskConflictError("Model never produced a valid final response after protocol repair")
                    repair_attempts += 1
                    emit_event(
                        event_sink,
                        "tool.protocol.error",
                        step="tool_call",
                        message="Invalid tool protocol response, requesting repair.",
                        status="tool_protocol_error",
                        route=route,
                        data={"attempt": repair_attempts},
                        session_id=session_id,
                        task_id=task_id,
                    )
                    messages.append({"role": "assistant", "content": str(result.get("content") or "")})
                    messages.append({"role": "user", "content": repair_message("Return either official tool_calls or a clean final answer.")})
                    continue
                else:
                    content = str(parsed_candidate["content"] or str(result.get("content") or ""))
                    if not event_sink:
                        emit_event(event_sink, "assistant.delta", step="final", message=content, status="completed", route=route, session_id=session_id, task_id=task_id)
                    state.complete_native_tool_session(task, result | {"content": content, "route": route, "tool_protocol": tool_protocol}, usage, tool_steps)
                    emit_event(event_sink, "turn.completed", step="final", message="Native tool execution completed.", status="completed", route=route, data={"usage": usage_from_result(usage), "content": content, "tool_steps": tool_steps}, session_id=session_id, task_id=task_id)
                    emit_event(event_sink, "response.completed", step="response", message="Response completed.", status="completed", route=route, data={"usage": usage_from_result(usage)}, session_id=session_id, task_id=task_id)
                    return {
                        "status": "completed",
                        "content": content,
                        "result": result | {"tool_protocol": tool_protocol},
                        "route": route,
                        "usage": usage,
                        "tool_steps": tool_steps,
                    }

            if fallback_parsed is not None:
                tool_calls = [{
                    "id": f"toolcall_{uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": str(fallback_parsed.get("tool_name") or ""),
                        "arguments": json.dumps(fallback_parsed.get("arguments") or {}, ensure_ascii=False),
                    },
                }]
                assistant_message["tool_calls"] = copy.deepcopy(tool_calls)
            else:
                tool_calls = copy.deepcopy(result.get("tool_calls") or [])

            messages.append(assistant_message)
            retry_requested = False
            for tool_call in tool_calls:
                function_payload = tool_call.get("function") or {}
                tool_name = str(function_payload.get("name") or "").strip()
                try:
                    arguments = json.loads(str(function_payload.get("arguments") or "{}"))
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must decode to an object")
                except Exception as exc:
                    if repair_attempts >= 1:
                        raise TaskConflictError(f"Invalid tool arguments JSON for {tool_name}: {exc}")
                    repair_attempts += 1
                    emit_event(
                        event_sink,
                        "tool.protocol.error",
                        step="tool_call",
                        message=f"Invalid tool arguments for {tool_name}, requesting repair.",
                        status="tool_protocol_error",
                        route=route,
                        data={"error": str(exc), "attempt": repair_attempts},
                        session_id=session_id,
                        task_id=task_id,
                    )
                    messages.append({"role": "user", "content": repair_message(f"{tool_name}.arguments must be valid JSON object text.")})
                    retry_requested = True
                    break
                if tool_name not in allowed_tool_names:
                    raise PolicyError(f"Tool is not allowed by response request: {tool_name}")
                target = arguments.get("path") or arguments.get("directory") or arguments.get("query") or ""
                emit_event(
                    event_sink,
                    "tool.call.started",
                    step="tool_call",
                    message=f"{tool_name} -> {target}",
                    status="tool_call",
                    route=route,
                    data={"tool_name": tool_name, "target": target, "turn": step + 1},
                    session_id=session_id,
                    task_id=task_id,
                )
                if tool_name == "repo_apply_patch":
                    patch_text = arguments.get("patch") or ""
                    tool_steps.append({"step": step + 1, "tool_name": tool_name, "target": target, "tool_protocol": tool_protocol, "status": "requires_action"})
                    append_log(state.log_path, {"kind": "tool_call", "task_id": task["task_id"], "tool_name": tool_name, "target": target, "step": step + 1, "error": ""})
                    continuation = state.register_continuation(
                        task=task,
                        patch_id=None,
                        messages=messages,
                        selected_mode=selected_mode,
                        allowed_tool_names=allowed_tool_names,
                        max_output_tokens=max_output_tokens,
                        tool_call_id=tool_call.get("id"),
                        session_id=session_id,
                        response_id=response_id,
                        usage=usage,
                        tool_steps=tool_steps,
                        next_step=step + 1,
                    )
                    patch = state.create_pending_patch(task, patch_text, tool_call_id=tool_call.get("id"), continuation=continuation)
                    continuation["patch_id"] = patch["patch_id"]
                    patch["continuation_id"] = continuation["continuation_id"]
                    state.save_task_store()
                    emit_event(
                        event_sink,
                        "patch.preview",
                        step="patch_preview",
                        message="Patch preview generated.",
                        status="patch_ready",
                        route=route,
                        data={"patch_id": patch["patch_id"], "patch_summary": copy.deepcopy(patch["summary"])},
                        session_id=session_id,
                        task_id=task_id,
                    )
                    emit_event(
                        event_sink,
                        "patch.approval.required",
                        step="patch_preview",
                        message="Patch approval required before apply.",
                        status="requires_action",
                        route=route,
                        data={"patch_id": patch["patch_id"], "summary": copy.deepcopy(patch["summary"])},
                        session_id=session_id,
                        task_id=task_id,
                    )
                    emit_event(
                        event_sink,
                        "tool.call.completed",
                        step="tool_call",
                        message=f"{tool_name} waiting for approval.",
                        status="requires_action",
                        route=route,
                        data={"tool_name": tool_name, "target": target, "result": {"status": "waiting_for_patch_approval", "patch_id": patch["patch_id"], "summary": copy.deepcopy(patch["summary"])}},
                        session_id=session_id,
                        task_id=task_id,
                    )
                    emit_event(
                        event_sink,
                        "response.requires_action",
                        step="response",
                        message="Response requires patch approval.",
                        status="requires_action",
                        route=route,
                        data={"type": "patch_approval", "task_id": task["task_id"], "patch_id": patch["patch_id"], "summary": copy.deepcopy(patch["summary"])},
                        session_id=session_id,
                        task_id=task_id,
                    )
                    return {
                        "status": "requires_action",
                        "content": "",
                        "result": result | {"tool_protocol": tool_protocol},
                        "route": route,
                        "usage": usage,
                        "tool_steps": tool_steps,
                        "required_action": {
                            "type": "patch_approval",
                            "task_id": task["task_id"],
                            "patch_id": patch["patch_id"],
                            "summary": copy.deepcopy(patch["summary"]),
                        },
                    }

                tool_error = None
                try:
                    tool_result = state.execute_native_tool(task, tool_name, arguments)
                except (FileNotFoundError, PolicyError, ValueError) as exc:
                    tool_error = exc
                    tool_result = {"error": str(exc), "error_category": classify_error(exc), "recoverable": True}
                tool_step = {"step": step + 1, "tool_name": tool_name, "target": target, "tool_protocol": tool_protocol}
                if tool_error is not None:
                    tool_step["error"] = str(tool_error)
                tool_steps.append(tool_step)
                append_log(state.log_path, {"kind": "tool_call", "task_id": task["task_id"], "tool_name": tool_name, "target": target, "step": step + 1, "error": str(tool_error) if tool_error is not None else ""})
                emit_event(
                    event_sink,
                    "tool.call.completed",
                    step="tool_call",
                    message=f"{tool_name} completed." if tool_error is None else f"{tool_name} failed: {tool_error}",
                    status="tool_call" if tool_error is None else "tool_error",
                    route=route,
                    data={"tool_name": tool_name, "target": target, "result": tool_result, "turn": step + 1},
                    session_id=session_id,
                    task_id=task_id,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id"),
                    "content": json.dumps({"tool_name": tool_name, "result": tool_result}, ensure_ascii=False),
                })
            if retry_requested:
                continue
        raise TaskConflictError("Native tool loop exceeded max_tool_steps")
    except Exception as exc:
        state.fail_native_tool_session(task, exc)
        emit_event(
            event_sink,
            "turn.failed",
            step="final",
            message=str(exc),
            status="failed",
            route=route,
            data={"error_category": classify_error(exc)},
            session_id=session_id,
            task_id=task_id,
        )
        raise


def coerce_path_argument(arguments, key="path"):
    value = arguments.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    return normalize_rel_path(value)


class RuntimeState:
    def __init__(self, project_root, log_path, port, user_config_path, task_store_path, session_store_path):
        self.project_root = Path(project_root).resolve()
        self.log_path = str(Path(log_path))
        self.port = port
        self.user_config_path = Path(user_config_path)
        self.task_store_path = Path(task_store_path)
        self.session_store_path = Path(session_store_path)
        self.config = self.load_user_config()
        self.agent_index = build_agent_index(self.config)
        self.task_store = self.load_task_store()
        self.sessions = self.load_session_store()
        self.pending_continuations = {}
        self.patches_dir = self.project_root / ".codex" / "runtime" / "patches"

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
        for task in data["tasks"]:
            if isinstance(task, dict):
                task.setdefault("pending_patches", [])
        return data

    def save_task_store(self):
        self.task_store_path.parent.mkdir(parents=True, exist_ok=True)
        self.task_store_path.write_text(json.dumps(self.task_store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def load_session_store(self):
        if not self.session_store_path.exists():
            self.session_store_path.parent.mkdir(parents=True, exist_ok=True)
            self.session_store_path.write_text(json.dumps(DEFAULT_SESSION_STORE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return {}
        with self.session_store_path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
            raise RuntimeError(f"Invalid session store format: {self.session_store_path}")
        return {session["session_id"]: session for session in data["sessions"] if isinstance(session, dict) and session.get("session_id")}

    def save_session_store(self):
        self.session_store_path.parent.mkdir(parents=True, exist_ok=True)
        sessions = []
        for session in self.sessions.values():
            sanitized = copy.deepcopy(session)
            sanitized["events"] = [sanitize_event_for_storage(event) for event in sanitized.get("events", [])]
            if isinstance(sanitized.get("response"), dict):
                sanitized["response"] = {
                    "route": copy.deepcopy(sanitized["response"].get("route")),
                    "usage": copy.deepcopy(sanitized["response"].get("usage")),
                    "required_action": copy.deepcopy(sanitized["response"].get("required_action")),
                }
            sessions.append(sanitized)
        payload = {"sessions": sessions}
        self.session_store_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def find_task(self, task_id):
        for task in self.task_store["tasks"]:
            if task["task_id"] == task_id:
                return task
        return None

    def health(self):
        self.cleanup_pending_continuations()
        return {
            "ok": True,
            "service": "deepseek-scheduler",
            "port": self.port,
            "project_root": str(self.project_root),
            "user_config_path": str(self.user_config_path),
            "task_store_path": str(self.task_store_path),
            "session_store_path": str(self.session_store_path),
            "agents": len([agent for agent in self.config["connected_agents"] if agent.get("enabled", True)]),
            "tasks": len(self.task_store["tasks"]),
            "sessions": len(self.sessions),
            "pending_continuations": len(self.pending_continuations),
            "capabilities": runtime_capabilities(),
        }

    def _now_epoch(self):
        return int(time.time())

    def _future_epoch(self, seconds):
        return self._now_epoch() + int(seconds)

    def _max_continuation_expiry(self, continuation):
        created_at = int(continuation.get("created_epoch") or self._now_epoch())
        return created_at + MAX_CONTINUATION_LIFETIME_SECONDS

    def cleanup_pending_continuations(self):
        now = self._now_epoch()
        expired = []
        for continuation_id, continuation in self.pending_continuations.items():
            expires_at = int(continuation.get("expires_epoch") or 0)
            max_expiry = self._max_continuation_expiry(continuation)
            if expires_at <= now or max_expiry <= now:
                expired.append(continuation_id)
        for continuation_id in expired:
            self.pending_continuations.pop(continuation_id, None)

    def register_continuation(
        self,
        task,
        patch_id,
        messages,
        selected_mode,
        allowed_tool_names,
        max_output_tokens,
        tool_call_id,
        session_id=None,
        response_id=None,
        usage=None,
        tool_steps=None,
        next_step=0,
    ):
        continuation_id = f"cont_{uuid4().hex}"
        created_epoch = self._now_epoch()
        continuation = {
            "continuation_id": continuation_id,
            "task_id": task["task_id"],
            "patch_id": patch_id,
            "messages": copy.deepcopy(messages),
            "selected_mode": selected_mode,
            "allowed_tool_names": list(allowed_tool_names),
            "max_output_tokens": int(max_output_tokens),
            "tool_call_id": tool_call_id,
            "session_id": session_id,
            "response_id": response_id,
            "created_at": utc_now(),
            "created_epoch": created_epoch,
            "expires_at": utc_now(),
            "expires_epoch": min(created_epoch + DEFAULT_CONTINUATION_TTL_SECONDS, created_epoch + MAX_CONTINUATION_LIFETIME_SECONDS),
            "usage": copy.deepcopy(usage or {}),
            "tool_steps": copy.deepcopy(tool_steps or []),
            "next_step": int(next_step),
        }
        continuation["expires_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(continuation["expires_epoch"]))
        self.pending_continuations[continuation_id] = continuation
        return continuation

    def get_continuation(self, continuation_id):
        self.cleanup_pending_continuations()
        continuation = self.pending_continuations.get(continuation_id)
        if not continuation:
            return None
        now = self._now_epoch()
        max_expiry = self._max_continuation_expiry(continuation)
        continuation["expires_epoch"] = min(now + DEFAULT_CONTINUATION_TTL_SECONDS, max_expiry)
        continuation["expires_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(continuation["expires_epoch"]))
        return continuation

    def clear_continuation(self, continuation_id):
        self.pending_continuations.pop(continuation_id, None)

    def create_session(self, payload):
        session_id = str(payload.get("session_id") or f"session_{uuid4().hex}")
        session = {
            "session_id": session_id,
            "status": "created",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "route": None,
            "summary": {
                "mode": payload.get("mode") or payload.get("execution_mode") or "text_delegate",
                "has_tools": bool(payload.get("tools")),
            },
            "events": [],
            "response": None,
        }
        self.sessions[session_id] = session
        self.save_session_store()
        return session

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def append_session_event(self, session, event):
        session["events"].append(event)
        if event.get("route"):
            session["route"] = event["route"]
        session["updated_at"] = utc_now()
        append_log(self.log_path, {
            "kind": "session_event",
            "session_id": session["session_id"],
            "event": sanitize_event_for_storage(event),
        })
        self.save_session_store()

    def _patch_path(self, patch_id):
        return self.patches_dir / f"{patch_id}.patch"

    def _write_patch_body(self, patch_id, patch_text):
        self.patches_dir.mkdir(parents=True, exist_ok=True)
        self._patch_path(patch_id).write_text(str(patch_text or ""), encoding="utf-8")

    def _read_patch_body(self, patch_id):
        path = self._patch_path(patch_id)
        if not path.exists():
            raise KeyError(patch_id)
        return path.read_text(encoding="utf-8")

    def _remove_patch_body(self, patch_id):
        path = self._patch_path(patch_id)
        if path.exists():
            path.unlink()

    def list_patches(self, task_id):
        task = self.find_task(task_id)
        if not task:
            raise KeyError(task_id)
        return copy.deepcopy(task.get("pending_patches") or [])

    def find_pending_patch(self, task_id, patch_id):
        task = self.find_task(task_id)
        if not task:
            raise KeyError(task_id)
        for patch in task.get("pending_patches") or []:
            if patch.get("patch_id") == patch_id:
                return patch
        raise KeyError(patch_id)

    def create_pending_patch(self, task, patch_text, tool_call_id=None, continuation=None):
        summary = summarize_patch(str(patch_text or ""))
        patch_id = f"patch_{uuid4().hex}"
        patch = {
            "patch_id": patch_id,
            "task_id": task["task_id"],
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "status": "waiting",
            "summary": {
                "files": copy.deepcopy(summary.get("files") or []),
                "additions": int(summary.get("additions") or 0),
                "deletions": int(summary.get("deletions") or 0),
                "sha256": summary.get("sha256"),
            },
            "tool_call_id": tool_call_id,
            "continuation_id": continuation.get("continuation_id") if isinstance(continuation, dict) else None,
            "approved_at": None,
            "rejected_at": None,
            "applied_at": None,
        }
        self._write_patch_body(patch_id, patch_text)
        task.setdefault("pending_patches", []).append(patch)
        task["status"] = "waiting_for_patch_approval"
        task["timestamps"]["updated_at"] = utc_now()
        self.save_task_store()
        return patch

    def approve_patch(self, task_id, patch_id, payload):
        patch = self.find_pending_patch(task_id, patch_id)
        if patch["status"] != "waiting":
            raise TaskConflictError(f"Patch status does not allow approval: {patch['status']}")
        patch["status"] = "approved"
        patch["approved_at"] = utc_now()
        patch["updated_at"] = utc_now()
        patch["approval_note"] = str((payload or {}).get("approval_note") or "")
        self.save_task_store()
        return copy.deepcopy(patch)

    def reject_patch(self, task_id, patch_id, payload):
        patch = self.find_pending_patch(task_id, patch_id)
        if patch["status"] in {"rejected", "applied"}:
            raise TaskConflictError(f"Patch status does not allow rejection: {patch['status']}")
        patch["status"] = "rejected"
        patch["rejected_at"] = utc_now()
        patch["updated_at"] = utc_now()
        patch["rejection_note"] = str((payload or {}).get("rejection_note") or "")
        task = self.find_task(task_id)
        task["status"] = "failed"
        task["timestamps"]["updated_at"] = utc_now()
        self.clear_continuation(patch.get("continuation_id"))
        self.save_task_store()
        return copy.deepcopy(patch)

    def apply_approved_patch(self, task, patch_id):
        patch = self.find_pending_patch(task["task_id"], patch_id)
        if patch["status"] != "approved":
            raise TaskConflictError(f"Patch status does not allow apply: {patch['status']}")
        continuation = self.get_continuation(patch.get("continuation_id"))
        if not continuation:
            raise TaskConflictError("pending_turn_context_missing")
        patch_text = self._read_patch_body(patch_id)
        summary = summarize_patch(patch_text)
        if summary.get("sha256") != patch.get("summary", {}).get("sha256"):
            raise TaskConflictError("patch sha256 mismatch")
        self.apply_patch(patch_text, task["tool_policy"])
        patch["status"] = "applied"
        patch["applied_at"] = utc_now()
        patch["updated_at"] = utc_now()
        task["timestamps"]["updated_at"] = utc_now()
        messages = copy.deepcopy(continuation["messages"])
        messages.append({
            "role": "tool",
            "tool_call_id": patch.get("tool_call_id"),
            "content": json.dumps(
                {
                    "status": "patch_applied",
                    "patch_id": patch_id,
                    "summary": copy.deepcopy(patch.get("summary") or {}),
                },
                ensure_ascii=False,
            ),
        })
        session = self.get_session(continuation.get("session_id")) if continuation.get("session_id") else None

        def session_sink(event):
            if session:
                self.append_session_event(session, event)

        if session:
            session_sink(make_event(
                "patch.approval.confirmed",
                step="patch_preview",
                message="Patch approval confirmed.",
                status="patch_approved",
                data={"patch_id": patch_id, "summary": copy.deepcopy(patch.get("summary") or {})},
                session_id=session["session_id"],
                task_id=task["task_id"],
            ))
            session_sink(make_event(
                "patch.applied",
                step="patch_preview",
                message="Patch applied.",
                status="patch_applied",
                data={"patch_id": patch_id, "summary": copy.deepcopy(patch.get("summary") or {})},
                session_id=session["session_id"],
                task_id=task["task_id"],
            ))

        try:
            result = run_native_tool_turn(
                self,
                task,
                continuation["allowed_tool_names"],
                [],
                continuation["max_output_tokens"],
                continuation["selected_mode"],
                event_sink=session_sink if session else None,
                session_id=continuation.get("session_id"),
                response_id=continuation.get("response_id"),
                messages_override=messages,
                usage_override=copy.deepcopy(continuation.get("usage") or {}),
                tool_steps_override=copy.deepcopy(continuation.get("tool_steps") or []),
                start_step=int(continuation.get("next_step") or 0),
                response_started=True,
            )
            if session:
                session["status"] = "requires_action" if result.get("status") == "requires_action" else "completed"
                session["route"] = result.get("route")
                session["response"] = {
                    "content": result.get("content"),
                    "usage": usage_from_result(result.get("usage") or {}),
                    "route": result.get("route"),
                    "required_action": copy.deepcopy(result.get("required_action")),
                }
                session["updated_at"] = utc_now()
                self.save_session_store()
            self.clear_continuation(continuation["continuation_id"])
            self.save_task_store()
            return result
        except Exception:
            self.clear_continuation(continuation["continuation_id"])
            self.save_task_store()
            raise

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
            "pending_patches": [],
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
            turn = run_text_turn(
                [{"role": "user", "content": build_task_prompt(task)}],
                selected_mode=mode,
                max_tokens=2048,
                retry=self.config.get("runtime", {}).get("retry"),
            )
            result = turn["result"]
            task["result"] = {
                "content": turn["content"],
                "model": result["model"],
                "model_label": result["model_label"],
                "route": turn["route"],
                "finish_reason": result.get("finish_reason"),
                "reasoning_content_discarded": True,
            }
            task["usage"] = turn["usage"]
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
                "prompt_tokens": task["usage"].get("input_tokens"),
                "completion_tokens": task["usage"].get("output_tokens"),
                "reasoning_tokens": task["usage"].get("reasoning_tokens"),
                "total_tokens": task["usage"].get("total_tokens"),
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
        if task["status"] not in {"approved", "running", "success", "waiting_for_patch_approval"}:
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
            "route": result.get("route"),
            "finish_reason": result.get("finish_reason"),
            "tool_protocol": result.get("tool_protocol") or "native",
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
        "repo_list_files": "List files under an approved directory.",
        "repo_read_file": "Read one approved file.",
        "repo_search_text": "Search approved files for exact text.",
        "repo_apply_patch": "Request applying a unified diff patch to approved writable files. The runtime will preview it and require user approval before applying.",
        "repo_write_file": "Create a new approved file, or rewrite one file only when explicitly approved. Always provide create_only=true unless full rewrite approval is explicit.",
        "repo_delete_file": "Delete an approved file only when delete is explicitly approved.",
    }
    system_lines = [
        "You are the DeepSeek native repository worker.",
        "You must operate only through approved tools and approved paths.",
        "Use the provided official function tools when needed.",
        "Always provide all required tool arguments.",
        "Do not invent tools. Do not request shell access.",
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
            if self.path.startswith("/v1/sessions/") and self.path.endswith("/events"):
                session_id = self.path.split("/v1/sessions/", 1)[1].rsplit("/events", 1)[0]
                session = state.get_session(session_id)
                if not session:
                    raise KeyError(session_id)
                write_sse_headers(self)
                for event in session["events"]:
                    write_sse_event(self, event)
                return
            if self.path.startswith("/v1/sessions/"):
                session_id = self.path.split("/v1/sessions/", 1)[1]
                session = state.get_session(session_id)
                if not session:
                    raise KeyError(session_id)
                write_json(self, 200, {
                    "session_id": session["session_id"],
                    "status": session["status"],
                    "created_at": session["created_at"],
                    "updated_at": session["updated_at"],
                    "route": session.get("route"),
                    "summary": session.get("summary"),
                    "event_count": len(session.get("events") or []),
                    "response": session.get("response"),
                })
                return
            if self.path.startswith("/v1/tasks/") and "/patches" in self.path:
                relative = self.path.split("/v1/tasks/", 1)[1]
                task_id, _, remainder = relative.partition("/patches")
                if not remainder or remainder == "":
                    write_json(self, 200, {"data": state.list_patches(task_id)})
                    return
                patch_id = remainder.strip("/").split("/", 1)[0]
                patch = copy.deepcopy(state.find_pending_patch(task_id, patch_id))
                patch["patch_text"] = state._read_patch_body(patch_id)
                write_json(self, 200, patch)
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
            if self.path == "/v1/sessions":
                payload = self.read_payload()
                return self.handle_session_create(payload)
            if self.path == "/v1/tasks":
                payload = self.read_payload()
                task = state.create_task(payload)
                write_json(self, 201, task)
                return
            if self.path.startswith("/v1/tasks/") and "/patches/" in self.path:
                relative = self.path.split("/v1/tasks/", 1)[1]
                task_id, _, remainder = relative.partition("/patches/")
                patch_id, _, action = remainder.partition("/")
                payload = self.read_payload()
                if action == "approve":
                    patch = state.approve_patch(task_id, patch_id, payload)
                    write_json(self, 200, patch)
                    return
                if action == "reject":
                    patch = state.reject_patch(task_id, patch_id, payload)
                    write_json(self, 200, patch)
                    return
                if action == "apply":
                    task = state.find_task(task_id)
                    if not task:
                        raise KeyError(task_id)
                    result = state.apply_approved_patch(task, patch_id)
                    write_json(self, 200, result)
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
                return self.handle_streaming_response(payload)

            if payload.get("tools"):
                return self.handle_native_tools_response(payload)
            return self.handle_text_response(payload)

        def handle_text_response(self, payload):
            messages = response_input_to_messages(payload.get("input"))
            if not messages:
                messages = [{"role": "user", "content": "Respond with exactly: ok"}]

            effort = str((payload.get("metadata") or {}).get("deepseek_reasoning_effort") or os.environ.get("DEEPSEEK_THINKING_DEFAULT") or "disabled")
            selected_mode = select_mode(
                requested_mode=(payload.get("metadata") or {}).get("deepseek_mode"),
                requested_model=payload.get("model"),
                effort=effort,
                default_mode="pro-thinking",
            )
            turn = run_text_turn(
                messages,
                selected_mode=selected_mode,
                max_tokens=int(payload.get("max_output_tokens") or 512),
                retry=state.config.get("runtime", {}).get("retry"),
            )
            result = turn["result"]
            content = turn["content"]
            reasoning_content = str(result.get("reasoning_content") or "")
            append_log(state.log_path, {
                "kind": "responses_usage",
                "path": self.path,
                "model": result.get("model"),
                "model_label": result["model_label"],
                "thinking_type": turn["route"]["thinking_type"],
                "request_input_chars": sum(len(m.get("content", "")) for m in messages),
                "message_count": len(messages),
                "prompt_tokens": result.get("prompt_tokens"),
                "completion_tokens": result.get("completion_tokens"),
                "reasoning_tokens": result.get("reasoning_tokens"),
                "prompt_cache_hit_tokens": result.get("prompt_cache_hit_tokens"),
                "prompt_cache_miss_tokens": result.get("prompt_cache_miss_tokens"),
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
                "model": result.get("model"),
                "model_label": result["model_label"],
                "route": turn["route"],
                "output": build_response_output(content),
                "output_text": content,
                "usage": turn["usage"],
            }
            write_json(self, 200, response)

        def handle_native_tools_response(self, payload):
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            task_id = metadata.get("scheduler_task_id")
            task = state.require_task_for_native_tools(task_id)
            allowed_tool_names = state.allowed_tool_names_for_response(payload.get("tools"), task["tool_policy"])
            if "shell_command" in allowed_tool_names:
                raise ValueError("shell_command is intentionally disabled in this runtime")
            user_messages = response_input_to_messages(payload.get("input"))
            agent = state.agent_index[task["assigned_agent"]]
            agent_default_mode = str((agent.get("defaults") or {}).get("mode") or "pro-thinking")
            selected_mode = metadata.get("deepseek_mode") or agent_default_mode
            turn = run_native_tool_turn(
                state,
                task,
                allowed_tool_names,
                user_messages,
                int(payload.get("max_output_tokens") or 1024),
                selected_mode,
            )
            append_log(state.log_path, {
                "kind": "responses_usage",
                "path": self.path,
                "model": turn["result"].get("model"),
                "model_label": turn["result"]["model_label"],
                "thinking_type": turn["route"]["thinking_type"],
                "prompt_tokens": turn["usage"].get("prompt_tokens"),
                "completion_tokens": turn["usage"].get("completion_tokens"),
                "reasoning_tokens": turn["usage"].get("reasoning_tokens"),
                "prompt_cache_hit_tokens": turn["usage"].get("prompt_cache_hit_tokens"),
                "prompt_cache_miss_tokens": turn["usage"].get("prompt_cache_miss_tokens"),
                "total_tokens": turn["usage"].get("total_tokens"),
                "mode": "native_tools",
                "task_id": task["task_id"],
                "tool_step_count": len(turn["tool_steps"]),
                "tool_protocol": (turn.get("result") or {}).get("tool_protocol") or "native",
            })
            if turn.get("status") == "requires_action":
                write_json(self, 200, {
                    "id": f"resp_{uuid4().hex}",
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "requires_action",
                    "error": None,
                    "model": turn["result"].get("model"),
                    "model_label": turn["result"]["model_label"],
                    "route": turn["route"],
                    "output": [],
                    "output_text": "",
                    "usage": usage_from_result(turn["usage"]),
                    "required_action": copy.deepcopy(turn.get("required_action")),
                })
                return
            response = {
                "id": f"resp_{uuid4().hex}",
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "error": None,
                "model": turn["result"].get("model"),
                "model_label": turn["result"]["model_label"],
                "route": turn["route"],
                "output": build_response_output(turn["content"]),
                "output_text": turn["content"],
                "usage": usage_from_result(turn["usage"]),
                "required_action": None,
            }
            write_json(self, 200, response)

        def handle_streaming_response(self, payload):
            events = []

            def event_sink(event):
                events.append(event)
                write_sse_event(self, event)

            write_sse_headers(self)
            try:
                if payload.get("tools"):
                    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                    task = state.require_task_for_native_tools(metadata.get("scheduler_task_id"))
                    allowed_tool_names = state.allowed_tool_names_for_response(payload.get("tools"), task["tool_policy"])
                    agent = state.agent_index[task["assigned_agent"]]
                    agent_default_mode = str((agent.get("defaults") or {}).get("mode") or "pro-thinking")
                    selected_mode = metadata.get("deepseek_mode") or agent_default_mode
                    turn = run_native_tool_turn(
                        state,
                        task,
                        allowed_tool_names,
                        response_input_to_messages(payload.get("input")),
                        int(payload.get("max_output_tokens") or 1024),
                        selected_mode,
                        event_sink=event_sink,
                    )
                else:
                    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                    selected_mode = select_mode(
                        requested_mode=metadata.get("deepseek_mode"),
                        requested_model=payload.get("model"),
                        effort=metadata.get("deepseek_reasoning_effort") or os.environ.get("DEEPSEEK_THINKING_DEFAULT") or "disabled",
                        default_mode="pro-thinking",
                    )
                    turn = run_text_turn(
                        response_input_to_messages(payload.get("input")) or [{"role": "user", "content": "Respond with exactly: ok"}],
                        selected_mode=selected_mode,
                        max_tokens=int(payload.get("max_output_tokens") or 512),
                        retry=state.config.get("runtime", {}).get("retry"),
                        event_sink=event_sink,
                    )
                self.close_connection = True
                return turn
            except Exception as exc:
                write_sse_event(self, make_event(
                    "turn.failed",
                    step="final",
                    message=str(exc),
                    status="failed",
                    data={"error_category": classify_error(exc)},
                ))
                self.close_connection = True

        def handle_session_create(self, payload):
            session = state.create_session(payload)

            def event_sink(event):
                state.append_session_event(session, event)

            state.append_session_event(session, make_event(
                "session.started",
                step="session",
                message="Session started.",
                status="created",
                session_id=session["session_id"],
            ))

            try:
                if payload.get("tools"):
                    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                    task = state.require_task_for_native_tools(metadata.get("scheduler_task_id"))
                    allowed_tool_names = state.allowed_tool_names_for_response(payload.get("tools"), task["tool_policy"])
                    agent = state.agent_index[task["assigned_agent"]]
                    agent_default_mode = str((agent.get("defaults") or {}).get("mode") or "pro-thinking")
                    selected_mode = metadata.get("deepseek_mode") or agent_default_mode
                    turn = run_native_tool_turn(
                        state,
                        task,
                        allowed_tool_names,
                        response_input_to_messages(payload.get("input")),
                        int(payload.get("max_output_tokens") or 1024),
                        selected_mode,
                        event_sink=event_sink,
                        session_id=session["session_id"],
                    )
                else:
                    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                    selected_mode = select_mode(
                        requested_mode=metadata.get("deepseek_mode"),
                        requested_model=payload.get("model"),
                        effort=metadata.get("deepseek_reasoning_effort") or os.environ.get("DEEPSEEK_THINKING_DEFAULT") or "disabled",
                        default_mode="pro-thinking",
                    )
                    turn = run_text_turn(
                        response_input_to_messages(payload.get("input")) or [{"role": "user", "content": "Respond with exactly: ok"}],
                        selected_mode=selected_mode,
                        max_tokens=int(payload.get("max_output_tokens") or 512),
                        retry=state.config.get("runtime", {}).get("retry"),
                        event_sink=event_sink,
                        session_id=session["session_id"],
                    )
                session["status"] = "requires_action" if turn.get("status") == "requires_action" else "completed"
                session["route"] = turn["route"]
                session["response"] = {
                    "content": turn.get("content"),
                    "usage": usage_from_result(turn["usage"]),
                    "route": turn["route"],
                    "required_action": copy.deepcopy(turn.get("required_action")),
                }
                session["updated_at"] = utc_now()
                state.append_session_event(session, make_event(
                    "session.completed" if session["status"] == "completed" else "response.requires_action",
                    step="session",
                    message="Session completed." if session["status"] == "completed" else "Session requires patch approval.",
                    status=session["status"],
                    route=turn["route"],
                    session_id=session["session_id"],
                    task_id=task["task_id"] if payload.get("tools") else None,
                ))
                state.save_session_store()
                write_json(self, 201, {
                    "session_id": session["session_id"],
                    "status": session["status"],
                    "route": session["route"],
                    "event_count": len(session["events"]),
                    "response": session["response"],
                })
            except Exception as exc:
                session["status"] = "failed"
                session["updated_at"] = utc_now()
                state.append_session_event(session, make_event(
                    "turn.failed",
                    step="final",
                    message=str(exc),
                    status="failed",
                    data={"error_category": classify_error(exc)},
                ))
                state.save_session_store()
                raise

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log-path", default=".codex/runtime/events.log.jsonl")
    parser.add_argument("--stdout-log", default="")
    parser.add_argument("--stderr-log", default="")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--user-config", default="user_config.json")
    parser.add_argument("--task-store", default=".codex/runtime/task_queue.json")
    parser.add_argument("--session-store", default=".codex/runtime/sessions.json")
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
        session_store_path=(project_root / args.session_store),
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), build_handler(state))
    print(f"DeepSeek scheduler listening on http://127.0.0.1:{args.port}/")
    print(f"Health: http://127.0.0.1:{args.port}/healthz")
    print(f"Log: {args.log_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
