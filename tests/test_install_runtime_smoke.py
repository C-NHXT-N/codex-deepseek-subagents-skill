import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BASH_SCRIPT = REPO_ROOT / "skills" / "codex-deepseek-subagents" / "scripts" / "deepseek-codex.sh"
POWERSHELL_SCRIPT = REPO_ROOT / "skills" / "codex-deepseek-subagents" / "scripts" / "deepseek-codex.ps1"


class FakeDeepSeekHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        messages = payload.get("messages") or []
        system_text = "\n".join(str(message.get("content") or "") for message in messages if message.get("role") == "system")
        combined_text = "\n".join(str(message.get("content") or "") for message in messages)
        if "DeepSeek native repository worker" in system_text:
            conversation = "\n".join(str(message.get("content") or "") for message in messages if message.get("role") != "system")
            if "src/app.py" in conversation:
                if '"tool_name": "repo_read_file"' not in conversation and '"tool_name":"repo_read_file"' not in conversation:
                    content = json.dumps({
                        "type": "tool_call",
                        "tool_name": "repo_read_file",
                        "arguments": {"path": "src/app.py"},
                    })
                elif '"tool_name": "repo_apply_patch"' not in conversation and '"tool_name":"repo_apply_patch"' not in conversation:
                    content = json.dumps({
                        "type": "tool_call",
                        "tool_name": "repo_apply_patch",
                        "arguments": {
                            "patch": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-print('hello')\n+print('install-native-e2e-ok')"
                        },
                    })
                else:
                    content = json.dumps({
                        "type": "final",
                        "content": "install-native-e2e-ok",
                    })
            else:
                if '"tool_name": "repo_read_file"' not in conversation and '"tool_name":"repo_read_file"' not in conversation:
                    content = json.dumps({
                        "type": "tool_call",
                        "tool_name": "repo_read_file",
                        "arguments": {"path": "README.md"},
                    })
                else:
                    content = json.dumps({
                        "type": "final",
                        "content": "install-native-ok",
                    })
        else:
            content = "runtime-ok" if "runtime-ok" in combined_text else "install-smoke-ok"
        body = {
            "id": "chatcmpl-install-smoke",
            "model": payload["model"],
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": content,
                    "reasoning_content": "hidden scratchpad",
                },
            }],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
                "completion_tokens_details": {"reasoning_tokens": 1},
            },
        }
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_health(base_url, runtime=None):
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=1) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception:
            time.sleep(0.1)
    if runtime is not None:
        stdout, stderr = runtime.communicate(timeout=1)
        raise AssertionError(f"runtime did not become healthy\nstdout={stdout}\nstderr={stderr}")
    raise AssertionError("runtime did not become healthy")


