from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import executors.openai_compat_executor as executor


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, body: str = "err") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("url", code, "reason", {}, io.BytesIO(body.encode("utf-8")))


def _completion(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode("utf-8")


_SETTINGS = {
    "base_url": "https://example.invalid/v1",
    "model": "glm-5.3-flash",
    "api_key_env": "TEST_ATLAS_API_KEY",
    "timeout_seconds": 60,
    "max_tokens": 1234,
}


class SettingsTests(unittest.TestCase):
    def test_reads_settings_from_project_config(self) -> None:
        settings = executor._settings()
        self.assertTrue(settings["model"], "项目配置必须声明模型")
        self.assertTrue(settings["api_key_env"], "项目配置必须声明密钥所在环境变量")
        self.assertTrue(str(settings["base_url"]).startswith("https://"), "必须使用 HTTPS 端点")

    def test_missing_settings_field_is_reported(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config" / "web.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"runtime": {"openai_compat": {
                "base_url": "  ", "model": "m", "api_key_env": "TEST_ATLAS_API_KEY"}}}), encoding="utf-8")
            with mock.patch.object(executor, "PROJECT_ROOT", root), \
                 mock.patch.dict(os.environ, {"TEST_ATLAS_API_KEY": "k"}):
                with self.assertRaises(RuntimeError) as ctx:
                    executor.run("analysis", "任务")
        self.assertIn("base_url", str(ctx.exception))

    def test_missing_api_key_is_reported(self) -> None:
        clean = {k: v for k, v in os.environ.items() if k != "TEST_ATLAS_API_KEY"}
        with mock.patch.dict(os.environ, clean, clear=True):
            with mock.patch.object(executor, "_settings", return_value=dict(_SETTINGS)):
                with self.assertRaises(RuntimeError) as ctx:
                    executor.run("analysis", "任务")
        self.assertIn("TEST_ATLAS_API_KEY", str(ctx.exception))


class PayloadTests(unittest.TestCase):
    def test_payload_wraps_task_with_role_instruction(self) -> None:
        payload = executor._chat_payload(dict(_SETTINGS), "analysis", "TASK-1")
        self.assertEqual(payload["model"], "glm-5.3-flash")
        self.assertEqual(payload["max_tokens"], 1234)
        system = payload["messages"][0]["content"]
        self.assertIn("歌单分析研究", system)
        self.assertIn("TASK-1", system)
        self.assertIn("MUSIC ATLAS TASK", system)
        self.assertEqual(payload["messages"][1]["content"], "TASK-1")

    def test_payload_disables_thinking_by_default(self) -> None:
        """实测 reasoning 占单次调用约 80% 耗时，因此默认关闭。"""

        payload = executor._chat_payload(dict(_SETTINGS), "analysis", "TASK-1")

        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_payload_keeps_thinking_when_explicitly_enabled(self) -> None:
        settings = dict(_SETTINGS, disable_thinking=False)

        payload = executor._chat_payload(settings, "analysis", "TASK-1")

        self.assertNotIn("thinking", payload)

    def test_unknown_role_is_rejected(self) -> None:
        with mock.patch.object(executor, "_settings", return_value=dict(_SETTINGS)):
            with self.assertRaises(ValueError):
                executor.run("unknown", "任务")


class RequestTests(unittest.TestCase):
    def test_success_parses_final_json(self) -> None:
        body = _completion('```json\n{"ok": true}\n```')
        with mock.patch.object(executor, "_settings", return_value=dict(_SETTINGS)), \
             mock.patch.dict(os.environ, {"TEST_ATLAS_API_KEY": "k"}), \
             mock.patch.object(executor.urllib.request, "urlopen", return_value=_FakeResponse(body)):
            result = executor.run("analysis", "任务", timeout=5)
        self.assertEqual(result, {"ok": True})

    def test_client_error_is_not_retried(self) -> None:
        calls: list[int] = []

        def fake_urlopen(request, timeout):  # noqa: ANN001
            calls.append(1)
            raise _http_error(401, "bad key")

        with mock.patch.object(executor.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.dict(os.environ, {"TEST_ATLAS_API_KEY": "k"}), \
             mock.patch.object(executor.time, "sleep") as slept:
            with self.assertRaises(RuntimeError) as ctx:
                executor._request(dict(_SETTINGS), "analysis", "任务", 5)
        self.assertEqual(len(calls), 1, "客户端错误不应重试")
        slept.assert_not_called()
        self.assertIn("401", str(ctx.exception))

    def test_server_error_is_retried_then_raises(self) -> None:
        calls: list[int] = []

        def fake_urlopen(request, timeout):  # noqa: ANN001
            calls.append(1)
            raise _http_error(502, "upstream")

        with mock.patch.object(executor.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.dict(os.environ, {"TEST_ATLAS_API_KEY": "k"}), \
             mock.patch.object(executor.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                executor._request(dict(_SETTINGS), "analysis", "任务", 5)
        self.assertEqual(len(calls), executor.MAX_ATTEMPTS, "服务端错误应重试至上限")
        self.assertIn("502", str(ctx.exception))

    def test_empty_content_is_reported(self) -> None:
        body = _completion("   ")
        with mock.patch.object(executor, "_settings", return_value=dict(_SETTINGS)), \
             mock.patch.dict(os.environ, {"TEST_ATLAS_API_KEY": "k"}), \
             mock.patch.object(executor.urllib.request, "urlopen", return_value=_FakeResponse(body)):
            with self.assertRaises(RuntimeError) as ctx:
                executor.run("analysis", "任务", timeout=5)
        self.assertIn("为空", str(ctx.exception))


class MainContractTests(unittest.TestCase):
    def test_main_prints_single_json_object(self) -> None:
        body = _completion('{"ok": true}')
        out, err = StringIO(), StringIO()
        with mock.patch.object(executor, "_settings", return_value=dict(_SETTINGS)), \
             mock.patch.dict(os.environ, {"TEST_ATLAS_API_KEY": "k"}), \
             mock.patch.object(executor.urllib.request, "urlopen", return_value=_FakeResponse(body)), \
             mock.patch.object(executor.sys.stdin, "read", return_value="任务文本"), \
             redirect_stdout(out), redirect_stderr(err):
            code = executor.main("recommendation")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"ok": True})
        self.assertEqual(err.getvalue(), "")

    def test_main_reports_failure_without_stdout_noise(self) -> None:
        out, err = StringIO(), StringIO()
        with mock.patch.object(executor, "_settings", side_effect=RuntimeError("配置缺失")), \
             mock.patch.object(executor.sys.stdin, "read", return_value="任务文本"), \
             redirect_stdout(out), redirect_stderr(err):
            code = executor.main("analysis")
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("配置缺失", err.getvalue())


class ProjectExecutorEntriesTests(unittest.TestCase):
    def test_thin_runners_exist_and_are_wired(self) -> None:
        for role in ("analysis", "recommendation", "taste"):
            path = Path(executor.PROJECT_ROOT / "executors" / f"openai_{role}.py")
            with self.subTest(role=role):
                self.assertTrue(path.is_file(), f"缺少 {path.name}")
                self.assertIn(f'main("{role}")', path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
