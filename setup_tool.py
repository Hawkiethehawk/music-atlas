"""Music Atlas 环境自检、依赖安装与 `atlas` 命令注册。

`install.ps1` / `install.sh` 只负责找到 Python 并调用这里的 `setup`，
真正的检查与安装逻辑集中在本模块，便于单独执行与测试：

    python atlas.py setup              # 检查 + 安装缺失依赖 + 注册 atlas
    python atlas.py setup --check-only # 只检查，不改动环境
    python atlas.py setup --json       # 机器可读输出

注册策略（幂等、可回退、不污染系统范围）：
- 生成一个只调用本仓库 `atlas.py` 的包装脚本，放到用户级 bin 目录；
- Windows 把该目录追加到用户 PATH（HKCU\\Environment），并广播设置变更；
- Linux/macOS 写 `~/.local/bin/atlas`，目录不在 PATH 时只提示，不改 shell 配置。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
TOOLS_DIR = ROOT / "tools"
CONFIG_PATH = ROOT / "config" / "web.json"

MIN_PYTHON = (3, 10)
MIN_NODE_MAJOR = 18
WINDOWS_BIN_DIR_NAME = Path("MusicAtlas") / "bin"
ATLAS_MARKER = "music-atlas-cli"

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_OPTIONAL = "optional"
STATUS_WARN = "warn"

REQUIRED_KEYS = ("python", "node", "npm", "tools_deps", "config", "keyring")


@dataclass
class Check:
    """一条环境检查结果。``fix`` 为空表示不需要修复动作。"""

    key: str
    label: str
    status: str
    detail: str
    fix: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


def _hidden_window_kwargs() -> dict[str, object]:
    """Windows 下隐藏 npm/npx 子进程的控制台窗口。"""

    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 900,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        **_hidden_window_kwargs(),
    )


def _which(name: str) -> str | None:
    return shutil.which(name)


def _version_tuple(text: str) -> tuple[int, ...]:
    digits = re.findall(r"\d+", text or "")
    return tuple(int(part) for part in digits[:3]) or (0,)


def _node_major(version_text: str) -> int:
    parsed = _version_tuple(version_text)
    return parsed[0] if parsed else 0


def windows_bin_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / WINDOWS_BIN_DIR_NAME


def posix_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def default_bin_dir() -> Path:
    return windows_bin_dir() if os.name == "nt" else posix_bin_dir()


def wrapper_path(bin_dir: Path) -> Path:
    return bin_dir / ("atlas.cmd" if os.name == "nt" else "atlas")


def wrapper_content(interpreter: str, atlas_py: Path) -> str:
    """生成只调用本仓库 CLI 的包装脚本内容。"""

    if os.name == "nt":
        return (
            "@echo off\r\n"
            f"rem {ATLAS_MARKER}\r\n"
            f'"{interpreter}" "{atlas_py}" %*\r\n'
        )
    return (
        "#!/bin/sh\n"
        f"# {ATLAS_MARKER}\n"
        f'exec "{interpreter}" "{atlas_py}" "$@"\n'
    )


def append_path_entry(current: str, entry: str) -> tuple[str, bool]:
    """把 ``entry`` 追加到 Windows PATH 值；已存在则原样返回。"""

    target = os.path.normpath(str(entry)).casefold()
    parts = [part.strip() for part in str(current or "").split(os.pathsep) if part.strip()]
    for part in parts:
        if os.path.normpath(part).casefold() == target:
            return current, False
    parts.append(str(entry))
    return os.pathsep.join(parts), True


def path_contains_dir(entry: Path, path_value: str | None = None) -> bool:
    raw = os.environ.get("PATH", "") if path_value is None else path_value
    target = os.path.normpath(str(entry)).casefold()
    for part in raw.split(os.pathsep):
        if part and os.path.normpath(part).casefold() == target:
            return True
    return False


def _read_user_path() -> str:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
        try:
            value, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return ""
    return str(value or "")


def _write_user_path(value: str) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)


def _broadcast_environment_change() -> None:
    """通知已运行的进程读取新的用户环境变量（失败不影响注册结果）。"""

    if os.name != "nt":
        return
    try:
        import ctypes

        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, ctypes.c_wchar_p("Environment"), 0x0002, 5000, ctypes.byref(result)
        )
    except Exception:  # noqa: BLE001 — 广播失败只影响即时可见性
        return


def ensure_windows_path(bin_dir: Path, *, reader=None, writer=None, broadcast=None) -> bool:
    """把 ``bin_dir`` 追加到用户 PATH；返回是否真的写入了。

    读写函数可注入，便于在不改真实环境变量的前提下测试幂等行为。
    """

    read = reader or _read_user_path
    write = writer or _write_user_path
    notify = broadcast or _broadcast_environment_change
    updated, changed = append_path_entry(read(), str(bin_dir))
    if changed:
        write(updated)
        notify()
    return changed


def register_command(bin_dir: Path | None = None) -> dict[str, object]:
    """写入包装脚本并确保其目录在 PATH 中；重复执行不产生重复条目。"""

    target_dir = Path(bin_dir) if bin_dir else default_bin_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    script = wrapper_path(target_dir)
    script.write_text(wrapper_content(sys.executable, ROOT / "atlas.py"), encoding="utf-8")
    if os.name != "nt":
        script.chmod(0o755)

    result: dict[str, object] = {
        "command_path": str(script),
        "bin_dir": str(target_dir),
        "path_added": False,
        "path_updated": False,
        "path_hint": None,
    }
    if os.name == "nt":
        result["path_added"] = ensure_windows_path(target_dir)
        result["path_updated"] = path_contains_dir(target_dir, _read_user_path())
    elif not path_contains_dir(target_dir):
        result["path_hint"] = (
            f"{target_dir} 不在 PATH 中；请把下面一行加入 shell 配置后重开终端：\n"
            f'  export PATH="{target_dir}:$PATH"'
        )
    return result


def _playwright_cache_dirs() -> list[Path]:
    candidates: list[Path] = []
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            candidates.append(Path(base) / "ms-playwright")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    else:
        candidates.append(Path.home() / ".cache" / "ms-playwright")
    return candidates


def browser_installed() -> bool:
    for directory in _playwright_cache_dirs():
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            if child.name.startswith(("chromium", "chromium_headless_shell")):
                return True
    return False


def check_python() -> Check:
    current = sys.version_info[:3]
    detail = f"Python {current[0]}.{current[1]}.{current[2]}（{sys.executable}）"
    if current[:2] >= MIN_PYTHON:
        return Check("python", "Python 运行时", STATUS_OK, detail)
    need = ".".join(str(part) for part in MIN_PYTHON)
    return Check("python", "Python 运行时", STATUS_MISSING, detail, f"需要 Python >= {need}")


def check_node() -> Check:
    executable = _which("node")
    if not executable:
        return Check("node", "Node.js", STATUS_MISSING, "未找到 node",
                     "安装 Node.js >= 18 后重新运行 setup（网页面板与 Apple 导出依赖它）")
    try:
        completed = _run([executable, "--version"], timeout=30)
        version = (completed.stdout or completed.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("node", "Node.js", STATUS_MISSING, f"无法执行 node：{exc}", "重新安装 Node.js")
    if _node_major(version) >= MIN_NODE_MAJOR:
        return Check("node", "Node.js", STATUS_OK, f"Node.js {version}（{executable}）")
    return Check("node", "Node.js", STATUS_MISSING, f"Node.js {version}",
                 f"需要 Node.js >= {MIN_NODE_MAJOR}")


def check_npm() -> Check:
    executable = _which("npm")
    if not executable:
        return Check("npm", "npm", STATUS_MISSING, "未找到 npm", "随 Node.js 一起安装 npm")
    try:
        completed = _run([executable, "--version"], timeout=60)
        version = (completed.stdout or completed.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("npm", "npm", STATUS_MISSING, f"无法执行 npm：{exc}", "重新安装 npm")
    return Check("npm", "npm", STATUS_OK, f"npm {version}")


def _deps_state(directory: Path, marker: Path) -> tuple[bool, str]:
    modules = directory / "node_modules"
    if not modules.is_dir() or not marker.exists():
        return False, f"{directory.name}/node_modules 未安装或不完整"
    return True, f"{directory.name}/node_modules 已安装"


def check_web_deps() -> Check:
    installed, detail = _deps_state(WEB_DIR, WEB_DIR / "node_modules" / "playwright")
    if installed:
        return Check("web_deps", "网页测试依赖", STATUS_OK, detail)
    return Check("web_deps", "网页测试依赖", STATUS_OPTIONAL, detail,
                 f"在 {WEB_DIR} 执行 npm ci（仅浏览器回归测试需要，生产运行 server.js 不需要）")


def check_tools_deps() -> Check:
    installed, detail = _deps_state(TOOLS_DIR, TOOLS_DIR / "node_modules" / "csv-parse")
    if installed:
        return Check("tools_deps", "Apple 导出依赖", STATUS_OK, detail)
    return Check("tools_deps", "Apple 导出依赖", STATUS_MISSING, detail,
                 f"在 {TOOLS_DIR} 执行 npm ci（Apple Music 歌单导出需要）")


def check_browser() -> Check:
    if browser_installed():
        return Check("browser", "Playwright Chromium", STATUS_OK, "已安装 Chromium")
    return Check("browser", "Playwright Chromium", STATUS_MISSING, "未找到 Playwright 浏览器",
                 f"在 {TOOLS_DIR} 执行 npx playwright install chromium --only-shell")


def check_config() -> Check:
    if not CONFIG_PATH.is_file():
        return Check("config", "网页配置", STATUS_MISSING, f"缺少 {CONFIG_PATH}",
                     "恢复被删除的 config/web.json")
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Check("config", "网页配置", STATUS_MISSING, f"无法解析：{exc}", "修复 config/web.json")
    if not isinstance(payload, dict):
        return Check("config", "网页配置", STATUS_MISSING, "config/web.json 必须是 JSON 对象",
                     "修复 config/web.json")
    port = (payload.get("server") or {}).get("port")
    return Check("config", "网页配置", STATUS_OK, f"config/web.json（端口 {port}）")


def check_modules() -> Check:
    missing: list[str] = []
    for name in ("workflow", "web_workflow", "web_view_model", "setup_tool"):
        try:
            __import__(name)
        except Exception as exc:  # noqa: BLE001 — 自检需要完整失败原因
            missing.append(f"{name}: {exc}")
    if missing:
        return Check("modules", "Python 模块自检", STATUS_MISSING, "；".join(missing), "检查仓库文件完整性")
    return Check("modules", "Python 模块自检", STATUS_OK, "核心模块可导入")


def check_keyring() -> Check:
    try:
        import keyring  # noqa: F401
    except ImportError:
        return Check("keyring", "系统密钥库", STATUS_MISSING, "未安装 keyring 包",
                     "运行 python -m pip install keyring")
    try:
        from secret_store import backend_name
        detail = f"keyring 可用（{backend_name()}）"
    except Exception as exc:  # noqa: BLE001 — 密钥库自检失败需如实报告
        return Check("keyring", "系统密钥库", STATUS_MISSING, f"keyring 不可用：{exc}",
                     "配置 Windows Credential Manager / macOS Keychain / Linux Secret Service")
    return Check("keyring", "系统密钥库", STATUS_OK, detail)


def check_atlas_command(bin_dir: Path | None = None) -> Check:
    target_dir = Path(bin_dir) if bin_dir else default_bin_dir()
    script = wrapper_path(target_dir)
    if not script.is_file():
        return Check("atlas", "atlas 命令", STATUS_MISSING, f"未找到 {script}", "运行 atlas setup 注册命令")
    if not path_contains_dir(target_dir):
        return Check("atlas", "atlas 命令", STATUS_WARN, f"已生成 {script}，但目录不在当前 PATH",
                     f"重开终端，或把 {target_dir} 加入 PATH")
    return Check("atlas", "atlas 命令", STATUS_OK, f"{script} 已在 PATH 中")


def collect_checks() -> list[Check]:
    return [
        check_python(),
        check_node(),
        check_npm(),
        check_tools_deps(),
        check_web_deps(),
        check_browser(),
        check_config(),
        check_modules(),
        check_keyring(),
        check_atlas_command(),
    ]


def _print_checks(checks: list[Check]) -> None:
    icons = {STATUS_OK: "✅", STATUS_MISSING: "❌", STATUS_OPTIONAL: "◻", STATUS_WARN: "⚠"}
    for check in checks:
        print(f"{icons.get(check.status, '·')} {check.label}：{check.detail}")
        if check.fix and check.status != STATUS_OK:
            print(f"    → {check.fix}")


def install_node_deps(directory: Path) -> str:
    npm = _which("npm")
    if not npm:
        raise RuntimeError("未找到 npm，无法安装 Node 依赖")
    completed = _run([npm, "ci", "--no-audit", "--no-fund"], cwd=directory)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")[:400]
        raise RuntimeError(f"npm ci 失败（{directory.name}）：{detail}")
    return f"{directory.name}/node_modules 已安装"


def install_browser() -> str:
    npx = _which("npx")
    if not npx:
        raise RuntimeError("未找到 npx，无法安装 Playwright Chromium")
    command = [npx, "--yes", "playwright", "install", "chromium", "--only-shell"]
    if os.name == "nt":
        completed = _run(["cmd", "/c", *command], cwd=TOOLS_DIR)
    else:
        completed = _run(command, cwd=TOOLS_DIR)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")[:400]
        raise RuntimeError(f"Playwright Chromium 安装失败：{detail}")
    return "Playwright Chromium 已安装"


def install_keyring() -> str:
    completed = _run([sys.executable, "-m", "pip", "install", "--quiet", "keyring"])
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")[:400]
        raise RuntimeError(f"keyring 安装失败：{detail}")
    return "keyring 已安装（跨平台系统密钥库）"


def _needs_install(checks: list[Check], key: str) -> bool:
    return any(check.key == key and not check.ok for check in checks)


def setup_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas setup", description="检查依赖、安装依赖并注册 atlas 命令")
    parser.add_argument("--check-only", action="store_true", help="只检查环境，不做任何安装或注册")
    parser.add_argument("--skip-node-deps", action="store_true", help="跳过 npm ci")
    parser.add_argument("--skip-browser", action="store_true", help="跳过 Playwright Chromium 安装")
    parser.add_argument("--skip-register", action="store_true", help="跳过 atlas 命令注册")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    report: dict[str, object] = {"check_only": args.check_only, "installed": [], "skipped": [], "errors": []}
    checks = collect_checks()
    if not args.json:
        print("=== Music Atlas 环境检查 ===")
        _print_checks(checks)

    required_failed = [check for check in checks if check.key in REQUIRED_KEYS and not check.ok]
    if args.check_only:
        report["checks"] = [check.as_dict() for check in checks]
        report["ok"] = not required_failed
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            print("\n检查完成：依赖缺失，未做任何修改。" if required_failed else "\n检查完成：必需依赖齐备。")
        return 0 if not required_failed else 1

    errors: list[str] = []
    for key, label, action, skip in (
        ("tools_deps", "Apple 导出依赖", lambda: install_node_deps(TOOLS_DIR), args.skip_node_deps),
        ("web_deps", "网页测试依赖", lambda: install_node_deps(WEB_DIR), args.skip_node_deps),
        ("browser", "Playwright Chromium", install_browser, args.skip_browser),
        ("keyring", "系统密钥库依赖", install_keyring, False),
    ):
        if not _needs_install(checks, key):
            continue
        if skip:
            report["skipped"].append(label)
            continue
        print(f"\n=== 安装 {label} ===")
        try:
            message = action()
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
            print(f"❌ {exc}")
        else:
            report["installed"].append(label)
            print(f"✅ {message}")

    if not args.skip_register:
        print("\n=== 注册 atlas 命令 ===")
        try:
            registration = register_command()
        except (OSError, ImportError) as exc:
            errors.append(f"注册 atlas 命令失败：{exc}")
            print(f"❌ 注册 atlas 命令失败：{exc}")
        else:
            report["registration"] = registration
            print(f"✅ 已写入 {registration['command_path']}")
            if registration.get("path_added"):
                print("   已把该目录加入用户 PATH；新开的终端即可直接使用 atlas。")
            if registration.get("path_hint"):
                print(f"⚠ {registration['path_hint']}")
    else:
        report["skipped"].append("atlas 命令注册")

    checks = collect_checks()
    if not args.json:
        print("\n=== 安装后复检 ===")
        _print_checks(checks)
    report["checks"] = [check.as_dict() for check in checks]
    remaining = [check for check in checks if check.key in REQUIRED_KEYS and not check.ok]
    report["errors"] = errors
    report["ok"] = not remaining and not errors

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    elif report["ok"]:
        print("\n✅ setup 完成：依赖齐备，atlas 命令可用。")
    else:
        blocked = "；".join(check.label for check in remaining) or "无"
        print(f"\n❌ setup 未完成；仍缺：{blocked}")
        for message in errors:
            print(f"   · {message}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(setup_main())
