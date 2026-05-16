# Managed by codex-deepseek-subagents
import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import ctypes
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

import deepseek_scheduler as scheduler


def project_path(project_root, relative):
    return Path(project_root).resolve() / relative


def read_text_if_exists(path):
    file_path = Path(path)
    if not file_path.exists():
        return ""
    return file_path.read_text(encoding="utf-8")


def parse_shell_env(text):
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        name, value = line[len("export "):].split("=", 1)
        name = name.strip()
        value = value.strip()
        try:
            parsed = shlex.split(value)
            env[name] = parsed[0] if parsed else ""
        except ValueError:
            env[name] = value.strip("'\"")
    expanded = {}
    for key, value in env.items():
        expanded_value = value
        for env_key, env_value in env.items():
            expanded_value = expanded_value.replace(f"${env_key}", env_value)
        expanded[key] = expanded_value
    return expanded


def load_project_env(project_root):
    env_path = project_path(project_root, ".codex/deepseek.local.env.sh")
    env = parse_shell_env(read_text_if_exists(env_path))
    for key, value in env.items():
        os.environ[key] = value
    return env


def detect_install_state(project_root):
    required = {
        "user_config": project_path(project_root, "user_config.json"),
        "config": project_path(project_root, ".codex/config.toml"),
        "worker": project_path(project_root, ".codex/agents/deepseek-worker.toml"),
        "env": project_path(project_root, ".codex/deepseek.local.env.sh"),
        "runtime": project_path(project_root, ".codex/runtime/deepseek_scheduler.py"),
    }
    existing = {name: path.exists() for name, path in required.items()}
    legacy_paths = [
        ".codex/deepseek-responses-shim.ps1",
        ".codex/deepseek_responses_shim.py",
        ".codex/test-deepseek-direct.ps1",
        ".codex/test-deepseek-direct.sh",
        ".codex/test-responses-proxy.ps1",
        ".codex/test-responses-proxy.sh",
        ".codex/deepseek-proxy.log.jsonl",
        ".codex/deepseek-proxy.pid",
        ".codex/deepseek-proxy.stdout.log",
        ".codex/deepseek-proxy.stderr.log",
    ]
    legacy = [relative for relative in legacy_paths if project_path(project_root, relative).exists()]
    if not any(existing.values()) and not legacy:
        return "not_installed", existing, legacy
    if all(existing.values()):
        return "ok", existing, legacy
    if legacy and not existing["runtime"]:
        return "stale_legacy_runtime", existing, legacy
    if not existing["runtime"]:
        return "stale_missing_runtime", existing, legacy
    return "incomplete", existing, legacy


def process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def pid_file(project_root):
    return project_path(project_root, ".codex/runtime/runtime.pid")


def wait_for_log_release(project_root, timeout_seconds=6):
    targets = [
        project_path(project_root, ".codex/runtime/stdout.log"),
        project_path(project_root, ".codex/runtime/stderr.log"),
    ]
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        locked = False
        for target in targets:
            if not target.exists():
                continue
            try:
                with target.open("a", encoding="utf-8"):
                    pass
            except OSError:
                locked = True
                break
        if not locked:
            return
        time.sleep(0.2)


def terminate_process_windows(pid):
    PROCESS_TERMINATE = 0x0001
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        ctypes.windll.kernel32.TerminateProcess(handle, 1)
        ctypes.windll.kernel32.WaitForSingleObject(handle, 5000)
        return True
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def read_pid(project_root):
    path = pid_file(project_root)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None


def wait_for_health(base_url, timeout_seconds=8):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/healthz", timeout=1) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"Runtime did not become healthy at {base_url}/healthz")


def load_state(project_root, port_override=None):
    load_project_env(project_root)
    config_path = project_path(project_root, "user_config.json")
    task_store = project_path(project_root, ".codex/runtime/task_queue.json")
    session_store = project_path(project_root, ".codex/runtime/sessions.json")
    log_path = project_path(project_root, ".codex/runtime/events.log.jsonl")
    config = scheduler.validate_user_config(json.loads(config_path.read_text(encoding="utf-8-sig")))
    port = port_override or int(config["runtime"]["port"])
    state = scheduler.RuntimeState(
        project_root=project_root,
        log_path=str(log_path),
        port=port,
        user_config_path=config_path,
        task_store_path=task_store,
        session_store_path=session_store,
    )
    return state


