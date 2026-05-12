# Codex DeepSeek Subagents Skill

让 Codex Desktop/CLI 保持 GPT 作为主控，让 DeepSeek 承担明确授权后的执行任务。这个 skill 把省钱、可控、可诊断的多 agent 工作流打包成一键安装工具：GPT 负责 plan、dispatch、review；DeepSeek 只在用户确认后作为 worker 接收最小必要上下文。

## 核心优势

- **GPT 主控，DeepSeek 执行**：把高判断力任务留给 GPT，把实现型任务交给低成本 DeepSeek worker。
- **显式上下文门禁**：每次调用 DeepSeek 前先提示将发送的任务、文件范围和探索权限，减少 token 消耗和数据外发。
- **跨平台一键配置**：支持 Windows PowerShell、PowerShell Core、Linux/macOS bash。
- **可靠 fallback**：即使 Codex Desktop 没有注册 `deepseek_worker` 原生子 agent，也能用 `delegate` 直接调用 DeepSeek。
- **可见模型标签**：fallback/proxy 日志显示 `deepseek-v4-pro(thinking)`、`deepseek-v4-flash(thinking)`、`deepseek-v4-pro` 或 `deepseek-v4-flash`。
- **安全卸载与诊断**：`doctor`、`desktop-doctor`、`redact`、`usage`、`uninstall` 帮你检查配置、密钥泄漏、token 用量和托管文件。

## 架构

```text
User
  -> GPT main agent: plan / delegate / review
  -> DeepSeek worker or delegate fallback: execute confirmed tasks only
  -> Local Responses proxy: /v1/responses -> DeepSeek Chat Completions
```

DeepSeek API key 只写入目标项目的 `.codex/*.local.*`，并自动加入 `.gitignore`。skill 目录本身不包含密钥、日志或本机私有路径。

## 快速开始

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 delegate -Mode pro-thinking -Prompt "只分析我明确提供的公开内容"
```

PowerShell Core:

```powershell
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
pwsh skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
```

Linux/macOS:

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh install --api-key <deepseek-key>
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh doctor
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh delegate --mode pro-thinking --prompt "只分析我明确提供的公开内容"
```

## 命令

- `install`：生成 Codex provider、DeepSeek worker、local env、proxy shim、测试脚本和 `.gitignore` 规则。
- `update`：更新 API key、模型、base URL、port、thinking 默认策略。
- `delegate`：当 Desktop 原生 subagent 不可用时，直接调用 DeepSeek fallback。
- `doctor` / `desktop-doctor`：检查文件、gitignore、DeepSeek direct API、thinking mode、proxy health 和 Desktop native subagent 提示。
- `start-proxy` / `stop-proxy` / `test-proxy`：管理本地 `/v1/responses` shim。
- `usage`：汇总 proxy 日志里的 prompt/completion/reasoning tokens，并按模型标签分组。
- `redact`：扫描非 `.local` 文件中的疑似 API key。
- `export-shareable`：导出不含密钥、日志、备份的 shareable skill zip。
- `uninstall`：只删除带 `# Managed by codex-deepseek-subagents` 标记的托管文件。

常用选项：

- `-Mode pro-thinking|flash-thinking|pro|flash` / `--mode ...`
- `-Prompt <text>` / `--prompt <text>`
- `-PromptFile <path>` / `--prompt-file <path>`
- `-MaxTokens <n>` / `--max-tokens <n>`
- `-DryRun` / `--dry-run`
- `-Force` / `--force`
- `-RemoveSkill` / `--remove-skill`

## 桌面端可视化说明

Codex Desktop 原生 subagent 卡片由 Desktop runtime 的 agent registry 控制。这个 skill 可以安装 `.codex/agents/deepseek-worker.toml`，但不能强制 Desktop 注册或渲染自定义 `deepseek_worker` 卡片。

当前可保证的显示方式是 fallback/proxy 输出：

