---
name: codex-deepseek-subagents
description: Install, update, test, or remove a cost-optimized Codex multi-agent setup where GPT acts as planner/reviewer and DeepSeek runs explicit worker tasks through a local Responses-compatible proxy. Use when the user asks about Codex cost reduction, GPT main agent with DeepSeek subagents, DeepSeek worker setup, context-gated delegation, DeepSeek thinking mode, one-click install/update/uninstall, proxy diagnostics, OpenCode/Claude-Code-style DeepSeek references, or sharing this setup as a reusable Codex skill.
---

# Codex DeepSeek Subagents

## Quick Start

Use the bundled scripts instead of hand-writing project config. Both the Bash and PowerShell installers generate the same managed cross-platform file set under `.codex/`, and API keys are written only to `.codex/*.local.*`.

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 start-proxy
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 test-proxy
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
```

PowerShell Core on any platform:

```powershell
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 start-proxy
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 test-proxy
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 delegate -Mode pro-thinking -ThinkingView hidden -Prompt "Summarize the files I explicitly provide."
```

Linux/macOS shell:

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh install --api-key <deepseek-key>
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh start-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh test-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh doctor
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh delegate --mode pro-thinking --thinking-view hidden --prompt "Summarize the files I explicitly provide."
```

Custom port example:

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh install \
  --api-key <deepseek-key> \
  --port 5001

bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh start-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh test-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh doctor
```

Later `start-proxy`, `test-proxy`, and `doctor` reuse the installed `DEEPSEEK_PROXY_BASE_URL` port unless you explicitly pass `--port` or `-Port`.

Run destructive or broad actions with `-DryRun` first:

```powershell
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 uninstall -DryRun
```

After installing or copying this skill into Codex, verify that only one active copy exists under `CODEX_HOME/skills`. Do not keep backups under `CODEX_HOME/skills/backups`, because Codex scans nested `SKILL.md` files there as active skills. Move backups outside the skills tree, such as `CODEX_HOME/skill-backups`.

## Commands

- `install`: create managed Codex provider, DeepSeek worker, local env files, proxy shims, tests, and `.gitignore` rules.
- `update`: refresh key, model, base URL, port, and thinking defaults while preserving non-managed user files.
- `uninstall`: remove only files marked `# Managed by codex-deepseek-subagents`, stop the local proxy when needed, and remove proxy runtime files.
- `doctor`: check local files, gitignore, direct DeepSeek API, thinking mode, and proxy health.
- `desktop-doctor`: same checks as `doctor`, with a note about Codex Desktop native subagent availability.
- `delegate`: direct fallback call to DeepSeek when Codex Desktop has not registered `deepseek_worker`.
- `start-proxy`, `stop-proxy`, `test-proxy`: manage the local `/v1/responses` shim.
- `usage`: summarize prompt, completion, and reasoning tokens from proxy logs, grouped by visible model label when available.
- `redact`: scan for leaked `sk-...` keys outside `.local` files, logs, backups, and `.git`.
- `export-shareable`: create a zip of the skill folder without project `.codex` secrets.

Useful options:

- `-ProjectRoot <path>`: install into another project.
- `-ApiKey <key>`: set or update the DeepSeek key.
- `-Port 4000`: choose the local proxy port. Explicit command-line port values override the installed env port.
- `-Prompt <text>` or `-PromptFile <path>`: explicit content to send with `delegate`; no files are read automatically.
- `-DryRun`: print intended changes without writing.
- `-Force`: allow overwriting non-managed target files after review.

## Managed Files

Both installers generate the same helper set:

```text
.codex/config.toml
.codex/agents/deepseek-worker.toml
.codex/deepseek.local.env.sh
.codex/deepseek.local.env.ps1
.codex/deepseek_responses_shim.py
.codex/deepseek-responses-shim.ps1
.codex/test-deepseek-direct.sh
.codex/test-deepseek-direct.ps1
.codex/test-responses-proxy.sh
.codex/test-responses-proxy.ps1
```

The installer appends these gitignore rules when missing:

```text
.codex/*.local.*
.codex/deepseek-proxy.log.jsonl
.codex/deepseek-proxy.pid
.codex/deepseek-proxy.stdout.log
.codex/deepseek-proxy.stderr.log
.codex/backups/
```

## Delegation Rules

Before spawning the DeepSeek worker, the GPT main agent must tell the user exactly what will be sent:

```text
I can send this to DeepSeek worker now.
Scope to be sent: <task summary>; files/paths: <list>; exploration allowed: <none|listed paths only|broader search>.
This may send repository content to DeepSeek or the configured proxy. Confirm before I delegate.
```

Default to `listed paths only`. If the task can be done from a summary, send the summary and paths rather than full file contents. There should be no DeepSeek proxy log entry before the user confirms delegation.

If Codex Desktop reports `agent type is currently not available` for `deepseek_worker`, use the `delegate` fallback after the same confirmation. It sends only `-Prompt` or `-PromptFile` content and never reads repository files automatically.

## Thinking Mode

Use DeepSeek thinking mode only when it is worth the token cost:

- Simple edits, formatting, and mechanical tasks: use `pro` or `flash`.
- Complex implementation or debugging: use `pro-thinking` or `flash-thinking`.
- Ambiguous architecture or hard agentic work: use max reasoning effort only after warning about extra cost.

Never persist or feed `reasoning_content` back into prompts. Keep only coarse metadata such as reasoning token counts and whether reasoning was present.

## Architecture Notes

Codex custom providers require a Responses-compatible endpoint. DeepSeek's public API is OpenAI/Anthropic-compatible, so this skill installs a local smoke-test shim that exposes `/v1/responses` and forwards to DeepSeek Chat Completions.

The local shim is a smoke-test/basic-experiment proxy, not a production-complete Responses implementation. Unsupported features such as `stream=true`, `tools`, and `tool_choice` fail with a clear `400` error instead of being silently ignored.

If you need full Codex tool-call or streaming support, replace the shim with a mature Responses-compatible proxy and validate those behaviors yourself.

The default local proxy is the Python shim at `.codex/deepseek_responses_shim.py`. The PowerShell shim is installed only as a compatibility/reference file because `System.Net.HttpListener` is not reliable in every Windows host environment.

## Safety Checks

Run `doctor`, `redact`, and `usage` before sharing results. The skill folder must not contain API keys, logs, backups, or machine-specific `.codex` state. Project installs are intentionally separate from the shareable skill folder.
