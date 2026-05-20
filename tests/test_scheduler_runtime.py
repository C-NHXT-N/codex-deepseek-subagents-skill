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
        if payload.get("stream"):
            self.send_stream(body, payload)
            return
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_stream(self, body, payload):
        choice = body["choices"][0]
        message = choice["message"]
        usage = body.get("usage") or {}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def write_event(data):
            chunk = f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
            self.wfile.write(chunk)
            self.wfile.flush()

        delta = {}
        if message.get("reasoning_content"):
            delta["reasoning_content"] = message["reasoning_content"]
        if message.get("content"):
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = []
            for index, tool_call in enumerate(message["tool_calls"]):
                delta["tool_calls"].append({
                    "index": index,
                    "id": tool_call.get("id"),
                    "type": "function",
                    "function": {
                        "name": tool_call.get("function", {}).get("name", ""),
                        "arguments": tool_call.get("function", {}).get("arguments", ""),
                    },
                })
        write_event({
            "id": body["id"],
            "model": body["model"],
            "choices": [{"delta": delta, "finish_reason": None}],
        })
        write_event({
            "id": body["id"],
            "model": body["model"],
            "choices": [{"delta": {}, "finish_reason": choice.get("finish_reason")}],
            "usage": usage,
        })
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def build_body(self, payload, system_text, messages):
        if "DeepSeek native repository worker" not in system_text:
            return self.text_response(payload, "worker-ok", "internal reasoning that must not be logged", 11, 7, 18, 3)

        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            return self.tool_response(
                payload,
                "native step 1",
                [{
                    "id": "call_read_file",
                    "type": "function",
                    "function": {"name": "repo_read_file", "arguments": json.dumps({"path": "src/app.py"})},
                }],
                13,
                5,
                18,
                2,
            )
        latest_tool_text = "\n".join(str(message.get("content") or "") for message in tool_messages)
        if "patch_applied" not in latest_tool_text:
            return self.tool_response(
                payload,
                "native step 2",
                [{
                    "id": "call_apply_patch",
                    "type": "function",
                    "function": {
                        "name": "repo_apply_patch",
                        "arguments": json.dumps({
                            "patch": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-print('hello')\n+print('native-ok')"
                        }),
                    },
                }],
                14,
                6,
                20,
                2,
            )
        return self.text_response(payload, "native-tools-ok", "native done", 9, 4, 13, 1)

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
                "prompt_cache_hit_tokens": 2,
                "prompt_cache_miss_tokens": 5,
                "total_tokens": total_tokens,
                "completion_tokens_details": {
                    "reasoning_tokens": reasoning_tokens,
                },
            },
        }

    def tool_response(self, payload, reasoning, tool_calls, prompt_tokens, completion_tokens, total_tokens, reasoning_tokens):
        return {
            "id": "chatcmpl-test",
            "model": payload["model"],
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "reasoning_content": reasoning,
                    "tool_calls": tool_calls,
                },
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "prompt_cache_hit_tokens": 3,
                "prompt_cache_miss_tokens": 7,
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
        self.log_path = self.root / ".codex" / "runtime" / "events.log.jsonl"
        self.task_store_path = self.root / ".codex" / "runtime" / "task_queue.json"
        self.session_store_path = self.root / ".codex" / "runtime" / "sessions.json"
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
            session_store_path=self.session_store_path,
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

    def test_normalize_user_config_for_write_preserves_values_and_drops_unknown_top_level_keys(self):
        template = {
            "runtime": {"port": 4000, "log_level": "info"},
            "ui": {"default_mode": "stream-cli", "show_reasoning": True},
            "connected_agents": [
                {"name": "Codex Main", "kind": "codex_main", "endpoint": "local/codex-main", "enabled": True, "capabilities": [], "defaults": {}},
                {"name": "DeepSeek Worker", "kind": "deepseek_worker", "endpoint": "local/deepseek-worker", "enabled": True, "capabilities": ["execution"], "defaults": {"mode": "pro-thinking"}},
            ],
            "defaults": {"execution_agent": "DeepSeek Worker", "review_agent": "Codex Main", "tool_policy": {"allowed_paths": ["."], "allowed_tools": ["repo_read_file"], "read_extensions": [".py"], "write_extensions": [".py"], "max_file_read_bytes": 1, "max_search_results": 1, "max_tool_steps": 1}},
        }
        existing = {
            "runtime": {"port": 5001},
            "defaults": {"verbose": False, "default_allowed_tools": ["repo_search_text"]},
            "deprecated": True,
        }
        normalized = SCHEDULER.normalize_user_config_for_write(existing, template)
        self.assertEqual(normalized["runtime"]["port"], 5001)
        self.assertFalse(normalized["defaults"]["verbose"])
        self.assertNotIn("deprecated", normalized)
        self.assertNotIn("default_allowed_tools", normalized["defaults"])
        self.assertEqual(normalized["defaults"]["tool_policy"]["allowed_tools"], ["repo_search_text"])

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
        original = SCHEDULER.invoke_deepseek_chat_completion
        snapshots = []
        original_save = state.save_task_store

        def capture_save():
            original_save()
            snapshots.append(json.loads(self.task_store_path.read_text(encoding="utf-8")))

        def fail_chat(messages, mode, max_tokens, retry=None, **kwargs):
            raise RuntimeError("planned failure")

        try:
            state.save_task_store = capture_save
            SCHEDULER.invoke_deepseek_chat_completion = fail_chat
            approved = state.approve_task(task["task_id"], {"approval_token": "approved-by-user"})
        finally:
            SCHEDULER.invoke_deepseek_chat_completion = original

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
            self.assertTrue(health["capabilities"]["runtime_ready"])
            self.assertTrue(health["capabilities"]["text_delegate_ready"])
            self.assertTrue(health["capabilities"]["native_tool_agent_ready"])
            self.assertTrue(health["capabilities"]["stream_supported"])
            self.assertTrue(str(health["session_store_path"]).replace("\\", "/").endswith(".codex/runtime/sessions.json"))

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
            self.assertEqual(response["model_label"], "deepseek-v4-pro")
            self.assertEqual(response["route"]["display_label"], "deepseek-v4-pro")
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
            self.assertEqual(tool_response["status"], "requires_action")
            self.assertEqual(tool_response["model_label"], "deepseek-v4-pro(thinking)")
            self.assertEqual(tool_response["route"]["display_label"], "deepseek-v4-pro(thinking)")
            self.assertEqual(tool_response["required_action"]["type"], "patch_approval")
            patch_id = tool_response["required_action"]["patch_id"]
            self.assertEqual((self.root / "src" / "app.py").read_text(encoding="utf-8"), "print('hello')\n")

            with urllib.request.urlopen(f"{base}/v1/tasks/{created_task['task_id']}/patches", timeout=5) as res:
                patches = json.loads(res.read().decode("utf-8"))
            self.assertEqual(1, len(patches["data"]))
            self.assertEqual(patch_id, patches["data"][0]["patch_id"])

            approve_patch_req = urllib.request.Request(
                f"{base}/v1/tasks/{created_task['task_id']}/patches/{patch_id}/approve",
                data=json.dumps({"approval_note": "ok"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(approve_patch_req, timeout=5) as res:
                approved_patch = json.loads(res.read().decode("utf-8"))
            self.assertEqual("approved", approved_patch["status"])

            apply_patch_req = urllib.request.Request(
                f"{base}/v1/tasks/{created_task['task_id']}/patches/{patch_id}/apply",
                data=json.dumps({}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(apply_patch_req, timeout=5) as res:
                apply_result = json.loads(res.read().decode("utf-8"))
            self.assertEqual("completed", apply_result["status"])
            self.assertEqual("native-tools-ok", apply_result["content"])
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

    def test_runtime_stream_and_session_endpoints(self):
        state = self.build_state()
        handler = SCHEDULER.build_handler(state)
        runtime_server, runtime_thread = self.start_server(handler)
        try:
            base = f"http://127.0.0.1:{runtime_server.server_port}"
            stream_req = urllib.request.Request(
                f"{base}/v1/responses",
                data=json.dumps({
                    "model": "deepseek-v4-pro",
                    "input": [{"role": "user", "content": "say ok"}],
                    "metadata": {"deepseek_reasoning_effort": "disabled"},
                    "stream": True,
                    "max_output_tokens": 64,
                }).encode("utf-8"),
                headers={
                    "Authorization": "Bearer sk-test-placeholder",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(stream_req, timeout=5) as res:
                body = res.read().decode("utf-8")
            self.assertIn("event: route.selected", body)
            self.assertIn("event: reasoning.delta", body)
            self.assertIn("event: turn.completed", body)

            session_req = urllib.request.Request(
                f"{base}/v1/sessions",
                data=json.dumps({
                    "model": "deepseek-v4-pro",
                    "input": [{"role": "user", "content": "say ok"}],
                    "metadata": {"deepseek_reasoning_effort": "disabled"},
                    "max_output_tokens": 64,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(session_req, timeout=5) as res:
                session = json.loads(res.read().decode("utf-8"))
            self.assertEqual(session["status"], "completed")
            self.assertGreater(session["event_count"], 0)
            with urllib.request.urlopen(f"{base}/v1/sessions/{session['session_id']}", timeout=5) as res:
                session_info = json.loads(res.read().decode("utf-8"))
            self.assertEqual(session_info["route"]["display_label"], "deepseek-v4-pro")
            with urllib.request.urlopen(f"{base}/v1/sessions/{session['session_id']}/events", timeout=5) as res:
                session_events = res.read().decode("utf-8")
            self.assertIn("event: route.selected", session_events)
            session_store = json.loads(self.session_store_path.read_text(encoding="utf-8"))
            self.assertEqual(len(session_store["sessions"]), 1)
            serialized = json.dumps(session_store, ensure_ascii=False)
            self.assertNotIn("internal reasoning", serialized)
            self.assertNotIn("hidden scratchpad", serialized)
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

    def test_native_tool_turn_recovers_from_missing_path_error(self):
        state = self.build_state()
        task = state.create_task({
            "type": "execution",
            "description": "Recover from missing path",
            "tool_policy": {
                "allowed_paths": [".", "src", "README.md"],
                "allowed_tools": ["repo_list_files", "repo_read_file"],
                "read_extensions": [".py", ".md"],
                "write_extensions": [".py"],
                "max_tool_steps": 4,
            },
            "approval_scope": {
                "summary": "recover after bad path",
                "files": [".", "README.md"],
                "exploration": "listed paths only",
            },
        })
        approved = state.approve_task(task["task_id"], {"approval_token": "approved-by-user"})
        (self.root / "README.md").write_text("hello\n", encoding="utf-8")

        calls = {"count": 0}
        original_invoke = SCHEDULER.invoke_deepseek_chat_completion
        original_stream = SCHEDULER.stream_deepseek_chat_completion
        events = []

        def fake_invoke(messages, mode, max_tokens, retry=None, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                content = json.dumps({"type": "tool_call", "tool_name": "repo_list_files", "arguments": {"directory": "codex"}})
            elif calls["count"] == 2:
                content = json.dumps({"type": "tool_call", "tool_name": "repo_read_file", "arguments": {"path": "README.md"}})
            else:
                content = json.dumps({"type": "final", "content": "recovered-ok"})
            return {
                "model": "deepseek-v4-pro",
                "model_label": "deepseek-v4-pro(thinking)",
                "content": content,
                "tool_calls": [],
                "reasoning_content": "",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 0,
                "total_tokens": 2,
                "reasoning_tokens": 0,
            }

        try:
            SCHEDULER.invoke_deepseek_chat_completion = fake_invoke
            SCHEDULER.stream_deepseek_chat_completion = lambda **kwargs: iter(())
            result = SCHEDULER.run_native_tool_turn(
                state,
                approved,
                ["repo_list_files", "repo_read_file"],
                [{"role": "user", "content": "Recover from a missing directory path and continue."}],
                128,
                "pro-thinking",
                event_sink=events.append,
            )
        finally:
            SCHEDULER.invoke_deepseek_chat_completion = original_invoke
            SCHEDULER.stream_deepseek_chat_completion = original_stream

        self.assertEqual("recovered-ok", result["content"])
        completed = [event for event in events if event["type"] == "tool.call.completed"]
        self.assertTrue(any(
            isinstance((event.get("data") or {}).get("result"), dict)
            and ((event.get("data") or {}).get("result") or {}).get("recoverable") is True
            for event in completed
        ))
        self.assertEqual("response.completed", events[-1]["type"])

    def test_native_tool_turn_rejects_fake_final_after_protocol_retries(self):
        state = self.build_state()
        task = state.create_task({
            "type": "execution",
            "description": "Reject fake final",
            "tool_policy": {
                "allowed_paths": ["."],
                "allowed_tools": ["repo_read_file"],
                "read_extensions": [".py", ".md", ".json", ".sh", ".ps1", ".toml"],
                "write_extensions": [".py"],
                "max_tool_steps": 4,
            },
            "approval_scope": {
                "summary": "reject fake final",
                "files": ["."],
                "exploration": "listed paths only",
            },
        })
        approved = state.approve_task(task["task_id"], {"approval_token": "approved-by-user"})

        raw_fake_final = (
            '{"type": "tool_call", "tool_name": "repo_read_file", "arguments": {"path": "tests/test_install_runtime_smoke.py"}}\n'
            '{"type": "tool_call", "tool_name": "repo_read_file", "arguments": {"path": "tests/test_runtime_ux.py"}}'
        )
        calls = {"count": 0}
        original_invoke = SCHEDULER.invoke_deepseek_chat_completion
        original_stream = SCHEDULER.stream_deepseek_chat_completion
        events = []

        def fake_invoke(messages, mode, max_tokens, retry=None, **kwargs):
            calls["count"] += 1
            return {
                "model": "deepseek-v4-pro",
                "model_label": "deepseek-v4-pro(thinking)",
                "content": raw_fake_final,
                "tool_calls": [],
                "reasoning_content": "",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 0,
                "total_tokens": 2,
                "reasoning_tokens": 0,
            }

        try:
            SCHEDULER.invoke_deepseek_chat_completion = fake_invoke
            SCHEDULER.stream_deepseek_chat_completion = lambda **kwargs: iter(())
            with self.assertRaisesRegex(SCHEDULER.TaskConflictError, "valid final response"):
                SCHEDULER.run_native_tool_turn(
                    state,
                    approved,
                    ["repo_read_file"],
                    [{"role": "user", "content": "Return a real final answer only."}],
                    128,
                    "pro-thinking",
                    event_sink=events.append,
                )
        finally:
            SCHEDULER.invoke_deepseek_chat_completion = original_invoke
            SCHEDULER.stream_deepseek_chat_completion = original_stream

        self.assertEqual(1, len([event for event in events if event["type"] == "tool.protocol.error"]))
        self.assertEqual("turn.failed", events[-1]["type"])
        self.assertFalse(any(event["type"] == "assistant.delta" for event in events))
        self.assertFalse(any(event["type"] == "turn.completed" for event in events))


if __name__ == "__main__":
    unittest.main()
