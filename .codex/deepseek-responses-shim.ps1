# Managed by codex-deepseek-subagents
param(
    [int]$Port = 4000,
    [string]$LogPath = ".codex/deepseek-proxy.log.jsonl"
)

$ErrorActionPreference = "Stop"

if (-not $env:DEEPSEEK_API_KEY) {
    throw "DEEPSEEK_API_KEY is not set. Run: . .codex/deepseek.local.env.ps1"
}

$baseUrl = if ($env:DEEPSEEK_OPENAI_BASE_URL) { $env:DEEPSEEK_OPENAI_BASE_URL } else { "https://api.deepseek.com" }
$listener = [System.Net.HttpListener]::new()
$prefix = "http://127.0.0.1:$Port/"
$listener.Prefixes.Add($prefix)
$listener.Start()
Write-Host "DeepSeek Responses shim listening on $prefix"
Write-Host "Log: $LogPath"

function Read-RequestBody {
    param($Request)
    $reader = [System.IO.StreamReader]::new($Request.InputStream, $Request.ContentEncoding)
    try {
        return $reader.ReadToEnd()
    }
    finally {
        $reader.Dispose()
    }
}

function Write-JsonResponse {
    param($Response, [int]$StatusCode, $Object)
    $json = $Object | ConvertTo-Json -Depth 20
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $Response.StatusCode = $StatusCode
    $Response.ContentType = "application/json; charset=utf-8"
    $Response.ContentLength64 = $bytes.Length
    $Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $Response.OutputStream.Close()
}

function Append-ProxyLog {
    param($Entry)
    $dir = Split-Path -Parent $LogPath
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    ($Entry | ConvertTo-Json -Depth 20 -Compress) | Add-Content -Path $LogPath -Encoding UTF8
}

function Get-WebExceptionBody {
    param($Exception)
    if (-not $Exception.Response) { return $null }
    try {
        $stream = $Exception.Response.GetResponseStream()
        if (-not $stream) { return $null }
        $reader = [System.IO.StreamReader]::new($stream)
        try {
            return $reader.ReadToEnd()
        }
        finally {
            $reader.Dispose()
        }
    }
    catch {
        return $null
    }
}

function Invoke-DeepSeekChat {
    param([hashtable]$ChatBody)
    Add-Type -AssemblyName System.Net.Http
    $json = $ChatBody | ConvertTo-Json -Depth 20 -Compress
    $client = [System.Net.Http.HttpClient]::new()
    try {
        $client.Timeout = [TimeSpan]::FromSeconds(180)
        $client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $env:DEEPSEEK_API_KEY)
        $content = [System.Net.Http.StringContent]::new($json, [System.Text.Encoding]::UTF8, "application/json")
        $result = $client.PostAsync("$($baseUrl.TrimEnd('/'))/chat/completions", $content).GetAwaiter().GetResult()
        $text = $result.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $result.IsSuccessStatusCode) {
            throw "DeepSeek HTTP $([int]$result.StatusCode): $text"
        }
        return $text | ConvertFrom-Json
    }
    finally {
        $client.Dispose()
    }
}

function Convert-ResponseInputToMessages {
    param([Parameter(Mandatory = $false)]$ResponseInput)
    $messages = @()

    if ($ResponseInput -is [string]) {
        if ($ResponseInput.Trim().Length -gt 0) {
            $messages += @{ role = "user"; content = $ResponseInput }
        }
        return $messages
    }

    $items = if ($ResponseInput -is [System.Array]) { $ResponseInput } else { @($ResponseInput) }

    foreach ($item in $items) {
        if ($null -eq $item) { continue }

        if ($item.role -and $item.content) {
            $role = [string]$item.role
            $content = $item.content
            if ($content -is [string]) {
                $messages += @{ role = $role; content = $content }
            }
            else {
                $textParts = @()
                foreach ($part in @($content)) {
                    if ($part.text) { $textParts += [string]$part.text }
                    elseif ($part.type -eq "input_text" -and $part.text) { $textParts += [string]$part.text }
                    elseif ($part.type -eq "output_text" -and $part.text) { $textParts += [string]$part.text }
                }
                if ($textParts.Count -gt 0) {
                    $messages += @{ role = $role; content = ($textParts -join "`n") }
                }
            }
        }
        elseif ($item.type -eq "message" -and $item.role -and $item.content) {
            $messages += @{ role = [string]$item.role; content = [string]$item.content }
        }
        elseif ($item.type -eq "input_text" -and $item.text) {
            $messages += @{ role = "user"; content = [string]$item.text }
        }
    }

    return $messages
}

