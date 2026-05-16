# Codex DeepSeek Subagents

Keep Codex/GPT as the planner-reviewer and use DeepSeek through a single Python-first runtime line: local scheduler, readable CLI feedback, `analyze`, approved native repository tools, and SSE-backed `/v1/responses` / session APIs.

## Install

Use two stages:

1. Install the skill from GitHub with Codex `skill-installer`.
2. Inside the target project, materialize the local runtime under `.codex/`.

Windows PowerShell for the initial install step:

```powershell
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
```

After install, prefer the generated local wrappers:

```powershell
.\.codex\deepseek-codex.cmd start-runtime
.\.codex\deepseek-codex.cmd test-runtime
.\.codex\deepseek-codex.cmd doctor
.\.codex\deepseek-codex.cmd analyze --prompt "Analyze this repository."
.\.codex\deepseek-codex.cmd tui
```

Linux/macOS:

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh install --api-key <deepseek-key>
./.codex/deepseek-codex.sh start-runtime
./.codex/deepseek-codex.sh test-runtime
./.codex/deepseek-codex.sh doctor
./.codex/deepseek-codex.sh analyze --prompt "Analyze this repository."
./.codex/deepseek-codex.sh tui
```

`start-proxy`, `stop-proxy`, and `test-proxy` still work as compatibility aliases, but the project now documents only the runtime naming.

## Installed Files

Project-managed files:

```text
user_config.json
.codex/config.toml
.codex/agents/deepseek-worker.toml
.codex/deepseek.local.env.sh
.codex/deepseek.local.env.ps1
.codex/deepseek-codex.cmd
.codex/deepseek-codex.sh
.codex/runtime/deepseek_scheduler.py
.codex/runtime/deepseek_runtime.py
.codex/runtime/deepseek_client.py
.codex/runtime/events.py
.codex/runtime/render.py
.codex/runtime/patch_preview.py
.codex/runtime/tool_protocol.py
.codex/runtime/usage.py
.codex/runtime/doctor.py
.codex/runtime/tui.py
.codex/runtime/task_queue.json
.codex/runtime/sessions.json
.codex/runtime/events.log.jsonl
.codex/runtime/stdout.log
.codex/runtime/stderr.log
.codex/test-runtime.sh
.codex/test-runtime.ps1
```

Secrets still live only in `.codex/*.local.*` or environment variables. `user_config.json` is shareable and is rewritten into the current schema during `update`.

## Runtime Model

The local runtime is a single Python service that provides:

- `GET /healthz`
- `GET /v1/agents`
- `POST /v1/tasks`
- `GET /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/approve`
- `POST /v1/tasks/{task_id}/retry`
- `POST /v1/responses`
- `POST /v1/sessions`
- `GET /v1/sessions/{session_id}`
- `GET /v1/sessions/{session_id}/events`

`/v1/responses` supports:

- text delegation
- approved native repository tools bound to `metadata.scheduler_task_id`
- `stream=true` via SSE event streaming

Shell command execution remains disabled.

## Commands

Primary commands:

- `install`
- `update`
- `doctor`
- `start-runtime`
- `stop-runtime`
- `test-runtime`
- `delegate`
- `analyze`
- `tui`
- `usage`

Maintenance commands still available:

- `uninstall`
- `usage`
- `redact`
- `export-shareable`

## Safety Rules

Before sending work to DeepSeek, tell the user exactly what will be sent:

```text
I can send this to DeepSeek worker now.
Scope to be sent: <task summary>; files/paths: <list>; exploration allowed: <none|listed paths only|broader search>.
This may send repository content to DeepSeek or the configured proxy. Confirm before I delegate.
```

Default to `listed paths only`. `analyze` is the preferred read-only path and only enables repository listing, reading, and text search.

## What You Will See

Default interaction uses `stream-cli`. Each `delegate` or `analyze` session shows:

- route card: model family, resolved model, thinking state, mode, shell disabled
- scope card: summary, read/write paths, allowed tools, DeepSeek endpoint
- live timeline: route selection, approval, reasoning state, tool calls, patch preview, completion
- usage summary at the end

Useful runtime flags:

- `--thinking-view hidden|summary|raw`
- `--patch-view hidden|summary|full`
- `--ui stream|tui`
- `doctor --deep`
- `usage --json`

`hidden` is the default thinking view. Raw reasoning is only shown when the user explicitly asks for `--thinking-view raw`, and it is not written to runtime logs or session storage.

`tui` opens a runtime dashboard only. It does not send a DeepSeek request by itself. If there is an active session it shows that session; otherwise it shows runtime status, recent sessions, and a doctor summary. When TUI is unavailable, the runtime prints a clear fallback message and continues with `stream-cli`.

## Capability Boundary

The current line is:

- Python-first runtime as the single implementation source
- visible route metadata: model family, resolved model, thinking state
- interactive CLI plus SSE session events
- approved native repository tools: `repo_list_files`, `repo_read_file`, `repo_search_text`, `repo_apply_patch`, `repo_write_file`, and optional `repo_delete_file`

It does not provide arbitrary shell execution and does not persist raw `reasoning_content`.

## Development

Repo checks:

```bash
bash tests/test_install_templates.sh
pwsh ./tests/test_static_checks.ps1
python3 -m unittest discover -s tests -p "test_*.py"
```

## License

MIT License. See [LICENSE](LICENSE).