def runtime_base_url(project_root, port_override=None):
    env = load_project_env(project_root)
    if port_override:
        return f"http://127.0.0.1:{port_override}/v1"
    return env.get("DEEPSEEK_PROXY_BASE_URL") or "http://127.0.0.1:4000/v1"


def format_bool(flag):
    return "ON" if flag else "OFF"


def render_status_card(route, mode_label, status):
    return [
        "Skill Starting...",
        f"Model: {route['model_family']}",
        f"Thinking: {format_bool(route['thinking_type'] == 'enabled')}",
        f"Resolved Model: {route['display_label']}",
        f"Mode: {mode_label}",
        f"Status: {status}",
    ]


def render_permission_card(summary, allowed_paths, allowed_tools, route):
    lines = [
        "Send Scope:",
        f"  Summary: {summary or '(none)'}",
        f"  Paths: {', '.join(allowed_paths or ['(none)'])}",
        f"  Tools: {', '.join(allowed_tools or ['(none)'])}",
        f"  Model: {route['display_label']}",
        f"  Thinking: {format_bool(route['thinking_type'] == 'enabled')}",
    ]
    return lines


def render_events(events, verbose=True):
    lines = []
    for event in events:
        event_type = event["type"]
        if event_type == "route.selected":
            continue
        if event_type == "approval.required":
            lines.append("[DeepSeek] Approval confirmed for this session")
            continue
        if event_type == "step.started":
            lines.append(f"[DeepSeek] Step: {event.get('step')} -> {event.get('message')}")
            continue
        if event_type == "reasoning.delta":
            if verbose:
                lines.append(f"[DeepSeek] Reasoning: {event.get('message')}")
            continue
        if event_type == "tool.call.started":
            lines.append(f"[DeepSeek] Tool start: {event['data']['tool_name']} -> {event['data']['target']}")
            continue
        if event_type == "patch.preview":
            lines.append("[DeepSeek] Patch preview ready")
            if verbose:
                lines.append(event["data"].get("patch") or "")
            continue
        if event_type == "tool.call.completed":
            lines.append(f"[DeepSeek] Tool success: {event['data']['tool_name']}")
            continue
        if event_type == "assistant.delta":
            lines.append(f"[DeepSeek] Output: {event.get('message')}")
            continue
        if event_type == "turn.completed":
            lines.append("✅ All tasks completed successfully")
            continue
        if event_type == "turn.failed":
            lines.append(f"⚠ {event.get('message')}")
            lines.append("Press Enter to retry or Ctrl+C to abort")
            continue
    return lines


def maybe_confirm(auto_yes):
    if auto_yes or not sys.stdin.isatty():
        return
    input("Press Enter to continue or Ctrl+C to abort...")


