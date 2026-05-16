param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "update", "uninstall", "doctor", "desktop-doctor", "delegate", "analyze", "start-proxy", "stop-proxy", "test-proxy", "test-runtime", "start-runtime", "stop-runtime", "usage", "tui", "redact", "export-shareable")]
    [string]$Command = "doctor",

    [string]$ProjectRoot = (Get-Location).Path,
    [string]$ApiKey = "",
    [string]$Model = "deepseek-v4-pro",
    [string]$FastModel = "deepseek-v4-flash",
    [string]$BaseUrl = "https://api.deepseek.com",
    [string]$AnthropicBaseUrl = "https://api.deepseek.com/anthropic",
    [int]$Port = 4000,
    [ValidateSet("disabled", "high", "max")]
    [string]$ThinkingDefault = "disabled",
    [switch]$DryRun,
    [switch]$NoBackup,
    [switch]$Force,
    [switch]$RemoveSkill,
    [ValidateSet("pro-thinking", "flash-thinking", "pro", "flash")]
    [string]$Mode = "",
    [string]$ThinkingModel = "",
    [string]$Thinking = "",
    [ValidateSet("hidden", "summary", "raw")]
    [string]$ThinkingView = "hidden",
    [ValidateSet("hidden", "summary", "full")]
    [string]$PatchView = "summary",
    [ValidateSet("stream", "tui")]
    [string]$Ui = "stream",
    [string]$Prompt = "",
    [string]$PromptFile = "",
    [int]$MaxTokens = 2048,
    [string]$OutFile = "",
    [switch]$Json,
    [switch]$Yes,
    [switch]$Deep
)

$ErrorActionPreference = "Stop"
$PortExplicit = $PSBoundParameters.ContainsKey("Port")
$ManagedMarker = "# Managed by codex-deepseek-subagents"
$SkillRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$TemplateRoot = Join-Path $SkillRoot "templates"
$SchedulerRoot = Join-Path $SkillRoot "scheduler"

function Resolve-FullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Get-ProjectPath {
    param([string]$RelativePath)
    return Join-Path (Resolve-FullPath $ProjectRoot) $RelativePath
}

function Write-Step {
    param([string]$Message)
    Write-Host "[codex-deepseek-subagents] $Message"
}

function Convert-ErrorCategory {
    param([string]$Message)
    if ($Message -match 'DEEPSEEK_API_KEY|401|403|Unauthorized|Invalid proxy authorization') { return "api_key_missing_or_invalid" }
    if ($Message -match 'Proxy is not running|No connection could be made|actively refused|connection refused') { return "proxy_not_running" }
    if ($Message -match 'Only one usage of each socket address|address already in use|port') { return "port_in_use" }
    if ($Message -match 'timed out|GetResult|NameResolutionFailure|network|SSL|TLS') { return "network_or_api_error" }
    if ($Message -like "*发送请求*") { return "network_or_api_error" }
    return "unknown_error"
}

function Escape-TemplateValue {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) { return "" }
    return $Value.Replace("\", "\\").Replace("'", "''")
}

function Escape-TomlString {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) { return "" }
    return $Value.Replace("\", "\\").Replace('"', '\"')
}

function Escape-ShString {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) { return "" }
    return $Value.Replace("'", "'\''")
}

function Expand-ContentTemplate {
    param([string]$Content)
    $content = $content.Replace("__API_KEY_PS__", (Escape-TemplateValue $ApiKey))
    $content = $content.Replace("__BASE_URL_PS__", (Escape-TemplateValue $BaseUrl))
    $content = $content.Replace("__ANTHROPIC_BASE_URL_PS__", (Escape-TemplateValue $AnthropicBaseUrl))
    $content = $content.Replace("__MODEL_PS__", (Escape-TemplateValue $Model))
    $content = $content.Replace("__FAST_MODEL_PS__", (Escape-TemplateValue $FastModel))
    $content = $content.Replace("__THINKING_DEFAULT_PS__", (Escape-TemplateValue $ThinkingDefault))
    $content = $content.Replace("__MODEL_TOML__", (Escape-TomlString $Model))
    $content = $content.Replace("__API_KEY_SH__", (Escape-ShString $ApiKey))
    $content = $content.Replace("__BASE_URL_SH__", (Escape-ShString $BaseUrl))
    $content = $content.Replace("__ANTHROPIC_BASE_URL_SH__", (Escape-ShString $AnthropicBaseUrl))
    $content = $content.Replace("__MODEL_SH__", (Escape-ShString $Model))
    $content = $content.Replace("__FAST_MODEL_SH__", (Escape-ShString $FastModel))
    $content = $content.Replace("__THINKING_DEFAULT_SH__", (Escape-ShString $ThinkingDefault))
    $content = $content.Replace("__PORT__", [string]$Port)
    return $content
}

