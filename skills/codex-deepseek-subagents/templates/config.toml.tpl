# Managed by codex-deepseek-subagents
# Project-level Codex config for GPT main agent + DeepSeek worker.
# Keep the main/default provider in user config; this file only adds the
# DeepSeek worker provider behind a local scheduler runtime.

[agents]
max_threads = 6
max_depth = 1

[model_providers.deepseek_responses]
name = "DeepSeek via Local Scheduler Runtime"
base_url = "http://127.0.0.1:__PORT__/v1"
wire_api = "responses"
env_key = "DEEPSEEK_PROXY_API_KEY"
