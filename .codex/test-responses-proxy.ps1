# Managed by codex-deepseek-subagents
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\deepseek.local.env.ps1"

$headers = @{
    Authorization = "Bearer $env:DEEPSEEK_PROXY_API_KEY"
    "Content-Type" = "application/json"
}

$body = @{
    model = "deepseek-v4-pro"
    input = @(
        @{ role = "user"; content = "Return exactly this JSON and nothing else: {""status"":""proxy-ok""}" }
    )
    metadata = @{
        deepseek_reasoning_effort = "disabled"
    }
    max_output_tokens = 64
} | ConvertTo-Json -Depth 8

$response = Invoke-RestMethod -Method Post -Uri "$env:DEEPSEEK_PROXY_BASE_URL/responses" -Headers $headers -Body $body
[pscustomobject]@{
    id = $response.id
    status = $response.status
    model = $response.model
    model_label = $response.model_label
    output_text = $response.output_text
    contains_proxy_ok = ([string]$response.output_text).Contains("proxy-ok")
    input_tokens = $response.usage.input_tokens
    output_tokens = $response.usage.output_tokens
    reasoning_tokens = $response.usage.reasoning_tokens
    total_tokens = $response.usage.total_tokens
} | ConvertTo-Json -Compress