def print_json(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def command_doctor(args):
    project_root = Path(args.project_root).resolve()
    install_state, existing, legacy = detect_install_state(project_root)
    report = {
        "project_root": str(project_root),
        "install_state": install_state,
        "user_config_exists": existing["user_config"],
        "config_exists": existing["config"],
        "worker_exists": existing["worker"],
        "env_exists": existing["env"],
        "runtime_entry_exists": existing["runtime"],
        "stale_legacy_artifacts": legacy,
    }
    if existing["user_config"]:
        try:
            state = load_state(project_root, port_override=args.port)
            report["user_config_valid"] = True
            report["agent_registry_summary"] = [
                {"name": agent["name"], "kind": agent["kind"], "endpoint": agent["endpoint"]}
                for agent in state.config["connected_agents"]
                if agent.get("enabled", True)
            ]
            report["collaboration_capabilities"] = scheduler.runtime_capabilities()
            report["runtime_ready"] = True
            report["route_display_ready"] = True
            report["interactive_cli_ready"] = True
            report["reasoning_stream_ready"] = True
            report["native_tool_agent_ready"] = True
        except Exception as exc:
            report["user_config_valid"] = False
            report["user_config_error"] = str(exc)
    pid = read_pid(project_root)
    report["runtime_pid_exists"] = pid is not None
    report["runtime_process_alive"] = process_alive(pid)
    try:
        base_url = runtime_base_url(project_root, args.port)
        with urllib.request.urlopen(base_url.rstrip("/v1") + "/healthz", timeout=2) as res:
            report["runtime_health"] = json.loads(res.read().decode("utf-8"))
    except Exception as exc:
        report["runtime_health_error"] = str(exc)
        report["runtime_health_error_category"] = scheduler.classify_error(exc)
    print_json(report)


def command_start_runtime(args):
    project_root = Path(args.project_root).resolve()
    load_project_env(project_root)
    runtime_file = project_path(project_root, ".codex/runtime/deepseek_scheduler.py")
    if not runtime_file.exists():
        raise RuntimeError(f"Runtime entrypoint is missing: {runtime_file}")
    base_url = runtime_base_url(project_root, args.port).rstrip("/v1")
    existing_pid = read_pid(project_root)
    if process_alive(existing_pid):
        try:
            health = wait_for_health(base_url, timeout_seconds=2)
            if health.get("ok"):
                print(f"[codex-deepseek-subagents] Runtime already running with PID {existing_pid}")
                return
        except Exception:
            pass
    port = args.port or scheduler.DEFAULT_PORT
    if not args.port:
        config_path = project_path(project_root, "user_config.json")
        if config_path.exists():
            config = scheduler.validate_user_config(json.loads(config_path.read_text(encoding="utf-8-sig")))
            port = int(config["runtime"]["port"])
    env = os.environ.copy()
    kwargs = {
        "cwd": str(project_root),
        "env": env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        [
            sys.executable,
            str(runtime_file),
            "--port",
            str(port),
            "--log-path",
            ".codex/runtime/events.log.jsonl",
            "--stdout-log",
            ".codex/runtime/stdout.log",
            "--stderr-log",
            ".codex/runtime/stderr.log",
            "--project-root",
            ".",
            "--user-config",
            "user_config.json",
            "--task-store",
            ".codex/runtime/task_queue.json",
            "--session-store",
            ".codex/runtime/sessions.json",
        ],
        **kwargs,
    )
    pid_file(project_root).write_text(str(process.pid), encoding="ascii")
    wait_for_health(f"http://127.0.0.1:{port}", timeout_seconds=8)
    print(f"[codex-deepseek-subagents] Started runtime PID {process.pid} on port {port}")


def command_stop_runtime(args):
    project_root = Path(args.project_root).resolve()
    pid = read_pid(project_root)
    if not pid:
        print("[codex-deepseek-subagents] No runtime pid file found.")
        return
    if os.name == "nt":
        terminate_process_windows(pid)
        wait_for_log_release(project_root, timeout_seconds=6)
        print(f"[codex-deepseek-subagents] Stopped runtime PID {pid}")
    elif process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            os.kill(pid, signal.SIGKILL)
        for _ in range(20):
            if not process_alive(pid):
                break
            time.sleep(0.1)
        print(f"[codex-deepseek-subagents] Stopped runtime PID {pid}")
    else:
        print(f"[codex-deepseek-subagents] Runtime PID {pid} was not running.")
    try:
        pid_file(project_root).unlink()
    except FileNotFoundError:
        pass


def runtime_request(project_root, body):
    base = runtime_base_url(project_root)
    req = urllib.request.Request(
        base.rstrip("/") + "/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + os.environ.get("DEEPSEEK_PROXY_API_KEY", ""),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as res:
        return json.loads(res.read().decode("utf-8"))


def command_test_runtime(args):
    project_root = Path(args.project_root).resolve()
    load_project_env(project_root)
    response = runtime_request(project_root, {
        "model": os.environ.get("DEEPSEEK_OPENAI_MODEL"),
        "input": [{"role": "user", "content": 'Return exactly this JSON and nothing else: {"status":"runtime-ok"}'}],
        "metadata": {"deepseek_reasoning_effort": "disabled"},
        "max_output_tokens": 64,
    })
    if args.json:
        usage = response.get("usage") or {}
        print_json({
            "id": response.get("id"),
            "status": response.get("status"),
            "model": response.get("model"),
            "model_label": response.get("model_label"),
            "route": response.get("route"),
            "output_text": response.get("output_text"),
            "contains_runtime_ok": "runtime-ok" in str(response.get("output_text")),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "total_tokens": usage.get("total_tokens"),
        })
        return
    route = response.get("route") or {
        "model_family": "flash" if "flash" in str(response.get("model_label") or "").lower() else "pro",
        "thinking_type": "enabled" if "thinking" in str(response.get("model_label") or "") else "disabled",
        "display_label": response.get("model_label") or response.get("model"),
    }
    for line in render_status_card(route, "test-runtime", response.get("status")):
        print(line)
    print(f"[DeepSeek] Output: {response.get('output_text')}")


def command_delegate(args):
    project_root = Path(args.project_root).resolve()
    load_project_env(project_root)
    prompt = args.prompt or ""
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if not prompt.strip():
        raise RuntimeError("delegate requires --prompt or --prompt-file")
    selected_mode = scheduler.select_mode(
        requested_mode=args.mode,
        requested_model=os.environ.get("DEEPSEEK_OPENAI_FAST_MODEL") if str(args.model).strip() == "flash" else os.environ.get("DEEPSEEK_OPENAI_MODEL"),
        effort="high" if args.thinking == "on" else "disabled",
        default_mode=args.mode,
    )
    route = scheduler.build_route(selected_mode)
    if not args.json:
        for line in render_status_card(route, "text_delegate", "routing"):
            print(line)
        for line in render_permission_card(prompt[:240], [], [], route):
            print(line)
    maybe_confirm(args.yes)
    events = []
    turn = scheduler.run_text_turn(
        [{"role": "user", "content": prompt}],
        selected_mode=selected_mode,
        max_tokens=args.max_tokens,
        retry={"max_attempts": 3, "backoff_seconds": 1},
        event_sink=events.append,
    )
    if args.json:
        print_json({
            "ok": True,
            "route": turn["route"],
            "usage": turn["usage"],
            "content": turn["content"],
            "events": events,
        })
        return
    for line in render_events(events, verbose=args.verbose):
        print(line)


def command_analyze(args):
    project_root = Path(args.project_root).resolve()
    state = load_state(project_root, port_override=args.port)
    prompt = args.prompt or "Analyze this repository and explain architecture, key modules, risks, and recommendations."
    allowed_paths = args.paths or ["."]
    selected_mode = scheduler.select_mode(
        requested_mode=args.mode,
        requested_model=os.environ.get("DEEPSEEK_OPENAI_FAST_MODEL") if str(args.model).strip() == "flash" else os.environ.get("DEEPSEEK_OPENAI_MODEL"),
        effort="high" if args.thinking == "on" else "disabled",
        default_mode=args.mode,
    )
    route = scheduler.build_route(selected_mode)
    if not args.json:
        for line in render_status_card(route, "analyze", "routing"):
            print(line)
        for line in render_permission_card(prompt, allowed_paths, ["repo_list_files", "repo_read_file", "repo_search_text"], route):
            print(line)
    task = state.create_task({
        "type": "execution",
        "description": "Read-only repository analysis",
        "tool_policy": {
            "allowed_paths": allowed_paths,
            "allowed_tools": ["repo_list_files", "repo_read_file", "repo_search_text"],
            "read_extensions": state.config["defaults"]["tool_policy"]["read_extensions"],
            "write_extensions": [".md"],
            "allow_full_rewrite": False,
            "allow_delete": False,
            "max_tool_steps": int(args.max_tool_steps),
        },
        "approval_scope": {
            "summary": prompt,
            "files": allowed_paths,
            "exploration": "listed paths only",
        },
    })
    approved = state.approve_task(task["task_id"], {"approval_token": "approved-by-user", "approval_note": "analyze command"})
    maybe_confirm(args.yes)
    events = []
    turn = scheduler.run_native_tool_turn(
        state,
        approved,
        ["repo_list_files", "repo_read_file", "repo_search_text"],
        [{"role": "user", "content": prompt}],
        args.max_tokens,
        selected_mode,
        event_sink=events.append,
    )
    if args.json:
        print_json({
            "ok": True,
            "route": turn["route"],
            "usage": turn["usage"],
            "content": turn["content"],
            "events": events,
            "task_id": task["task_id"],
        })
        return
    for line in render_events(events, verbose=args.verbose):
        print(line)


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["doctor", "start-runtime", "stop-runtime", "test-runtime", "test-proxy", "delegate", "analyze"])
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--prompt", default="")
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--mode", default="pro-thinking", choices=list(scheduler.SUPPORTED_MODES))
    parser.add_argument("--model", default="pro", choices=["flash", "pro"])
    parser.add_argument("--thinking", default="on", choices=["on", "off"])
    parser.add_argument("--verbose", dest="verbose", action="store_true")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--max-tool-steps", type=int, default=8)
    parser.add_argument("--paths", nargs="*", default=None)
    parser.set_defaults(verbose=True)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "doctor":
        return command_doctor(args)
    if args.command == "start-runtime":
        return command_start_runtime(args)
    if args.command == "stop-runtime":
        return command_stop_runtime(args)
    if args.command in {"test-runtime", "test-proxy"}:
        return command_test_runtime(args)
    if args.command == "delegate":
        return command_delegate(args)
    if args.command == "analyze":
        return command_analyze(args)


if __name__ == "__main__":
    main()
