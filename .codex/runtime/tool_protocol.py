# Managed by codex-deepseek-subagents
import json
import re


def parse_jsonish_object(content):
    text = str(content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_tool_loop_response(content):
    parsed = parse_jsonish_object(content)
    if not parsed:
        return {"type": "final", "content": str(content or "").strip()}
    response_type = str(parsed.get("type") or "").strip()
    if response_type == "final":
        return {"type": "final", "content": str(parsed.get("content") or "")}
    if response_type == "tool_call":
        tool_name = str(parsed.get("tool_name") or parsed.get("name") or "").strip()
        arguments = parsed.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("tool_call.arguments must be an object")
        if not tool_name:
            raise ValueError("tool_call.tool_name is required")
        return {"type": "tool_call", "tool_name": tool_name, "arguments": arguments}
    if parsed.get("tool_name") or parsed.get("name"):
        arguments = parsed.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("tool_call.arguments must be an object")
        return {
            "type": "tool_call",
            "tool_name": str(parsed.get("tool_name") or parsed.get("name")),
            "arguments": arguments,
        }
    return {"type": "final", "content": str(parsed.get("content") or str(content or "").strip())}


def looks_like_failed_tool_protocol(content):
    text = str(content or "").lower()
    markers = (
        "repo_read_file",
        "repo_list_files",
        "repo_apply_patch",
        "repo_write_file",
        "repo_search_text",
        "repo_delete_file",
        "tool_call",
        "```json",
        '"type"',
        '"arguments"',
    )
    return any(marker in text for marker in markers)


def is_invalid_final_response(content):
    text = str(content or "").strip()
    lowered = text.lower()
    if not text:
        return False
    parsed = parse_jsonish_object(content)
    if isinstance(parsed, dict):
        response_type = str(parsed.get("type") or "").strip()
        if response_type == "final":
            return looks_like_failed_tool_protocol(parsed.get("content") or "")
        return False
    if lowered.count('"type"') > 1:
        return True
    if lowered.count('"tool_name"') > 1:
        return True
    if lowered.count('"arguments"') > 1:
        return True
    if lowered.count("tool_call") > 1:
        return True
    if re.search(r'```\s*(?:json)?\s*{', lowered) and "tool_call" in lowered:
        return True
    if re.search(r'}\s*{', text):
        return True
    if re.search(r'}\s*\n\s*{', text):
        return True
    return looks_like_failed_tool_protocol(content)


def repair_message(error_detail):
    return (
        "Your previous response did not follow the required JSON tool protocol. "
        f"{error_detail} "
        "Return JSON only, either "
        '{"type":"tool_call","tool_name":"...","arguments":{...}} '
        'or {"type":"final","content":"..."}.'
    )
