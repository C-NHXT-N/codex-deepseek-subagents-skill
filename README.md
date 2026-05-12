# Codex DeepSeek Subagents Skill

让 Codex Desktop/CLI 保持 GPT 作为主控，让 DeepSeek 承担明确授权后的执行任务。这个 skill 把省钱、可控、可诊断的多 agent 工作流打包成一键安装工具：GPT 负责 plan、dispatch、review；DeepSeek 只在用户确认后作为 worker 接收最小必要上下文。

## 核心优势

- **GPT 主控，DeepSeek 执行**：把高判断力任务留给 GPT，把实现型任务交给低成本 DeepSeek worker。
- **显式上下文门禁**：每次派发 DeepSeek 前先提示会发送的任务、文件范围和探索权限，减少 token 消耗和数据外发。
- **跨平台一键配置**：支持 Windows PowerShell、PowerShell Core、Linux/macOS bash。
- **Thinking mode 可控**：简单任务关闭思考，复杂实现/调试按需开启并记录 reasoning token 统计。
- **本地 Responses proxy**：为 Codex 的 `wire_api = "responses"` 需求提供可测试 shim，便于后续替换为 LiteLLM/Julep 等生产代理。
- **安全卸载与诊断**：`doctor`、`redact`、`usage`、`uninstall` 帮你检查配置、密钥泄漏、token 用量和清理托管文件。

## 架构

```text
User
  -> GPT main agent: plan / delegate / review
  -> DeepSeek worker: implement only confirmed tasks
  -> Local Responses proxy: /v1/responses -> DeepSeek Chat Completions
```

DeepSeek API key 只写入目标项目的 `.codex/*.local.*`，并自动加入 `.gitignore`。skill 目录本身不包含密钥、日志或本机私有路径。

## 快速开始

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 install -ApiKey <deepseek-key>
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 doctor
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 start-proxy
powershell -ExecutionPolicy Bypass -File skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1 test-proxy
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
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh start-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh test-proxy
```

## 命令

- `install`：生成 Codex provider、DeepSeek worker、local env、proxy shim、测试脚本和 `.gitignore` 规则。
- `update`：更新 API key、模型、base URL、port、thinking 默认策略。
- `uninstall`：只删除带 `# Managed by codex-deepseek-subagents` 标记的托管文件。
- `doctor`：检查文件、gitignore、DeepSeek direct API、thinking mode 和 proxy health。
- `start-proxy` / `stop-proxy` / `test-proxy`：管理本地 `/v1/responses` shim。
- `usage`：汇总 proxy 日志里的 prompt/completion/reasoning tokens。
- `redact`：扫描非 `.local` 文件中的疑似 API key。
- `export-shareable`：导出不含密钥、日志、备份的 shareable skill zip。

常用选项：

- `-ProjectRoot <path>` / `--project-root <path>`：安装到指定项目。
- `-Model` / `--model`：设置 DeepSeek worker 模型，默认 `deepseek-v4-pro`。
- `-FastModel` / `--fast-model`：设置快速模型，默认 `deepseek-v4-flash`。
- `-Port` / `--port`：设置本地 proxy 端口，默认 `4000`。
- `-ThinkingDefault disabled|high|max` / `--thinking-default disabled|high|max`。
- `-DryRun` / `--dry-run`：只展示将要执行的动作。
- `-Force` / `--force`：确认后覆盖非托管目标文件。
- `-RemoveSkill` / `--remove-skill`：卸载时连 skill 文件夹一起删除。

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
- 默认发送最小任务说明和必要路径，避免泛读全仓库。
- `reasoning_content` 不会写入日志，也不应回灌到后续 prompt。
- 运行 `redact` 检查密钥泄漏，运行 `usage` 查看 token 成本。
- 本地 smoke-test proxy 适合验证链路；生产环境建议评估 LiteLLM、Julep Open Responses 或自研更完整的 Responses gateway。

## FAQ

**这个 skill 会自动把所有上下文发给 DeepSeek 吗？**  
不会。设计目标是 GPT 主 agent 在派发前明确提示范围，用户确认后才调用 DeepSeek worker。

**为什么需要 proxy？**  
Codex 自定义 provider 需要 Responses-compatible endpoint，而 DeepSeek 公开 API 主要是 OpenAI/Anthropic-compatible。这个 skill 提供本地 shim 用于测试 `/v1/responses` 链路。

**能完全禁止 GPT 写文件吗？**  
skill 能提供行为约束和工作流门禁，但硬隔离需要额外权限边界或 gateway 设计。

---

# Codex DeepSeek Subagents Skill

Keep GPT as the Codex planner/reviewer and delegate explicit implementation work to DeepSeek only after user confirmation. This skill packages a cost-aware, context-gated multi-agent workflow with one-command setup, diagnostics, local proxy testing, and safe uninstall.

## Highlights

- **GPT controls, DeepSeek implements**: GPT plans, dispatches, and reviews; DeepSeek handles bounded worker tasks.
- **Context gate by default**: show the task, paths, and exploration scope before sending anything to DeepSeek.
- **Cross-platform setup**: Windows PowerShell, PowerShell Core, Linux, and macOS are supported.
- **Thinking mode policy**: disable thinking for simple tasks; enable it for complex implementation or debugging with token accounting.
- **Local Responses proxy**: test Codex `wire_api = "responses"` flow against DeepSeek Chat Completions.
- **Friendly operations**: install, update, doctor, usage, redact, export, and safe uninstall.

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
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh start-proxy
bash skills/codex-deepseek-subagents/scripts/deepseek-codex.sh test-proxy
```

Use the `.ps1` script for the same commands on Windows.

## Commands

- `install`: create managed Codex config, DeepSeek worker, local env, proxy, tests, and gitignore rules.
- `update`: refresh API key, model, URLs, port, and thinking defaults.
- `uninstall`: remove only managed files; add `-RemoveSkill` or `--remove-skill` to remove the skill folder.
- `doctor`: verify local files, DeepSeek API, thinking mode, and proxy health.
- `usage`: summarize token usage from proxy logs.
- `redact`: scan for leaked keys outside local secret files.
- `export-shareable`: build a clean shareable zip.

## Safety

API keys are written only to `.codex/*.local.*` and are ignored by git. Repository content should be sent to DeepSeek only after an explicit handoff confirmation. The proxy logs token metadata and discards hidden reasoning content.

## License

MIT License. See [LICENSE](LICENSE).

