# Managed by codex-deepseek-subagents
import copy
import json
import time
from pathlib import Path
from uuid import uuid4


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def make_event(
    event_type,
    step=None,
    message="",
    status=None,
    route=None,
    data=None,
    session_id=None,
    task_id=None,
):
    return {
        "event_id": f"evt_{uuid4().hex}",
        "session_id": session_id,
        "task_id": task_id,
        "ts": utc_now(),
        "type": event_type,
        "status": status,
        "step": step,
        "message": message,
        "route": route,
        "data": data or {},
    }


def emit_event(event_sink, event_type, **kwargs):
    event = make_event(event_type, **kwargs)
    if event_sink:
        event_sink(event)
    return event


def build_sse_payload(event):
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


def write_sse_headers(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.end_headers()


def write_sse_event(handler, event):
    handler.wfile.write(build_sse_payload(event))
    handler.wfile.flush()


def sanitize_event_for_storage(event):
    event_copy = copy.deepcopy(event)
    data = event_copy.get("data") or {}
    if event_copy.get("type") == "reasoning.delta":
        event_copy["message"] = "[hidden reasoning]"
        event_copy["data"] = {
            "chars": data.get("chars"),
            "reasoning_tokens": data.get("reasoning_tokens"),
            "hash": data.get("hash"),
        }
    if event_copy.get("type") == "assistant.delta":
        event_copy["message"] = "[hidden assistant output]"
        event_copy["data"] = {"delta_chars": len(str(event.get("message") or ""))}
    if event_copy.get("type") == "patch.preview":
        event_copy["data"] = {
            "patch_id": data.get("patch_id"),
            "patch_summary": copy.deepcopy(data.get("patch_summary") or data.get("summary") or {}),
        }
        event_copy["message"] = "Patch preview generated."
    if event_copy.get("type") == "patch.applied":
        event_copy["data"] = {
            "patch_id": data.get("patch_id"),
            "changed_files": copy.deepcopy((data.get("summary") or {}).get("files") or data.get("changed_files") or []),
            "additions": (data.get("summary") or {}).get("additions", data.get("additions")),
            "deletions": (data.get("summary") or {}).get("deletions", data.get("deletions")),
            "sha256": (data.get("summary") or {}).get("sha256", data.get("sha256")),
        }
    if event_copy.get("type") == "tool.call.completed":
        result = data.get("result")
        if isinstance(result, str):
            event_copy["data"]["result"] = {
                "result_type": "text",
                "byte_count": len(result.encode("utf-8")),
                "line_count": len(result.splitlines()),
                "path": data.get("target"),
            }
        elif isinstance(result, dict) and "result" in result:
            inner = result.get("result")
            if isinstance(inner, str):
                event_copy["data"]["result"] = {
                    "result_type": "text",
                    "byte_count": len(inner.encode("utf-8")),
                    "line_count": len(inner.splitlines()),
                    "path": data.get("target"),
                }
        elif isinstance(result, dict) and result.get("error"):
            event_copy["data"]["result"] = {
                "result_type": "error",
                "error": result.get("error"),
                "error_category": result.get("error_category"),
            }
    if event_copy.get("type") in {"turn.completed", "response.completed"}:
        event_copy.setdefault("data", {})
        if "content" in event_copy["data"]:
            event_copy["data"]["content"] = "[hidden final content]"
        if "output_text" in event_copy["data"]:
            event_copy["data"]["output_text"] = "[hidden assistant output]"
    return event_copy


def append_log(log_path, entry):
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": utc_now(), **entry}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
