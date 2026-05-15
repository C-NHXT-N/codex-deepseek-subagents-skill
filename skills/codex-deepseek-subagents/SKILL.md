---
name: codex-deepseek-subagents
description: Install, update, test, or remove a cost-optimized Codex multi-agent setup where GPT acts as planner/reviewer and DeepSeek runs explicit worker tasks through a local scheduler runtime and Responses-compatible shim.
---

# Codex DeepSeek Subagents

Use this skill when the user wants GPT to stay as the main Codex planner/reviewer while DeepSeek handles explicit, confirmable worker tasks.

## Install Flow

Use the official Codex `skill-installer` to install this skill from GitHub. After the skill is present in `CODEX_HOME/skills`, use the bundled installer inside the target project:

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 start-runtime
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 test-proxy
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
```

Linux/macOS shell:

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh install --api-key <deepseek-key>
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh start-runtime
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh test-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh doctor
```

Compatibility aliases:

- `start-proxy` -> `start-runtime`
- `stop-proxy` -> `stop-runtime`

## Managed Files

The installer writes project-local files only:

```text
user_config.json
.codex/config.toml
.codex/agents/deepseek-worker.toml
.codex/deepseek.local.env.sh
.codex/deepseek.local.env.ps1
.codex/deepseek_responses_shim.py
.codex/runtime/deepseek_scheduler.py
.codex/runtime/task_queue.json
.codex/test-deepseek-direct.sh
.codex/test-deepseek-direct.ps1
.codex/test-responses-proxy.sh
.codex/test-responses-proxy.ps1
```

`user_config.json` is non-secret and shareable. API keys must stay in `.codex/*.local.*` or environment variables only.

## Runtime Responsibilities

The local scheduler runtime provides:

- `GET /healthz`
- `GET /v1/agents`
- `POST /v1/tasks`
- `GET /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/approve`
- `POST /v1/tasks/{task_id}/retry`
- `POST /v1/responses`

`/v1/responses` remains a smoke-test endpoint. It does not implement `stream=true`, `tools`, or `tool_choice`. In v1, DeepSeek can receive approved text delegation through the scheduler, but it is not a native tool-calling Codex execution agent.

## Delegation Rules

Before spawning or delegating to DeepSeek, the GPT main agent must tell the user exactly what will be sent:

```text
I can send this to DeepSeek worker now.
Scope to be sent: <task summary>; files/paths: <list>; exploration allowed: <none|listed paths only|broader search>.
This may send repository content to DeepSeek or the configured proxy. Confirm before I delegate.
```

Default to `listed paths only`. If the task can be done from a summary, send the summary instead of full file contents. There should be no DeepSeek dispatch before the user confirms.

If Codex Desktop says `agent type is currently not available` for `deepseek_worker`, use the `delegate` command as the fallback. It sends only the explicit prompt or prompt file content and does not auto-read repository files.

## Agent Registry

`user_config.json` supports two built-in agent kinds in v1:

- `codex_main`: logical planner/reviewer route only
- `deepseek_worker`: real execution worker route

Default routes:

- `analysis` and `review` -> `codex_main`
- `execution` -> `deepseek_worker`

Execution tasks require approval through `/v1/tasks/{task_id}/approve` before the scheduler dispatches them.

## Thinking Mode

Use DeepSeek thinking mode only when it is worth the token cost:

- Simple edits or formatting: `pro` or `flash`
- Complex implementation or debugging: `pro-thinking` or `flash-thinking`
- Ambiguous architecture: max reasoning only after warning about extra cost

Never persist or replay raw `reasoning_content`. Logs should keep token metadata and coarse execution summaries only.

## Maintenance

- `update` should migrate older shim-only installs to the scheduler runtime.
- `doctor` should report config presence, registry summary, text-delegation readiness, native tool-agent readiness, direct API health, and runtime health separately.
- `export-shareable` should exclude local secrets, logs, backups, and runtime state.
