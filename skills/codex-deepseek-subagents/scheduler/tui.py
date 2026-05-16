import os
import sys
import time

from render import box, render_event


def tui_support_reason(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if not getattr(stdin, "isatty", lambda: False)():
        return "stdin is not interactive"
    if not getattr(stdout, "isatty", lambda: False)():
        return "stdout is not a tty"
    term = os.environ.get("TERM", "")
    if not term or term == "dumb":
        return "terminal does not support ANSI alternate screen"
    return ""


def tui_available(stdin=None, stdout=None):
    return tui_support_reason(stdin=stdin, stdout=stdout) == ""


class DashboardState:
    def __init__(self):
        self.header = []
        self.scope = []
        self.timeline = []
        self.trace = []
        self.output = []

    def apply(self, event, thinking_view="hidden", patch_view="summary"):
        lines = render_event(event, thinking_view=thinking_view, patch_view=patch_view)
        if event["type"] == "route.selected":
            self.timeline = self.timeline[-20:] + lines
        elif event["type"] == "scope.presented":
            self.scope = lines
        elif event["type"] in {"assistant.delta", "patch.preview", "turn.failed", "turn.completed"}:
            self.output = lines
            self.timeline = self.timeline[-20:] + lines[:1]
        else:
            self.trace = (self.trace + lines)[-10:]
            self.timeline = (self.timeline + lines[:1])[-20:]


def render_dashboard(state):
    parts = []
    parts.extend(box("Runtime", state.header or ["No active session"]))
    parts.extend(box("Scope", state.scope or ["No scope"]))
    parts.extend(box("Timeline", state.timeline or ["No events yet"]))
    parts.extend(box("DeepSeek Work Trace", state.trace or ["No trace yet"]))
    parts.extend(box("Output / Patch Preview", state.output or ["No output yet"]))
    return parts


def render_runtime_dashboard_snapshot(active_session=None, doctor_summary=None, recent_sessions=None):
    state = DashboardState()
    if active_session:
        state.header = [f"Active session: {active_session.get('session_id')}", f"Status: {active_session.get('status')}"]
    else:
        state.header = ["No active session", f"Recent sessions: {len(recent_sessions or [])}"]
    state.scope = doctor_summary or ["Runtime dashboard only", "No DeepSeek request sent."]
    return render_dashboard(state)
