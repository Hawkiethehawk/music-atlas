from __future__ import annotations

import json
import unittest

from executors.local_codex_executor import _last_agent_message, _parse_json_object


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


if __name__ == "__main__":
    unittest.main()