try {
    while ($listener.IsListening) {
        $context = $listener.GetContext()
        $request = $context.Request
        $response = $context.Response

        try {
            if ($request.HttpMethod -eq "GET" -and $request.Url.AbsolutePath -eq "/health") {
                Write-JsonResponse $response 200 @{ ok = $true; service = "deepseek-responses-shim" }
                continue
            }

            $expectedProxyKey = $env:DEEPSEEK_PROXY_API_KEY
            if ($expectedProxyKey) {
                $authorization = $request.Headers["Authorization"]
                if ($authorization -ne "Bearer $expectedProxyKey") {
                    Write-JsonResponse $response 401 @{ error = @{ message = "Invalid proxy authorization." } }
                    continue
                }
            }

            if ($request.HttpMethod -ne "POST" -or $request.Url.AbsolutePath -ne "/v1/responses") {
                Write-JsonResponse $response 404 @{ error = @{ message = "Only POST /v1/responses is implemented by this smoke-test shim." } }
                continue
            }

            $rawBody = Read-RequestBody $request
            $payload = $rawBody | ConvertFrom-Json
            $model = if ($payload.model) { [string]$payload.model } elseif ($env:DEEPSEEK_OPENAI_MODEL) { $env:DEEPSEEK_OPENAI_MODEL } else { "deepseek-v4-pro" }
            $messages = Convert-ResponseInputToMessages -ResponseInput $payload.input
            if ($messages.Count -eq 0) {
                $messages = @(@{ role = "user"; content = "Respond with exactly: ok" })
            }

            $defaultEffort = if ($env:DEEPSEEK_THINKING_DEFAULT) { $env:DEEPSEEK_THINKING_DEFAULT } else { "disabled" }
            $effort = if ($payload.metadata -and $payload.metadata.deepseek_reasoning_effort) { [string]$payload.metadata.deepseek_reasoning_effort } else { $defaultEffort }
            $thinking = if ($effort -eq "disabled" -or $effort -eq "none" -or $effort -eq "low-cost") {
                @{ type = "disabled" }
            }
            else {
                @{ type = "enabled"; reasoning_effort = $effort }
            }
            $modelLabel = if ($thinking.type -eq "enabled") { "$model(thinking)" } else { $model }

            $messageArray = @($messages)
            $chatBody = @{
                model = $model
                messages = $messageArray
                thinking = $thinking
                max_tokens = if ($payload.max_output_tokens) { [int]$payload.max_output_tokens } else { 512 }
                stream = $false
            }

            try {
                $chatResponse = Invoke-DeepSeekChat -ChatBody $chatBody
            }
            catch {
                Append-ProxyLog @{
                    ts = (Get-Date).ToUniversalTime().ToString("o")
                    path = $request.Url.AbsolutePath
                    upstream_error = $_.Exception.Message
                    model = $model
                    model_label = $modelLabel
                    thinking_type = $thinking.type
                    message_count = $messageArray.Count
                    request_input_chars = ($messageArray | ForEach-Object { $_.content.Length } | Measure-Object -Sum).Sum
                }
                throw
            }

            $message = $chatResponse.choices[0].message
            $content = if ($message.content) { [string]$message.content } else { "" }
            $reasoningChars = if ($message.reasoning_content) { ([string]$message.reasoning_content).Length } else { 0 }
            $reasoningTokens = $null
            if ($chatResponse.usage -and $chatResponse.usage.completion_tokens_details) {
                $reasoningTokens = $chatResponse.usage.completion_tokens_details.reasoning_tokens
            }

            $id = if ($chatResponse.id) { [string]$chatResponse.id } else { "resp_" + [Guid]::NewGuid().ToString("N") }
            $created = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            $result = @{
                id = $id
                object = "response"
                created_at = $created
                status = "completed"
                error = $null
                model = $model
                output = @(
                    @{
                        id = "msg_" + [Guid]::NewGuid().ToString("N")
                        type = "message"
                        status = "completed"
                        role = "assistant"
                        content = @(
                            @{
                                type = "output_text"
                                text = $content
                                annotations = @()
                            }
                        )
                    }
                )
                output_text = $content
                usage = @{
                    input_tokens = $chatResponse.usage.prompt_tokens
                    output_tokens = $chatResponse.usage.completion_tokens
                    total_tokens = $chatResponse.usage.total_tokens
                    reasoning_tokens = $reasoningTokens
                }
            }

            Append-ProxyLog @{
                ts = (Get-Date).ToUniversalTime().ToString("o")
                path = $request.Url.AbsolutePath
                model = $model
                model_label = $modelLabel
                thinking_type = $thinking.type
                reasoning_effort = if ($thinking.reasoning_effort) { $thinking.reasoning_effort } else { $null }
                request_input_chars = ($messageArray | ForEach-Object { $_.content.Length } | Measure-Object -Sum).Sum
                message_count = $messageArray.Count
                prompt_tokens = $chatResponse.usage.prompt_tokens
                completion_tokens = $chatResponse.usage.completion_tokens
                reasoning_tokens = $reasoningTokens
                reasoning_chars_discarded = $reasoningChars
                total_tokens = $chatResponse.usage.total_tokens
            }

            Write-JsonResponse $response 200 $result
        }
        catch {
            Append-ProxyLog @{
                ts = (Get-Date).ToUniversalTime().ToString("o")
                path = $request.Url.AbsolutePath
                error = $_.Exception.Message
            }
            Write-JsonResponse $response 500 @{ error = @{ message = $_.Exception.Message } }
        }
    }
}
finally {
    $listener.Stop()
}

