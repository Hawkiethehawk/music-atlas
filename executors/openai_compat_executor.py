"""Project-local executor speaking the OpenAI Chat Completions protocol.

与 local_codex_executor 保持同一 stdin/stdout 契约：任务文本从标准输入读入，
最终只向标准输出生成一个 JSON 对象，契约层（Skill 边界）不变。

配置来源（非密钥，均在项目内）：
- ``config/web.json`` → ``runtime.openai_compat``：``base_url``、``model``、
  ``api_key_env``，以及可选的 ``timeout_seconds`` / ``max_tokens`` / ``temperature``。
- API key 只从 ``api_key_env`` 指定的环境变量读取，绝不写入仓库。

模型本身不内置联网检索工具：角色指令明确要求只凭既有知识给出可核验的公开
来源，事实核验仍由程序（evidence.py）与契约层负责。
"""

from __future__ import annotations

import json
import os
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
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_MAX_TOKENS = 60000
DEFAULT_TEMPERATURE = 0.2
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 3.0
REQUIRED_SETTINGS = ("base_url", "model", "api_key_env")


def _settings() -> dict[str, Any]:
    try:
        config = json.loads((PROJECT_ROOT / "config" / "web.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取项目配置：{exc}") from exc
    runtime = config.get("runtime") if isinstance(config, dict) else None
    settings = runtime.get("openai_compat") if isinstance(runtime, dict) else None
    if not isinstance(settings, dict):
        raise RuntimeError("config/web.json 缺少 runtime.openai_compat 配置")
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
    return {
        "model": str(settings["model"]).strip(),
        "messages": [
            {"role": "system", "content": _system_prompt(role).replace("{task}", task)},
            {"role": "user", "content": task},
        ],
        "max_tokens": int(settings.get("max_tokens") or DEFAULT_MAX_TOKENS),
        "temperature": float(settings.get("temperature", DEFAULT_TEMPERATURE)),
    }


def _request_once(url: str, api_key: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _request(settings: dict[str, Any], role: str, task: str, timeout: int) -> dict[str, Any]:
    url = str(settings["base_url"]).strip().rstrip("/") + "/chat/completions"
    api_key = os.environ.get(str(settings["api_key_env"]).strip(), "")
    if not api_key:
        raise RuntimeError(
            f"环境变量 {settings['api_key_env']} 未设置；无法调用 {settings['model']}（密钥只存本机）")
    payload = _chat_payload(settings, role, task)
    last_error: RuntimeError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
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
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = RuntimeError(f"请求失败：{exc}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_BASE_DELAY_SECONDS * attempt)
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
