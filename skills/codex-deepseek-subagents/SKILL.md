---
name: codex-deepseek-subagents
description: Install, update, test, or remove a cost-optimized Codex multi-agent setup where GPT acts as planner/reviewer and DeepSeek runs explicit worker tasks through a local Responses-compatible proxy. Use when the user asks about Codex cost reduction, GPT main agent with DeepSeek subagents, DeepSeek worker setup, context-gated delegation, DeepSeek thinking mode, one-click install/update/uninstall, proxy diagnostics, OpenCode/Claude-Code-style DeepSeek references, or sharing this setup as a reusable Codex skill.
---

# Codex DeepSeek Subagents

## Quick Start

Use the bundled PowerShell script instead of hand-writing project config. The script installs only into the target project `.codex/` folder and writes API keys only to `.codex/deepseek.local.env.ps1`, which it adds to `.gitignore`.

```powershell
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 start-proxy
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 test-proxy
```

On Windows systems without PowerShell Core, use `powershell -ExecutionPolicy Bypass -File` instead of `pwsh`.

Run destructive or broad actions with `-DryRun` first:

```powershell
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 uninstall -DryRun
```

## Commands

- `install`: create managed Codex provider, DeepSeek worker, local env, proxy shim, tests, and `.gitignore` rules.
- `update`: refresh key/model/base URL/port/thinking defaults while preserving non-managed user files.
- `uninstall`: remove only files marked `# Managed by codex-deepseek-subagents`; use `-RemoveSkill` to delete the skill folder too.
- `doctor`: check local files, gitignore, direct DeepSeek API, thinking mode, and proxy health.
- `start-proxy`, `stop-proxy`, `test-proxy`: manage the local `/v1/responses` shim.
- `usage`: summarize prompt/completion/reasoning tokens from proxy logs.
- `redact`: scan for leaked `sk-...` keys outside `.local` files, logs, backups, and `.git`.
- `export-shareable`: create a zip of the skill folder without project `.codex` secrets.

Useful options:

- `-ProjectRoot <path>`: install into another project.
- `-ApiKey <key>`: set or update the DeepSeek key.
- `-Model deepseek-v4-pro`, `-FastModel deepseek-v4-flash`: choose worker and fallback models.
- `-Port 4000`: choose the local proxy port.
- `-ThinkingDefault disabled|high|max`: set proxy default thinking behavior.
- `-DryRun`: print intended changes without writing.
- `-Force`: allow overwriting non-managed target files after review.
- `-NoBackup`: skip backups; default behavior backs up changed files to `.codex/backups/`.

## Delegation Rules

Before spawning the DeepSeek worker, the GPT main agent must tell the user exactly what will be sent:

```text
I can send this to DeepSeek worker now.
Scope to be sent: <task summary>; files/paths: <list>; exploration allowed: <none|listed paths only|broader search>.
This may send repository content to DeepSeek or the configured proxy. Confirm before I delegate.
```

Default to `listed paths only`. If the task can be done from a summary, send the summary and paths rather than full file contents. There should be no DeepSeek proxy log entry before the user confirms delegation.

## Thinking Mode

Use DeepSeek thinking mode only when it is worth the token cost:

- Simple edits, formatting, and mechanical tasks: `thinking.type = "disabled"`.
- Complex implementation or debugging: `thinking.type = "enabled"` with `reasoning_effort = "high"`.
- Ambiguous architecture or hard agentic work: use `reasoning_effort = "max"` only after warning about extra cost.

Never persist or feed `reasoning_content` back into prompts. Keep only coarse metadata such as reasoning token counts and whether reasoning was present.

## Architecture Notes

Codex custom providers require a Responses-compatible endpoint. DeepSeek's public API is OpenAI/Anthropic-compatible, so this skill installs a local smoke-test shim that exposes `/v1/responses` and forwards to DeepSeek Chat Completions. Prefer a mature open-source proxy such as LiteLLM or Julep Open Responses for production use if it passes Codex tool-call and streaming tests.

Use DeepSeek OpenCode/Claude Code integration docs as provider/model references, not as proof that Codex can directly use those endpoints. Codex still needs its configured provider to satisfy the current Codex `wire_api = "responses"` behavior.

## Safety Checks

Run `doctor`, `redact`, and `usage` before sharing results. The skill folder must not contain API keys, logs, backups, or machine-specific `.codex` state. Project installs are intentionally separate from the shareable skill folder.
