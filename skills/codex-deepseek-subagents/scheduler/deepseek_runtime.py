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
from doctor import build_doctor_report, render_doctor_report
from render import render_permission_card as render_scope_card
from render import render_status_card as render_status_box
from render import StreamCliRenderer, render_patch_block
from tui import DashboardState, render_dashboard, render_runtime_dashboard_snapshot, tui_support_reason
from usage import load_usage_rows, render_usage, summarize_usage


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


def maybe_confirm(auto_yes, required=False):
    if auto_yes:
        return True
    if not required:
        return True
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Confirmation required but stdin is not interactive. Re-run with --yes after reviewing the displayed scope."
        )
    input("Press Enter to continue or Ctrl+C to abort...")
    return True


def print_json(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def print_compact_json(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")


def get_default_mode_from_config(project_root):
    config_path = project_path(project_root, "user_config.json")
    if not config_path.exists():
        return "pro-thinking"
    try:
        config = scheduler.validate_user_config(json.loads(config_path.read_text(encoding="utf-8-sig")))
    except Exception:
        return "pro-thinking"
    execution_name = config["defaults"]["execution_agent"]
    for agent in config["connected_agents"]:
        if agent["name"] == execution_name:
            return str((agent.get("defaults") or {}).get("mode") or "pro-thinking")
    return "pro-thinking"


def resolve_mode(args, project_root):
    if args.mode is not None:
        return args.mode
    default_mode = get_default_mode_from_config(project_root)
    if args.model is not None or args.thinking is not None:
        model_family = args.model or ("flash" if "flash" in default_mode else "pro")
        thinking_enabled = args.thinking or ("on" if "thinking" in default_mode else "off")
        requested_model_name = os.environ.get("DEEPSEEK_OPENAI_FAST_MODEL") if model_family == "flash" else os.environ.get("DEEPSEEK_OPENAI_MODEL")
        effort = "high" if thinking_enabled == "on" else "disabled"
    else:
        requested_model_name = None
        effort = None
    return scheduler.select_mode(
        requested_mode=None,
        requested_model=requested_model_name,
        effort=effort,
        default_mode=default_mode,
    )


def format_elapsed(start_monotonic):
    elapsed = max(0, int(time.monotonic() - start_monotonic))
    return f"{elapsed // 60:02d}:{elapsed % 60:02d}"


class TuiRenderer:
    def __init__(self, out_stream, thinking_view="hidden", patch_view="summary"):
        self.out_stream = out_stream
        self.thinking_view = thinking_view
        self.patch_view = patch_view
        self.state = DashboardState()

    def redraw(self):
        self.out_stream.write("\x1b[?1049h\x1b[2J\x1b[H")
        self.out_stream.write("\n".join(render_dashboard(self.state)) + "\n")
        self.out_stream.flush()

    def print_block(self, lines):
        self.state.scope = [str(line) for line in lines]
        self.redraw()

    def render_header(self, route, mode_label, status):
        self.state.header = [
            f"Model: {route['display_label']}",
            f"Mode: {mode_label}",
            f"Status: {status}",
            f"Thinking: {'ON' if route['thinking_type'] == 'enabled' else 'OFF'}",
            "Shell: disabled",
        ]
        self.redraw()

    def render_scope(self, lines):
        self.state.scope = [str(line) for line in lines]
        self.redraw()

    def render_event(self, event):
        self.state.apply(event, thinking_view=self.thinking_view, patch_view=self.patch_view)
        self.redraw()

    def close(self):
        self.out_stream.write("\x1b[?1049l")
        self.out_stream.flush()


def build_renderer(args):
    if args.ui == "tui":
        reason = tui_support_reason()
        if reason:
            print(f"TUI is not available in this terminal. Falling back to stream-cli. Reason: {reason}")
            return StreamCliRenderer(sys.stdout, thinking_view=args.thinking_view, patch_view=args.patch_view)
        return TuiRenderer(sys.stdout, thinking_view=args.thinking_view, patch_view=args.patch_view)
    return StreamCliRenderer(sys.stdout, thinking_view=args.thinking_view, patch_view=args.patch_view)


def print_route_and_scope(renderer, route, mode_label, summary, read_paths, write_paths, tools):
    if isinstance(renderer, TuiRenderer):
        renderer.render_header(route, mode_label, "routing")
        renderer.render_scope(render_scope_card(summary, read_paths, write_paths, tools, "DeepSeek endpoint"))
        return
    renderer.print_block(render_status_box(route, mode_label, "routing"))
    renderer.print_block(render_scope_card(summary, read_paths, write_paths, tools, "DeepSeek endpoint"))


def make_live_event_sink(renderer, start_monotonic, json_mode=False):
    events = []

    def event_sink(event):
        event.setdefault("data", {})
        event["data"]["_elapsed"] = format_elapsed(start_monotonic)
        events.append(event)
        if not json_mode:
            renderer.render_event(event)

    return events, event_sink


def confirm_patch(auto_yes, patch_view, patch_text):
    if patch_view in {"summary", "full"}:
        for line in render_patch_block(patch_text, patch_view):
            print(line)
    if auto_yes:
        return True
    if not sys.stdin.isatty():
        raise RuntimeError("Patch application requires confirmation. Re-run with --yes in non-interactive mode.")
    answer = input("Apply patch? [y/N]\n").strip().lower()
    return answer in {"y", "yes"}


def command_doctor(args):
    project_root = Path(args.project_root).resolve()
    install_state, existing, legacy = detect_install_state(project_root)
    state = None
    user_config_valid = None
    if existing["user_config"]:
        try:
            state = load_state(project_root, port_override=args.port)
            user_config_valid = True
        except Exception as exc:
            user_config_valid = False
            state = None
            user_config_error = str(exc)
    else:
        user_config_error = ""
    pid = read_pid(project_root)
    runtime_health = None
    runtime_health_error = ""
    try:
        base_url = runtime_base_url(project_root, args.port)
        with urllib.request.urlopen(base_url.rstrip("/v1") + "/healthz", timeout=2) as res:
            runtime_health = json.loads(res.read().decode("utf-8"))
    except Exception as exc:
        runtime_health_error = str(exc)
    deep_checks = {}
    if args.deep and state is not None:
        try:
            response = runtime_request(project_root, {
                "model": os.environ.get("DEEPSEEK_OPENAI_MODEL"),
                "input": [{"role": "user", "content": "say ok"}],
                "metadata": {"deepseek_reasoning_effort": "disabled"},
                "max_output_tokens": 16,
            })
            deep_checks["direct_text_smoke"] = response.get("status") == "completed"
            stream_req = urllib.request.Request(
                runtime_base_url(project_root, args.port).rstrip("/") + "/responses",
                data=json.dumps({
                    "model": os.environ.get("DEEPSEEK_OPENAI_MODEL"),
                    "input": [{"role": "user", "content": "say ok"}],
                    "metadata": {"deepseek_reasoning_effort": "disabled"},
                    "stream": True,
                    "max_output_tokens": 16,
                }).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + os.environ.get("DEEPSEEK_PROXY_API_KEY", ""),
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(stream_req, timeout=5) as res:
                stream_body = res.read().decode("utf-8")
            deep_checks["sse_stream_smoke"] = "event: route.selected" in stream_body
            deep_checks["native_tool_smoke"] = "skipped"
        except Exception as exc:
            deep_checks["deep_error"] = str(exc)
    report = build_doctor_report(
        install_state=install_state,
        existing=existing,
        runtime_health=runtime_health,
        runtime_health_error=runtime_health_error or user_config_error,
        user_config_valid=user_config_valid,
        pid_exists=pid is not None,
        process_alive=process_alive(pid),
        deep_checks=deep_checks,
        stale_legacy_artifacts=legacy,
    )
    report["project_root"] = str(project_root)
    if args.json:
        print_json(report)
    else:
        for line in render_doctor_report(report):
            print(line)


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
        print_compact_json({
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
    for line in render_status_box(route, "test-runtime", response.get("status")):
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
    selected_mode = resolve_mode(args, project_root)
    route = scheduler.build_route(selected_mode)
    renderer = build_renderer(args)
    if not args.json:
        print_route_and_scope(renderer, route, "text_delegate", prompt[:240], [], [], [])
    maybe_confirm(args.yes, required=False)
    start_monotonic = time.monotonic()
    events, event_sink = make_live_event_sink(renderer, start_monotonic, json_mode=args.json)
    scheduler.emit_event(event_sink, "scope.presented", step="scope", message="Scope presented to user.", route=route)
    turn = scheduler.run_text_turn(
        [{"role": "user", "content": prompt}],
        selected_mode=selected_mode,
        max_tokens=args.max_tokens,
        retry={"max_attempts": 3, "backoff_seconds": 1},
        event_sink=event_sink,
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
    if isinstance(renderer, TuiRenderer):
        renderer.close()


def command_analyze(args):
    project_root = Path(args.project_root).resolve()
    load_project_env(project_root)
    prompt = args.prompt or "Analyze this repository and explain architecture, key modules, risks, and recommendations."
    allowed_paths = args.paths or ["."]
    selected_mode = resolve_mode(args, project_root)
    route = scheduler.build_route(selected_mode)
    renderer = build_renderer(args)
    if not args.json:
        print_route_and_scope(renderer, route, "analyze", prompt, allowed_paths, [], ["repo_list_files", "repo_read_file", "repo_search_text"])
    maybe_confirm(args.yes, required=True)
    state = load_state(project_root, port_override=args.port)
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
    start_monotonic = time.monotonic()
    events, event_sink = make_live_event_sink(renderer, start_monotonic, json_mode=args.json)
    scheduler.emit_event(event_sink, "scope.presented", step="scope", message="Scope presented to user.", route=route, task_id=task["task_id"])
    scheduler.emit_event(event_sink, "approval.confirmed", step="approval", message="Read-only repo analysis approved.", route=route, task_id=task["task_id"])
    turn = scheduler.run_native_tool_turn(
        state,
        approved,
        ["repo_list_files", "repo_read_file", "repo_search_text"],
        [{"role": "user", "content": prompt}],
        args.max_tokens,
        selected_mode,
        event_sink=event_sink,
        patch_confirm=lambda _task, _summary, patch_text: confirm_patch(args.yes, args.patch_view, patch_text),
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
    if isinstance(renderer, TuiRenderer):
        renderer.close()


def command_usage(args):
    project_root = Path(args.project_root).resolve()
    rows = load_usage_rows(project_path(project_root, ".codex/runtime/events.log.jsonl"))
    summary = summarize_usage(rows)
    if args.json:
        print_json(summary)
        return
    for line in render_usage(summary):
        print(line)


def command_tui(args):
    project_root = Path(args.project_root).resolve()
    reason = tui_support_reason()
    if reason:
        print(f"TUI is not available in this terminal. Falling back to stream-cli. Reason: {reason}")
        doctor_args = argparse.Namespace(project_root=str(project_root), port=args.port, json=False, deep=False)
        return command_doctor(doctor_args)
    sessions_path = project_path(project_root, ".codex/runtime/sessions.json")
    recent_sessions = []
    active_session = None
    if sessions_path.exists():
        try:
            payload = json.loads(sessions_path.read_text(encoding="utf-8-sig"))
            recent_sessions = payload.get("sessions") or []
            for session in reversed(recent_sessions):
                if session.get("status") not in {"completed", "failed"}:
                    active_session = session
                    break
        except Exception:
            recent_sessions = []
    install_state, existing, legacy = detect_install_state(project_root)
    pid = read_pid(project_root)
    doctor_report = build_doctor_report(
        install_state=install_state,
        existing=existing,
        runtime_health=None,
        user_config_valid=existing.get("user_config", False),
        pid_exists=pid is not None,
        process_alive=process_alive(pid),
        stale_legacy_artifacts=legacy,
    )
    lines = render_runtime_dashboard_snapshot(active_session=active_session, doctor_summary=render_doctor_report(doctor_report), recent_sessions=recent_sessions)
    print("\x1b[?1049h\x1b[2J\x1b[H" + "\n".join(lines))
    try:
        input("\nPress Enter to exit TUI dashboard...")
    finally:
        print("\x1b[?1049l", end="")


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["doctor", "start-runtime", "stop-runtime", "test-runtime", "test-proxy", "delegate", "analyze", "usage", "tui"])
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--prompt", default="")
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--mode", default=None, choices=list(scheduler.SUPPORTED_MODES))
    parser.add_argument("--model", default=None, choices=["flash", "pro"])
    parser.add_argument("--thinking", default=None, choices=["on", "off"])
    parser.add_argument("--thinking-view", default="hidden", choices=["hidden", "summary", "raw"])
    parser.add_argument("--patch-view", default="summary", choices=["hidden", "summary", "full"])
    parser.add_argument("--ui", default="stream", choices=["stream", "tui"])
    parser.add_argument("--verbose", dest="verbose", action="store_true")
    parser.add_argument("--quiet", dest="verbose", action="store_false")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--max-tool-steps", type=int, default=8)
    parser.add_argument("--paths", nargs="*", default=None)
    parser.set_defaults(verbose=False)
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
    if args.command == "usage":
        return command_usage(args)
    if args.command == "tui":
        return command_tui(args)


if __name__ == "__main__":
    main()
