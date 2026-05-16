import argparse
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_DIR = REPO_ROOT / "skills" / "codex-deepseek-subagents" / "scheduler"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._sys_path = list(os.sys.path)
        if str(SCHEDULER_DIR) not in os.sys.path:
            os.sys.path.insert(0, str(SCHEDULER_DIR))
        cls.runtime = load_module("deepseek_runtime_test", SCHEDULER_DIR / "deepseek_runtime.py")
        cls.events = load_module("deepseek_events_test", SCHEDULER_DIR / "events.py")
        cls.render = load_module("deepseek_render_test", SCHEDULER_DIR / "render.py")
        cls.doctor = load_module("deepseek_doctor_test", SCHEDULER_DIR / "doctor.py")
        cls.usage = load_module("deepseek_usage_test", SCHEDULER_DIR / "usage.py")

    @classmethod
    def tearDownClass(cls):
        os.sys.path[:] = cls._sys_path

    def test_default_cli_hides_reasoning(self):
        event = {
            "type": "reasoning.delta",
            "data": {"_elapsed": "00:01", "chars": 1284, "reasoning_tokens": 512},
            "message": "secret chain of thought",
        }
        lines = self.render.render_event(event, thinking_view="hidden")
        self.assertEqual(1, len(lines))
        self.assertIn("Thinking active, raw reasoning hidden", lines[0])
        self.assertNotIn("secret chain of thought", lines[0])

    def test_summary_thinking_view_does_not_print_raw_reasoning(self):
        event = {
            "type": "reasoning.delta",
            "data": {"_elapsed": "00:01", "chars": 1284, "reasoning_tokens": 512},
            "message": "secret chain of thought",
        }
        lines = self.render.render_event(event, thinking_view="summary")
        self.assertIn("1284 chars", lines[0])
        self.assertIn("512 tokens", lines[0])
        self.assertNotIn("secret chain of thought", lines[0])

    def test_raw_thinking_view_prints_reasoning(self):
        event = {
            "type": "reasoning.delta",
            "data": {"_elapsed": "00:01", "chars": 1284, "reasoning_tokens": 512},
            "message": "secret chain of thought",
        }
        lines = self.render.render_event(event, thinking_view="raw")
        self.assertIn("secret chain of thought", lines[0])

    def test_mode_model_thinking_precedence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "user_config.json").write_text(
                json.dumps(
                    {
                        "runtime": {"port": 4000, "log_level": "info"},
                        "connected_agents": [
                            {"name": "Codex Main", "kind": "codex_main", "endpoint": "local/codex-main", "enabled": True, "capabilities": [], "defaults": {}},
                            {"name": "DeepSeek Worker", "kind": "deepseek_worker", "endpoint": "local/deepseek-worker", "enabled": True, "capabilities": ["execution"], "defaults": {"mode": "flash"}},
                        ],
                        "defaults": {
                            "execution_agent": "DeepSeek Worker",
                            "review_agent": "Codex Main",
                            "tool_policy": {
                                "allowed_paths": ["."],
                                "allowed_tools": ["repo_read_file"],
                                "read_extensions": [".py"],
                                "write_extensions": [".py"],
                                "max_file_read_bytes": 1,
                                "max_search_results": 1,
                                "max_tool_steps": 1,
                            },
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"DEEPSEEK_OPENAI_MODEL": "deepseek-v4-pro", "DEEPSEEK_OPENAI_FAST_MODEL": "deepseek-v4-flash"}, clear=False):
                self.assertEqual(
                    "pro-thinking",
                    self.runtime.resolve_mode(argparse.Namespace(mode="pro-thinking", model=None, thinking=None), root),
                )
                self.assertEqual(
                    "flash",
                    self.runtime.resolve_mode(argparse.Namespace(mode=None, model="flash", thinking="off"), root),
                )
                self.assertEqual(
                    "flash-thinking",
                    self.runtime.resolve_mode(argparse.Namespace(mode=None, model="flash", thinking="on"), root),
                )
                self.assertEqual(
                    "flash",
                    self.runtime.resolve_mode(argparse.Namespace(mode=None, model=None, thinking=None), root),
                )

    def test_non_tty_analyze_requires_yes_before_task_creation(self):
        args = argparse.Namespace(
            project_root=".",
            prompt="Analyze this repository.",
            prompt_file="",
            port=0,
            mode=None,
            model=None,
            thinking=None,
            thinking_view="hidden",
            patch_view="summary",
            ui="stream",
            json=False,
            yes=False,
            max_tokens=128,
            max_tool_steps=4,
            paths=None,
            verbose=False,
        )
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False
        with mock.patch.object(self.runtime, "load_project_env"), \
             mock.patch.object(self.runtime, "build_renderer", return_value=self.render.StreamCliRenderer(io.StringIO())), \
             mock.patch.object(self.runtime.scheduler, "build_route", return_value={"model_family": "pro", "display_label": "deepseek-v4-pro", "thinking_type": "enabled"}), \
             mock.patch.object(self.runtime, "load_state", side_effect=AssertionError("load_state should not be called before confirmation")), \
             mock.patch("sys.stdin", fake_stdin):
            with self.assertRaisesRegex(RuntimeError, "Confirmation required but stdin is not interactive"):
                self.runtime.command_analyze(args)

    def test_patch_preview_storage_omits_full_patch(self):
        patch = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-print('hello')\n+print('ok')\n"
        event = self.events.make_event(
            "patch.preview",
            data={"patch": patch, "patch_summary": {"files": ["src/app.py"], "additions": 1, "deletions": 1, "sha256": "abc"}},
        )
        stored = self.events.sanitize_event_for_storage(event)
        self.assertNotIn("patch", stored["data"])
        self.assertEqual(["src/app.py"], stored["data"]["patch_summary"]["files"])

    def test_doctor_report_separates_declared_capabilities_and_checks(self):
        report = self.doctor.build_doctor_report(
            install_state="ok",
            existing={"user_config": True, "config": True, "worker": True, "env": True, "runtime": True},
            runtime_health={"ok": True, "capabilities": {"route_display_ready": True, "reasoning_stream_ready": True, "native_tool_agent_ready": True, "interactive_cli_ready": True}},
            user_config_valid=True,
            pid_exists=True,
            process_alive=True,
            deep_checks={"sse_stream_smoke": True},
            stale_legacy_artifacts=[],
        )
        self.assertIn("capabilities_declared", report)
        self.assertIn("checks", report)
        self.assertTrue(report["checks"]["runtime_health"])
        self.assertTrue(report["checks"]["sse_stream_smoke"])

    def test_usage_human_output(self):
        rows = [
            {"kind": "responses_usage", "model_label": "deepseek-v4-pro(thinking)", "prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 3, "total_tokens": 18},
            {"kind": "responses_usage", "model_label": "deepseek-v4-flash", "prompt_tokens": 7, "completion_tokens": 2, "reasoning_tokens": 0, "total_tokens": 9},
        ]
        summary = self.usage.summarize_usage(rows)
        lines = self.usage.render_usage(summary)
        text = "\n".join(lines)
        self.assertIn("Requests: 2", text)
        self.assertIn("deepseek-v4-pro(thinking)", text)
        self.assertIn("deepseek-v4-flash", text)

