from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import setup_tool
from setup_tool import (
    ATLAS_MARKER,
    MIN_PYTHON,
    Check,
    append_path_entry,
    check_atlas_command,
    check_config,
    check_python,
    collect_checks,
    ensure_windows_path,
    register_command,
    setup_main,
    wrapper_content,
    wrapper_path,
)


def _check_list(*, tools_status: str, web_status: str, browser_status: str, atlas_status: str) -> list[Check]:
    return [
        Check("python", "Python 运行时", "ok", "ok"),
        Check("node", "Node.js", "ok", "ok"),
        Check("npm", "npm", "ok", "ok"),
        Check("tools_deps", "Apple 导出依赖", tools_status, "detail", "fix"),
        Check("web_deps", "网页测试依赖", web_status, "detail", "fix"),
        Check("browser", "Playwright Chromium", browser_status, "detail", "fix"),
        Check("config", "网页配置", "ok", "ok"),
        Check("modules", "Python 模块自检", "ok", "ok"),
        Check("keyring", "系统密钥库", "ok", "ok"),
        Check("atlas", "atlas 命令", atlas_status, "detail", "fix"),
    ]


class PathEntryTests(unittest.TestCase):
    def test_appends_missing_entry(self) -> None:
        joined, changed = append_path_entry("/a/b", "/c/d")

        self.assertTrue(changed)
        self.assertEqual(joined, os.pathsep.join(["/a/b", "/c/d"]))

    def test_keeps_existing_entry_once(self) -> None:
        existing = os.pathsep.join(["/a/b", "/c/d"])
        for candidate in ("/c/d", "/c/d/", "/C/D"):
            with self.subTest(candidate=candidate):
                joined, changed = append_path_entry(existing, candidate)
                self.assertFalse(changed)
                self.assertEqual(joined, existing)

    def test_handles_empty_current_value(self) -> None:
        joined, changed = append_path_entry("", "/only")

        self.assertTrue(changed)
        self.assertEqual(joined, "/only")

    def test_drops_blank_segments(self) -> None:
        joined, changed = append_path_entry(os.pathsep.join(["", "  ", ""]), "/x")

        self.assertTrue(changed)
        self.assertEqual(joined, "/x")


class EnsureWindowsPathTests(unittest.TestCase):
    def test_writes_once_and_stays_idempotent(self) -> None:
        store = {"path": "/usr/bin"}
        written: list[str] = []
        broadcasts: list[bool] = []

        def writer(value: str) -> None:
            store["path"] = value
            written.append(value)

        def run() -> bool:
            return ensure_windows_path(
                Path("/opt/atlas/bin"),
                reader=lambda: store["path"],
                writer=writer,
                broadcast=lambda: broadcasts.append(True),
            )

        self.assertTrue(run())
        self.assertFalse(run(), "重复注册不应再次写入用户 PATH")
        self.assertEqual(len(written), 1)
        self.assertEqual(len(broadcasts), 1)
        self.assertIn(str(Path("/opt/atlas/bin")), store["path"])


class WrapperTests(unittest.TestCase):
    def test_wrapper_invokes_repository_cli(self) -> None:
        content = wrapper_content(sys.executable, Path("/repo/atlas.py"))

        self.assertIn(ATLAS_MARKER, content)
        self.assertIn(str(Path("/repo/atlas.py")), content)
        self.assertIn(sys.executable, content)

    def test_wrapper_path_uses_platform_extension(self) -> None:
        expected = "atlas.cmd" if os.name == "nt" else "atlas"

        self.assertEqual(wrapper_path(Path("/bin")).name, expected)


class RegisterCommandTests(unittest.TestCase):
    def test_writes_wrapper_into_given_bin_dir(self) -> None:
        with TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            store = {"path": ""}
            with mock.patch.object(setup_tool, "_read_user_path", lambda: store["path"]), \
                 mock.patch.object(setup_tool, "_write_user_path", lambda value: store.update(path=value)), \
                 mock.patch.object(setup_tool, "_broadcast_environment_change", lambda: None):
                first = register_command(bin_dir)
                second = register_command(bin_dir)

            script = wrapper_path(bin_dir)
            self.assertTrue(script.is_file())
            self.assertIn(ATLAS_MARKER, script.read_text(encoding="utf-8"))
            self.assertEqual(first["command_path"], str(script))
            self.assertEqual(second["command_path"], str(script))
            if os.name == "nt":
                self.assertTrue(first["path_added"])
                self.assertFalse(second["path_added"], "重复注册不应再次追加 PATH")
                self.assertEqual(len([part for part in store["path"].split(os.pathsep) if part]), 1)
            else:
                self.assertIsNotNone(second["path_hint"])
                self.assertEqual(script.stat().st_mode & 0o111, 0o111, "POSIX 包装脚本必须可执行")


