"""读取 Music Atlas 的项目配置与本机网页设置覆盖。

基础配置仍来自 ``config/web.json``；网页设置保存到 Git 忽略的运行时覆盖文件，
避免网页表单改写仓库内配置。子进程通过同样的环境变量读取这份覆盖，因此模型
配置和读取器配置会在下一次任务启动时生效。
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def config_path(project_root: Path | None = None) -> Path:
    root = (project_root or PROJECT_ROOT).resolve()
    configured = os.environ.get("ATLAS_WEB_CONFIG", "").strip()
    return Path(configured).expanduser().resolve() if configured else root / "config" / "web.json"


def settings_path(project_root: Path | None = None) -> Path:
    root = (project_root or PROJECT_ROOT).resolve()
    configured = os.environ.get("ATLAS_WEB_SETTINGS", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    # 测试用临时 ATLAS_WEB_CONFIG 时，把覆盖文件放在同一临时目录，避免污染真实 runtime。
    if os.environ.get("ATLAS_WEB_CONFIG", "").strip():
        return config_path(root).with_name("settings.json")
    return root / "runtime" / "web" / "settings.json"


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取配置 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"配置必须是 JSON 对象：{path}")
    return value


def load_web_config(project_root: Path | None = None) -> dict[str, Any]:
    base_path = config_path(project_root)
    base = _read_object(base_path)
    override_path = settings_path(project_root)
    if not override_path.is_file():
        return base
    return _deep_merge(base, _read_object(override_path))


def crawler_settings(project_root: Path | None = None) -> dict[str, Any]:
    config = load_web_config(project_root)
    crawler = config.get("crawler")
    return crawler if isinstance(crawler, dict) else {}


def openai_compat_settings(project_root: Path | None = None) -> dict[str, Any]:
    config = load_web_config(project_root)
    runtime = config.get("runtime")
    settings = runtime.get("openai_compat") if isinstance(runtime, dict) else None
    return settings if isinstance(settings, dict) else {}
