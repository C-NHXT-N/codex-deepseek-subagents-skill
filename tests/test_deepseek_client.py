import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "skills" / "codex-deepseek-subagents" / "scheduler" / "deepseek_client.py"
SPEC = importlib.util.spec_from_file_location("deepseek_client", MODULE_PATH)
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)


class FakeResponse:
    def __init__(self, body):
        self._stream = io.BytesIO(body)

    def read(self):
        return self._stream.read()

    def __iter__(self):
        return iter(self._stream.readline, b"")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class DeepSeekClientTests(unittest.TestCase):
    def setUp(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-test"

    def test_non_stream_completion_parses_reasoning_tool_calls_and_usage(self):
        payload = {
            "model": "deepseek-v4-pro",
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "reasoning_content": "scratchpad",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "repo_read_file", "arguments": "{\"path\":\"README.md\"}"},
                    }],
                },
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_cache_hit_tokens": 7,
                "prompt_cache_miss_tokens": 3,
                "total_tokens": 15,
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        }
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(payload).encode("utf-8"))):
            result = CLIENT.invoke_deepseek_chat_completion(
                messages=[{"role": "user", "content": "hello"}],
                model="deepseek-v4-pro",
                thinking={"type": "enabled", "reasoning_effort": "high"},
                max_tokens=32,
            )
        self.assertEqual("scratchpad", result["reasoning_content"])
        self.assertEqual("tool_calls", result["finish_reason"])
        self.assertEqual("repo_read_file", result["tool_calls"][0]["function"]["name"])
        self.assertEqual(7, result["prompt_cache_hit_tokens"])
        self.assertEqual(3, result["prompt_cache_miss_tokens"])
        self.assertEqual(2, result["reasoning_tokens"])

    def test_stream_completion_emits_deltas_usage_and_done(self):
        chunks = [
            b'data: {"id":"chatcmpl_1","model":"deepseek-v4-pro","choices":[{"delta":{"reasoning_content":"think","content":"hello ","tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"repo_","arguments":"{\\"path\\":"}}]},"finish_reason":null}]}\n\n',
            b'data: {"id":"chatcmpl_1","model":"deepseek-v4-pro","choices":[{"delta":{"content":"world","tool_calls":[{"index":0,"type":"function","function":{"name":"read_file","arguments":"\\"README.md\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":10,"completion_tokens":4,"prompt_cache_hit_tokens":6,"prompt_cache_miss_tokens":4,"total_tokens":14,"completion_tokens_details":{"reasoning_tokens":2}}}\n\n',
            b"data: [DONE]\n\n",
        ]
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"".join(chunks))):
            events = list(CLIENT.stream_deepseek_chat_completion(
                messages=[{"role": "user", "content": "hello"}],
                model="deepseek-v4-pro",
                thinking={"type": "enabled", "reasoning_effort": "high"},
                max_tokens=32,
                stream_options={"include_usage": True},
            ))
        event_types = [event["type"] for event in events]
        self.assertIn("reasoning_delta", event_types)
        self.assertIn("content_delta", event_types)
        self.assertIn("tool_call_delta", event_types)
        self.assertIn("usage", event_types)
        self.assertEqual("done", event_types[-1])
        usage = next(event["usage"] for event in events if event["type"] == "usage")
        self.assertEqual(6, usage["prompt_cache_hit_tokens"])
        self.assertEqual(4, usage["prompt_cache_miss_tokens"])
        tool_deltas = [event for event in events if event["type"] == "tool_call_delta"]
        self.assertEqual("repo_", tool_deltas[0]["name_delta"])
        self.assertEqual("read_file", tool_deltas[1]["name_delta"])
