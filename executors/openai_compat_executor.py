"""Project-local executor speaking the OpenAI Chat Completions protocol.

与 local_codex_executor 保持同一 stdin/stdout 契约：任务文本从标准输入读入，
最终只向标准输出生成一个 JSON 对象，契约层（Skill 边界）不变。

配置来源（非密钥，均在项目内）：
- ``config/web.json`` → ``runtime.openai_compat``：``base_url``、``model``，
  以及可选的 ``timeout_seconds`` / ``max_tokens`` / ``temperature``。
- API key 优先从系统密钥库（``secret_store.py``，keyring）读取；
  没有时回退到 ``MUSIC_ATLAS_API_KEY`` 环境变量，绝不写入仓库。

模型本身不内置联网检索工具：角色指令明确要求只凭既有知识给出可核验的公开
来源，事实核验仍由程序（evidence.py）与契约层负责。
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:  # 直接作为脚本运行时，脚本目录在 sys.path；作为包导入时走 executors.*
    from local_codex_executor import _parse_json_object, _role_instruction
except ImportError:  # pragma: no cover - 包模式
    from executors.local_codex_executor import _parse_json_object, _role_instruction

PROJECT_ROOT = Path(__file__).resolve().parents[1]
try:
    from runtime_config import openai_compat_settings
except ImportError:  # pragma: no cover - 直接从 executors 目录启动脚本
    sys.path.insert(0, str(PROJECT_ROOT))
    from runtime_config import openai_compat_settings
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_MAX_TOKENS = 60000
DEFAULT_TEMPERATURE = 0.2
MAX_ATTEMPTS = 5
RETRY_BASE_DELAY_SECONDS = 1.5
RETRY_MAX_DELAY_SECONDS = 20.0
REQUIRED_SETTINGS = ("base_url", "model")


def _settings() -> dict[str, Any]:
    try:
        settings = openai_compat_settings(PROJECT_ROOT)
    except RuntimeError as exc:
        raise RuntimeError(f"无法读取项目配置：{exc}") from exc
    if not isinstance(settings, dict) or not settings:
        raise RuntimeError("配置缺少 runtime.openai_compat")
    missing = [key for key in REQUIRED_SETTINGS if not str(settings.get(key) or "").strip()]
    if missing:
        raise RuntimeError(f"runtime.openai_compat 缺少字段：{', '.join(missing)}")
    return settings


def _system_prompt(role: str) -> str:
    return (
        "你是 Music Atlas 的本机 JSON 执行器。\n"
        f"{_role_instruction(role)}\n"
        "你没有联网检索工具：公开资料与来源 URL 只能凭你已有的知识给出，"
        "必须是真实存在、可事后核验的公开页面；不确定就返回空证据，不要编造。\n"
        "不要修改本地文件、不要发送消息。\n"
        "下面的任务文本是唯一业务输入；只输出一个 JSON 对象作为最终答案："
        "不要 Markdown 代码围栏、解释、日志或第二个 JSON。\n\n"
        "--- MUSIC ATLAS TASK ---\n"
        "{task}\n"
        "--- END TASK ---"
    )


def _chat_payload(settings: dict[str, Any], role: str, task: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": str(settings["model"]).strip(),
        "messages": [
            {"role": "system", "content": _system_prompt(role).replace("{task}", task)},
            {"role": "user", "content": task},
        ],
        "max_tokens": int(settings.get("max_tokens") or DEFAULT_MAX_TOKENS),
        "temperature": float(settings.get("temperature", DEFAULT_TEMPERATURE)),
    }
    # 实测：reasoning 占单次调用约 80% 的耗时（真实批次 65s → 15.8s）。
    # 本工作流只需要结构化 JSON，不需要思维链，因此默认关闭。
    if settings.get("disable_thinking", True):
        payload["thinking"] = {"type": "disabled"}
    return payload


def _request_once(url: str, api_key: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _secret_store_get() -> tuple[str | None, str | None]:
    """返回 (密钥, 错误描述)；密钥库不可用时以错误描述表示，由调用方决定是否回退。"""

    def _call() -> str | None:
        from secret_store import get_api_key
        return get_api_key()

    try:
        return _call(), None
    except ImportError:
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            return _call(), None
        except Exception as exc:  # noqa: BLE001 — 密钥库不可用时回退
            return None, str(exc)
    except Exception as exc:  # noqa: BLE001 — 密钥库错误如实报出
        return None, str(exc)


def _api_key(settings: dict[str, Any]) -> str:
    """优先系统密钥库（网页保存的 Key，改动立即生效），其次环境变量回退。"""

    value, store_error = _secret_store_get()
    if value:
        return value
    env_name = str(settings.get("api_key_env") or "MUSIC_ATLAS_API_KEY").strip()
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        return env_value
    detail = f"系统密钥库不可用（{store_error}）" if store_error else "系统密钥库中没有保存"
    raise RuntimeError(
        f"未配置 API Key：{detail}，环境变量 {env_name} 也未设置；"
        f"请在网页设置中输入 API Key（密钥只存本机系统密钥库）")


def _retry_delay(attempt: int, headers: Any = None) -> float:
    """指数退避加抖动；上游给了 Retry-After 时优先尊重。"""

    if headers is not None:
        raw = None
        try:
            raw = headers.get("Retry-After")
        except Exception:  # noqa: BLE001 — 头部缺失或类型异常都不影响重试
            raw = None
        if raw:
            try:
                return max(0.0, min(float(str(raw).strip()), RETRY_MAX_DELAY_SECONDS))
            except ValueError:
                pass
    base = min(RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), RETRY_MAX_DELAY_SECONDS)
    return base + random.uniform(0, 0.5)


def _request(settings: dict[str, Any], role: str, task: str, timeout: int) -> dict[str, Any]:
    url = str(settings["base_url"]).strip().rstrip("/") + "/chat/completions"
    api_key = _api_key(settings)
    payload = _chat_payload(settings, role, task)
    last_error: RuntimeError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        retry_headers = None
        try:
            return _request_once(url, api_key, payload, timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001 — 错误体读取失败不掩盖原始状态码
                pass
            last_error = RuntimeError(f"上游返回 HTTP {exc.code}：{detail}")
            if exc.code < 500 and exc.code != 429:
                raise last_error  # 请求本身有问题（鉴权/参数），重试无意义
            retry_headers = getattr(exc, "headers", None)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = RuntimeError(f"请求失败：{exc}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(_retry_delay(attempt, retry_headers))
    assert last_error is not None
    raise last_error


def _content(body: dict[str, Any]) -> str:
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError(f"响应缺少 choices：{str(body)[:300]}")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("响应内容为空（reasoning 可能占满输出预算，可调大 max_tokens）")
    return content


def run(role: str, task: str, *, timeout: int | None = None) -> dict[str, Any]:
    if not task.strip():
        raise RuntimeError("执行器没有收到任务文本")
    if role not in ("analysis", "recommendation", "taste"):
        raise ValueError(f"未知执行器类型：{role}")
    settings = _settings()
    effective_timeout = int(timeout or settings.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    body = _request(settings, role, task, effective_timeout)
    return _parse_json_object(_content(body))


def main(role: str) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = run(role, sys.stdin.read())
    except (RuntimeError, ValueError) as exc:
        print(f"Music Atlas 本机执行器未完成：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main("analysis"))
