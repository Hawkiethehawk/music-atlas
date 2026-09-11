"""Music Atlas 网页面板服务管理。

防护模型移植自 nocap 的 console-service：
- /api/health 携带服务身份（service/pid/web_root），只有身份匹配的本项目
  面板才会被复用或停止，不误伤同端口的其他服务；
- 状态文件 PID 存活但 health 不可用时拒绝启动覆盖（陈旧状态保护）；
- 停止只针对健康验证过的 PID，并在停止后轮询确认。

本模块不选择模型、不访问网络（仅本机回环健康探测）、不发送消息。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SERVICE_NAME = "music-atlas-web"
WEB_DIR = ROOT / "web"
STATE_PATH = ROOT / "runtime" / "web" / "service-state.json"
LOG_PATH = ROOT / "runtime" / "web" / "service.log"
ERR_LOG_PATH = ROOT / "runtime" / "web" / "service-error.log"
DEFAULT_PORT = 8420
HEALTH_TIMEOUT_SECONDS = 20
STOP_TIMEOUT_SECONDS = 10


class ServiceError(RuntimeError):
    """服务管理操作失败（含防护拒绝）。"""


def _same_path(left: Any, right: Any) -> bool:
    try:
        return Path(str(left)).resolve() == Path(str(right)).resolve()
    except (OSError, ValueError):
        return False


def _read_config_port() -> int:
    config_path = ROOT / "config" / "web.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_PORT
    server = config.get("server") if isinstance(config, dict) else None
    port = server.get("port") if isinstance(server, dict) else None
    if isinstance(port, int) and 1 <= port <= 65535:
        return port
    return DEFAULT_PORT


def resolve_port(raw: str | int | None) -> int:
    if raw is None or raw == "":
        return _read_config_port()
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ServiceError(f"端口无效：{raw}") from exc
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise ServiceError(f"端口必须是 1 到 65535：{port}")
    return port


def read_health(port: int) -> dict[str, Any] | None:
    """探测本机面板健康端点；无响应返回 None，任何 HTTP 状态都尝试解析身份。"""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def is_compatible(health: dict[str, Any] | None) -> bool:
    """四项身份全匹配才认领：服务名、存活标记、PID、web 根目录。"""
    if not isinstance(health, dict):
        return False
    if health.get("service") != SERVICE_NAME or health.get("pid") is None:
        return False
    if not _same_path(health.get("web_root"), ROOT):
        return False
    return bool(process_exists(int(health["pid"])))


def process_exists(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, timeout=10,
    )
    return str(pid) in (result.stdout or "")


def read_state() -> dict[str, Any] | None:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def write_state(port: int, pid: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({
        "service": SERVICE_NAME,
        "port": port,
        "pid": pid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "web_root": str(ROOT),
        "log_path": str(LOG_PATH),
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def clear_state(port: int | None = None, pid: int | None = None) -> bool:
    state = read_state()
    if not state:
        return False
    if port is not None and int(state.get("port", -1)) != int(port):
        return False
    if pid is not None and int(state.get("pid", -1)) != int(pid):
        return False
    try:
        STATE_PATH.unlink()
    except OSError:
        pass
    return True


def kill_pid(pid: int) -> None:
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, timeout=15,
    )
    if result.returncode != 0:
        raise ServiceError(f"taskkill 失败：{(result.stdout or result.stderr or '').strip()}")


def wait_for_health(port: int, proc: subprocess.Popen | None, timeout: int = HEALTH_TIMEOUT_SECONDS) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise ServiceError(f"面板进程启动后立即退出，退出码 {proc.returncode}；请查看 {ERR_LOG_PATH}")
        health = read_health(port)
        if health is not None:
            if is_compatible(health):
                return health
            raise ServiceError(
                f"端口 {port} 的健康响应不是本项目面板（service={health.get('service')!r}）；请检查端口占用")
        time.sleep(0.25)
    raise ServiceError(f"面板在 {timeout} 秒内未就绪；请查看 {ERR_LOG_PATH}")


def wait_for_stopped(port: int, timeout: int = STOP_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_health(port) is None:
            return
        time.sleep(0.15)
    raise ServiceError(f"面板未能停止：端口 {port} 仍有健康响应")


def spawn_server(port: int) -> subprocess.Popen:
    node = None
    for candidate in ("node.exe", "node"):
        node = __import__("shutil").which(candidate)
        if node:
            break
    if not node:
        raise ServiceError("找不到 node；网页面板由 Node 运行，请先安装 Node.js")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    stdout_fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    stderr_fd = os.open(ERR_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            [node, "server.js"],
            cwd=str(WEB_DIR),
            env={**os.environ, "ATLAS_WEB_PORT": str(port)},
            stdin=subprocess.DEVNULL,
            stdout=stdout_fd,
            stderr=stderr_fd,
            creationflags=creationflags,
        )
    finally:
        os.close(stdout_fd)
        os.close(stderr_fd)


def open_browser(url: str) -> None:
    if os.name == "nt":
        os.startfile(url)  # noqa: S606 — 本机面板，固定回环地址
    else:
        subprocess.Popen(["xdg-open", url])


def start(port: int, *, open_page: bool = True, force: bool = False) -> dict[str, Any]:
    health = read_health(port)
    if health is not None:
        if is_compatible(health):
            return {"action": "start", "running": True, "reused": True,
                    "pid": int(health["pid"]), "port": port,
                    "url": f"http://127.0.0.1:{port}", "detail": "面板已在运行，直接复用"}
        if not force:
            raise ServiceError(
                f"端口 {port} 被其他服务占用（service={health.get('service')!r}）；"
                "如确认可替换请使用 --force")
        pid = health.get("pid")
        if isinstance(pid, int) and pid > 0:
            kill_pid(pid)
            wait_for_stopped(port)
    state = read_state()
    if state and int(state.get("port", -1)) == port and process_exists(int(state.get("pid", -1) or 0)):
        raise ServiceError(
            f"状态文件 PID {state.get('pid')} 存活但健康端点不可用，拒绝覆盖；"
            f"请先确认该进程（可查看 {LOG_PATH}）或手动停止")
    clear_state(port)
    proc = spawn_server(port)
    health = wait_for_health(port, proc)
    pid = int(health["pid"])
    write_state(port, pid)
    url = f"http://127.0.0.1:{port}"
    if open_page:
        open_browser(url)
    return {"action": "start", "running": True, "reused": False, "pid": pid,
            "port": port, "url": url, "log_path": str(LOG_PATH)}


def stop(port: int) -> dict[str, Any]:
    health = read_health(port)
    if health is not None:
        if not is_compatible(health):
            raise ServiceError(
                f"拒绝停止：端口 {port} 上的服务不是本项目面板（service={health.get('service')!r}）")
        pid = int(health["pid"])
        kill_pid(pid)
        wait_for_stopped(port)
        clear_state(port, pid)
        return {"action": "stop", "running": False, "stopped": True, "pid": pid, "port": port}
    state = read_state()
    state_pid = int(state.get("pid", 0) or 0) if state else 0
    if state and int(state.get("port", -1)) == port and state_pid and process_exists(state_pid):
        raise ServiceError(
            f"状态文件 PID {state_pid} 存活但健康端点不可用，拒绝停止；"
            f"请先人工确认该进程（tasklist /FI \"PID eq {state_pid}\"）")
    stale = clear_state(port)
    return {"action": "stop", "running": False, "stopped": False, "port": port,
            "detail": "已清理过期状态文件" if stale else "面板未在运行"}


def restart(port: int, *, open_page: bool = True) -> dict[str, Any]:
    stop(port)
    return start(port, open_page=open_page)


def status(port: int) -> dict[str, Any]:
    health = read_health(port)
    state = read_state()
    return {
        "action": "status",
        "running": bool(health),
        "compatible": is_compatible(health),
        "port": port,
        "pid": int(health["pid"]) if isinstance(health, dict) and health.get("pid") else None,
        "data_available": bool(health.get("data_available")) if health else None,
        "state": state,
        "url": f"http://127.0.0.1:{port}",
        "log_path": str(LOG_PATH),
        "error_log_path": str(ERR_LOG_PATH),
    }


def tail_logs(count: int) -> str:
    if not LOG_PATH.is_file():
        return f"日志文件不存在：{LOG_PATH}"
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max(1, count):])


def _print(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    action = result.get("action")
    if action == "start":
        if result.get("reused"):
            print(f"📡 面板已在运行，直接复用：{result['url']}")
        else:
            print(f"📡 面板已启动：{result['url']}（PID {result['pid']}）")
        print(f"   日志：{result.get('log_path', LOG_PATH)}")
    elif action == "stop":
        if result.get("stopped"):
            print(f"🛑 面板已停止（PID {result['pid']}）")
        else:
            print(f"🛑 面板未在运行。{result.get('detail', '')}")
    elif action == "status":
        state = "运行中" if result["running"] else "未运行"
        match = "（本项目面板）" if result["compatible"] else ("（端口被其他服务占用）" if result["running"] else "")
        print(f"📊 端口 {result['port']}：{state}{match}")
        if result["pid"]:
            print(f"   PID：{result['pid']} · 数据可用：{result['data_available']}")
        print(f"   URL：{result['url']}")
        print(f"   日志：{result['log_path']}")


def service_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="atlas", description="Music Atlas 网页面板服务管理")
    parser.add_argument("action", choices=("start", "stop", "restart", "status", "logs"))
    parser.add_argument("--port", default=None, help="覆盖 config/web.json 的端口")
    parser.add_argument("--no-open", action="store_true", help="start 时不自动打开浏览器")
    parser.add_argument("--force", action="store_true", help="start 时允许替换占用端口的非本项目服务")
    parser.add_argument("--tail", type=int, default=40, help="logs 输出行数")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)
    try:
        if args.action == "logs":
            print(tail_logs(args.tail))
            return 0
        port = resolve_port(args.port)
        if args.action == "start":
            result = start(port, open_page=not args.no_open, force=args.force)
        elif args.action == "stop":
            result = stop(port)
        elif args.action == "restart":
            result = restart(port, open_page=not args.no_open)
        else:
            result = status(port)
        _print(result, args.json)
        return 0
    except ServiceError as exc:
        print(f"atlas {args.action} 失败：{exc}", file=sys.stderr)
        return 2
