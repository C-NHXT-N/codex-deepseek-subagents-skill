# Managed by codex-deepseek-subagents
# Local DeepSeek configuration. This file may contain an API key and must stay ignored by git.

$env:DEEPSEEK_API_KEY = '__API_KEY_PS__'

# DeepSeek OpenAI-compatible API.
$env:DEEPSEEK_OPENAI_BASE_URL = '__BASE_URL_PS__'
$env:DEEPSEEK_OPENAI_MODEL = '__MODEL_PS__'
$env:DEEPSEEK_OPENAI_FAST_MODEL = '__FAST_MODEL_PS__'
$env:DEEPSEEK_THINKING_DEFAULT = '__THINKING_DEFAULT_PS__'

# DeepSeek Anthropic-compatible API, useful for Claude-Code/OpenCode-style integrations.
$env:ANTHROPIC_BASE_URL = '__ANTHROPIC_BASE_URL_PS__'
$env:ANTHROPIC_AUTH_TOKEN = $env:DEEPSEEK_API_KEY
$env:ANTHROPIC_API_KEY = $env:DEEPSEEK_API_KEY
$env:ANTHROPIC_MODEL = '__MODEL_PS__[1m]'
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = '__MODEL_PS__[1m]'
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = '__MODEL_PS__[1m]'
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = '__FAST_MODEL_PS__'
$env:CLAUDE_CODE_SUBAGENT_MODEL = '__FAST_MODEL_PS__'
$env:CLAUDE_CODE_EFFORT_LEVEL = 'max'

# Local Responses-compatible proxy used by Codex.
$env:DEEPSEEK_PROXY_BASE_URL = 'http://127.0.0.1:__PORT__/v1'
$env:DEEPSEEK_PROXY_API_KEY = $env:DEEPSEEK_API_KEY

