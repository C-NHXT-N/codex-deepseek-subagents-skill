# Managed by codex-deepseek-subagents
# Local DeepSeek configuration. This file may contain an API key and must stay ignored by git.

export DEEPSEEK_API_KEY='__API_KEY_SH__'

# DeepSeek OpenAI-compatible API.
export DEEPSEEK_OPENAI_BASE_URL='__BASE_URL_SH__'
export DEEPSEEK_OPENAI_MODEL='__MODEL_SH__'
export DEEPSEEK_OPENAI_FAST_MODEL='__FAST_MODEL_SH__'
export DEEPSEEK_THINKING_DEFAULT='__THINKING_DEFAULT_SH__'

# DeepSeek Anthropic-compatible API, useful for Claude-Code/OpenCode-style integrations.
export ANTHROPIC_BASE_URL='__ANTHROPIC_BASE_URL_SH__'
export ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY"
export ANTHROPIC_API_KEY="$DEEPSEEK_API_KEY"
export ANTHROPIC_MODEL='__MODEL_SH__[1m]'
export ANTHROPIC_DEFAULT_OPUS_MODEL='__MODEL_SH__[1m]'
export ANTHROPIC_DEFAULT_SONNET_MODEL='__MODEL_SH__[1m]'
export ANTHROPIC_DEFAULT_HAIKU_MODEL='__FAST_MODEL_SH__'
export CLAUDE_CODE_SUBAGENT_MODEL='__FAST_MODEL_SH__'
export CLAUDE_CODE_EFFORT_LEVEL='max'

# Local Responses-compatible proxy used by Codex.
export DEEPSEEK_PROXY_BASE_URL='http://127.0.0.1:__PORT__/v1'
export DEEPSEEK_PROXY_API_KEY="$DEEPSEEK_API_KEY"

