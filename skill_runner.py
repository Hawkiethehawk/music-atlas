"""Provider-neutral Skill execution adapters for Music Atlas.

The workflow never selects an AI model. An external executor may use any
model, vendor, SDK or retrieval implementation as long as it accepts the task
on stdin and returns one JSON object on stdout. Legacy Agent imports remain in
``agent_runner`` for compatibility with existing callers and fixtures.
"""

from __future__ import annotations

from typing import Any

from agent_runner import parse_skill_json, run_external_skill, run_skill

__all__ = ["parse_skill_json", "run_external_skill", "run_skill"]


def executor_metadata(*, mock: bool = False) -> dict[str, Any]:
    """Return model-neutral metadata for runtime reports."""

    return {
        "executor_kind": "mock" if mock else "generic_external_executor",
        "model_requirement": None,
        "provider_requirement": None,
    }
