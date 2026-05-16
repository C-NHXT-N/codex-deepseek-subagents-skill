REM Managed by codex-deepseek-subagents
@echo off
setlocal
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0runtime\deepseek_runtime.py" %*
) else (
  python "%~dp0runtime\deepseek_runtime.py" %*
)
