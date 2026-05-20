---
name: codex-deepseek-subagents
description: Install, update, test, or remove a cost-optimized Codex multi-agent setup where GPT stays the planner-reviewer and DeepSeek runs through a single Python runtime with visible routing, SSE events, and approved repository tools.
---

# Codex DeepSeek Subagents

Use this skill when the user wants Codex/GPT to remain the main planner-reviewer while DeepSeek handles explicit, confirmable worker tasks through the local runtime.

## Install Flow

Install the skill with Codex `skill-installer`, then materialize project-local files inside the target repository.

Windows PowerShell for the initial install:

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

Compatibility aliases still exist for `start-proxy`, `stop-proxy`, and `test-proxy`, but the canonical command surface is the runtime naming.

## Managed Files

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

`user_config.json` is shareable and non-secret. API keys must stay in `.codex/*.local.*` or environment variables.

## Runtime Responsibilities

The runtime provides:

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
- approved native repository tools for `execution` tasks
- `stream=true` through SSE events
- official DeepSeek `tools` / `tool_calls`
- patch approval `requires_action` responses

Shell command execution remains disabled.

## Delegation Rules

Before delegating to DeepSeek, the main agent must describe exactly what will be sent:

```text
I can send this to DeepSeek worker now.
Scope to be sent: <task summary>; files/paths: <list>; exploration allowed: <none|listed paths only|broader search>.
This may send repository content to DeepSeek or the configured proxy. Confirm before I delegate.
```

Default to `listed paths only`. Prefer `analyze` for read-only repository analysis. There should be no DeepSeek dispatch before user confirmation.

## Agent Registry

`user_config.json` keeps two built-in agent kinds:

- `codex_main`
- `deepseek_worker`

Default routing:

- `analysis` -> `codex_main`
- `review` -> `codex_main`
- `execution` -> `deepseek_worker`

Native repository reads and writes must stay inside the approved tool policy for the execution task.

## Thinking Mode

Use thinking mode only when the task complexity justifies the extra cost:

- simple tasks: `pro` or `flash`
- complex implementation or debugging: `pro-thinking` or `flash-thinking`

Default `--thinking-view` is `hidden`. `summary` only shows local hidden-chars/token metadata. Raw `reasoning_content` is shown only when the user explicitly chooses `--thinking-view raw`, and it must not be persisted or replayed.

## UX Surface

- `delegate` and `analyze` must show a route card and scope card before work is sent.
- `analyze` is the preferred read-only path and only enables list/read/search repository tools.
- native patch tasks must show `patch.preview` before `patch.applied`.
- patch application must always wait for explicit approval through `list-patches`, `show-patch`, `approve-patch`, `reject-patch`, and `apply-patch`.
- `tui` opens a runtime dashboard only and must not send a DeepSeek request by itself.
- if TUI is unavailable in the current terminal, the runtime must print a fallback message and continue with `stream-cli`.

## Maintenance

- `update` rewrites `user_config.json` into the current schema and removes stale legacy artifacts.
- `doctor` reports runtime readiness, route display, reasoning stream support, native tool readiness, and stale legacy artifacts separately.
- `export-shareable` excludes local runtime state, logs, caches, and secrets.
