# Codex DeepSeek Subagents

Keep GPT as the Codex planner/reviewer and delegate explicit implementation work to DeepSeek only after user confirmation. This skill installs a project-local DeepSeek worker configuration, a local Responses-compatible smoke-test shim, and cross-platform helper scripts for Bash and PowerShell.

## Quick Start

Use the bundled script instead of hand-writing project config. Both installers generate the same managed cross-platform file set under `.codex/`, including `.sh`, `.ps1`, and Python helpers.

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 start-proxy
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 test-proxy
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
```

PowerShell Core:

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

Custom proxy port example:

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh install \
  --api-key <deepseek-key> \
  --port 5001

bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh start-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh test-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh doctor
```

If you install with a custom port, later `start-proxy`, `test-proxy`, and `doctor` reuse the installed `DEEPSEEK_PROXY_BASE_URL` port unless you explicitly pass `--port` or `-Port`.

Refresh an existing or stale project install before starting the proxy:

```powershell
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 update
```

Run destructive or broad actions with `-DryRun` first:

```powershell
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 uninstall -DryRun
```

After installing or copying this skill into Codex, verify that only one active copy exists under `CODEX_HOME/skills`. Do not keep backups under `CODEX_HOME/skills/backups`, because Codex scans nested `SKILL.md` files there as active skills. Move backups outside the skills tree, such as `CODEX_HOME/skill-backups`.

## Commands

- `install`: create managed Codex provider, DeepSeek worker, local env files, proxy shims, tests, and `.gitignore` rules.
- `update`: refresh key, model, base URL, port, and thinking defaults while preserving non-managed user files.
- `uninstall`: remove only files marked `# Managed by codex-deepseek-subagents`, stop the local proxy when needed, and remove proxy runtime files. Use `-RemoveSkill` to delete the skill folder too.
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
- `-Model deepseek-v4-pro`, `-FastModel deepseek-v4-flash`: choose worker and fallback models.
- `-Port 4000`: choose the local proxy port. Explicit command-line port values override the installed env port.
- `-ThinkingDefault disabled|high|max`: set proxy default thinking behavior.
- `-Mode pro-thinking|flash-thinking|pro|flash`: select the DeepSeek delegate model and thinking mode.
- `-ThinkingView hidden|summary|raw`: choose how `delegate` handles DeepSeek reasoning content.
- `-Prompt <text>` or `-PromptFile <path>`: explicit content to send with `delegate`; no files are read automatically.
- `-MaxTokens <n>`: output token cap for `delegate`.
- `-DryRun`: print intended changes without writing.
- `-Force`: allow overwriting non-managed target files after review.
- `-NoBackup`: skip backups; default behavior backs up changed files to `.codex/backups/`.

## Managed Files

Both Bash and PowerShell installers generate the same project-local helper set:

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

API keys are written only to `.codex/*.local.*`. The installer appends these gitignore rules when missing:

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

If Codex Desktop reports `agent type is currently not available` for `deepseek_worker`, use the `delegate` fallback after the same confirmation. It sends only `-Prompt` or `-PromptFile` content and does not read repository files automatically.

## Thinking Mode

Use DeepSeek thinking mode only when it is worth the token cost:

- Simple edits, formatting, and mechanical tasks: use `pro` or `flash` with `thinking.type = "disabled"`.
- Complex implementation or debugging: use `pro-thinking` or `flash-thinking` with `thinking.type = "enabled"` and `reasoning_effort = "high"`.
- Ambiguous architecture or hard agentic work: use `reasoning_effort = "max"` only after warning about extra cost.

Never persist or feed `reasoning_content` back into prompts. Keep only coarse metadata such as reasoning token counts and whether reasoning was present.

`delegate` has three user-facing reasoning display modes:

- `hidden`: do not print raw `reasoning_content`; show model label and token counts only.
- `summary`: ask DeepSeek to add a short reasoning summary to the final answer while still hiding raw `reasoning_content`.
- `raw`: print raw `reasoning_content` in this command's JSON output only. Do not store it in logs or reuse it in follow-up prompts.

Visible labels used by fallback and proxy logs:

- `pro-thinking`: `deepseek-v4-pro(thinking)`
- `flash-thinking`: `deepseek-v4-flash(thinking)`
- `pro`: `deepseek-v4-pro`
- `flash`: `deepseek-v4-flash`

## Architecture Notes

Codex custom providers require a Responses-compatible endpoint. DeepSeek's public API is OpenAI/Anthropic-compatible, so this skill installs a local smoke-test shim that exposes `/v1/responses` and forwards to DeepSeek Chat Completions.

The local shim is intentionally a basic experiment, not a production-complete Responses proxy. It does not implement full Codex-compatible tool calling or streaming. Unsupported fields such as `stream=true`, `tools`, and `tool_choice` now fail with a clear `400` error instead of being silently ignored.

If you need production-grade Codex tool-call or streaming support, replace the shim with a mature Responses-compatible proxy and validate those behaviors yourself. Prefer a mature open-source proxy such as LiteLLM or Julep Open Responses if it passes your Codex tool-call and streaming tests.

The default local proxy is the Python shim at `.codex/deepseek_responses_shim.py`. The PowerShell shim is installed only as a compatibility/reference file because `System.Net.HttpListener` is not reliable in every Windows host environment. If `doctor` reports `install_state = "stale_missing_python_shim"` or says the Python proxy shim is missing, run `update` and then retry `start-proxy`.

`doctor` separates local install state from external API health. A missing shim or stopped proxy is a local install/startup issue; direct API errors categorized as `network_or_api_error` or `api_key_missing_or_invalid` require checking network access, service availability, or the key in `.codex/deepseek.local.env.*`.

Codex Desktop native subagent cards are controlled by the Desktop runtime. A skill can install `.codex/agents/deepseek-worker.toml`, but it cannot force the app to register or visually render that agent type. When native registration is unavailable, `delegate` is the supported fallback and provides visible model/thinking labels in command output and logs.

## Safety Checks

Run `doctor`, `redact`, and `usage` before sharing results. The skill folder must not contain API keys, logs, backups, or machine-specific `.codex` state. Project installs are intentionally separate from the shareable skill folder.

For a clean Desktop install, `doctor` should report file state, DeepSeek direct API, thinking mode, proxy health, and Desktop native subagent caveats separately. If two `codex-deepseek-subagents` entries appear in the Codex skill list, search `CODEX_HOME/skills` recursively for duplicate `SKILL.md` files and move backup copies outside that directory.

## License

MIT License. See [LICENSE](LICENSE).
