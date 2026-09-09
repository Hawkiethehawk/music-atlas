from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from channel_delivery import (
    build_remote_openclaw_command,
    build_remote_pi_wechatbot_command,
    resolve_weixin_target,
    run_local_weixin_sender,
    run_remote_openclaw_sender,
    run_remote_pi_wechatbot_sender,
)
from channels import render_for_channel
from contracts import ContractError, write_json
from recommender import rank_bundle
from test_hardening import pool_bundle
import workflow


class ChannelDeliveryTests(unittest.TestCase):
    def test_weixin_renderer_is_reader_facing(self) -> None:
        pool, packet = pool_bundle()
        ranked = rank_bundle(pool, packet)
        text = render_for_channel("weixin", ranked, packet)
        self.assertIn("🎧 本周音乐推荐｜研究草稿", text)
        first = ranked["recommendations"][0]
        self.assertIn(
            f"{first['title']} — {first['artist']}\n\n• 路线：",
            text,
        )
        self.assertIn("  \n• 推荐理由：", text)
        self.assertIn("  \n• 听感倾向：", text)
        self.assertIn("  \n• 试听：", text)
        self.assertIn("路线：同艺人", text)
        self.assertIn("试听：youtube ", text)
        self.assertIn("推荐理由：", text)
        self.assertIn("听感倾向：", text)
        self.assertNotIn("（spotify）", text)
        self.assertNotIn("artist_continuation", text)
        self.assertNotIn("综合分：", text)
        self.assertEqual(text.count("公开事实"), 2)

    def test_weixin_target_requires_direct_user_suffix(self) -> None:
        self.assertEqual(resolve_weixin_target("alice@im.wechat"), "alice@im.wechat")
        for value in ("", "alice", "alice @im.wechat", "alice@im.wechat\nextra"):
            with self.subTest(value=value), self.assertRaises(ContractError):
                resolve_weixin_target(value)

    def test_local_sender_receives_message_on_stdin_and_config_in_environment(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["sender"], returncode=0, stdout='{"ok":true,"messageId":"dry"}', stderr=""
        )
        with patch("channel_delivery.subprocess.run", return_value=completed) as run:
            result = run_local_weixin_sender(
                "sender",
                "研究草稿\n第二行",
                target="local-user@im.wechat",
                account_id="account-1",
                timeout=9,
                dry_run=True,
            )
        self.assertEqual(result["messageId"], "dry")
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["input"], "研究草稿\n第二行")
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 9)
        self.assertEqual(kwargs["env"]["OPENCLAW_WEIXIN_TARGET"], "local-user@im.wechat")
        self.assertEqual(kwargs["env"]["OPENCLAW_WEIXIN_ACCOUNT_ID"], "account-1")
        self.assertEqual(kwargs["env"]["OPENCLAW_WEIXIN_DRY_RUN"], "1")

    def test_remote_command_keeps_message_and_target_out_of_shell_text(self) -> None:
        message = "内容 '含引号'\n含换行与中文"
        target = "encoded-target-7f3c@im.wechat"
        command = build_remote_openclaw_command(
            message,
            target=target,
            remote_host="cloud.example",
            remote_user="ubuntu",
            account_id="account-1",
        )
        rendered = " ".join(command)
        self.assertEqual(command[0], "ssh")
        self.assertEqual(command[-2], "ubuntu@cloud.example")
        self.assertNotIn(message, rendered)
        self.assertNotIn(target, rendered)
        self.assertIn("python3", command[-1])

    def test_remote_sender_requires_openclaw_ok_result(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout='{"ok":true,"messageId":"remote-1"}', stderr=""
        )
        with patch("channel_delivery.subprocess.run", return_value=completed) as run:
            result = run_remote_openclaw_sender(
                "消息",
                target="remote-user@im.wechat",
                remote_host="cloud.example",
                timeout=11,
            )
        self.assertEqual(result["messageId"], "remote-1")
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["timeout"], 11)

        failed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout='{"ok":false,"error":"unknown target"}', stderr=""
        )
        with patch("channel_delivery.subprocess.run", return_value=failed), self.assertRaises(ContractError):
            run_remote_openclaw_sender(
                "消息",
                target="remote-user@im.wechat",
                remote_host="cloud.example",
            )

    def test_pi_remote_command_keeps_message_and_target_out_of_shell_text(self) -> None:
        message = "Pi 渠道内容 '含引号'\n含换行"
        target = "pi-target-7f3c@im.wechat"
        command = build_remote_pi_wechatbot_command(
            message,
            target=target,
            remote_host="cloud.example",
            remote_user="ubuntu",
            wechatbot_module="/home/ubuntu/.pi/agent/npm/node_modules/@wechatbot/wechatbot",
            storage_dir="/home/ubuntu/.wechatbot",
        )
        rendered = " ".join(command)
        self.assertEqual(command[0], "ssh")
        self.assertEqual(command[-2], "ubuntu@cloud.example")
        self.assertNotIn(message, rendered)
        self.assertNotIn(target, rendered)
        self.assertIn("node", command[-1])
        self.assertIn("--input-type=module", command[-1])

    def test_pi_remote_sender_requires_wechatbot_ok_result(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout='{"ok":true,"transport":"pi-agent-wechatbot"}', stderr=""
        )
        with patch("channel_delivery.subprocess.run", return_value=completed) as run:
            result = run_remote_pi_wechatbot_sender(
                "Pi 消息",
                target="pi-user@im.wechat",
                remote_host="cloud.example",
                timeout=13,
            )
        self.assertEqual(result["transport"], "pi-agent-wechatbot")
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["timeout"], 13)

    def test_send_weixin_pi_defaults_to_plan(self) -> None:
        pool, packet = pool_bundle()
        ranked = rank_bundle(pool, packet)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis_path = root / "musician_analysis.json"
            bundle_path = root / "recommendation_bundle.json"
            write_json(analysis_path, packet)
            write_json(bundle_path, ranked)
            stdout = io.StringIO()
            with redirect_stdout(stdout), patch("channel_delivery.subprocess.run") as run:
                code = workflow.main([
                    "send-weixin-pi",
                    "--analysis", str(analysis_path),
                    "--bundle", str(bundle_path),
                    "--target", "pi-plan-user@im.wechat",
                    "--remote-host", "cloud.example",
                    "--allow-draft",
                ])
            self.assertEqual(code, 0)
            run.assert_not_called()
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["status"], "delivery_plan")
            self.assertEqual(summary["transport"], "ssh_pi_wechatbot")
            self.assertFalse(summary["send_performed"])

    def test_send_weixin_pi_rejects_draft_without_explicit_allowance(self) -> None:
        pool, packet = pool_bundle()
        ranked = rank_bundle(pool, packet)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis_path = root / "musician_analysis.json"
            bundle_path = root / "recommendation_bundle.json"
            write_json(analysis_path, packet)
            write_json(bundle_path, ranked)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = workflow.main([
                    "send-weixin-pi",
                    "--analysis", str(analysis_path),
                    "--bundle", str(bundle_path),
                    "--target", "pi-draft-user@im.wechat",
                    "--remote-host", "cloud.example",
                    "--send",
                ])
            self.assertEqual(code, 2)
            self.assertIn("--allow-draft", stderr.getvalue())

    def test_send_weixin_defaults_to_plan_and_does_not_call_sender(self) -> None:
        pool, packet = pool_bundle()
        ranked = rank_bundle(pool, packet)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis_path = root / "musician_analysis.json"
            bundle_path = root / "recommendation_bundle.json"
            write_json(analysis_path, packet)
            write_json(bundle_path, ranked)
            stdout = io.StringIO()
            with redirect_stdout(stdout), patch("channel_delivery.subprocess.run") as run:
                code = workflow.main([
                    "send-weixin",
                    "--analysis", str(analysis_path),
                    "--bundle", str(bundle_path),
                    "--target", "plan-user@im.wechat",
                    "--allow-draft",
                ])
            self.assertEqual(code, 0)
            run.assert_not_called()
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["status"], "delivery_plan")
            self.assertEqual(summary["transport"], "unconfigured")
            self.assertFalse(summary["send_performed"])
            self.assertEqual(summary["recommendation_count"], len(ranked["recommendations"]))

    def test_send_weixin_rejects_draft_without_explicit_allowance(self) -> None:
        pool, packet = pool_bundle()
        ranked = rank_bundle(pool, packet)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis_path = root / "musician_analysis.json"
            bundle_path = root / "recommendation_bundle.json"
            write_json(analysis_path, packet)
            write_json(bundle_path, ranked)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = workflow.main([
                    "send-weixin",
                    "--analysis", str(analysis_path),
                    "--bundle", str(bundle_path),
                    "--target", "draft-user@im.wechat",
                    "--send",
                ])
            self.assertEqual(code, 2)
            self.assertIn("--allow-draft", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
