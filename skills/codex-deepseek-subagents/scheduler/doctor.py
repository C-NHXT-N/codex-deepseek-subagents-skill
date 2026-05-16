from render import box


def build_doctor_report(install_state, existing, runtime_health=None, runtime_health_error=None, user_config_valid=None, pid_exists=False, process_alive=False, deep_checks=None, stale_legacy_artifacts=None):
    report = {
        "install_state": install_state,
        "capabilities_declared": {
            "runtime_ready": True,
            "route_display_ready": True,
            "reasoning_stream_ready": True,
            "native_tool_agent_ready": True,
            "interactive_cli_ready": True,
        },
        "checks": {
            "user_config_exists": existing.get("user_config", False),
            "config_exists": existing.get("config", False),
            "worker_exists": existing.get("worker", False),
            "env_file_exists": existing.get("env", False),
            "runtime_entry_exists": existing.get("runtime", False),
            "user_config_valid": user_config_valid,
            "runtime_pid_exists": pid_exists,
            "runtime_process_alive": process_alive,
            "runtime_health": runtime_health.get("ok") if isinstance(runtime_health, dict) else False,
        },
        "stale_legacy_artifacts": stale_legacy_artifacts or [],
        "suggestions": [],
    }
    if runtime_health_error:
        report["checks"]["runtime_health_error"] = runtime_health_error
    if deep_checks:
        report["checks"].update(deep_checks)
    if not report["checks"]["env_file_exists"]:
        report["suggestions"].append("Run install/update to create .codex/deepseek.local.env.*")
    if report["checks"].get("runtime_health") is False:
        report["suggestions"].append("Run .codex/deepseek-codex start-runtime")
    if report["stale_legacy_artifacts"]:
        report["suggestions"].append("Run update to remove stale legacy artifacts.")
    if not report["suggestions"]:
        report["suggestions"].append("No action required.")
    return report


def render_doctor_report(report):
    checks = report.get("checks") or {}
    lines = [
        f"Install state: {report.get('install_state')}",
        f"Runtime: {'running' if checks.get('runtime_process_alive') else 'stopped'}",
        f"API key: {'present' if checks.get('env_file_exists') else 'missing'}",
        f"Config: {'valid' if checks.get('user_config_valid') else 'invalid'}",
        f"Health: {'ok' if checks.get('runtime_health') else 'unavailable'}",
    ]
    lines.extend(f"Suggestion: {item}" for item in report.get("suggestions") or [])
    return box("Doctor", lines)
