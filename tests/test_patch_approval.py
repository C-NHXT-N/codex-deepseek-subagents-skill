import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "skills" / "codex-deepseek-subagents" / "scheduler" / "deepseek_scheduler.py"
SPEC = importlib.util.spec_from_file_location("deepseek_scheduler_patch", MODULE_PATH)
SCHEDULER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCHEDULER)


class PatchApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / ".codex" / "runtime").mkdir(parents=True, exist_ok=True)
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
        self.user_config = self.root / "user_config.json"
        self.user_config.write_text(json.dumps({
            "runtime": {"port": 4000, "log_level": "info"},
            "connected_agents": [
                {"name": "Codex Main", "kind": "codex_main", "endpoint": "local/codex-main", "enabled": True, "capabilities": [], "defaults": {}},
                {"name": "DeepSeek Worker", "kind": "deepseek_worker", "endpoint": "local/deepseek-worker", "enabled": True, "capabilities": ["execution"], "defaults": {"mode": "pro-thinking"}},
            ],
            "tool_calling": {"mode": "native", "fallback_json_protocol": True, "strict": False},
            "defaults": {
                "execution_agent": "DeepSeek Worker",
                "review_agent": "Codex Main",
                "tool_policy": {
                    "allowed_paths": ["src"],
                    "allowed_tools": ["repo_read_file", "repo_apply_patch"],
                    "read_extensions": [".py"],
                    "write_extensions": [".py"],
                    "max_file_read_bytes": 262144,
                    "max_search_results": 20,
                    "max_tool_steps": 4,
                    "allow_full_rewrite": False,
                    "allow_delete": False,
                },
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.state = SCHEDULER.RuntimeState(
            project_root=self.root,
            log_path=str(self.root / ".codex" / "runtime" / "events.log.jsonl"),
            port=0,
            user_config_path=self.user_config,
            task_store_path=self.root / ".codex" / "runtime" / "task_queue.json",
            session_store_path=self.root / ".codex" / "runtime" / "sessions.json",
        )
        os.environ["DEEPSEEK_OPENAI_MODEL"] = "deepseek-v4-pro"
        os.environ["DEEPSEEK_OPENAI_FAST_MODEL"] = "deepseek-v4-flash"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_patch_requires_approval_then_apply_resumes(self):
        task = self.state.create_task({
            "type": "execution",
            "description": "Patch app.py",
            "tool_policy": {
                "allowed_paths": ["src"],
                "allowed_tools": ["repo_apply_patch"],
                "read_extensions": [".py"],
                "write_extensions": [".py"],
                "max_tool_steps": 4,
            },
            "approval_scope": {"summary": "patch src/app.py", "files": ["src/app.py"], "exploration": "listed paths only"},
        })
        approved = self.state.approve_task(task["task_id"], {"approval_token": "approved"})
        calls = {"count": 0}
        original_invoke = SCHEDULER.invoke_deepseek_chat_completion

        def fake_invoke(messages, mode, max_tokens, retry=None, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return {
                    "model": "deepseek-v4-pro",
                    "model_label": "deepseek-v4-pro(thinking)",
                    "content": "",
                    "reasoning_content": "reasoning-1",
                    "tool_calls": [{
                        "id": "call_patch",
                        "type": "function",
                        "function": {
                            "name": "repo_apply_patch",
                            "arguments": json.dumps({"patch": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-print('hello')\n+print('patched')"}),
                        },
                    }],
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 0,
                    "total_tokens": 2,
                    "reasoning_tokens": 1,
                }
            return {
                "model": "deepseek-v4-pro",
                "model_label": "deepseek-v4-pro(thinking)",
                "content": "done",
                "reasoning_content": "",
                "tool_calls": [],
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 0,
                "total_tokens": 2,
                "reasoning_tokens": 0,
            }

        try:
            SCHEDULER.invoke_deepseek_chat_completion = fake_invoke
            turn = SCHEDULER.run_native_tool_turn(
                self.state,
                approved,
                ["repo_apply_patch"],
                [{"role": "user", "content": "Patch the file."}],
                128,
                "pro-thinking",
            )
            self.assertEqual("requires_action", turn["status"])
            patch_id = turn["required_action"]["patch_id"]
            self.assertEqual("print('hello')\n", (self.root / "src" / "app.py").read_text(encoding="utf-8"))
            with self.assertRaises(SCHEDULER.TaskConflictError):
                self.state.apply_approved_patch(approved, patch_id)
            self.state.approve_patch(approved["task_id"], patch_id, {})
            result = self.state.apply_approved_patch(approved, patch_id)
            self.assertEqual("completed", result["status"])
            self.assertEqual("done", result["content"])
            self.assertEqual("print('patched')\n", (self.root / "src" / "app.py").read_text(encoding="utf-8"))
        finally:
            SCHEDULER.invoke_deepseek_chat_completion = original_invoke
