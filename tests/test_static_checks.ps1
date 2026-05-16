$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repoRoot "skills/codex-deepseek-subagents/scripts/deepseek-codex.ps1"
$powershellExe = (Get-Process -Id $PID).Path

$parseErrors = $null
$null = [System.Management.Automation.PSParser]::Tokenize((Get-Content -Raw $script), [ref]$parseErrors)

if ($parseErrors.Count -gt 0) {
    throw "PowerShell parse errors: $($parseErrors | Out-String)"
}

. $script

$unixCases = @(
    @{ Root = "/repo"; Full = "/repo/.git/objects/aa"; Name = "aa"; ShouldScan = $false },
    @{ Root = "/repo"; Full = "/repo/backups/file.txt"; Name = "file.txt"; ShouldScan = $false },
    @{ Root = "/repo"; Full = "/repo/.codex/deepseek.local.env.ps1"; Name = "deepseek.local.env.ps1"; ShouldScan = $false },
    @{ Root = "/repo"; Full = "/repo/.codex/runtime/events.log.jsonl"; Name = "events.log.jsonl"; ShouldScan = $false },
    @{ Root = "/repo"; Full = "/repo/src/file.txt"; Name = "file.txt"; ShouldScan = $true }
)

$windowsCases = @(
    @{ Root = "C:\repo"; Full = "C:\repo\.git\objects\aa"; Name = "aa"; ShouldScan = $false },
    @{ Root = "C:\repo"; Full = "C:\repo\backups\file.txt"; Name = "file.txt"; ShouldScan = $false },
    @{ Root = "C:\repo"; Full = "C:\repo\folder\secret.local.env.ps1"; Name = "secret.local.env.ps1"; ShouldScan = $false },
    @{ Root = "C:\repo"; Full = "C:\repo\folder\runtime\events.log.jsonl"; Name = "events.log.jsonl"; ShouldScan = $false },
    @{ Root = "C:\repo"; Full = "C:\repo\src\file.txt"; Name = "file.txt"; ShouldScan = $true }
)

foreach ($case in @($unixCases + $windowsCases)) {
    $actual = Test-ShouldScanPath -RootPath $case.Root -FullPath $case.Full -Name $case.Name
    if ($actual -ne $case.ShouldScan) {
        throw "Unexpected redact path decision for $($case.Full): expected $($case.ShouldScan), got $actual"
    }
}

$tmp = New-Item -ItemType Directory -Force -Path ([System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "codex-deepseek-test-" + [guid]::NewGuid()))

try {
    & $script install `
        -ProjectRoot $tmp.FullName `
        -ApiKey "sk-test-placeholder" `
        -Port 5001

    $required = @(
        "user_config.json",
        ".codex/config.toml",
        ".codex/agents/deepseek-worker.toml",
        ".codex/deepseek.local.env.ps1",
        ".codex/deepseek.local.env.sh",
        ".codex/runtime/deepseek_scheduler.py",
        ".codex/runtime/deepseek_runtime.py",
        ".codex/runtime/task_queue.json",
        ".codex/runtime/sessions.json",
        ".codex/runtime/events.log.jsonl",
        ".codex/deepseek-codex.cmd",
        ".codex/deepseek-codex.sh",
        ".codex/test-runtime.ps1",
        ".codex/test-runtime.sh"
    )

    foreach ($rel in $required) {
        $path = Join-Path $tmp.FullName $rel
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Missing expected file: $rel"
        }
    }

    Set-Content -LiteralPath (Join-Path $tmp.FullName ".codex/test-responses-proxy.ps1") -Value "# Managed by codex-deepseek-subagents`nWrite-Host 'legacy'" -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $tmp.FullName ".codex/deepseek_responses_shim.py") -Value "# Managed by codex-deepseek-subagents`nprint('legacy')" -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $tmp.FullName ".codex/deepseek-proxy.log.jsonl") -Value "legacy" -Encoding UTF8

    & $script update `
        -ProjectRoot $tmp.FullName

    foreach ($rel in @(
        ".codex/test-responses-proxy.ps1",
        ".codex/deepseek_responses_shim.py",
        ".codex/deepseek-proxy.log.jsonl"
    )) {
        if (Test-Path -LiteralPath (Join-Path $tmp.FullName $rel)) {
            throw "Legacy artifact was not removed by update: $rel"
        }
    }

    foreach ($rel in @(
        ".codex/config.toml",
        ".codex/deepseek.local.env.ps1",
        ".codex/deepseek.local.env.sh"
    )) {
        $content = Get-Content -Raw (Join-Path $tmp.FullName $rel)
        if ($content -notmatch "127\.0\.0\.1:5001") {
            throw "$rel did not contain expected port 5001"
        }
    }

    $userConfig = Get-Content -Raw (Join-Path $tmp.FullName "user_config.json") | ConvertFrom-Json
    if (@($userConfig.PSObject.Properties.Name) -contains "deepseek_api_key") {
        throw "user_config.json must not contain deepseek_api_key"
    }
    if ($userConfig.defaults.execution_agent -ne "DeepSeek Worker") {
        throw "user_config.json did not contain expected execution_agent default"
    }

    $runtimeFiles = @(
        ".codex/runtime/events.log.jsonl",
        ".codex/runtime/runtime.pid",
        ".codex/runtime/stdout.log",
        ".codex/runtime/stderr.log",
        ".codex/runtime/task_queue.json",
        ".codex/runtime/sessions.json"
    )
    foreach ($rel in $runtimeFiles) {
        Set-Content -LiteralPath (Join-Path $tmp.FullName $rel) -Value "runtime" -Encoding UTF8
    }

    Set-Content -LiteralPath (Join-Path $tmp.FullName ".codex/test-runtime.ps1") -Value 'Write-Host "user file"' -Encoding UTF8

    $dryRunOutput = (& $powershellExe -NoProfile -File $script uninstall -ProjectRoot $tmp.FullName -DryRun 2>&1 | Out-String)

    if ($dryRunOutput -notmatch "Skipping non-managed file") {
        throw "Dry-run uninstall did not report skipping a non-managed managed-path target."
    }

    foreach ($rel in $runtimeFiles) {
        $runtimePattern = [regex]::Escape($rel).Replace('/', '[\\/]')
        if ($dryRunOutput -notmatch $runtimePattern) {
            throw "Dry-run uninstall did not mention runtime file $rel"
        }
        if (-not (Test-Path -LiteralPath (Join-Path $tmp.FullName $rel))) {
            throw "Dry-run uninstall removed runtime file $rel"
        }
    }
}
finally {
    Remove-Item -Recurse -Force $tmp.FullName -ErrorAction SilentlyContinue
}
