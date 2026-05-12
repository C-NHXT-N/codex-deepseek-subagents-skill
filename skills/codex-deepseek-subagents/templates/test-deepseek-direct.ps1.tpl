# Managed by codex-deepseek-subagents
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\deepseek.local.env.ps1"

function Invoke-DeepSeekProbe {
    param(
        [string]$ThinkingType,
        [string]$ReasoningEffort = "high",
        [int]$MaxTokens = 256
    )

    $thinking = if ($ThinkingType -eq "enabled") {
        @{ type = "enabled"; reasoning_effort = $ReasoningEffort }
    }
    else {
        @{ type = "disabled" }
    }

    Add-Type -AssemblyName System.Net.Http
    $body = @{
        model = "__MODEL_PS__"
        messages = @(
            @{ role = "user"; content = "Which number is larger, 9.11 or 9.8? Reply with only the larger number." }
        )
        thinking = $thinking
        max_tokens = $MaxTokens
        stream = $false
    } | ConvertTo-Json -Depth 8 -Compress

    $client = [System.Net.Http.HttpClient]::new()
    try {
        $client.Timeout = [TimeSpan]::FromSeconds(180)
        $client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $env:DEEPSEEK_API_KEY)
        $content = [System.Net.Http.StringContent]::new($body, [System.Text.Encoding]::UTF8, "application/json")
        $result = $client.PostAsync("$($env:DEEPSEEK_OPENAI_BASE_URL.TrimEnd('/'))/chat/completions", $content).GetAwaiter().GetResult()
        $text = $result.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $result.IsSuccessStatusCode) {
            throw "DeepSeek HTTP $([int]$result.StatusCode): $text"
        }
        $response = $text | ConvertFrom-Json
    }
    finally {
        $client.Dispose()
    }
    $message = $response.choices[0].message
    $modelLabel = if ($ThinkingType -eq "enabled") { "$($response.model)(thinking)" } else { [string]$response.model }

    [pscustomobject]@{
        thinking_type = $ThinkingType
        reasoning_effort = if ($ThinkingType -eq "enabled") { $ReasoningEffort } else { $null }
        model = $response.model
        model_label = $modelLabel
        finish_reason = $response.choices[0].finish_reason
        content = $message.content
        has_reasoning_content = [bool]$message.reasoning_content
        reasoning_chars = if ($message.reasoning_content) { ([string]$message.reasoning_content).Length } else { 0 }
        reasoning_tokens = $response.usage.completion_tokens_details.reasoning_tokens
        prompt_tokens = $response.usage.prompt_tokens
        completion_tokens = $response.usage.completion_tokens
        total_tokens = $response.usage.total_tokens
    }
}

@(
    (Invoke-DeepSeekProbe -ThinkingType "disabled")
    (Invoke-DeepSeekProbe -ThinkingType "enabled" -ReasoningEffort "high" -MaxTokens 1024)
) | ConvertTo-Json -Depth 8
