# Codex DeepSeek Subagents

Keep GPT as the Codex planner/reviewer and delegate explicit implementation work to DeepSeek only after user confirmation. This v2 layout installs a project-local scheduler runtime under `.codex/runtime/`, exposes a local Responses-compatible endpoint for Codex, and adds a simple task/approval API for future multi-agent workflows.

## Install

Use two stages:

1. Install the skill from GitHub with Codex's official `skill-installer`.
2. Inside the target project, run the bundled installer to materialize local runtime files under `.codex/`.

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 start-runtime
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 test-proxy
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
```

PowerShell Core:

```powershell
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 start-runtime
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 test-proxy
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
```

Linux/macOS shell:

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh install --api-key <deepseek-key>
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh start-runtime
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh test-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh doctor
```

`start-proxy` and `stop-proxy` remain as compatibility aliases for `start-runtime` and `stop-runtime`.

## What Gets Installed

Project-managed files:

```text
user_config.json
.codex/config.toml
.codex/agents/deepseek-worker.toml
.codex/deepseek.local.env.sh
.codex/deepseek.local.env.ps1
.codex/deepseek_responses_shim.py
.codex/deepseek-responses-shim.ps1
.codex/runtime/deepseek_scheduler.py
.codex/runtime/task_queue.json
.codex/test-deepseek-direct.sh
.codex/test-deepseek-direct.ps1
.codex/test-responses-proxy.sh
.codex/test-responses-proxy.ps1
```

Secrets still live only in `.codex/*.local.*` or environment variables. `user_config.json` is intentionally non-secret and shareable.

## Runtime Model

The local scheduler runtime is a single Python process. It serves:

- `GET /healthz`
- `GET /v1/agents`
- `POST /v1/tasks`
- `GET /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/approve`
- `POST /v1/tasks/{task_id}/retry`
- `POST /v1/responses`

`/v1/responses` now supports two modes:

- Text compatibility mode for the existing smoke-test path.
- Approved native tool mode for `execution` tasks that carry a `tool_policy` and are referenced through `metadata.scheduler_task_id`.

`stream=true` still returns `400`. Shell command execution is intentionally disabled in v1.

## Agent Registry

`user_config.json` defines the runtime registry. v1 supports two agent kinds:

- `codex_main`: logical GPT planner/reviewer entry used for routing and audit only
- `deepseek_worker`: real execution worker invoked by the scheduler

Default routes:

- `analysis` -> `codex_main`
- `review` -> `codex_main`
- `execution` -> `deepseek_worker`

Execution tasks must be approved before the scheduler dispatches them. Native tool execution binds every repository read/write to an approved execution task.

## Commands

- `install`: create or refresh the project-local runtime, config, env files, tests, and gitignore rules.
- `update`: migrate older shim-only installs to the scheduler runtime while preserving managed local secrets.
- `uninstall`: remove managed project files and runtime state.
- `doctor`: validate config presence, user config shape, registry summary, collaboration capability boundaries, direct API health, thinking mode, and runtime health.
- `desktop-doctor`: same checks as `doctor`.
- `delegate`: explicit DeepSeek fallback when Codex native subagent registration is unavailable.
- `start-runtime`, `stop-runtime`: start or stop the local scheduler.
- `start-proxy`, `stop-proxy`: compatibility aliases.
- `test-proxy`: send a smoke-test request through the local Responses endpoint.
- `usage`: summarize usage from runtime logs.
- `redact`: scan for leaked `sk-...` keys outside ignored local files.
- `export-shareable`: export the skill folder without project runtime state or secrets.

## Safety Rules

Before any DeepSeek delegation, the GPT main agent should tell the user exactly what will be sent:

```text
I can send this to DeepSeek worker now.
Scope to be sent: <task summary>; files/paths: <list>; exploration allowed: <none|listed paths only|broader search>.
This may send repository content to DeepSeek or the configured proxy. Confirm before I delegate.
```

Do not persist or replay raw `reasoning_content`. Logs record task metadata, status, token usage, and execution summaries only.

## Capability Boundary

`doctor` reports two separate readiness flags:

- `text_delegate_ready`: approved text delegation through the scheduler can be used.
- `native_tool_agent_ready`: approved repository tools are available through the scheduler runtime.

Supported native tools are `repo_list_files`, `repo_read_file`, `repo_search_text`, `repo_apply_patch`, `repo_write_file`, and optionally `repo_delete_file` when a task explicitly approves deletion. `stream=true` and shell execution remain out of scope for this version.

## Development

Repo checks:

```bash
bash tests/test_install_templates.sh
pwsh ./tests/test_static_checks.ps1
python3 -m unittest discover -s tests -p "test_scheduler_runtime.py"
```

## License

MIT License. See [LICENSE](LICENSE).
