---
name: codex-deepseek-subagents
description: Install, update, test, or remove a cost-optimized Codex multi-agent setup where GPT acts as planner/reviewer and DeepSeek runs explicit worker tasks through a local Responses-compatible proxy. Use when the user asks about Codex cost reduction, GPT main agent with DeepSeek subagents, DeepSeek worker setup, context-gated delegation, DeepSeek thinking mode, one-click install/update/uninstall, proxy diagnostics, OpenCode/Claude-Code-style DeepSeek references, or sharing this setup as a reusable Codex skill.
---

# Codex DeepSeek Subagents

## Quick Start

Use the bundled script instead of hand-writing project config. The scripts install only into the target project `.codex/` folder and write API keys only to `.codex/deepseek.local.env.ps1` or `.codex/deepseek.local.env.sh`, which they add to `.gitignore`.

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
```

PowerShell Core on any platform:

```powershell
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 delegate -Mode pro-thinking -Prompt "Summarize the files I explicitly provide."
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 start-proxy
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 test-proxy
```

Linux/macOS shell:

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh install --api-key <deepseek-key>
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh doctor
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh delegate --mode pro-thinking --prompt "Summarize the files I explicitly provide."
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh start-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh test-proxy
```

Run destructive or broad actions with `-DryRun` first:

```powershell
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 uninstall -DryRun
```

## Commands

- `install`: create managed Codex provider, DeepSeek worker, local env, proxy shim, tests, and `.gitignore` rules.
- `update`: refresh key/model/base URL/port/thinking defaults while preserving non-managed user files.
- `uninstall`: remove only files marked `# Managed by codex-deepseek-subagents`; use `-RemoveSkill` to delete the skill folder too.
- `doctor`: check local files, gitignore, direct DeepSeek API, thinking mode, and proxy health.
- `desktop-doctor`: same checks as `doctor`, with a note about Codex Desktop native subagent availability.
- `delegate`: direct fallback call to DeepSeek when Codex Desktop has not registered `deepseek_worker`.
- `start-proxy`, `stop-proxy`, `test-proxy`: manage the local `/v1/responses` shim.
- `usage`: summarize prompt/completion/reasoning tokens from proxy logs, grouped by visible model label when available.
- `redact`: scan for leaked `sk-...` keys outside `.local` files, logs, backups, and `.git`.
- `export-shareable`: create a zip of the skill folder without project `.codex` secrets.

Useful options:

- `-ProjectRoot <path>`: install into another project.
- `-ApiKey <key>`: set or update the DeepSeek key.
- `-Model deepseek-v4-pro`, `-FastModel deepseek-v4-flash`: choose worker and fallback models.
- `-Port 4000`: choose the local proxy port.
- `-ThinkingDefault disabled|high|max`: set proxy default thinking behavior.
- `-Mode pro-thinking|flash-thinking|pro|flash`: select the DeepSeek delegate model and thinking mode.
- `-Prompt <text>` or `-PromptFile <path>`: explicit content to send with `delegate`; no files are read automatically.
- `-MaxTokens <n>`: output token cap for `delegate`.
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

If Codex Desktop reports `agent type is currently not available` for `deepseek_worker`, use the `delegate` fallback after the same confirmation. It sends only `-Prompt` or `-PromptFile` content and prints the visible model label and token accounting:

```powershell
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 delegate -Mode pro-thinking -Prompt "<confirmed task>"
```

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh delegate --mode pro-thinking --prompt "<confirmed task>"
```

## Thinking Mode

Use DeepSeek thinking mode only when it is worth the token cost:

- Simple edits, formatting, and mechanical tasks: use `pro` or `flash` (`thinking.type = "disabled"`).
- Complex implementation or debugging: use `pro-thinking` or `flash-thinking` (`thinking.type = "enabled"`, `reasoning_effort = "high"`).
- Ambiguous architecture or hard agentic work: use `reasoning_effort = "max"` only after warning about extra cost.

Never persist or feed `reasoning_content` back into prompts. Keep only coarse metadata such as reasoning token counts and whether reasoning was present.

Visible labels used by fallback and proxy logs:

- `pro-thinking`: `deepseek-v4-pro(thinking)`
- `flash-thinking`: `deepseek-v4-flash(thinking)`
- `pro`: `deepseek-v4-pro`
- `flash`: `deepseek-v4-flash`

## Architecture Notes

Codex custom providers require a Responses-compatible endpoint. DeepSeek's public API is OpenAI/Anthropic-compatible, so this skill installs a local smoke-test shim that exposes `/v1/responses` and forwards to DeepSeek Chat Completions. Prefer a mature open-source proxy such as LiteLLM or Julep Open Responses for production use if it passes Codex tool-call and streaming tests.

Use DeepSeek OpenCode/Claude Code integration docs as provider/model references, not as proof that Codex can directly use those endpoints. Codex still needs its configured provider to satisfy the current Codex `wire_api = "responses"` behavior.

Codex Desktop native subagent cards are controlled by the Desktop runtime. A skill can install `.codex/agents/deepseek-worker.toml`, but it cannot force the app to register or visually render that agent type. When native registration is unavailable, `delegate` is the supported fallback and provides visible model/thinking labels in command output and logs.

## Safety Checks

Run `doctor`, `redact`, and `usage` before sharing results. The skill folder must not contain API keys, logs, backups, or machine-specific `.codex` state. Project installs are intentionally separate from the shareable skill folder.
