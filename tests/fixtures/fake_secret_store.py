#!/usr/bin/env python3
"""网页测试用的假密钥库，行为与 secret_store.py 的 CLI 一致。

只把 API Key 写入 ATLAS_SECRET_STATE_FILE 指定的临时文件，
不触碰真实系统密钥库（Windows Credential Manager / Keychain / Secret Service）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _state_path() -> Path:
    configured = os.environ.get("ATLAS_SECRET_STATE_FILE", "").strip()
    if not configured:
        raise SystemExit("ATLAS_SECRET_STATE_FILE 未设置")
    return Path(configured)


def _load() -> dict[str, str]:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(value: dict[str, str]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _report(value: dict[str, str]) -> None:
    print(json.dumps(
        {"ok": True, "configured": bool(value.get("key")), "backend": "fake-file"},
        ensure_ascii=False,
    ))


def main(argv: list[str]) -> int:
    action = argv[0] if argv else "status"
    value = _load()
    if action == "get":
        if not value.get("key"):
            return 1
        sys.stdout.write(value["key"])
        return 0
    if action == "set":
        supplied = sys.stdin.read().rstrip("\r\n")
        if not supplied:
            print("API Key 不能为空", file=sys.stderr)
            return 2
        value["key"] = supplied
        _save(value)
        _report(value)
        return 0
    if action == "delete":
        existed = bool(value.get("key"))
        value.pop("key", None)
        _save(value)
        print(json.dumps(
            {"ok": True, "deleted": existed, "configured": False, "backend": "fake-file"},
            ensure_ascii=False,
        ))
        return 0
    _report(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
