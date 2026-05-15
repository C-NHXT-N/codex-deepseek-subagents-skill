# Managed by codex-deepseek-subagents
import runpy
import sys
from pathlib import Path


def main():
    runtime_entry = Path(__file__).with_name("runtime") / "deepseek_scheduler.py"
    if not runtime_entry.exists():
        raise SystemExit(f"Missing runtime entrypoint: {runtime_entry}")
    sys.argv[0] = str(runtime_entry)
    runpy.run_path(str(runtime_entry), run_name="__main__")


if __name__ == "__main__":
    main()
