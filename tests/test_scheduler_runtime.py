import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "skills" / "codex-deepseek-subagents" / "scheduler" / "deepseek_scheduler.py"
SPEC = importlib.util.spec_from_file_location("deepseek_scheduler", MODULE_PATH)
SCHEDULER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCHEDULER)


class FakeDeepSeekHandler(BaseHTTPRequestHandler):
    responses = []

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        FakeDeepSeekHandler.responses.append(payload)
        body = {
            "id": "chatcmpl-test",
            "model": payload["model"],
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": "worker-ok",
                    "reasoning_content": "internal reasoning that must not be logged",
                },
            }],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "completion_tokens_details": {
                    "reasoning_tokens": 3,
                },
            },
        }
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class SchedulerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / ".codex" / "runtime").mkdir(parents=True, exist_ok=True)
        self.log_path = self.root / ".codex" / "deepseek-proxy.log.jsonl"
        self.task_store_path = self.root / ".codex" / "runtime" / "task_queue.json"
        self.user_config_path = self.root / "user_config.json"
        self.write_user_config()
        self.upstream_server, self.upstream_thread = self.start_server(FakeDeepSeekHandler)
        os.environ["DEEPSEEK_API_KEY"] = "sk-test-placeholder"
        os.environ["DEEPSEEK_OPENAI_BASE_URL"] = f"http://127.0.0.1:{self.upstream_server.server_port}"
        os.environ["DEEPSEEK_OPENAI_MODEL"] = "deepseek-v4-pro"
        os.environ["DEEPSEEK_OPENAI_FAST_MODEL"] = "deepseek-v4-flash"
        os.environ["DEEPSEEK_THINKING_DEFAULT"] = "disabled"
        os.environ["DEEPSEEK_PROXY_API_KEY"] = "sk-test-placeholder"
        FakeDeepSeekHandler.responses = []

    def tearDown(self):
        self.upstream_server.shutdown()
        self.upstream_server.server_close()
        self.upstream_thread.join(timeout=2)
        self.tmpdir.cleanup()

    def start_server(self, handler_factory):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def write_user_config(self):
        config = {
            "runtime": {"port": 0, "log_level": "info"},
            "connected_agents": [
                {
                    "name": "Codex Main",
                    "kind": "codex_main",
                    "endpoint": "local/codex-main",
                    "enabled": True,
                    "capabilities": ["analysis", "review"],
                    "defaults": {},
                },
                {
                    "name": "DeepSeek Worker",
                    "kind": "deepseek_worker",
                    "endpoint": "local/deepseek-worker",
                    "enabled": True,
                    "capabilities": ["execution"],
                    "defaults": {"mode": "pro-thinking"},
                },
            ],
            "defaults": {
                "execution_agent": "DeepSeek Worker",
                "review_agent": "Codex Main",
            },
        }
        self.user_config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def build_state(self):
        return SCHEDULER.RuntimeState(
            project_root=self.root,
            log_path=str(self.log_path),
            port=0,
            user_config_path=self.user_config_path,
            task_store_path=self.task_store_path,
        )

    def test_validate_user_config_rejects_api_key(self):
        with self.assertRaisesRegex(ValueError, "must not contain deepseek_api_key"):
            SCHEDULER.validate_user_config({"deepseek_api_key": "sk-test"})

    def test_create_task_waits_for_approval(self):
        state = self.build_state()
        task = state.create_task({
            "type": "execution",
            "description": "Edit one file",
            "inputs": [{"prompt": "do work"}],
            "allowed_paths": ["src/app.py"],
        })
        self.assertEqual(task["assigned_agent"], "DeepSeek Worker")
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertIsNone(task["result"])

    def test_retry_failed_task_creates_child(self):
        state = self.build_state()
        task = state.create_task({
            "type": "execution",
            "description": "Original task",
        })
        task["status"] = "failed"
        state.save_task_store()
        retried = state.retry_task(task["task_id"], {"description": "Retry task"})
        self.assertEqual(retried["parent_task_id"], task["task_id"])
        self.assertEqual(retried["attempt"], 1)
        self.assertEqual(retried["status"], "awaiting_approval")

    def test_runtime_endpoints_and_logs(self):
        state = self.build_state()
        handler = SCHEDULER.build_handler(state)
        runtime_server, runtime_thread = self.start_server(handler)
        try:
            base = f"http://127.0.0.1:{runtime_server.server_port}"
            with urllib.request.urlopen(f"{base}/healthz", timeout=2) as res:
                health = json.loads(res.read().decode("utf-8"))
            self.assertTrue(health["ok"])

            req = urllib.request.Request(
                f"{base}/v1/responses",
                data=json.dumps({
                    "model": "deepseek-v4-pro",
                    "input": [{"role": "user", "content": "say ok"}],
                    "metadata": {"deepseek_reasoning_effort": "disabled"},
                    "max_output_tokens": 64,
                }).encode("utf-8"),
                headers={
                    "Authorization": "Bearer sk-test-placeholder",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as res:
                response = json.loads(res.read().decode("utf-8"))
            self.assertEqual(response["status"], "completed")
            self.assertEqual(response["output_text"], "worker-ok")

            unsupported_req = urllib.request.Request(
                f"{base}/v1/responses",
                data=json.dumps({"stream": True}).encode("utf-8"),
                headers={
                    "Authorization": "Bearer sk-test-placeholder",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(unsupported_req, timeout=5)
            self.assertEqual(ctx.exception.code, 400)

            task_req = urllib.request.Request(
                f"{base}/v1/tasks",
                data=json.dumps({
                    "type": "execution",
                    "description": "Run worker task",
                    "inputs": [{"prompt": "execute"}],
                    "allowed_paths": ["src/file.py"],
                    "approval_scope": {
                        "summary": "send only task summary",
                        "files": ["src/file.py"],
                        "exploration": "listed paths only",
                    },
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(task_req, timeout=5) as res:
                created_task = json.loads(res.read().decode("utf-8"))
            self.assertEqual(created_task["status"], "awaiting_approval")

            approve_req = urllib.request.Request(
                f"{base}/v1/tasks/{created_task['task_id']}/approve",
                data=json.dumps({"approval_token": "approved-by-user"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(approve_req, timeout=5) as res:
                approved_task = json.loads(res.read().decode("utf-8"))
            self.assertEqual(approved_task["status"], "success")
            self.assertTrue(approved_task["approval_scope"]["approval_token_present"])
            self.assertNotIn("approval_token", approved_task["approval_scope"])

            with urllib.request.urlopen(f"{base}/v1/tasks/{created_task['task_id']}", timeout=5) as res:
                fetched_task = json.loads(res.read().decode("utf-8"))
            self.assertEqual(fetched_task["status"], "success")
            self.assertEqual(fetched_task["result"]["content"], "worker-ok")

            with urllib.request.urlopen(f"{base}/v1/agents", timeout=5) as res:
                agents = json.loads(res.read().decode("utf-8"))
            self.assertEqual(len(agents["data"]), 2)

            log_text = self.log_path.read_text(encoding="utf-8")
            self.assertNotIn("reasoning_content", log_text)
            self.assertIn("responses_usage", log_text)
            self.assertIn("task_event", log_text)
        finally:
            runtime_server.shutdown()
            runtime_server.server_close()
            runtime_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
