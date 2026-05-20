# Managed by codex-deepseek-subagents
import io
import os

from patch_preview import patch_summary_lines, summarize_patch


def format_bool(flag):
    return "ON" if flag else "OFF"


def box(title, lines):
    content = [str(line) for line in lines]
    width = max([len(title)] + [len(line) for line in content]) if content else len(title)
    top = f"+- {title} " + "-" * max(0, width - len(title) + 1) + "+"
    body = [f"| {line.ljust(width)} |" for line in content]
    bottom = "+" + "-" * (len(top) - 2) + "+"
    return [top, *body, bottom]


def render_status_card(route, mode_label, status, shell_enabled=False):
    lines = [
        f"Mode: {mode_label}",
        f"Model: {route['model_family']}",
        f"Resolved: {route['display_label']}",
        f"Thinking: {format_bool(route['thinking_type'] == 'enabled')}",
        f"Reasoning effort: {route.get('reasoning_effort') or 'disabled'}",
        f"Status: {status}",
        f"Shell: {'enabled' if shell_enabled else 'disabled'}",
    ]
    return box("Codex DeepSeek Worker", lines)


def render_permission_card(summary, read_paths, write_paths, allowed_tools, endpoint_label):
    lines = [
        f"Summary: {summary or '(none)'}",
        f"Read paths: {', '.join(read_paths or ['none'])}",
        f"Write paths: {', '.join(write_paths or ['none'])}",
        f"Tools: {', '.join(allowed_tools or ['none'])}",
        f"Sends content to: {endpoint_label}",
    ]
    return box("Send Scope", lines)


def render_patch_block(patch_text, patch_view):
    summary = summarize_patch(patch_text)
    lines = patch_summary_lines(summary)
    if patch_view == "full" and patch_text:
        lines.append("")
        lines.extend(str(patch_text).splitlines())
    return box("Patch Preview", lines)


def render_patch_summary_block(patch_id, summary):
    lines = [
        f"Patch ID: {patch_id or '(unknown)'}",
        *patch_summary_lines(summary or {}),
        f"SHA256: {(summary or {}).get('sha256') or ''}",
    ]
    if os.name == "nt":
        lines.extend([
            "Approve:",
            f"  .\\.codex\\deepseek-codex.cmd approve-patch --task-id <task> --patch-id {patch_id}",
            "Reject:",
            f"  .\\.codex\\deepseek-codex.cmd reject-patch --task-id <task> --patch-id {patch_id}",
        ])
    else:
        lines.extend([
            "Approve:",
            f"  ./.codex/deepseek-codex.sh approve-patch --task-id <task> --patch-id {patch_id}",
            "Reject:",
            f"  ./.codex/deepseek-codex.sh reject-patch --task-id <task> --patch-id {patch_id}",
        ])
    return box("Patch Preview", lines)


def render_event(event, thinking_view="hidden", patch_view="summary"):
    event_type = event["type"]
    timestamp = event.get("data", {}).get("_elapsed", "00:00")
    prefix = f"[{timestamp}] {event_type:<22} "
    if event_type == "route.selected":
        route = event.get("route") or {}
        effort = route.get("reasoning_effort") or "disabled"
        return [prefix + f"{route.get('requested_mode')} -> {route.get('display_label')} ({effort})"]
    if event_type == "scope.presented":
        return [prefix + "scope presented"]
    if event_type == "approval.required":
        return [prefix + (event.get("message") or "approval required")]
    if event_type == "approval.confirmed":
        return [prefix + "scope confirmed"]
    if event_type == "request.sending":
        return [prefix + "sending request to DeepSeek"]
    if event_type == "reasoning.started":
        return [prefix + (event.get("message") or "thinking active")]
    if event_type == "reasoning.delta":
        data = event.get("data") or {}
        chars = data.get("chars") or 0
        tokens = data.get("reasoning_tokens") or 0
        if thinking_view == "raw":
            return [prefix + f"Reasoning: {event.get('message') or ''}"]
        if thinking_view == "summary":
            return [prefix + f"Thinking active, reasoning hidden: {chars} chars, {tokens} tokens"]
        return [prefix + "Thinking active, raw reasoning hidden"]
    if event_type == "assistant.delta":
        return [prefix + (event.get("message") or "assistant output")]
    if event_type == "tool.call.delta":
        data = event.get("data") or {}
        return [prefix + f"tool delta idx={data.get('index')} name+={data.get('name_delta') or ''} args+={len(str(data.get('arguments_delta') or ''))}ch"]
    if event_type == "tool.call.started":
        data = event.get("data") or {}
        return [prefix + f"{data.get('tool_name')} {data.get('target') or ''}".rstrip()]
    if event_type == "tool.call.completed":
        data = event.get("data") or {}
        result = data.get("result")
        if isinstance(result, dict) and result.get("error"):
            return [prefix + f"error: {result.get('error')}"]
        if isinstance(result, list):
            detail = f"{len(result)} result(s)"
        elif isinstance(result, dict) and result.get("updated_files"):
            detail = f"{len(result['updated_files'])} file(s) updated"
        elif isinstance(result, str):
            detail = f"{len(result.splitlines())} line(s)"
        else:
            detail = "completed"
        return [prefix + detail]
    if event_type == "tool.protocol.error":
        return [prefix + (event.get("message") or "invalid tool protocol, retrying")]
    if event_type == "patch.preview":
        data = event.get("data") or {}
        if data.get("patch_summary") or data.get("summary"):
            return render_patch_summary_block(data.get("patch_id"), data.get("patch_summary") or data.get("summary") or {})
        patch = data.get("patch") or ""
        return render_patch_block(patch, patch_view)
    if event_type == "patch.approval.required":
        return [prefix + "patch approval required"]
    if event_type == "patch.approval.confirmed":
        return [prefix + "patch approved"]
    if event_type == "patch.applied":
        return [prefix + "patch applied"]
    if event_type == "response.requires_action":
        return [prefix + "response requires patch approval"]
    if event_type == "usage.updated":
        data = event.get("data") or {}
        hit = data.get("prompt_cache_hit_tokens") or 0
        miss = data.get("prompt_cache_miss_tokens") or 0
        ratio = data.get("cache_hit_ratio")
        ratio_text = f"{ratio:.0%}" if isinstance(ratio, (float, int)) else "n/a"
        return [prefix + f"tokens total={data.get('total_tokens') or 0}, reasoning={data.get('reasoning_tokens') or 0}, cache={hit}/{miss} ({ratio_text})"]
    if event_type == "turn.completed":
        return [prefix + "All tasks completed successfully"]
    if event_type == "turn.failed":
        return [
            prefix + (event.get("message") or "task failed"),
            "Suggestion: rerun doctor, review scope, or retry with --yes if confirmation was required.",
        ]
    if event_type == "session.completed":
        return [prefix + "session completed"]
    return [prefix + (event.get("message") or event_type)]


class StreamCliRenderer:
    def __init__(self, out_stream, thinking_view="hidden", patch_view="summary"):
        self.out_stream = out_stream
        self.thinking_view = thinking_view
        self.patch_view = patch_view

    def print_block(self, lines):
        for line in lines:
            self.out_stream.write(str(line) + "\n")
        self.out_stream.flush()

    def render_event(self, event):
        self.print_block(render_event(event, thinking_view=self.thinking_view, patch_view=self.patch_view))