function Expand-Template {
    param([string]$TemplateName)
    return Expand-ContentTemplate -Content (Get-Content -Raw (Join-Path $TemplateRoot $TemplateName))
}

function Expand-SchedulerSource {
    param([string]$RelativePath)
    $content = Expand-ContentTemplate -Content (Get-Content -Raw (Join-Path $SchedulerRoot $RelativePath))
    if ($content -notmatch '^\s*# Managed by codex-deepseek-subagents') {
        return "# Managed by codex-deepseek-subagents`n$content"
    }
    return $content
}

function Test-ManagedFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $header = Get-Content -LiteralPath $Path -TotalCount 3 -ErrorAction SilentlyContinue
    return [bool](@($header | ForEach-Object { [string]$_ }) -match [regex]::Escape("Managed by codex-deepseek-subagents"))
}

function Ensure-Directory {
    param([string]$Path)
    if ($DryRun) {
        Write-Step "Would ensure directory: $Path"
        return
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Write-TextFile {
    param(
        [string]$Path,
        [AllowNull()]$Content
    )
    $encoding = [System.Text.UTF8Encoding]::new($false)
    $text = if ($Content -is [string]) {
        $Content
    }
    elseif ($Content -is [System.Array]) {
        (($Content | ForEach-Object { [string]$_ }) -join [Environment]::NewLine) + [Environment]::NewLine
    }
    else {
        [string]$Content
    }
    [System.IO.File]::WriteAllText($Path, $text, $encoding)
}

function Backup-File {
    param([string]$Path)
    if ($NoBackup -or -not (Test-Path -LiteralPath $Path)) { return }
    if ((Split-Path -Leaf $Path) -like "*.local.*") {
        Write-Step "Skipping backup for local secret file: $Path"
        return
    }
    $backupRoot = Get-ProjectPath ".codex/backups"
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $name = Split-Path -Leaf $Path
    $backupPath = Join-Path $backupRoot "$timestamp-$name"
    if ($DryRun) {
        Write-Step "Would backup $Path -> $backupPath"
        return
    }
    Ensure-Directory $backupRoot
    Copy-Item -LiteralPath $Path -Destination $backupPath -Force
}

function Write-ManagedFile {
    param([string]$Path, [string]$Content, [switch]$Secret)
    $exists = Test-Path -LiteralPath $Path
    if ($exists -and -not (Test-ManagedFile $Path) -and -not $Force) {
        throw "Refusing to overwrite non-managed file: $Path. Re-run with -Force after reviewing it."
    }
    if ($exists) { Backup-File $Path }
    Ensure-Directory (Split-Path -Parent $Path)
    if ($DryRun) {
        $kind = if ($Secret) { "secret managed file" } else { "managed file" }
        Write-Step "Would write ${kind}: $Path"
        return
    }
    Write-TextFile -Path $Path -Content $Content
    if ($Path -match '\.(sh|py)$') {
        try {
            if (-not $IsWindows) { chmod +x $Path }
        }
        catch {}
    }
}

function Get-NormalizedUserConfigContent {
    param([string]$ExistingPath)
    $python = Get-PythonCommand
    $schedulerPath = Join-Path $SchedulerRoot "deepseek_scheduler.py"
    $templatePath = [System.IO.Path]::GetTempFileName()
    $pythonScript = @'
import importlib.util
import json
import sys
from pathlib import Path

template_path = Path(sys.argv[1])
existing_path = Path(sys.argv[2])
scheduler_path = Path(sys.argv[3])
scheduler_dir = str(scheduler_path.parent)
if scheduler_dir not in sys.path:
    sys.path.insert(0, scheduler_dir)

template = json.loads(template_path.read_text(encoding="utf-8-sig"))
existing = {}
if existing_path.exists():
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8-sig"))
    except Exception:
        existing = {}

spec = importlib.util.spec_from_file_location("deepseek_scheduler", scheduler_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
normalized = module.normalize_user_config_for_write(existing, template)
sys.stdout.write(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n")
'@
    try {
        Set-Content -LiteralPath $templatePath -Value (Expand-Template "user_config.json.tpl") -Encoding UTF8
        return ($pythonScript | & $python - $templatePath $ExistingPath $schedulerPath)
    }
    finally {
        Remove-Item -LiteralPath $templatePath -Force -ErrorAction SilentlyContinue
    }
}

function Write-UserConfig {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Backup-File $Path
    }
    Ensure-Directory (Split-Path -Parent $Path)
    if ($DryRun) {
        Write-Step "Would normalize user config: $Path"
        return
    }
    $content = Get-NormalizedUserConfigContent -ExistingPath $Path
    Write-TextFile -Path $Path -Content $content
}

function Initialize-RuntimeStateFiles {
    $jsonFiles = @(
        @{ Path = ".codex/runtime/task_queue.json"; Content = (@{ tasks = @() } | ConvertTo-Json -Depth 4) },
        @{ Path = ".codex/runtime/sessions.json"; Content = (@{ sessions = @() } | ConvertTo-Json -Depth 4) }
    )
    foreach ($entry in $jsonFiles) {
        $path = Get-ProjectPath $entry.Path
        if ((-not (Test-Path -LiteralPath $path)) -or $Force) {
            Ensure-Directory (Split-Path -Parent $path)
            if ($DryRun) {
                Write-Step "Would initialize runtime state file: $path"
            }
            else {
                Write-TextFile -Path $path -Content $entry.Content
            }
        }
    }
    foreach ($relative in @(
        ".codex/runtime/events.log.jsonl",
        ".codex/runtime/stdout.log",
        ".codex/runtime/stderr.log"
    )) {
        $path = Get-ProjectPath $relative
        if ((-not (Test-Path -LiteralPath $path)) -or $Force) {
            Ensure-Directory (Split-Path -Parent $path)
            if ($DryRun) {
                Write-Step "Would initialize runtime state file: $path"
            }
            else {
                Write-TextFile -Path $path -Content ""
            }
        }
    }
}

function Remove-LegacyArtifacts {
    foreach ($relative in @(
        ".codex/deepseek-responses-shim.ps1",
        ".codex/deepseek_responses_shim.py",
        ".codex/test-deepseek-direct.ps1",
        ".codex/test-deepseek-direct.sh",
        ".codex/test-responses-proxy.ps1",
        ".codex/test-responses-proxy.sh"
    )) {
        $path = Get-ProjectPath $relative
        if (Test-Path -LiteralPath $path) {
            if (Test-ManagedFile $path) {
                Remove-ManagedPath $path
            }
            else {
                Write-Step "Skipping non-managed legacy file: $path"
            }
        }
    }
    foreach ($relative in @(
        ".codex/deepseek-proxy.log.jsonl",
        ".codex/deepseek-proxy.pid",
        ".codex/deepseek-proxy.stdout.log",
        ".codex/deepseek-proxy.stderr.log"
    )) {
        Remove-RuntimePath (Get-ProjectPath $relative)
    }
}

function Add-GitIgnoreRules {
    $gitignore = Get-ProjectPath ".gitignore"
    $rules = @(
        ".codex/*.local.*",
        ".codex/runtime/task_queue.json",
        ".codex/runtime/sessions.json",
        ".codex/runtime/events.log.jsonl",
        ".codex/runtime/runtime.pid",
        ".codex/runtime/stdout.log",
        ".codex/runtime/stderr.log",
        ".codex/test-runtime.ps1",
        ".codex/test-runtime.sh",
        ".codex/backups/",
        "__pycache__/",
        "*.py[cod]"
    )
    $legacyRules = @(
        ".codex/deepseek-responses-shim.ps1",
        ".codex/deepseek_responses_shim.py",
        ".codex/test-deepseek-direct.ps1",
        ".codex/test-deepseek-direct.sh",
        ".codex/test-responses-proxy.ps1",
        ".codex/test-responses-proxy.sh",
        ".codex/deepseek-proxy.log.jsonl",
        ".codex/deepseek-proxy.pid",
        ".codex/deepseek-proxy.stdout.log",
        ".codex/deepseek-proxy.stderr.log"
    )
    $existing = if (Test-Path -LiteralPath $gitignore) { Get-Content -LiteralPath $gitignore } else { @() }
    $filtered = @($existing | Where-Object { $legacyRules -notcontains $_ })
    $missing = @($rules | Where-Object { $filtered -notcontains $_ })
    if (($missing.Count -eq 0) -and ($filtered.Count -eq $existing.Count)) {
        Write-Step ".gitignore already contains current DeepSeek local rules."
        return
    }
    if ($DryRun) {
        Write-Step "Would refresh .gitignore DeepSeek local rules."
        return
    }
    if (Test-Path -LiteralPath $gitignore) { Backup-File $gitignore }
    $output = @($filtered)
    if ($output.Count -gt 0 -and $output[-1] -ne "") {
        $output += ""
    }
    if ($output -notcontains "# Local Codex DeepSeek secrets and logs") {
        $output += "# Local Codex DeepSeek secrets and logs"
    }
    foreach ($rule in $rules) {
        if ($output -notcontains $rule) {
            $output += $rule
        }
    }
    Write-TextFile -Path $gitignore -Content $output
}

function Install-OrUpdate {
    param([bool]$IsUpdate)
    if (-not $ApiKey -and -not $IsUpdate) {
        throw "install requires -ApiKey. The key is written only to .codex/deepseek.local.env.ps1."
    }
    if ($IsUpdate) {
        Sync-PortFromUserConfig
    }
    if (-not $ApiKey -and $IsUpdate) {
        $existingEnv = Get-ProjectPath ".codex/deepseek.local.env.ps1"
        if (Test-ManagedFile $existingEnv) {
            $existing = Get-Content -Raw -LiteralPath $existingEnv
            $match = [regex]::Match($existing, '\$env:DEEPSEEK_API_KEY\s*=\s*''([^'']*)''')
            if ($match.Success) { $script:ApiKey = $match.Groups[1].Value }
        }
        if (-not $ApiKey) {
            throw "update requires -ApiKey when no existing managed key can be reused."
        }
    }

    Write-ManagedFile (Get-ProjectPath ".codex/config.toml") (Expand-Template "config.toml.tpl")
    Write-ManagedFile (Get-ProjectPath ".codex/agents/deepseek-worker.toml") (Expand-Template "deepseek-worker.toml.tpl")
    Write-UserConfig (Get-ProjectPath "user_config.json")
    Write-ManagedFile (Get-ProjectPath ".codex/deepseek.local.env.ps1") (Expand-Template "deepseek.local.env.ps1.tpl") -Secret
    Write-ManagedFile (Get-ProjectPath ".codex/deepseek.local.env.sh") (Expand-Template "deepseek.local.env.sh.tpl") -Secret
    Write-ManagedFile (Get-ProjectPath ".codex/runtime/deepseek_scheduler.py") (Expand-SchedulerSource "deepseek_scheduler.py")
    Write-ManagedFile (Get-ProjectPath ".codex/runtime/deepseek_runtime.py") (Expand-SchedulerSource "deepseek_runtime.py")
    Write-ManagedFile (Get-ProjectPath ".codex/runtime/deepseek_client.py") (Expand-SchedulerSource "deepseek_client.py")
    Write-ManagedFile (Get-ProjectPath ".codex/runtime/events.py") (Expand-SchedulerSource "events.py")
    Write-ManagedFile (Get-ProjectPath ".codex/runtime/render.py") (Expand-SchedulerSource "render.py")
    Write-ManagedFile (Get-ProjectPath ".codex/runtime/patch_preview.py") (Expand-SchedulerSource "patch_preview.py")
    Write-ManagedFile (Get-ProjectPath ".codex/runtime/tool_protocol.py") (Expand-SchedulerSource "tool_protocol.py")
    Write-ManagedFile (Get-ProjectPath ".codex/runtime/usage.py") (Expand-SchedulerSource "usage.py")
    Write-ManagedFile (Get-ProjectPath ".codex/runtime/doctor.py") (Expand-SchedulerSource "doctor.py")
    Write-ManagedFile (Get-ProjectPath ".codex/runtime/tui.py") (Expand-SchedulerSource "tui.py")
    Write-ManagedFile (Get-ProjectPath ".codex/test-runtime.ps1") (Expand-Template "test-runtime.ps1.tpl")
    Write-ManagedFile (Get-ProjectPath ".codex/test-runtime.sh") (Expand-Template "test-runtime.sh.tpl")
    Write-ManagedFile (Get-ProjectPath ".codex/deepseek-codex.cmd") (Expand-Template "deepseek-codex.cmd.tpl")
    Write-ManagedFile (Get-ProjectPath ".codex/deepseek-codex.sh") (Expand-Template "deepseek-codex.sh.tpl")
    Initialize-RuntimeStateFiles
    Remove-LegacyArtifacts
    Add-GitIgnoreRules
    Write-Step "$(if ($IsUpdate) { 'Update' } else { 'Install' }) complete."
    Write-Step "Post-install check: keep only one codex-deepseek-subagents skill under CODEX_HOME/skills, then run doctor, start-runtime, and test-runtime."
}

function Remove-ManagedPath {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if (-not (Test-ManagedFile $Path)) {
        Write-Step "Skipping non-managed file: $Path"
        return
    }
    Backup-File $Path
    if ($DryRun) {
        Write-Step "Would remove managed file: $Path"
        return
    }
    Remove-Item -LiteralPath $Path -Force
    Write-Step "Removed: $Path"
}

function Stop-ProxyForUninstall {
    $pidFile = Get-ProjectPath ".codex/runtime/runtime.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) { return }
    try {
        $pidText = (Get-Content -Raw -LiteralPath $pidFile).Trim()
        if ($pidText -and (Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue)) {
            if ($DryRun) {
                Write-Step "Would stop proxy PID $pidText before uninstall."
            }
            else {
                Stop-Process -Id ([int]$pidText) -Force
                Write-Step "Stopped proxy PID $pidText before uninstall."
            }
        }
    }
    catch {
        Write-Step "Could not inspect proxy PID during uninstall: $($_.Exception.Message)"
    }
}

function Remove-RuntimePath {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ($DryRun) {
        Write-Step "Would remove runtime file: $Path"
        return
    }
    Remove-Item -LiteralPath $Path -Force
    Write-Step "Removed runtime file: $Path"
}

function Uninstall-Project {
    $paths = @(
        "user_config.json",
        ".codex/config.toml",
        ".codex/agents/deepseek-worker.toml",
        ".codex/deepseek.local.env.ps1",
        ".codex/deepseek.local.env.sh",
        ".codex/runtime/deepseek_scheduler.py",
        ".codex/runtime/deepseek_runtime.py",
        ".codex/runtime/deepseek_client.py",
        ".codex/runtime/events.py",
        ".codex/runtime/render.py",
        ".codex/runtime/patch_preview.py",
        ".codex/runtime/tool_protocol.py",
        ".codex/runtime/usage.py",
        ".codex/runtime/doctor.py",
        ".codex/runtime/tui.py",
        ".codex/test-runtime.ps1",
        ".codex/test-runtime.sh",
        ".codex/deepseek-codex.cmd",
        ".codex/deepseek-codex.sh"
    )
    foreach ($relative in $paths) {
        Remove-ManagedPath (Get-ProjectPath $relative)
    }
    Stop-ProxyForUninstall
    $runtimePaths = @(
        ".codex/deepseek-proxy.log.jsonl",
        ".codex/deepseek-proxy.pid",
        ".codex/deepseek-proxy.stdout.log",
        ".codex/deepseek-proxy.stderr.log",
        ".codex/runtime/task_queue.json",
        ".codex/runtime/sessions.json",
        ".codex/runtime/events.log.jsonl",
        ".codex/runtime/runtime.pid",
        ".codex/runtime/stdout.log",
        ".codex/runtime/stderr.log"
    )
    foreach ($relative in $runtimePaths) {
        Remove-RuntimePath (Get-ProjectPath $relative)
    }
    if ($RemoveSkill) {
        if ($DryRun) {
            Write-Step "Would remove skill folder: $SkillRoot"
        }
        else {
            Remove-Item -LiteralPath $SkillRoot -Recurse -Force
            Write-Step "Removed skill folder: $SkillRoot"
        }
    }
}

function Import-LocalEnv {
    $envFile = Get-ProjectPath ".codex/deepseek.local.env.ps1"
    if (-not (Test-Path -LiteralPath $envFile)) {
        throw "Missing local env file: $envFile. Run install first."
    }
    . $envFile
}

function Sync-PortFromEnv {
    if ($PortExplicit) { return }
    if (-not $env:DEEPSEEK_PROXY_BASE_URL) { return }
    try {
        $uri = [Uri]$env:DEEPSEEK_PROXY_BASE_URL
        if ($uri.Port -gt 0) {
            $script:Port = $uri.Port
        }
    }
    catch {}
}

function Sync-PortFromUserConfig {
    if ($PortExplicit) { return }
    $configPath = Get-ProjectPath "user_config.json"
    if (-not (Test-Path -LiteralPath $configPath)) { return }
    try {
        $config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
        if ($config.runtime.port) {
            $script:Port = [int]$config.runtime.port
        }
    }
    catch {}
}

function Test-ShouldScanPath {
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$FullPath,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($Name -like "*.local.*" -or $Name -eq "events.log.jsonl") {
        return $false
    }

    $normalizedRoot = $RootPath.TrimEnd('\', '/')
    $relative = if ($FullPath.StartsWith($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        $FullPath.Substring($normalizedRoot.Length).TrimStart('\', '/')
    }
    else {
        [System.IO.Path]::GetRelativePath($RootPath, $FullPath)
    }
    $parts = $relative -split '[\\/]'
    return ($parts -notcontains ".git") -and ($parts -notcontains "backups")
}

function Get-InstallState {
    $configExists = Test-Path -LiteralPath (Get-ProjectPath ".codex/config.toml")
    $workerExists = Test-Path -LiteralPath (Get-ProjectPath ".codex/agents/deepseek-worker.toml")
    $envExists = Test-Path -LiteralPath (Get-ProjectPath ".codex/deepseek.local.env.ps1")
    $userConfigExists = Test-Path -LiteralPath (Get-ProjectPath "user_config.json")
    $runtimeEntryExists = Test-Path -LiteralPath (Get-ProjectPath ".codex/runtime/deepseek_scheduler.py")
    $legacyArtifacts = @(
        ".codex/deepseek-responses-shim.ps1",
        ".codex/deepseek_responses_shim.py",
        ".codex/test-deepseek-direct.ps1",
        ".codex/test-deepseek-direct.sh",
        ".codex/test-responses-proxy.ps1",
        ".codex/test-responses-proxy.sh"
    ) | Where-Object { Test-Path -LiteralPath (Get-ProjectPath $_) }
    if (-not ($configExists -or $workerExists -or $envExists -or $userConfigExists -or $runtimeEntryExists -or $legacyArtifacts.Count)) { return "not_installed" }
    if ($runtimeEntryExists -and $configExists -and $workerExists -and $envExists -and $userConfigExists) { return "ok" }
    if ($legacyArtifacts.Count -and -not $runtimeEntryExists) { return "stale_legacy_runtime" }
    if (-not $runtimeEntryExists) { return "stale_missing_runtime" }
    return "incomplete"
}

function Get-ProxyStatus {
    $pidFile = Get-ProjectPath ".codex/runtime/runtime.pid"
    $status = [ordered]@{
        proxy_pid_exists = Test-Path -LiteralPath $pidFile
        proxy_process_alive = $false
    }
    if ($status.proxy_pid_exists) {
        try {
            $pidText = (Get-Content -Raw -LiteralPath $pidFile).Trim()
            if ($pidText) {
                $process = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
                $status.proxy_process_alive = [bool]$process
            }
        }
        catch {
            $status.proxy_process_error = $_.Exception.Message
        }
    }
    return $status
}

function Quote-ProcessArgument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value.Replace('\', '\\').Replace('"', '\"')) + '"'
}

function Start-ProcessCleanEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$StandardOutputPath,
        [Parameter(Mandatory = $true)][string]$StandardErrorPath
    )
    Set-Content -LiteralPath $StandardOutputPath -Value "" -Encoding UTF8
    Set-Content -LiteralPath $StandardErrorPath -Value "" -Encoding UTF8
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    $psi.Arguments = ($ArgumentList | ForEach-Object { Quote-ProcessArgument $_ }) -join " "
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $true
    if ($IsWindows -or $PSVersionTable.PSEdition -eq "Desktop") {
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $psi
    $null = $process.Start()
    return $process
}

function Get-PythonCommand {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $python) { throw "Neither python nor python3 was found." }
    return $python.Source
}

function Invoke-RuntimeCli {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeCommand,
        [string[]]$ExtraArgs = @()
    )
    $python = Get-PythonCommand
    $runtimeCli = Join-Path $SchedulerRoot "deepseek_runtime.py"
    $arguments = @($runtimeCli, $RuntimeCommand, "--project-root", (Resolve-FullPath $ProjectRoot))
    if ($PortExplicit) {
        $arguments += @("--port", [string]$Port)
    }
    & $python @arguments @ExtraArgs
}

function Get-DeepSeekModeSpec {
    param([string]$SelectedMode)
    switch ($SelectedMode) {
        "pro-thinking" {
            return [pscustomobject]@{
                model = $env:DEEPSEEK_OPENAI_MODEL
                thinking = @{ type = "enabled"; reasoning_effort = "high" }
                model_label = "$env:DEEPSEEK_OPENAI_MODEL(thinking)"
            }
        }
        "flash-thinking" {
            return [pscustomobject]@{
                model = $env:DEEPSEEK_OPENAI_FAST_MODEL
                thinking = @{ type = "enabled"; reasoning_effort = "high" }
                model_label = "$env:DEEPSEEK_OPENAI_FAST_MODEL(thinking)"
            }
        }
        "pro" {
            return [pscustomobject]@{
                model = $env:DEEPSEEK_OPENAI_MODEL
                thinking = @{ type = "disabled" }
                model_label = $env:DEEPSEEK_OPENAI_MODEL
            }
        }
        "flash" {
            return [pscustomobject]@{
                model = $env:DEEPSEEK_OPENAI_FAST_MODEL
                thinking = @{ type = "disabled" }
                model_label = $env:DEEPSEEK_OPENAI_FAST_MODEL
            }
        }
    }
}

function Invoke-DeepSeekChat {
    param(
        [Parameter(Mandatory = $true)][object[]]$Messages,
        [Parameter(Mandatory = $true)][string]$SelectedMode,
        [int]$TokenLimit = 2048
    )
    Import-LocalEnv
    $spec = Get-DeepSeekModeSpec -SelectedMode $SelectedMode
    if (-not $env:DEEPSEEK_API_KEY) { throw "DEEPSEEK_API_KEY is not set." }
    if (-not $env:DEEPSEEK_OPENAI_BASE_URL) { $env:DEEPSEEK_OPENAI_BASE_URL = "https://api.deepseek.com" }

    Add-Type -AssemblyName System.Net.Http
    $body = @{
        model = $spec.model
        messages = $Messages
        thinking = $spec.thinking
        max_tokens = $TokenLimit
        stream = $false
    } | ConvertTo-Json -Depth 20 -Compress

    $client = [System.Net.Http.HttpClient]::new()
    try {
        $client.Timeout = [TimeSpan]::FromSeconds(300)
        $client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $env:DEEPSEEK_API_KEY)
        $content = [System.Net.Http.StringContent]::new($body, [System.Text.Encoding]::UTF8, "application/json")
        $result = $client.PostAsync("$($env:DEEPSEEK_OPENAI_BASE_URL.TrimEnd('/'))/chat/completions", $content).GetAwaiter().GetResult()
        $text = $result.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $result.IsSuccessStatusCode) {
            throw "DeepSeek HTTP $([int]$result.StatusCode): $text"
        }
        $response = $text | ConvertFrom-Json
        $message = $response.choices[0].message
        $usage = $response.usage
        $reasoningTokens = $null
        if ($usage -and $usage.completion_tokens_details) {
            $reasoningTokens = $usage.completion_tokens_details.reasoning_tokens
        }
        return [pscustomobject]@{
            ok = $true
            mode = $SelectedMode
            model = $response.model
            model_label = $spec.model_label
            thinking_type = $spec.thinking.type
            reasoning_effort = if ($spec.thinking.reasoning_effort) { $spec.thinking.reasoning_effort } else { $null }
            finish_reason = $response.choices[0].finish_reason
            content = $message.content
            reasoning_content = $message.reasoning_content
            has_reasoning_content = [bool]$message.reasoning_content
            reasoning_chars_discarded = if ($message.reasoning_content) { ([string]$message.reasoning_content).Length } else { 0 }
            prompt_tokens = $usage.prompt_tokens
            completion_tokens = $usage.completion_tokens
            reasoning_tokens = $reasoningTokens
            total_tokens = $usage.total_tokens
        }
    }
    finally {
        $client.Dispose()
    }
}

function Test-DeepSeekDirect {
    $response = Invoke-DeepSeekChat -SelectedMode "pro" -TokenLimit 32 -Messages @(@{ role = "user"; content = "Reply with exactly: direct-ok" })
    return [pscustomobject]@{
        ok = ([string]$response.content).Contains("direct-ok")
        model = $response.model
        model_label = $response.model_label
        total_tokens = $response.total_tokens
    }
}

function Test-DeepSeekThinking {
    $response = Invoke-DeepSeekChat -SelectedMode "pro-thinking" -TokenLimit 1024 -Messages @(@{ role = "user"; content = "Which number is larger, 9.11 or 9.8? Reply with only the larger number." })
    return [pscustomobject]@{
        ok = [bool]$response.has_reasoning_content
        content = $response.content
        model_label = $response.model_label
        reasoning_tokens = $response.reasoning_tokens
        total_tokens = $response.total_tokens
    }
}

function Start-Proxy {
    Invoke-RuntimeCli -RuntimeCommand "start-runtime"
}

function Stop-Proxy {
    Invoke-RuntimeCli -RuntimeCommand "stop-runtime"
}

function Test-Proxy {
    $extraArgs = @()
    if ($Json) { $extraArgs += "--json" }
    Invoke-RuntimeCli -RuntimeCommand "test-runtime" -ExtraArgs $extraArgs
}

function Test-Runtime {
    $extraArgs = @()
    if ($Json) { $extraArgs += "--json" }
    Invoke-RuntimeCli -RuntimeCommand "test-runtime" -ExtraArgs $extraArgs
}

function Invoke-Doctor {
    $extraArgs = @()
    if ($Json) { $extraArgs += "--json" }
    if ($Deep) { $extraArgs += "--deep" }
    Invoke-RuntimeCli -RuntimeCommand "doctor" -ExtraArgs $extraArgs
}

function Invoke-DesktopDoctor {
    Invoke-Doctor
}

function Invoke-Delegate {
    $extraArgs = @(
        "--thinking-view", $ThinkingView,
        "--patch-view", $PatchView,
        "--ui", $Ui,
        "--max-tokens", [string]$MaxTokens
    )
    if ($ThinkingModel -in @("flash", "pro")) { $extraArgs += @("--model", $ThinkingModel) }
    if ($Thinking -in @("on", "off")) { $extraArgs += @("--thinking", $Thinking) }
    if ($Mode) { $extraArgs += @("--mode", $Mode) }
    if ($Prompt) { $extraArgs += @("--prompt", $Prompt) }
    if ($PromptFile) { $extraArgs += @("--prompt-file", (Resolve-FullPath $PromptFile)) }
    if ($Json) { $extraArgs += "--json" }
    if ($Yes) { $extraArgs += "--yes" }
    if ($ThinkingView -eq "raw") { $extraArgs += "--verbose" }
    Invoke-RuntimeCli -RuntimeCommand "delegate" -ExtraArgs $extraArgs
}

function Invoke-Analyze {
    $extraArgs = @(
        "--thinking-view", $ThinkingView,
        "--patch-view", $PatchView,
        "--ui", $Ui,
        "--max-tokens", [string]$MaxTokens
    )
    if ($ThinkingModel -in @("flash", "pro")) { $extraArgs += @("--model", $ThinkingModel) }
    if ($Thinking -in @("on", "off")) { $extraArgs += @("--thinking", $Thinking) }
    if ($Mode) { $extraArgs += @("--mode", $Mode) }
    if ($Prompt) { $extraArgs += @("--prompt", $Prompt) }
    if ($PromptFile) { $extraArgs += @("--prompt-file", (Resolve-FullPath $PromptFile)) }
    if ($Json) { $extraArgs += "--json" }
    if ($Yes) { $extraArgs += "--yes" }
    if ($ThinkingView -eq "raw") { $extraArgs += "--verbose" }
    Invoke-RuntimeCli -RuntimeCommand "analyze" -ExtraArgs $extraArgs
}

function Show-Usage {
    $extraArgs = @()
    if ($Json) { $extraArgs += "--json" }
    Invoke-RuntimeCli -RuntimeCommand "usage" -ExtraArgs $extraArgs
}

function Show-Tui {
    Invoke-RuntimeCli -RuntimeCommand "tui"
}

function Invoke-RedactCheck {
    $root = Resolve-FullPath $ProjectRoot
    $findings = @()
    Get-ChildItem -LiteralPath $root -Recurse -File -Force |
        Where-Object {
            Test-ShouldScanPath -RootPath $root -FullPath $_.FullName -Name $_.Name
        } |
        ForEach-Object {
            try {
                $matches = Select-String -LiteralPath $_.FullName -Pattern 'sk-[A-Za-z0-9]{12,}' -ErrorAction Stop
                foreach ($match in $matches) {
                    $findings += [pscustomobject]@{ file = $_.FullName; line = $match.LineNumber }
                }
            }
            catch {}
        }
    if ($findings.Count -eq 0) {
        Write-Step "No non-local DeepSeek-looking keys found."
    }
    else {
        $findings | ConvertTo-Json -Depth 4
    }
}

function Export-Shareable {
    $destination = if ($OutFile) { $OutFile } else { Join-Path (Resolve-FullPath $ProjectRoot) "codex-deepseek-subagents.zip" }
    if ($DryRun) {
        Write-Step "Would export shareable skill zip to $destination"
        return
    }
    if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Force }
    $allowedRoots = @("SKILL.md", "agents", "scripts", "templates", "scheduler")
    $stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("codex-deepseek-subagents-export-" + [System.Guid]::NewGuid().ToString("N"))
    $stageSkillRoot = Join-Path $stageRoot "codex-deepseek-subagents"
    try {
        New-Item -ItemType Directory -Path $stageSkillRoot -Force | Out-Null
        foreach ($root in $allowedRoots) {
            $path = Join-Path $SkillRoot $root
            $target = Join-Path $stageSkillRoot $root
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                Copy-Item -LiteralPath $path -Destination $target -Force
            }
            elseif (Test-Path -LiteralPath $path -PathType Container) {
                Get-ChildItem -LiteralPath $path -Recurse -File | Where-Object {
                    (-not ($_.Name -like "*.local.*" -and $_.Name -notlike "*.tpl")) -and
                    $_.Name -ne "events.log.jsonl" -and
                    $_.Extension -ne ".pyc" -and
                    $_.FullName -notmatch '\\__pycache__\\' -and
                    $_.FullName -notmatch '\\backups\\'
                } | ForEach-Object {
                    $relative = $_.FullName.Substring($path.Length).TrimStart('\', '/')
                    $targetFile = Join-Path $target $relative
                    New-Item -ItemType Directory -Path (Split-Path -Parent $targetFile) -Force | Out-Null
                    Copy-Item -LiteralPath $_.FullName -Destination $targetFile -Force
                }
            }
        }
        $archiveItems = Get-ChildItem -LiteralPath $stageSkillRoot -Force
        Compress-Archive -LiteralPath @($archiveItems | ForEach-Object { $_.FullName }) -DestinationPath $destination -Force
    }
    finally {
        if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force }
    }
    Write-Step "Exported shareable skill zip: $destination"
}

if ($MyInvocation.InvocationName -ne '.') {
    switch ($Command) {
        "install" { Install-OrUpdate -IsUpdate $false }
        "update" { Install-OrUpdate -IsUpdate $true }
        "uninstall" { Uninstall-Project }
        "doctor" { Invoke-Doctor }
        "desktop-doctor" { Invoke-DesktopDoctor }
        "delegate" { Invoke-Delegate }
        "analyze" { Invoke-Analyze }
        "start-proxy" { Start-Proxy }
        "start-runtime" { Start-Proxy }
        "stop-proxy" { Stop-Proxy }
        "stop-runtime" { Stop-Proxy }
        "test-proxy" { Test-Proxy }
        "test-runtime" { Test-Runtime }
        "usage" { Show-Usage }
        "tui" { Show-Tui }
        "redact" { Invoke-RedactCheck }
        "export-shareable" { Export-Shareable }
    }
}
