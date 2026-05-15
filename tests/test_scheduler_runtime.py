import importlib.util
import json
import os
import tempfile
import threading
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
        messages = payload.get("messages") or []
        system_text = "\n".join(str(message.get("content") or "") for message in messages if message.get("role") == "system")
        body = self.build_body(payload, system_text, messages)
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def build_body(self, payload, system_text, messages):
        if "DeepSeek native repository worker" not in system_text:
            return self.text_response(payload, "worker-ok", "internal reasoning that must not be logged", 11, 7, 18, 3)

        combined = "\n".join(str(message.get("content") or "") for message in messages)
        if '"tool_name": "repo_read_file"' not in combined and '"tool_name":"repo_read_file"' not in combined:
            content = json.dumps({
                "type": "tool_call",
                "tool_name": "repo_read_file",
                "arguments": {"path": "src/app.py"},
            })
            return self.text_response(payload, content, "native step 1", 13, 5, 18, 2)
        if '"tool_name": "repo_apply_patch"' not in combined and '"tool_name":"repo_apply_patch"' not in combined:
            content = json.dumps({
                "type": "tool_call",
                "tool_name": "repo_apply_patch",
                "arguments": {
                    "patch": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-print('hello')\n+print('native-ok')"
                },
            })
            return self.text_response(payload, content, "native step 2", 14, 6, 20, 2)
        content = json.dumps({
            "type": "final",
            "content": "native-tools-ok",
        })
        return self.text_response(payload, content, "native done", 9, 4, 13, 1)

    def text_response(self, payload, content, reasoning, prompt_tokens, completion_tokens, total_tokens, reasoning_tokens):
        return {
            "id": "chatcmpl-test",
            "model": payload["model"],
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": content,
                    "reasoning_content": reasoning,
                },
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "completion_tokens_details": {
                    "reasoning_tokens": reasoning_tokens,
                },
            },
        }


class SchedulerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / ".codex" / "runtime").mkdir(parents=True, exist_ok=True)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
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
                    "capabilities": ["execution", "native_tools", "repo_patch", "repo_search"],
                    "defaults": {"mode": "pro-thinking"},
                },
            ],
            "defaults": {
                "execution_agent": "DeepSeek Worker",
                "review_agent": "Codex Main",
                "tool_policy": {
                    "allowed_paths": ["src"],
                    "read_extensions": [".py", ".md"],
                    "write_extensions": [".py"],
                    "allowed_tools": [
                        "repo_list_files",
                        "repo_read_file",
                        "repo_search_text",
                        "repo_apply_patch",
                        "repo_write_file",
                    ],
                    "max_file_read_bytes": 262144,
                    "max_search_results": 20,
                    "max_tool_steps": 6,
                    "allow_full_rewrite": False,
                    "allow_delete": False,
                },
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

    def test_user_config_accepts_utf8_bom_and_tool_policy(self):
        text = self.user_config_path.read_text(encoding="utf-8")
        self.user_config_path.write_text(text, encoding="utf-8-sig")
        state = self.build_state()
        self.assertEqual(state.config["defaults"]["execution_agent"], "DeepSeek Worker")
        self.assertIn("repo_apply_patch", state.config["defaults"]["tool_policy"]["allowed_tools"])

    def test_create_text_and_native_tasks(self):
        state = self.build_state()
        text_task = state.create_task({
            "type": "execution",
            "description": "Edit one file via text worker",
            "inputs": [{"prompt": "do work"}],
            "allowed_paths": ["src/app.py"],
        })
        self.assertEqual(text_task["execution_mode"], "text_delegate")
        self.assertEqual(text_task["status"], "awaiting_approval")

        native_task = state.create_task({
            "type": "execution",
            "description": "Edit one file via native tools",
            "tool_policy": {
                "allowed_paths": ["src"],
                "allowed_tools": ["repo_read_file", "repo_apply_patch"],
                "read_extensions": [".py"],
                "write_extensions": [".py"],
            },
        })
        self.assertEqual(native_task["execution_mode"], "native_tools")
        self.assertEqual(native_task["status"], "awaiting_approval")

    def test_read_and_write_extensions_are_separated(self):
        state = self.build_state()
        policy = {
            "allowed_paths": ["src"],
            "allowed_tools": ["repo_read_file", "repo_write_file"],
            "read_extensions": [".py", ".md"],
            "write_extensions": [".md"],
        }
        self.assertEqual(state.read_file("src/app.py", policy), "print('hello')\n")
        with self.assertRaisesRegex(SCHEDULER.PolicyError, "write extension is not allowed"):
            state.write_file("src/app.py", "print('blocked')\n", policy, create_only=False)

    def test_failed_approval_dispatch_persists_approved_and_failed_state(self):
        state = self.build_state()
        task = state.create_task({
            "type": "execution",
            "description": "Task that will fail",
            "approval_scope": {"summary": "approved failure test"},
        })
        original = SCHEDULER.invoke_deepseek_chat
        snapshots = []
        original_save = state.save_task_store

        def capture_save():
            original_save()
            snapshots.append(json.loads(self.task_store_path.read_text(encoding="utf-8")))

        def fail_chat(_task, mode):
            raise RuntimeError("planned failure")

        try:
            state.save_task_store = capture_save
            SCHEDULER.invoke_deepseek_chat = fail_chat
            approved = state.approve_task(task["task_id"], {"approval_token": "approved-by-user"})
        finally:
            SCHEDULER.invoke_deepseek_chat = original

        self.assertEqual(approved["status"], "failed")
        statuses = [snapshot["tasks"][0]["status"] for snapshot in snapshots]
        self.assertIn("awaiting_approval", statuses)
        self.assertIn("running", statuses)
        self.assertEqual(statuses[-1], "failed")

    def test_runtime_endpoints_support_text_and_native_tools(self):
        state = self.build_state()
        handler = SCHEDULER.build_handler(state)
        runtime_server, runtime_thread = self.start_server(handler)
        try:
            base = f"http://127.0.0.1:{runtime_server.server_port}"
            with urllib.request.urlopen(f"{base}/healthz", timeout=2) as res:
                health = json.loads(res.read().decode("utf-8"))
            self.assertTrue(health["ok"])
            self.assertTrue(health["capabilities"]["text_delegate_ready"])
            self.assertTrue(health["capabilities"]["native_tool_agent_ready"])
            self.assertFalse(health["capabilities"]["stream_supported"])

            text_req = urllib.request.Request(
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
            with urllib.request.urlopen(text_req, timeout=5) as res:
                response = json.loads(res.read().decode("utf-8"))
            self.assertEqual(response["status"], "completed")
            self.assertEqual(response["output_text"], "worker-ok")

            native_task_req = urllib.request.Request(
                f"{base}/v1/tasks",
                data=json.dumps({
                    "type": "execution",
                    "description": "Native tool execution",
                    "tool_policy": {
                        "allowed_paths": ["src"],
                        "allowed_tools": ["repo_read_file", "repo_apply_patch"],
                        "read_extensions": [".py"],
                        "write_extensions": [".py"],
                        "max_tool_steps": 4,
                    },
                    "approval_scope": {
                        "summary": "read src/app.py and patch it",
                        "files": ["src/app.py"],
                        "exploration": "listed paths only",
                    },
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(native_task_req, timeout=5) as res:
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
            self.assertEqual(approved_task["status"], "approved")

            tool_req = urllib.request.Request(
                f"{base}/v1/responses",
                data=json.dumps({
                    "model": "deepseek-v4-pro",
                    "input": [{"role": "user", "content": "Update src/app.py to print native-ok."}],
                    "tools": [
                        {"type": "function", "function": {"name": "repo_read_file"}},
                        {"type": "function", "function": {"name": "repo_apply_patch"}},
                    ],
                    "tool_choice": "auto",
                    "metadata": {
                        "scheduler_task_id": created_task["task_id"],
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
                tool_response = json.loads(res.read().decode("utf-8"))
            self.assertEqual(tool_response["status"], "completed")
            self.assertEqual(tool_response["output_text"], "native-tools-ok")
            self.assertEqual((self.root / "src" / "app.py").read_text(encoding="utf-8"), "print('native-ok')\n")

            with urllib.request.urlopen(f"{base}/v1/tasks/{created_task['task_id']}", timeout=5) as res:
                fetched_task = json.loads(res.read().decode("utf-8"))
            self.assertEqual(fetched_task["status"], "success")
            self.assertGreaterEqual(len(fetched_task["result"]["tool_steps"]), 1)

            with urllib.request.urlopen(f"{base}/v1/agents", timeout=5) as res:
                agents = json.loads(res.read().decode("utf-8"))
            self.assertEqual(len(agents["data"]), 2)
            self.assertTrue(agents["capabilities"]["responses_tool_calling"])

            log_text = self.log_path.read_text(encoding="utf-8")
            self.assertNotIn("reasoning_content", log_text)
            self.assertIn("responses_usage", log_text)
            self.assertIn("tool_call", log_text)
        finally:
            runtime_server.shutdown()
            runtime_server.server_close()
            runtime_thread.join(timeout=2)

    def test_tools_mode_requires_scheduler_task_id(self):
        state = self.build_state()
        handler = SCHEDULER.build_handler(state)
        runtime_server, runtime_thread = self.start_server(handler)
        try:
            base = f"http://127.0.0.1:{runtime_server.server_port}"
            req = urllib.request.Request(
                f"{base}/v1/responses",
                data=json.dumps({
                    "tools": [{"type": "function", "function": {"name": "repo_read_file"}}],
                    "tool_choice": "auto",
                }).encode("utf-8"),
                headers={
                    "Authorization": "Bearer sk-test-placeholder",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            runtime_server.shutdown()
            runtime_server.server_close()
            runtime_thread.join(timeout=2)

    def test_unapproved_native_task_is_rejected(self):
        state = self.build_state()
        task = state.create_task({
            "type": "execution",
            "description": "Native task",
            "tool_policy": {
                "allowed_paths": ["src"],
                "allowed_tools": ["repo_read_file"],
                "read_extensions": [".py"],
                "write_extensions": [".py"],
            },
        })
        handler = SCHEDULER.build_handler(state)
        runtime_server, runtime_thread = self.start_server(handler)
        try:
            base = f"http://127.0.0.1:{runtime_server.server_port}"
            req = urllib.request.Request(
                f"{base}/v1/responses",
                data=json.dumps({
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
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 403)
        finally:
            runtime_server.shutdown()
            runtime_server.server_close()
            runtime_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
