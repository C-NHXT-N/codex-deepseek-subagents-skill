# Codex Desktop E2E Checklist

1. Install the skill into a target repository.
2. Run `doctor`.
3. Start the runtime.
4. Run `test-runtime`.
5. Open Codex Desktop in the target repo.
6. Confirm `.codex/agents/deepseek-worker.toml` is present.
7. Ask Codex to delegate a read-only repo analysis to DeepSeek.
8. Confirm the scope card appears before the request is sent.
9. Confirm the route card displays resolved model, thinking state, reasoning effort, and shell disabled.
10. Confirm the live timeline displays route selection, reasoning state, tool deltas, and usage updates.
11. Ask for a small code patch.
12. Confirm `patch.preview` appears and the task returns `requires_action`.
13. Confirm the patch is not applied before approval.
14. Approve and apply the patch.
15. Confirm the patch is applied and the tool loop resumes automatically.
16. Run tests.
17. Confirm the final response reports changed files and verification.
