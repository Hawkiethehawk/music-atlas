#!/usr/bin/env python3
"""Music Atlas 统一 CLI 入口。

环境安装（本文件转发 setup_tool.py）：
  atlas setup [--check-only] [--json]        检查依赖、安装依赖并注册 atlas 命令

面板服务管理（本文件实现，防护模型见 web_service.py）：
  atlas start [--port N] [--no-open] [--force]   启动网页面板（已运行则复用）
  atlas stop                                     停止网页面板
  atlas restart                                  重启网页面板
  atlas status [--json]                          面板运行状态
  atlas logs [--tail N]                          查看面板日志尾部

其余子命令原样透传给 workflow.py（run/analyze/skill/validate/web-export/...）：
  atlas run --input tests/fixtures/playlist_sample.json --reader local_json ...
"""

from __future__ import annotations

import sys

SERVICE_ACTIONS = ("start", "stop", "restart", "status", "logs")
SETUP_ACTIONS = ("setup",)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if argv and argv[0] in SETUP_ACTIONS:
        from setup_tool import setup_main
        return setup_main(argv[1:])
    if argv and argv[0] in SERVICE_ACTIONS:
        from web_service import service_main
        return service_main(argv)
    from workflow import main as workflow_main
    return workflow_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