def parse_json_stdout(stdout):
    text = stdout.strip()
    if not text:
        raise AssertionError("expected JSON stdout, got empty output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            raise
        return json.loads(text[start:])


class InstalledRuntimeSmokeTests(unittest.TestCase):
    @unittest.skipIf(shutil.which("bash") is None, "bash is required for install smoke test")
    def test_bash_install_runtime_health_agents_text_and_native_tool_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeDeepSeekHandler)
            import threading
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            runtime = None
            try:
                port = free_port()
                install = subprocess.run(
                    [
                        "bash",
                        str(BASH_SCRIPT),
                        "install",
                        "--project-root",
                        str(root),
                        "--api-key",
                        "sk-test-placeholder",
                        "--base-url",
                        f"http://127.0.0.1:{upstream.server_port}",
                        "--port",
                        str(port),
                    ],
                    cwd=str(REPO_ROOT),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                self.assertIn("Install complete", install.stdout)

                env = os.environ.copy()
                env.update({
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "DEEPSEEK_API_KEY": "sk-test-placeholder",
                    "DEEPSEEK_PROXY_API_KEY": "sk-test-placeholder",
                    "DEEPSEEK_OPENAI_BASE_URL": f"http://127.0.0.1:{upstream.server_port}",
                    "DEEPSEEK_OPENAI_MODEL": "deepseek-v4-pro",
                    "DEEPSEEK_OPENAI_FAST_MODEL": "deepseek-v4-flash",
                    "DEEPSEEK_THINKING_DEFAULT": "disabled",
                })
                runtime = subprocess.Popen(
                    [
                        sys.executable,
                        ".codex/runtime/deepseek_scheduler.py",
                        "--port",
                        str(port),
                        "--log-path",
                        ".codex/runtime/events.log.jsonl",
                        "--project-root",
                        ".",
                        "--user-config",
                        "user_config.json",
                        "--task-store",
                        ".codex/runtime/task_queue.json",
                        "--session-store",
                        ".codex/runtime/sessions.json",
                    ],
                    cwd=str(root),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                base = f"http://127.0.0.1:{port}"
                health = wait_for_health(base, runtime=runtime)

                self.assertTrue(health["ok"])
                self.assertTrue(health["capabilities"]["text_delegate_ready"])
                self.assertTrue(health["capabilities"]["native_tool_agent_ready"])
                self.assertTrue(health["session_store_path"].endswith(".codex/runtime/sessions.json"))

                test_runtime = subprocess.run(
                    [
                        "bash",
                        str(root / ".codex" / "deepseek-codex.sh"),
                        "test-runtime",
                        "--json",
                    ],
                    cwd=str(root),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                test_runtime_data = parse_json_stdout(test_runtime.stdout)
                self.assertTrue(test_runtime_data["contains_runtime_ok"])

                with urllib.request.urlopen(f"{base}/v1/agents", timeout=2) as res:
                    agents = json.loads(res.read().decode("utf-8"))
                self.assertEqual(len(agents["data"]), 2)
                self.assertTrue(agents["capabilities"]["responses_tool_calling"])

                task_req = urllib.request.Request(
                    f"{base}/v1/tasks",
                    data=json.dumps({
                        "type": "execution",
                        "description": "Native smoke execution",
                        "tool_policy": {
                            "allowed_paths": ["README.md"],
                            "allowed_tools": ["repo_read_file"],
                            "read_extensions": [".md"],
                            "write_extensions": [".md"],
                        },
                        "approval_scope": {
                            "summary": "read README.md",
                            "files": ["README.md"],
                            "exploration": "listed paths only",
                        },
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(task_req, timeout=2) as res:
                    task = json.loads(res.read().decode("utf-8"))
                self.assertEqual(task["status"], "awaiting_approval")

                approve_req = urllib.request.Request(
                    f"{base}/v1/tasks/{task['task_id']}/approve",
                    data=json.dumps({"approval_token": "approved-by-user"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(approve_req, timeout=5) as res:
                    approved = json.loads(res.read().decode("utf-8"))
                self.assertEqual(approved["status"], "approved")

                tool_req = urllib.request.Request(
                    f"{base}/v1/responses",
                    data=json.dumps({
                        "input": [{"role": "user", "content": "Read README.md and summarize success."}],
                        "tools": [{"type": "function", "function": {"name": "repo_read_file"}}],
                        "tool_choice": "auto",
                        "metadata": {"scheduler_task_id": task["task_id"]},
                    }).encode("utf-8"),
                    headers={
                        "Authorization": "Bearer sk-test-placeholder",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(tool_req, timeout=5) as res:
                    response = json.loads(res.read().decode("utf-8"))
                self.assertEqual(response["status"], "completed")
                self.assertEqual(response["output_text"], "install-native-ok")

                analyze = subprocess.run(
                    [
                        sys.executable,
                        str(root / ".codex" / "runtime" / "deepseek_runtime.py"),
                        "analyze",
                        "--project-root",
                        str(root),
                        "--prompt",
                        "Analyze README.md only and return a short result.",
                        "--paths",
                        "README.md",
                        "--json",
                    ],
                    cwd=str(root),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                    env=env,
                )
                analyze_data = parse_json_stdout(analyze.stdout)
                self.assertEqual(analyze_data["route"]["display_label"], "deepseek-v4-pro(thinking)")
                self.assertEqual(analyze_data["content"], "install-native-ok")
            finally:
                if runtime is not None:
                    runtime.terminate()
                    try:
                        runtime.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        runtime.kill()
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join(timeout=2)

    @unittest.skipIf(shutil.which("powershell") is None, "Windows PowerShell is required for install e2e test")
    def test_powershell_install_runtime_native_patch_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(parents=True, exist_ok=True)
            (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeDeepSeekHandler)
            import threading
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            try:
                port = free_port()
                install = subprocess.run(
                    [
                        "powershell",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(POWERSHELL_SCRIPT),
                        "install",
                        "-ProjectRoot",
                        str(root),
                        "-ApiKey",
                        "sk-test-placeholder",
                        "-BaseUrl",
                        f"http://127.0.0.1:{upstream.server_port}",
                        "-Port",
                        str(port),
                    ],
                    cwd=str(REPO_ROOT),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                self.assertIn("Install complete", install.stdout)

                start = subprocess.run(
                    [
                        "powershell",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(POWERSHELL_SCRIPT),
                        "start-runtime",
                        "-ProjectRoot",
                        str(root),
                        "-Port",
                        str(port),
                    ],
                    cwd=str(REPO_ROOT),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )

                base = f"http://127.0.0.1:{port}"
                health = wait_for_health(base)
                self.assertTrue(health["ok"])
                self.assertTrue(health["capabilities"]["native_tool_agent_ready"])

                test_runtime = subprocess.run(
                    [
                        "powershell",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(root / ".codex" / "test-runtime.ps1"),
                    ],
                    cwd=str(root),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                test_runtime_data = parse_json_stdout(test_runtime.stdout)
                self.assertTrue(test_runtime_data["contains_runtime_ok"])

                task_req = urllib.request.Request(
                    f"{base}/v1/tasks",
                    data=json.dumps({
                        "type": "execution",
                        "description": "Patch src/app.py through native tools",
                        "tool_policy": {
                            "allowed_paths": ["src"],
                            "allowed_tools": ["repo_read_file", "repo_apply_patch"],
                            "read_extensions": [".py"],
                            "write_extensions": [".py"],
                            "max_tool_steps": 4,
                        },
                        "approval_scope": {
                            "summary": "read and patch src/app.py",
                            "files": ["src/app.py"],
                            "exploration": "listed paths only",
                        },
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(task_req, timeout=5) as res:
                    task = json.loads(res.read().decode("utf-8"))
                self.assertEqual(task["status"], "awaiting_approval")

                approve_req = urllib.request.Request(
                    f"{base}/v1/tasks/{task['task_id']}/approve",
                    data=json.dumps({"approval_token": "approved-by-user", "approval_note": "integration e2e"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(approve_req, timeout=5) as res:
                    approved = json.loads(res.read().decode("utf-8"))
                self.assertEqual(approved["status"], "approved")

                tool_req = urllib.request.Request(
                    f"{base}/v1/responses",
                    data=json.dumps({
                        "model": "deepseek-v4-pro",
                        "input": [{"role": "user", "content": "Update src/app.py to print install-native-e2e-ok."}],
                        "tools": [
                            {"type": "function", "function": {"name": "repo_read_file"}},
                            {"type": "function", "function": {"name": "repo_apply_patch"}},
                        ],
                        "tool_choice": "auto",
                        "metadata": {
                            "scheduler_task_id": task["task_id"],
                            "deepseek_reasoning_effort": "disabled",
                        },
                        "max_output_tokens": 128,
                    }).encode("utf-8"),
                    headers={
                        "Authorization": "Bearer sk-test-placeholder",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(tool_req, timeout=5) as res:
                    native_response = json.loads(res.read().decode("utf-8"))
                self.assertEqual(native_response["status"], "completed")
                self.assertEqual(native_response["model_label"], "deepseek-v4-pro(thinking)")
                self.assertEqual(native_response["route"]["display_label"], "deepseek-v4-pro(thinking)")
                self.assertEqual(native_response["output_text"], "install-native-e2e-ok")

                with urllib.request.urlopen(f"{base}/v1/tasks/{task['task_id']}", timeout=5) as res:
                    fetched = json.loads(res.read().decode("utf-8"))
                self.assertEqual(fetched["status"], "success")
                self.assertEqual((root / "src" / "app.py").read_text(encoding="utf-8"), "print('install-native-e2e-ok')\n")
                self.assertGreaterEqual(len(fetched["result"]["tool_steps"]), 2)

                stop = subprocess.run(
                    [
                        "powershell",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(POWERSHELL_SCRIPT),
                        "stop-runtime",
                        "-ProjectRoot",
                        str(root),
                    ],
                    cwd=str(REPO_ROOT),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                self.assertIn("Stopped runtime PID", stop.stdout)
            finally:
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
