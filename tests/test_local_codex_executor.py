from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from executors import local_codex_executor
from executors.local_codex_executor import _codex_overrides, _last_agent_message, _parse_json_object


class LocalCodexExecutorTests(unittest.TestCase):
    def test_extracts_only_the_final_agent_message_from_jsonl(self) -> None:
        output = "\n".join(
            [
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "reasoning", "text": "hidden"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": '{"ok":true}'},
                    }
                ),
            ]
        )
        self.assertEqual(_last_agent_message(output), '{"ok":true}')

    def test_parses_a_single_json_code_fence(self) -> None:
        self.assertEqual(_parse_json_object("```json\n{\"ok\": true}\n```"), {"ok": True})

    def test_rejects_non_object_json(self) -> None:
        with self.assertRaises(RuntimeError):
            _parse_json_object("[1, 2, 3]")


class CodexOverrideTests(unittest.TestCase):
    def _with_config(self, runtime: dict) -> list[str]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config" / "web.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"runtime": runtime}), encoding="utf-8")
            with mock.patch.object(local_codex_executor, "PROJECT_ROOT", root):
                return _codex_overrides()

    def test_reads_model_and_provider_overrides(self) -> None:
        overrides = self._with_config({
            "codex_reasoning_effort": "low",
            "codex_model": "glm-5.3-flash",
            "codex_model_provider": "zh-glm",
        })

        self.assertEqual(
            overrides,
            ["-c", 'model_reasoning_effort="low"', "-c", 'model="glm-5.3-flash"',
             "-c", 'model_provider="zh-glm"'],
        )

    def test_omits_blank_and_unknown_values(self) -> None:
        overrides = self._with_config({
            "codex_reasoning_effort": "impossible",
            "codex_model": "   ",
            "codex_model_provider": "",
        })

        self.assertEqual(overrides, [])

    def test_missing_config_file_yields_no_overrides(self) -> None:
        with TemporaryDirectory() as directory:
            with mock.patch.object(local_codex_executor, "PROJECT_ROOT", Path(directory)):
                self.assertEqual(_codex_overrides(), [])


if __name__ == "__main__":
    unittest.main()