class CheckTests(unittest.TestCase):
    def test_check_python_reports_running_interpreter(self) -> None:
        check = check_python()

        expected = "ok" if sys.version_info[:2] >= MIN_PYTHON else "missing"
        self.assertEqual(check.status, expected)
        self.assertIn(f"{sys.version_info[0]}.{sys.version_info[1]}", check.detail)

    def test_check_config_accepts_project_config(self) -> None:
        check = check_config()

        self.assertEqual(check.key, "config")
        self.assertEqual(check.status, "ok")

    def test_check_atlas_command_reports_missing_wrapper(self) -> None:
        with TemporaryDirectory() as directory:
            check = check_atlas_command(Path(directory) / "bin")

        self.assertEqual(check.status, "missing")
        self.assertIsNotNone(check.fix)

    def test_collect_checks_covers_every_expected_key(self) -> None:
        keys = {check.key for check in collect_checks()}

        self.assertEqual(
            keys,
            {"python", "node", "npm", "tools_deps", "web_deps", "browser", "config", "modules", "keyring", "atlas"},
        )


class SetupMainTests(unittest.TestCase):
    def test_setup_installs_missing_dependencies_then_registers(self) -> None:
        first = _check_list(tools_status="missing", web_status="optional", browser_status="missing",
                            atlas_status="missing")
        healthy = [replace(check, status="ok") for check in first]
        calls: list[str] = []

        def fake_node_deps(directory: Path) -> str:
            calls.append(directory.name)
            return f"{directory.name}/node_modules 已安装"

        def fake_register(*_args, **_kwargs) -> dict[str, object]:
            calls.append("register")
            return {"command_path": "/bin/atlas", "bin_dir": "/bin", "path_added": True,
                    "path_updated": True, "path_hint": None}

        with mock.patch.object(setup_tool, "collect_checks", side_effect=[first, healthy]), \
             mock.patch.object(setup_tool, "install_node_deps", side_effect=fake_node_deps), \
             mock.patch.object(setup_tool, "install_browser", side_effect=lambda: calls.append("browser") or "ok"), \
             mock.patch.object(setup_tool, "register_command", side_effect=fake_register), \
             redirect_stdout(io.StringIO()):
            code = setup_main([])

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["tools", "web", "browser", "register"])

    def test_check_only_never_installs_or_registers(self) -> None:
        checks = _check_list(tools_status="missing", web_status="optional", browser_status="missing",
                             atlas_status="missing")
        buffer = io.StringIO()

        with mock.patch.object(setup_tool, "collect_checks", return_value=checks), \
             mock.patch.object(setup_tool, "install_node_deps", side_effect=AssertionError("不应安装依赖")), \
             mock.patch.object(setup_tool, "install_browser", side_effect=AssertionError("不应安装浏览器")), \
             mock.patch.object(setup_tool, "register_command", side_effect=AssertionError("不应注册命令")), \
             redirect_stdout(buffer):
            code = setup_main(["--check-only"])

        self.assertEqual(code, 1, "缺少必需依赖时 check-only 应返回非零")
        self.assertIn("未做任何修改", buffer.getvalue())

    def test_check_only_json_is_machine_readable(self) -> None:
        checks = [replace(check, status="ok") for check in
                  _check_list(tools_status="ok", web_status="ok", browser_status="ok", atlas_status="ok")]
        buffer = io.StringIO()

        with mock.patch.object(setup_tool, "collect_checks", return_value=checks), redirect_stdout(buffer):
            code = setup_main(["--check-only", "--json"])

        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["check_only"])
        self.assertTrue(payload["ok"])
        self.assertEqual([item["key"] for item in payload["checks"]], [check.key for check in checks])

    def test_install_failure_is_reported_without_raising(self) -> None:
        first = _check_list(tools_status="missing", web_status="ok", browser_status="ok", atlas_status="ok")
        healthy = [replace(check, status="ok") for check in first]

        with mock.patch.object(setup_tool, "collect_checks", side_effect=[first, healthy]), \
             mock.patch.object(setup_tool, "install_node_deps", side_effect=RuntimeError("npm 不可用")), \
             mock.patch.object(setup_tool, "register_command", side_effect=lambda *a, **k: {
                 "command_path": "/bin/atlas", "bin_dir": "/bin", "path_added": False,
                 "path_updated": False, "path_hint": None}), \
             redirect_stdout(io.StringIO()):
            code = setup_main([])

        self.assertEqual(code, 1, "依赖安装失败应返回非零")

    def test_skip_flags_leave_dependencies_untouched(self) -> None:
        first = _check_list(tools_status="missing", web_status="optional", browser_status="missing",
                            atlas_status="missing")
        healthy = [replace(check, status="ok") for check in first]
        calls: list[str] = []

        with mock.patch.object(setup_tool, "collect_checks", side_effect=[first, healthy]), \
             mock.patch.object(setup_tool, "install_node_deps", side_effect=AssertionError("不应安装依赖")), \
             mock.patch.object(setup_tool, "install_browser", side_effect=AssertionError("不应安装浏览器")), \
             mock.patch.object(setup_tool, "register_command",
                               side_effect=lambda *a, **k: calls.append("register") or {
                                   "command_path": "/bin/atlas", "bin_dir": "/bin", "path_added": False,
                                   "path_updated": False, "path_hint": None}), \
             redirect_stdout(io.StringIO()):
            code = setup_main(["--skip-node-deps", "--skip-browser"])

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["register"])


if __name__ == "__main__":
    unittest.main()
