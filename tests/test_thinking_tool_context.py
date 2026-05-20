import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "skills" / "codex-deepseek-subagents" / "scheduler" / "deepseek_scheduler.py"
SPEC = importlib.util.spec_from_file_location("deepseek_scheduler_thinking", MODULE_PATH)
SCHEDULER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCHEDULER)


class ThinkingToolContextTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / ".codex" / "runtime").mkdir(parents=True, exist_ok=True)
        self.user_config = self.root / "user_config.json"
        self.user_config.write_text(json.dumps({
            "runtime": {"port": 4000, "log_level": "info"},
            "connected_agents": [
                {"name": "Codex Main", "kind": "codex_main", "endpoint": "local/codex-main", "enabled": True, "capabilities": [], "defaults": {}},
                {"name": "DeepSeek Worker", "kind": "deepseek_worker", "endpoint": "local/deepseek-worker", "enabled": True, "capabilities": ["execution"], "defaults": {"mode": "pro-thinking"}},
            ],
            "defaults": {
                "execution_agent": "DeepSeek Worker",
                "review_agent": "Codex Main",
                "tool_policy": {
                    "allowed_paths": ["."],
                    "allowed_tools": ["repo_read_file", "repo_apply_patch"],
                    "read_extensions": [".py", ".md"],
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

    def test_reasoning_is_kept_in_continuation_but_not_logs(self):
        task = self.state.create_task({
            "type": "execution",
            "description": "Create pending patch",
            "tool_policy": {
                "allowed_paths": ["."],
                "allowed_tools": ["repo_apply_patch"],
                "read_extensions": [".py", ".md"],
                "write_extensions": [".py"],
                "max_tool_steps": 4,
            },
            "approval_scope": {"summary": "patch", "files": ["."], "exploration": "listed paths only"},
        })
        approved = self.state.approve_task(task["task_id"], {"approval_token": "approved"})
        original_invoke = SCHEDULER.invoke_deepseek_chat_completion

        def fake_invoke(messages, mode, max_tokens, retry=None, **kwargs):
            return {
                "model": "deepseek-v4-pro",
                "model_label": "deepseek-v4-pro(thinking)",
                "content": "",
                "reasoning_content": "super secret reasoning",
                "tool_calls": [{
                    "id": "call_patch",
                    "type": "function",
                    "function": {
                        "name": "repo_apply_patch",
                        "arguments": json.dumps({"patch": "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+print('x')"}),
                    },
                }],
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 0,
                "total_tokens": 2,
                "reasoning_tokens": 1,
            }

        try:
            SCHEDULER.invoke_deepseek_chat_completion = fake_invoke
            turn = SCHEDULER.run_native_tool_turn(
                self.state,
                approved,
                ["repo_apply_patch"],
                [{"role": "user", "content": "Patch it."}],
                128,
                "pro-thinking",
            )
        finally:
            SCHEDULER.invoke_deepseek_chat_completion = original_invoke

        self.assertEqual("requires_action", turn["status"])
        continuation_id = turn["required_action"]["summary"].get("continuation_id") if isinstance(turn["required_action"].get("summary"), dict) else None
        patch = self.state.find_pending_patch(approved["task_id"], turn["required_action"]["patch_id"])
        continuation = self.state.get_continuation(patch["continuation_id"])
        assistant_message = continuation["messages"][-1]
        self.assertEqual("super secret reasoning", assistant_message["reasoning_content"])
        self.assertTrue(assistant_message["tool_calls"])
        log_text = (self.root / ".codex" / "runtime" / "events.log.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("super secret reasoning", log_text)