| Mode | Model label |
| --- | --- |
| `pro-thinking` | `deepseek-v4-pro(thinking)` |
| `flash-thinking` | `deepseek-v4-flash(thinking)` |
| `pro` | `deepseek-v4-pro` |
| `flash` | `deepseek-v4-flash` |

如果未来 Codex Desktop 支持自定义 agent registry，现有 `.codex/agents/deepseek-worker.toml` 可以继续作为原生路径使用。

## 文件结构

```text
skills/codex-deepseek-subagents/
  SKILL.md
  agents/openai.yaml
  scripts/
    deepseek-codex.ps1
    deepseek-codex.sh
  templates/
    config.toml.tpl
    deepseek-worker.toml.tpl
    deepseek.local.env.ps1.tpl
    deepseek.local.env.sh.tpl
    deepseek-responses-shim.ps1.tpl
    deepseek_responses_shim.py.tpl
    test-deepseek-direct.*
    test-responses-proxy.*
```

安装后目标项目会出现 `.codex/` 配置、worker agent、local env、proxy 和测试脚本。卸载只会删除带托管标记的文件，避免误删用户自己的 Codex 配置。

## 安全与隐私

- DeepSeek 只应在用户确认 delegation 后接收上下文。
- fallback `delegate` 只发送 `Prompt` 或 `PromptFile` 明确提供的内容，不自动读取仓库。
- `reasoning_content` 不写入日志，也不应回灌到后续 prompt。
- 运行 `redact` 检查密钥泄漏，运行 `usage` 查看 token 成本。

---

# Codex DeepSeek Subagents Skill

Keep GPT as the Codex planner/reviewer and delegate explicit implementation work to DeepSeek only after user confirmation. The skill provides one-command setup, diagnostics, local proxy testing, safe uninstall, and a reliable `delegate` fallback for Codex Desktop sessions that do not register custom subagents.

## Highlights

- **GPT controls, DeepSeek implements**: GPT plans, dispatches, and reviews; DeepSeek handles bounded worker tasks.
- **Context gate by default**: show the task, paths, and exploration scope before sending anything to DeepSeek.
- **Cross-platform setup**: Windows PowerShell, PowerShell Core, Linux, and macOS are supported.
- **Reliable fallback**: `delegate` works even when Codex Desktop does not expose a native `deepseek_worker` card.
- **Visible model labels**: outputs/logs show `deepseek-v4-pro(thinking)`, `deepseek-v4-flash(thinking)`, `deepseek-v4-pro`, or `deepseek-v4-flash`.
- **Friendly operations**: install, update, doctor, desktop-doctor, delegate, usage, redact, export, and safe uninstall.

## Install

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
```

Linux/macOS:

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh install --api-key <deepseek-key>
```

Then run:

```bash
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh doctor
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh delegate --mode pro-thinking --prompt "Analyze only the explicit context I provide."
```

Use the `.ps1` script for the same commands on Windows.

## Commands

- `install`: create managed Codex config, DeepSeek worker, local env, proxy, tests, and gitignore rules.
- `delegate`: direct DeepSeek fallback with explicit prompt or prompt file.
- `doctor` / `desktop-doctor`: verify files, DeepSeek API, thinking mode, proxy health, and Desktop native-subagent caveats.
- `usage`: summarize token usage from proxy logs by model label.
- `redact`: scan for leaked keys outside local secret files.
- `export-shareable`: build a clean shareable zip.
- `uninstall`: remove only managed files; add `-RemoveSkill` or `--remove-skill` to remove the skill folder.

## Desktop Visualization

Native subagent cards are controlled by Codex Desktop itself. This skill installs the agent config, but it cannot force Desktop to register or render a custom `deepseek_worker` card. When native registration is unavailable, use `delegate`; it prints the exact DeepSeek model/thinking label and token accounting.

## Safety

API keys are written only to `.codex/*.local.*` and are ignored by git. Repository content should be sent to DeepSeek only after an explicit handoff confirmation. The proxy logs token metadata and discards hidden reasoning content.

## License

MIT License. See [LICENSE](LICENSE).
