param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "update", "uninstall", "doctor", "start-proxy", "stop-proxy", "test-proxy", "usage", "redact", "export-shareable")]
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
    [string]$OutFile = ""
)

$ErrorActionPreference = "Stop"
$ManagedMarker = "# Managed by codex-deepseek-subagents"
$SkillRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$TemplateRoot = Join-Path $SkillRoot "templates"

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

function Expand-Template {
    param([string]$TemplateName)
    $content = Get-Content -Raw (Join-Path $TemplateRoot $TemplateName)
    $content = $content.Replace("__API_KEY_PS__", (Escape-TemplateValue $ApiKey))
    $content = $content.Replace("__BASE_URL_PS__", (Escape-TemplateValue $BaseUrl))
    $content = $content.Replace("__ANTHROPIC_BASE_URL_PS__", (Escape-TemplateValue $AnthropicBaseUrl))
    $content = $content.Replace("__MODEL_PS__", (Escape-TemplateValue $Model))
    $content = $content.Replace("__FAST_MODEL_PS__", (Escape-TemplateValue $FastModel))
    $content = $content.Replace("__THINKING_DEFAULT_PS__", (Escape-TemplateValue $ThinkingDefault))
    $content = $content.Replace("__MODEL_TOML__", (Escape-TomlString $Model))
    $content = $content.Replace("__PORT__", [string]$Port)
    return $content
}

function Test-ManagedFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $firstLine = Get-Content -LiteralPath $Path -TotalCount 1 -ErrorAction SilentlyContinue
    return $firstLine -eq $ManagedMarker
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
    Set-Content -LiteralPath $Path -Value $Content -Encoding UTF8
}

function Add-GitIgnoreRules {
    $gitignore = Get-ProjectPath ".gitignore"
    $rules = @(
        ".codex/*.local.*",
        ".codex/deepseek-proxy.log.jsonl",
        ".codex/backups/"
    )
    $existing = if (Test-Path -LiteralPath $gitignore) { Get-Content -LiteralPath $gitignore } else { @() }
    $missing = @($rules | Where-Object { $existing -notcontains $_ })
    if ($missing.Count -eq 0) {
        Write-Step ".gitignore already contains DeepSeek local rules."
        return
    }
    if ($DryRun) {
        Write-Step "Would append .gitignore rules: $($missing -join ', ')"
        return
    }
    if (Test-Path -LiteralPath $gitignore) { Backup-File $gitignore }
    Add-Content -LiteralPath $gitignore -Value @("", "# Local Codex DeepSeek secrets and logs") -Encoding UTF8
    Add-Content -LiteralPath $gitignore -Value $missing -Encoding UTF8
}

