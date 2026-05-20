import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "skills" / "codex-deepseek-subagents" / "scheduler" / "deepseek_scheduler.py"
SPEC = importlib.util.spec_from_file_location("deepseek_scheduler_native_tools", MODULE_PATH)
SCHEDULER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCHEDULER)


class NativeToolCallTests(unittest.TestCase):
    def test_build_official_tool_schemas_supports_strict(self):
        schemas = SCHEDULER.build_official_tool_schemas(["repo_read_file", "repo_write_file"], strict=True)
        self.assertEqual(2, len(schemas))
        self.assertTrue(all(schema["function"].get("strict") is True for schema in schemas))
        self.assertEqual(["path"], schemas[0]["function"]["parameters"]["required"])
        self.assertFalse(schemas[0]["function"]["parameters"]["additionalProperties"])

    def test_normalize_user_config_keeps_tool_calling_sections(self):
        template = {
            "runtime": {"port": 4000, "log_level": "info"},
            "ui": {},
            "connected_agents": [
                {"name": "Codex Main", "kind": "codex_main", "endpoint": "local/codex-main", "enabled": True, "capabilities": [], "defaults": {}},
                {"name": "DeepSeek Worker", "kind": "deepseek_worker", "endpoint": "local/deepseek-worker", "enabled": True, "capabilities": ["execution"], "defaults": {"mode": "pro-thinking"}},
            ],
            "tool_calling": {"mode": "native", "fallback_json_protocol": True, "strict": False},
            "routing": {"default": "flash"},
            "privacy": {"persist_raw_reasoning": False},
            "defaults": {"execution_agent": "DeepSeek Worker", "review_agent": "Codex Main", "tool_policy": {"allowed_paths": ["."], "allowed_tools": ["repo_read_file"], "read_extensions": [".py"], "write_extensions": [".py"], "max_file_read_bytes": 1, "max_search_results": 1, "max_tool_steps": 1}},
        }
        existing = {
            "tool_calling": {"strict": True},
            "routing": {"execution": "pro-thinking"},
            "privacy": {"persist_assistant_output": False},
        }
        merged = SCHEDULER.normalize_user_config_for_write(existing, template)
        self.assertTrue(merged["tool_calling"]["strict"])
        self.assertEqual("pro-thinking", merged["routing"]["execution"])
        self.assertFalse(merged["privacy"]["persist_assistant_output"])
