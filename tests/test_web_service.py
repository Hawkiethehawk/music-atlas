"""atlas 面板服务管理测试：生命周期、复用、陈旧状态与身份防护。

所有用例使用随机空闲端口和隔离配置启动真实 server.js（Node），
不触碰 config/web.json 与生产 8420 服务。
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import web_service

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()
    return port


def _write_state(port: int, pid: int) -> None:
    web_service.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    web_service.STATE_PATH.write_text(json.dumps(
        {"service": web_service.SERVICE_NAME, "port": port, "pid": pid}), encoding="utf-8")


class WebServiceLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.port = _free_port()
        if web_service.STATE_PATH.exists():
            web_service.STATE_PATH.unlink()

    def tearDown(self):
        try:
            web_service.stop(self.port)
        except web_service.ServiceError:
            pass
        if web_service.STATE_PATH.exists():
            web_service.STATE_PATH.unlink()

    def test_start_status_reuse_stop(self):
        result = web_service.start(self.port, open_page=False)
        self.assertFalse(result["reused"])
        self.assertTrue(result["pid"] > 0)

        # 已运行时再次 start 应复用，不重启。
        again = web_service.start(self.port, open_page=False)
        self.assertTrue(again["reused"])
        self.assertEqual(again["pid"], result["pid"])

        state = web_service.status(self.port)
        self.assertTrue(state["running"])
        self.assertTrue(state["compatible"])
        self.assertTrue(state["state"], "状态文件应存在")

        stopped = web_service.stop(self.port)
        self.assertTrue(stopped["stopped"])
        after = web_service.status(self.port)
        self.assertFalse(after["running"])
        self.assertIsNone(after["state"], "停止后状态文件应被清理")

    def test_restart(self):
        first = web_service.start(self.port, open_page=False)
        restarted = web_service.restart(self.port, open_page=False)
        self.assertFalse(restarted["reused"])
        self.assertNotEqual(restarted["pid"], first["pid"])
        self.assertTrue(web_service.status(self.port)["running"])

    def test_stale_state_file_is_cleared_on_start(self):
        # 写入一个指向不存在 PID 的陈旧状态文件：start 应清理并正常启动。
        _write_state(self.port, 999999)
        result = web_service.start(self.port, open_page=False)
        self.assertFalse(result["reused"])
        self.assertTrue(web_service.status(self.port)["running"])

    def test_start_refuses_when_state_pid_alive_without_health(self):
        # 状态文件指向一个存活但无 health 的 PID（当前测试进程）：拒绝覆盖。
        _write_state(self.port, web_service.process_exists and __import__("os").getpid())
        with self.assertRaises(web_service.ServiceError):
            web_service.start(self.port, open_page=False)
        # 但 stop 只清理陈旧状态（该 PID 是当前进程，属于误报场景时 stop 也拒绝）。
        with self.assertRaises(web_service.ServiceError):
            web_service.stop(self.port)
        web_service.clear_state(self.port)

    def test_stop_without_service(self):
        result = web_service.stop(self.port)
        self.assertFalse(result["stopped"])


class ForeignServiceGuardTest(unittest.TestCase):
    """端口被非本项目服务占用时的防护：默认拒绝，--force 才替换。"""

    def setUp(self):
        self.port = _free_port()

        class FakeHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"ok": True, "service": "some-other-service", "pid": 1}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", self.port), FakeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        if web_service.STATE_PATH.exists():
            web_service.STATE_PATH.unlink()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if web_service.STATE_PATH.exists():
            web_service.STATE_PATH.unlink()

    def test_start_rejected_without_force(self):
        with self.assertRaises(web_service.ServiceError):
            web_service.start(self.port, open_page=False)

    def test_stop_rejected_for_foreign_service(self):
        with self.assertRaises(web_service.ServiceError):
            web_service.stop(self.port)

    def test_status_reports_incompatible(self):
        state = web_service.status(self.port)
        self.assertTrue(state["running"])
        self.assertFalse(state["compatible"])


if __name__ == "__main__":
    unittest.main()
