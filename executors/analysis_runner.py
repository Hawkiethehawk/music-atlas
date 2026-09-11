"""Project-local Step 2 executor backed by the installed Codex CLI."""

from __future__ import annotations

from local_codex_executor import main


if __name__ == "__main__":
    raise SystemExit(main("analysis"))

