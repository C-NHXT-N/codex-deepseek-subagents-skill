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


class FakeDeepSeekHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        messages = payload.get("messages") or []
        system_text = "\n".join(str(message.get("content") or "") for message in messages if message.get("role") == "system")
        if "DeepSeek native repository worker" in system_text:
            joined = "\n".join(str(message.get("content") or "") for message in messages)
            if '"tool_name": "repo_read_file"' not in joined and '"tool_name":"repo_read_file"' not in joined:
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
            content = "install-smoke-ok"
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
                        ".codex/deepseek-proxy.log.jsonl",
                        "--project-root",
                        ".",
                        "--user-config",
                        "user_config.json",
                        "--task-store",
                        ".codex/runtime/task_queue.json",
                    ],
                    cwd=str(root),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                base = f"http://127.0.0.1:{port}"
                for _ in range(40):
                    try:
                        with urllib.request.urlopen(f"{base}/healthz", timeout=1) as res:
                            health = json.loads(res.read().decode("utf-8"))
                        break
                    except Exception:
                        time.sleep(0.1)
                else:
                    stdout, stderr = runtime.communicate(timeout=1)
                    self.fail(f"runtime did not become healthy\nstdout={stdout}\nstderr={stderr}")

                self.assertTrue(health["ok"])
                self.assertTrue(health["capabilities"]["text_delegate_ready"])
                self.assertTrue(health["capabilities"]["native_tool_agent_ready"])

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


if __name__ == "__main__":
    unittest.main()
