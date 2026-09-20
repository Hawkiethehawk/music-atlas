from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from runtime_config import crawler_settings, load_web_config, openai_compat_settings


class RuntimeConfigTests(unittest.TestCase):
    def test_settings_override_deep_merges_without_replacing_base(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "web.json"
            override = root / "settings.json"
            base.write_text(json.dumps({
                "runtime": {"openai_compat": {"model": "base", "api_key_env": "KEY"}},
                "crawler": {"request_timeout_seconds": 30, "qq_page_size": 100},
                "workflow": {"analysis_parallelism": 5},
            }), encoding="utf-8")
            override.write_text(json.dumps({
                "runtime": {"openai_compat": {"model": "override"}},
                "crawler": {"qq_page_size": 80},
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "ATLAS_WEB_CONFIG": str(base),
                "ATLAS_WEB_SETTINGS": str(override),
            }, clear=False):
                config = load_web_config()
                self.assertEqual(config["runtime"]["openai_compat"]["model"], "override")
                self.assertEqual(config["runtime"]["openai_compat"]["api_key_env"], "KEY")
                self.assertEqual(crawler_settings()["qq_page_size"], 80)
                self.assertEqual(openai_compat_settings()["model"], "override")


if __name__ == "__main__":
    unittest.main()
