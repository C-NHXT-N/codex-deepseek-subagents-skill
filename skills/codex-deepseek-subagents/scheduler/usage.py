import json
from pathlib import Path

from render import box


def load_usage_rows(log_path):
    path = Path(log_path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize_usage(rows):
    usage_rows = [row for row in rows if row.get("kind") == "responses_usage"]
    by_model = {}
    for row in usage_rows:
        label = row.get("model_label") or row.get("model") or "unknown"
        entry = by_model.setdefault(label, {"model_label": label, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0})
        entry["requests"] += 1
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"):
            entry[key] += int(row.get(key) or 0)
    return {
        "requests": len(usage_rows),
        "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in usage_rows),
        "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in usage_rows),
        "reasoning_tokens": sum(int(row.get("reasoning_tokens") or 0) for row in usage_rows),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in usage_rows),
        "by_model": [by_model[key] for key in sorted(by_model)],
    }


def render_usage(summary):
    lines = [
        f"Requests: {summary['requests']}",
        f"Total tokens: {summary['total_tokens']}",
        f"Prompt tokens: {summary['prompt_tokens']}",
        f"Completion tokens: {summary['completion_tokens']}",
        f"Reasoning tokens: {summary['reasoning_tokens']}",
    ]
    for item in summary["by_model"]:
        lines.append(f"{item['model_label']}: {item['requests']} req / {item['total_tokens']} tokens")
    return box("DeepSeek Usage", lines)
