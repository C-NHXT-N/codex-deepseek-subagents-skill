import copy
import json
import time
from pathlib import Path
from uuid import uuid4

from patch_preview import summarize_patch


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
    if event_copy.get("type") == "reasoning.delta":
        event_copy["message"] = "[hidden reasoning]"
    if event_copy.get("type") == "assistant.delta":
        event_copy["message"] = "[hidden assistant output]"
    if event_copy.get("type") == "patch.preview":
        patch = str((event_copy.get("data") or {}).get("patch") or "")
        summary = summarize_patch(patch)
        event_copy["data"] = {"patch_summary": summary}
        event_copy["message"] = "Patch preview generated."
    if event_copy.get("type") == "patch.applied":
        data = event_copy.get("data") or {}
        patch = str(data.get("patch") or "")
        summary = summarize_patch(patch)
        event_copy["data"] = {
            "changed_files": summary["files"],
            "additions": summary["additions"],
            "deletions": summary["deletions"],
            "sha256": summary["sha256"],
        }
    if event_copy.get("type") == "turn.completed":
        event_copy.setdefault("data", {})
        if "content" in event_copy["data"]:
            event_copy["data"]["content"] = "[hidden final content]"
    return event_copy


def append_log(log_path, entry):
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": utc_now(), **entry}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
