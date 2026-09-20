"""外部 Agent 调用的瞬时故障重试。

Step 2 分析批次与 Step 3 候选研究 worker 共用同一套判断：
只有上游 5xx/429、连接中断、超时等**瞬时外部故障**才重试；
契约、内容、鉴权等问题属于确定性问题，重试没有意义。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

AGENT_RETRY_ATTEMPTS = 1
AGENT_RETRY_DELAY_SECONDS = 2.0
_RETRYABLE_HTTP_STATUS = re.compile(r"HTTP (?:429|5\d\d)")
_RETRYABLE_MARKERS = (
    "service temporarily unavailable",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "connection refused",
    "timed out",
    "timeout",
    "远程主机强迫关闭",
    "连接中断",
)


def is_retryable_agent_error(message: str) -> bool:
    """仅瞬时外部故障值得重试。"""

    text = str(message or "")
    if _RETRYABLE_HTTP_STATUS.search(text):
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _RETRYABLE_MARKERS)


def call_agent_with_retry(
    execute: Callable[..., Any],
    command: str,
    prompt_text: str,
    *,
    timeout: int,
    attempts: int = AGENT_RETRY_ATTEMPTS,
    on_retry: Callable[[int, str], None] | None = None,
) -> Any:
    """调用一次外部 Agent；瞬时故障时额外重试 `attempts` 次。"""

    total = max(1, int(attempts) + 1)
    for attempt in range(1, total + 1):
        try:
            return execute(command, prompt_text, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — 是否重试由错误文本决定
            if attempt >= total or not is_retryable_agent_error(str(exc)):
                raise
            if on_retry is not None:
                on_retry(attempt, str(exc))
            time.sleep(AGENT_RETRY_DELAY_SECONDS)
    raise RuntimeError("Agent 重试未产生结果")  # pragma: no cover - 循环内必有返回或抛出
