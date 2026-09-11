"""子进程启动参数helper：Windows 下隐藏控制台窗口。

网页面板由 detached 进程运行，每次启动 Python 执行器、Codex、npm 或 node 子进程时，
Windows 都会为它们分配一个新的控制台窗口（用户看到一堆 python.exe 黑窗）。
所有后台调用统一带上 ``CREATE_NO_WINDOW`` 即可安静运行；其他平台返回空参数。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_window_kwargs() -> dict[str, Any]:
    """Return the creation flags that suppress the Windows console window."""

    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
