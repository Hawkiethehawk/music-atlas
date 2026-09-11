from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

import proc_util


class HiddenWindowTests(unittest.TestCase):
    def test_windows_returns_create_no_window(self) -> None:
        with mock.patch.object(proc_util.os, "name", "nt"):
            kwargs = proc_util.hidden_window_kwargs()

        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            self.assertEqual(kwargs, {"creationflags": subprocess.CREATE_NO_WINDOW})
        else:  # pragma: no cover - 非 Windows 解释器上不会走到
            self.assertEqual(kwargs, {})

    def test_posix_returns_empty(self) -> None:
        with mock.patch.object(proc_util.os, "name", "posix"):
            self.assertEqual(proc_util.hidden_window_kwargs(), {})

    def test_real_platform_is_consistent(self) -> None:
        kwargs = proc_util.hidden_window_kwargs()
        if os.name == "nt":  # pragma: no cover - 取决于运行平台
            self.assertIn("creationflags", kwargs)
        else:
            self.assertEqual(kwargs, {})


if __name__ == "__main__":
    unittest.main()
