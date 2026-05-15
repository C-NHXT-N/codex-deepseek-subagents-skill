{
  "runtime": {
    "port": __PORT__,
    "log_level": "info"
  },
  "connected_agents": [
    {
      "name": "Codex Main",
      "kind": "codex_main",
      "endpoint": "local/codex-main",
      "enabled": true,
      "capabilities": [
        "analysis",
        "review"
      ],
      "defaults": {
        "role": "planner-reviewer"
      }
    },
    {
      "name": "DeepSeek Worker",
      "kind": "deepseek_worker",
      "endpoint": "local/deepseek-worker",
      "enabled": true,
      "capabilities": [
        "execution"
      ],
      "defaults": {
        "mode": "pro-thinking"
      }
    }
  ],
  "defaults": {
    "execution_agent": "DeepSeek Worker",
    "review_agent": "Codex Main"
  }
}
