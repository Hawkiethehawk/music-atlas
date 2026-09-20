"""跨平台保存 Music Atlas 的 AI API Key。

通过 ``keyring`` 调用操作系统密钥库：Windows Credential Manager、macOS
Keychain、Linux Secret Service。不会接受 keyrings.alt 或明文文件后端。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

SERVICE_NAME = "music-atlas"
ACCOUNT_NAME = "ai_api_key"


class SecretStoreError(RuntimeError):
    """密钥库不可用或操作失败。"""


def _load_keyring() -> tuple[Any, Any]:
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - 取决于本机安装
        raise SecretStoreError("未安装 keyring；请运行 `python atlas.py setup` 安装安全密钥库依赖") from exc
    try:
        backend = keyring.get_keyring()
    except Exception as exc:  # noqa: BLE001 - 后端探测由第三方库完成
        raise SecretStoreError(f"无法初始化系统密钥库：{exc}") from exc
    _ensure_secure_backend(backend)
    return keyring, backend


def _backend_label(backend: Any) -> str:
    cls = type(backend)
    return f"{cls.__module__}.{cls.__name__}"


def _backend_parts(backend: Any) -> list[Any]:
    parts = [backend]
    nested = getattr(backend, "backends", None)
    if isinstance(nested, (list, tuple)):
        for item in nested:
            parts.extend(_backend_parts(item))
    return parts


def _ensure_secure_backend(backend: Any) -> None:
    parts = _backend_parts(backend)
    labels = [_backend_label(item).lower() for item in parts]
    insecure_markers = ("keyrings.alt", "plaintext", "null", ".fail.")
    if any(any(marker in label for marker in insecure_markers) for label in labels):
        raise SecretStoreError(
            "当前 keyring 后端不是安全系统密钥库；请配置 Windows Credential Manager、"
            "macOS Keychain、GNOME Keyring、KWallet 或 Secret Service"
        )
    secure_markers = ("secretservice", "windows", "macos", "kwallet", "keychain", "credential")
    if not any(any(marker in label for marker in secure_markers) for label in labels):
        raise SecretStoreError(f"未识别的安全 keyring 后端：{_backend_label(backend)}")


def backend_name() -> str:
    _keyring, backend = _load_keyring()
    return _backend_label(backend)


def get_api_key() -> str | None:
    keyring, _backend = _load_keyring()
    try:
        value = keyring.get_password(SERVICE_NAME, ACCOUNT_NAME)
    except Exception as exc:  # noqa: BLE001 - 系统密钥库错误
        raise SecretStoreError(f"读取系统密钥库失败：{exc}") from exc
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SecretStoreError("系统密钥库中的 API Key 无效")
    return value


def set_api_key(value: str) -> None:
    if not isinstance(value, str):
        raise SecretStoreError("API Key 必须是文本")
    value = value.rstrip("\r\n")
    if not value.strip() or len(value) > 10000 or "\x00" in value:
        raise SecretStoreError("API Key 不能为空、不能包含 NUL，且长度不能超过 10000 个字符")
    keyring, _backend = _load_keyring()
    try:
        keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, value)
    except Exception as exc:  # noqa: BLE001 - 系统密钥库错误
        raise SecretStoreError(f"写入系统密钥库失败：{exc}") from exc


def delete_api_key() -> bool:
    keyring, _backend = _load_keyring()
    try:
        existing = keyring.get_password(SERVICE_NAME, ACCOUNT_NAME)
        if existing is None:
            return False
        keyring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
        return True
    except Exception as exc:  # noqa: BLE001 - 系统密钥库错误
        raise SecretStoreError(f"删除系统密钥库中的 API Key 失败：{exc}") from exc


def status() -> dict[str, Any]:
    backend = backend_name()
    configured = get_api_key() is not None
    return {"configured": configured, "backend": backend}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Music Atlas 跨平台系统密钥库")
    parser.add_argument("action", choices=("get", "set", "delete", "status"))
    args = parser.parse_args(argv)
    try:
        if args.action == "get":
            value = get_api_key()
            if value is None:
                return 1
            sys.stdout.write(value)
            return 0
        if args.action == "set":
            set_api_key(sys.stdin.read())
            print(json.dumps({"ok": True, **status()}, ensure_ascii=False))
            return 0
        if args.action == "delete":
            deleted = delete_api_key()
            print(json.dumps({"ok": True, "deleted": deleted, **status()}, ensure_ascii=False))
            return 0
        print(json.dumps({"ok": True, **status()}, ensure_ascii=False))
        return 0
    except SecretStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
