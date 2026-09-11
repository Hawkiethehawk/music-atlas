"""Project-local executor entry (recommendation) over the OpenAI-compatible endpoint."""

from __future__ import annotations

try:
    from openai_compat_executor import main
except ImportError:  # pragma: no cover - 包模式
    from executors.openai_compat_executor import main


if __name__ == "__main__":
    raise SystemExit(main("recommendation"))