function Install-OrUpdate {
    param([bool]$IsUpdate)
    if (-not $ApiKey -and -not $IsUpdate) {
        throw "install requires -ApiKey. The key is written only to .codex/deepseek.local.env.ps1."
    }
    if (-not $ApiKey -and $IsUpdate) {
        $existingEnv = Get-ProjectPath ".codex/deepseek.local.env.ps1"
        if (Test-Path -LiteralPath $existingEnv) {
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
    Write-ManagedFile (Get-ProjectPath ".codex/deepseek.local.env.ps1") (Expand-Template "deepseek.local.env.ps1.tpl") -Secret
    Write-ManagedFile (Get-ProjectPath ".codex/deepseek-responses-shim.ps1") (Expand-Template "deepseek-responses-shim.ps1.tpl")
    Write-ManagedFile (Get-ProjectPath ".codex/test-deepseek-direct.ps1") (Expand-Template "test-deepseek-direct.ps1.tpl")
    Write-ManagedFile (Get-ProjectPath ".codex/test-responses-proxy.ps1") (Expand-Template "test-responses-proxy.ps1.tpl")
    Add-GitIgnoreRules
    Write-Step "$(if ($IsUpdate) { 'Update' } else { 'Install' }) complete."
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

function Uninstall-Project {
    $paths = @(
        ".codex/config.toml",
        ".codex/agents/deepseek-worker.toml",
        ".codex/deepseek.local.env.ps1",
        ".codex/deepseek-responses-shim.ps1",
        ".codex/test-deepseek-direct.ps1",
        ".codex/test-responses-proxy.ps1"
    )
    foreach ($relative in $paths) {
        Remove-ManagedPath (Get-ProjectPath $relative)
    }
    $logPath = Get-ProjectPath ".codex/deepseek-proxy.log.jsonl"
    if (Test-Path -LiteralPath $logPath) {
        if ($DryRun) {
            Write-Step "Would remove proxy log: $logPath"
        }
        else {
            Remove-Item -LiteralPath $logPath -Force
            Write-Step "Removed proxy log: $logPath"
        }
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

function Test-DeepSeekDirect {
    Import-LocalEnv
    $headers = @{ Authorization = "Bearer $env:DEEPSEEK_API_KEY"; "Content-Type" = "application/json" }
    $body = @{
        model = $env:DEEPSEEK_OPENAI_MODEL
        messages = @(@{ role = "user"; content = "Reply with exactly: direct-ok" })
        thinking = @{ type = "disabled" }
        max_tokens = 32
        stream = $false
    } | ConvertTo-Json -Depth 8
    $response = Invoke-RestMethod -Method Post -Uri "$env:DEEPSEEK_OPENAI_BASE_URL/chat/completions" -Headers $headers -Body $body
    return [pscustomobject]@{
        ok = ([string]$response.choices[0].message.content).Contains("direct-ok")
        model = $response.model
        total_tokens = $response.usage.total_tokens
    }
}

function Test-DeepSeekThinking {
    Import-LocalEnv
    $headers = @{ Authorization = "Bearer $env:DEEPSEEK_API_KEY"; "Content-Type" = "application/json" }
    $body = @{
        model = $env:DEEPSEEK_OPENAI_MODEL
        messages = @(@{ role = "user"; content = "Which number is larger, 9.11 or 9.8? Reply with only the larger number." })
        thinking = @{ type = "enabled"; reasoning_effort = "high" }
        max_tokens = 1024
        stream = $false
    } | ConvertTo-Json -Depth 8
    $response = Invoke-RestMethod -Method Post -Uri "$env:DEEPSEEK_OPENAI_BASE_URL/chat/completions" -Headers $headers -Body $body
    return [pscustomobject]@{
        ok = [bool]$response.choices[0].message.reasoning_content
        content = $response.choices[0].message.content
        reasoning_tokens = $response.usage.completion_tokens_details.reasoning_tokens
        total_tokens = $response.usage.total_tokens
    }
}

function Start-Proxy {
    Import-LocalEnv
    $shim = Get-ProjectPath ".codex/deepseek-responses-shim.ps1"
    $pidFile = Get-ProjectPath ".codex/deepseek-proxy.pid"
    if (Test-Path -LiteralPath $pidFile) {
        $oldPid = Get-Content -Raw -LiteralPath $pidFile
        if ($oldPid -and (Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue)) {
            Write-Step "Proxy already running with PID $oldPid"
            return
        }
    }
    if ($DryRun) {
        Write-Step "Would start proxy: $shim on port $Port"
        return
    }
    $process = Start-Process -FilePath "powershell" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $shim, "-Port", [string]$Port, "-LogPath", ".codex/deepseek-proxy.log.jsonl") -WorkingDirectory (Resolve-FullPath $ProjectRoot) -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ASCII
    Write-Step "Started proxy PID $($process.Id) on port $Port"
}

function Stop-Proxy {
    $pidFile = Get-ProjectPath ".codex/deepseek-proxy.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        Write-Step "No proxy pid file found."
        return
    }
    $pidText = Get-Content -Raw -LiteralPath $pidFile
    if ($pidText -and (Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue)) {
        if ($DryRun) {
            Write-Step "Would stop proxy PID $pidText"
        }
        else {
            Stop-Process -Id ([int]$pidText) -Force
            Write-Step "Stopped proxy PID $pidText"
        }
    }
    if (-not $DryRun) { Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue }
}

function Test-Proxy {
    Import-LocalEnv
    $script = Get-ProjectPath ".codex/test-responses-proxy.ps1"
    & $script
}

function Invoke-Doctor {
    $checks = [ordered]@{}
    $checks.project_root = Resolve-FullPath $ProjectRoot
    $checks.config_exists = Test-Path -LiteralPath (Get-ProjectPath ".codex/config.toml")
    $checks.worker_exists = Test-Path -LiteralPath (Get-ProjectPath ".codex/agents/deepseek-worker.toml")
    $checks.env_exists = Test-Path -LiteralPath (Get-ProjectPath ".codex/deepseek.local.env.ps1")
    $checks.env_ignored = $false
    $gitignore = Get-ProjectPath ".gitignore"
    if (Test-Path -LiteralPath $gitignore) {
        $gitignoreText = Get-Content -Raw -LiteralPath $gitignore
        $checks.env_ignored = $gitignoreText.Contains(".codex/*.local.*")
    }
    try { $checks.direct_api = Test-DeepSeekDirect } catch { $checks.direct_api_error = $_.Exception.Message }
    try { $checks.thinking = Test-DeepSeekThinking } catch { $checks.thinking_error = $_.Exception.Message }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        $checks.proxy_health = $health
    }
    catch {
        $checks.proxy_health_error = "Proxy is not running on port $Port. Run start-proxy."
    }
    [pscustomobject]$checks | ConvertTo-Json -Depth 8
}

function Show-Usage {
    $log = Get-ProjectPath ".codex/deepseek-proxy.log.jsonl"
    if (-not (Test-Path -LiteralPath $log)) {
        Write-Step "No usage log found: $log"
        return
    }
    $entries = Get-Content -LiteralPath $log | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json }
    $summary = [pscustomobject]@{
        requests = @($entries | Where-Object { $_.total_tokens }).Count
        prompt_tokens = (@($entries | ForEach-Object { $_.prompt_tokens }) | Measure-Object -Sum).Sum
        completion_tokens = (@($entries | ForEach-Object { $_.completion_tokens }) | Measure-Object -Sum).Sum
        reasoning_tokens = (@($entries | ForEach-Object { $_.reasoning_tokens }) | Measure-Object -Sum).Sum
        total_tokens = (@($entries | ForEach-Object { $_.total_tokens }) | Measure-Object -Sum).Sum
    }
    $summary | ConvertTo-Json -Compress
}

function Invoke-RedactCheck {
    $root = Resolve-FullPath $ProjectRoot
    $findings = @()
    Get-ChildItem -LiteralPath $root -Recurse -File -Force |
        Where-Object {
            $_.FullName -notmatch '\\.git\\' -and
            $_.Name -notlike "*.local.*" -and
            $_.FullName -notmatch '\\backups\\' -and
            $_.Name -ne "deepseek-proxy.log.jsonl"
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
    $allowedRoots = @("SKILL.md", "agents", "scripts", "templates")
    $files = foreach ($root in $allowedRoots) {
        $path = Join-Path $SkillRoot $root
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Get-Item -LiteralPath $path
        }
        elseif (Test-Path -LiteralPath $path -PathType Container) {
            Get-ChildItem -LiteralPath $path -Recurse -File | Where-Object {
                (-not ($_.Name -like "*.local.*" -and $_.Name -notlike "*.tpl")) -and
                $_.Name -ne "deepseek-proxy.log.jsonl" -and
                $_.FullName -notmatch '\\backups\\'
            }
        }
    }
    Compress-Archive -LiteralPath @($files | ForEach-Object { $_.FullName }) -DestinationPath $destination -Force
    Write-Step "Exported shareable skill zip: $destination"
}

switch ($Command) {
    "install" { Install-OrUpdate -IsUpdate $false }
    "update" { Install-OrUpdate -IsUpdate $true }
    "uninstall" { Uninstall-Project }
    "doctor" { Invoke-Doctor }
    "start-proxy" { Start-Proxy }
    "stop-proxy" { Stop-Proxy }
    "test-proxy" { Test-Proxy }
    "usage" { Show-Usage }
    "redact" { Invoke-RedactCheck }
    "export-shareable" { Export-Shareable }
}
