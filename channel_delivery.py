#!/usr/bin/env python3
"""Explicit external delivery for rendered workflow output.

Rendering remains separate from delivery.  This module only accepts a
validated Weixin message and hands it to either a configured local sender
command, an OpenClaw CLI, or the WeChatBot SDK installed with the remote Pi
agent over SSH.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from contracts import ContractError


WEIXIN_CHANNEL = "openclaw-weixin"
WEIXIN_TARGET_SUFFIX = "@im.wechat"


_REMOTE_BRIDGE = """
import base64
import json
import subprocess
import sys

payload = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
command = [
    payload["openclaw"],
    "message",
    "send",
    "--channel",
    "openclaw-weixin",
    "--target",
    payload["target"],
    "--message",
    payload["message"],
    "--json",
]
if payload.get("account_id"):
    command.extend(["--account", payload["account_id"]])
if payload.get("dry_run"):
    command.append("--dry-run")
completed = subprocess.run(
    command,
    capture_output=True,
    text=True,
    encoding="utf-8",
    check=False,
    shell=False,
)
sys.stdout.write(completed.stdout)
sys.stderr.write(completed.stderr)
raise SystemExit(completed.returncode)
""".strip()


_PI_WECHATBOT_REMOTE_BRIDGE = r"""
(async () => {
  const { existsSync, statSync } = await import("node:fs")
  const { homedir } = await import("node:os")
  const { join, resolve } = await import("node:path")
  const { pathToFileURL } = await import("node:url")

  const payload = JSON.parse(Buffer.from(process.argv[1], "base64").toString("utf8"))
  const candidates = []
  if (payload.wechatbot_module) candidates.push(payload.wechatbot_module)
  if (process.env.PI_WECHATBOT_MODULE) candidates.push(process.env.PI_WECHATBOT_MODULE)
  candidates.push(join(homedir(), ".pi", "agent", "npm", "node_modules", "@wechatbot", "wechatbot", "dist", "index.js"))
  candidates.push("@wechatbot/wechatbot")

  let wechatbot
  let lastImportError
  for (const candidate of candidates) {
    try {
      let specifier = candidate
      if (!candidate.startsWith("@")) {
        let filePath = candidate
        if (filePath.startsWith("~/")) filePath = join(homedir(), filePath.slice(2))
        filePath = resolve(filePath)
        if (existsSync(filePath) && statSync(filePath).isDirectory()) {
          filePath = join(filePath, "dist", "index.js")
        }
        specifier = pathToFileURL(filePath).href
      }
      wechatbot = await import(specifier)
      break
    } catch (error) {
      lastImportError = error
    }
  }
  if (!wechatbot?.WeChatBot) {
    throw new Error(`找不到 @wechatbot/wechatbot：${lastImportError?.message ?? "模块未导出 WeChatBot"}`)
  }

  const storageDir = payload.storage_dir
    ? (payload.storage_dir.startsWith("~/") ? join(homedir(), payload.storage_dir.slice(2)) : payload.storage_dir)
    : join(homedir(), ".wechatbot")
  const bot = new wechatbot.WeChatBot({ storage: "file", storageDir, logLevel: "error" })
  const credentials = await bot.storage.get("credentials")
  if (!credentials) throw new Error("Pi wechatbot 没有已保存的微信凭据；请先在 pi 中完成 /wechat 登录")
  if (payload.account_id && credentials.accountId !== payload.account_id) {
    throw new Error("Pi wechatbot 已保存账号与指定 account-id 不一致")
  }
  const contextTokens = await bot.storage.get("context_tokens")
  if (!contextTokens || typeof contextTokens !== "object" || !contextTokens[payload.target]) {
    throw new Error("目标用户没有已保存的 context_token；请先让该用户给 Pi wechatbot 发过消息")
  }

  let startPromise
  try {
    if (!payload.dry_run) {
      await bot.login({ force: false })
      let resolveReady
      let rejectReady
      const ready = new Promise((resolve, reject) => {
        resolveReady = resolve
        rejectReady = reject
      })
      bot.on("poll:start", () => resolveReady())
      bot.on("error", (error) => rejectReady(error))
      startPromise = bot.start().catch((error) => {
        rejectReady(error)
        throw error
      })
      await ready
      await bot.send(payload.target, payload.message)
    }
    process.stdout.write(JSON.stringify({
      ok: true,
      transport: "pi-agent-wechatbot",
      dry_run: Boolean(payload.dry_run),
      account_configured: Boolean(credentials.accountId),
      target_configured: true,
    }))
  } finally {
    if (startPromise) {
      bot.stop()
      await startPromise.catch(() => {})
    }
  }
})().catch((error) => {
  process.stderr.write(`${error?.stack ?? error}\n`)
  process.exitCode = 1
})
""".strip()


def resolve_weixin_target(value: str | None = None) -> str:
    """Resolve and validate a direct Weixin recipient ID.

    The Tencent WeChatBot/OpenClaw channels accept direct recipient IDs ending
    in ``@im.wechat``.  The value can be passed explicitly or supplied through
    ``OPENCLAW_WEIXIN_TARGET`` / ``PI_WECHATBOT_TARGET``.
    """

    target = (
        value
        or os.environ.get("OPENCLAW_WEIXIN_TARGET")
        or os.environ.get("PI_WECHATBOT_TARGET", "")
    ).strip()
    if not target:
        raise ContractError("缺少微信目标；请传入 --target 或设置 OPENCLAW_WEIXIN_TARGET")
    if any(character.isspace() for character in target) or not target.endswith(WEIXIN_TARGET_SUFFIX):
        raise ContractError(f"微信目标必须是以 {WEIXIN_TARGET_SUFFIX} 结尾的直接用户 ID")
    return target


def _validate_positive_timeout(timeout: int) -> int:
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ContractError("微信发送 timeout 必须是正整数秒数")
    return timeout


def _command_argv(command: str) -> str | list[str]:
    if not isinstance(command, str) or not command.strip():
        raise ContractError("微信发送命令不能为空")
    if os.name == "nt":
        return command.strip()
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ContractError(f"无法解析微信发送命令：{exc}") from exc
    if not argv:
        raise ContractError("微信发送命令不能为空")
    return argv


def _detail(completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")
    return detail[:500] or "无错误详情"


def _parse_result(output: str) -> dict[str, Any]:
    text = output.strip()
    if not text:
        raise ContractError("OpenClaw 发送器未返回 JSON")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"OpenClaw 发送器输出不是合法 JSON：第 {exc.lineno} 行") from exc
    if not isinstance(result, dict):
        raise ContractError("OpenClaw 发送器输出必须是 JSON 对象")
    if result.get("ok") is not True:
        message = result.get("error") or result.get("message") or "未返回 ok=true"
        raise ContractError(f"OpenClaw 未确认发送成功：{message}")
    return result


def _run_json_process(
    command: str | list[str],
    *,
    stdin_text: str,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            input=stdin_text,
            text=True,
            encoding="utf-8",
            env=environment,
            capture_output=True,
            timeout=_validate_positive_timeout(timeout),
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise ContractError(f"找不到微信发送命令：{command}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ContractError(f"微信发送命令超时：{timeout} 秒") from exc
    if completed.returncode != 0:
        raise ContractError(f"微信发送命令返回码为 {completed.returncode}：{_detail(completed)}")
    return _parse_result(completed.stdout)


def run_local_weixin_sender(
    command: str,
    message: str,
    *,
    target: str,
    account_id: str | None,
    timeout: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a local wrapper command with the rendered message on stdin."""

    target = resolve_weixin_target(target)
    if not isinstance(message, str) or not message.strip():
        raise ContractError("微信发送内容不能为空")
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["OPENCLAW_WEIXIN_TARGET"] = target
    if account_id:
        environment["OPENCLAW_WEIXIN_ACCOUNT_ID"] = account_id
    else:
        environment.pop("OPENCLAW_WEIXIN_ACCOUNT_ID", None)
    if dry_run:
        environment["OPENCLAW_WEIXIN_DRY_RUN"] = "1"
    else:
        environment.pop("OPENCLAW_WEIXIN_DRY_RUN", None)
    return _run_json_process(
        _command_argv(command),
        stdin_text=message,
        timeout=timeout,
        environment=environment,
    )


def _safe_remote_part(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(character.isspace() for character in value):
        raise ContractError(f"{label} 不能为空且不能包含空白字符")
    return value.strip()


def build_remote_openclaw_command(
    message: str,
    *,
    target: str,
    remote_host: str,
    remote_user: str,
    openclaw_command: str = "openclaw",
    ssh_command: str = "ssh",
    ssh_identity: str | None = None,
    account_id: str | None = None,
    dry_run: bool = False,
    connect_timeout: int = 15,
) -> list[str]:
    """Build an SSH command whose user data stays in a base64 JSON payload.

    The remote command uses ``subprocess.run(..., shell=False)`` for the
    OpenClaw invocation, so message text and recipient IDs are not interpolated
    into a remote shell command.
    """

    target = resolve_weixin_target(target)
    if not isinstance(message, str) or not message.strip():
        raise ContractError("微信发送内容不能为空")
    remote_host = _safe_remote_part(remote_host, "云服务器地址")
    remote_user = _safe_remote_part(remote_user, "云服务器用户")
    openclaw_command = _safe_remote_part(openclaw_command, "远端 OpenClaw 命令")
    ssh_command = _safe_remote_part(ssh_command, "SSH 命令")
    _validate_positive_timeout(connect_timeout)
    payload = {
        "openclaw": openclaw_command,
        "target": target,
        "message": message,
        "account_id": account_id,
        "dry_run": dry_run,
    }
    payload_b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")
    bridge_b64 = base64.b64encode(_REMOTE_BRIDGE.encode("utf-8")).decode("ascii")
    remote_command = f'python3 -c "import base64;exec(base64.b64decode(\'{bridge_b64}\'))" {payload_b64}'
    destination = f"{remote_user}@{remote_host}"
    command = [ssh_command, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}"]
    if ssh_identity:
        identity = str(Path(ssh_identity).expanduser())
        if not identity.strip():
            raise ContractError("SSH 身份文件路径不能为空")
        command.extend(["-i", identity])
    command.extend([destination, remote_command])
    return command


def run_remote_openclaw_sender(
    message: str,
    *,
    target: str,
    remote_host: str,
    remote_user: str = "ubuntu",
    openclaw_command: str = "openclaw",
    ssh_command: str = "ssh",
    ssh_identity: str | None = None,
    account_id: str | None = None,
    timeout: int = 60,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send through the OpenClaw CLI on a remote cloud server."""

    timeout = _validate_positive_timeout(timeout)
    command = build_remote_openclaw_command(
        message,
        target=target,
        remote_host=remote_host,
        remote_user=remote_user,
        openclaw_command=openclaw_command,
        ssh_command=ssh_command,
        ssh_identity=ssh_identity,
        account_id=account_id,
        dry_run=dry_run,
        connect_timeout=min(15, timeout),
    )
    return _run_json_process(command, stdin_text="", timeout=timeout)


def build_remote_pi_wechatbot_command(
    message: str,
    *,
    target: str,
    remote_host: str,
    remote_user: str,
    node_command: str = "node",
    wechatbot_module: str | None = None,
    storage_dir: str | None = None,
    ssh_command: str = "ssh",
    ssh_identity: str | None = None,
    account_id: str | None = None,
    dry_run: bool = False,
    connect_timeout: int = 15,
) -> list[str]:
    """Build an SSH command for the WeChatBot SDK installed with Pi."""

    target = resolve_weixin_target(target)
    if not isinstance(message, str) or not message.strip():
        raise ContractError("微信发送内容不能为空")
    remote_host = _safe_remote_part(remote_host, "云服务器地址")
    remote_user = _safe_remote_part(remote_user, "云服务器用户")
    node_command = _safe_remote_part(node_command, "远端 Node.js 命令")
    ssh_command = _safe_remote_part(ssh_command, "SSH 命令")
    _validate_positive_timeout(connect_timeout)
    payload = {
        "target": target,
        "message": message,
        "account_id": account_id,
        "dry_run": dry_run,
    }
    if wechatbot_module:
        payload["wechatbot_module"] = _safe_remote_part(wechatbot_module, "远端 wechatbot 模块路径")
    if storage_dir:
        payload["storage_dir"] = storage_dir
    payload_b64 = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    bridge_b64 = base64.b64encode(_PI_WECHATBOT_REMOTE_BRIDGE.encode("utf-8")).decode("ascii")
    remote_command = (
        f'{node_command} --input-type=module -e '
        f'"eval(Buffer.from(\'{bridge_b64}\',\'base64\').toString())" {payload_b64}'
    )
    destination = f"{remote_user}@{remote_host}"
    command = [ssh_command, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}"]
    if ssh_identity:
        identity = str(Path(ssh_identity).expanduser())
        if not identity.strip():
            raise ContractError("SSH 身份文件路径不能为空")
        command.extend(["-i", identity])
    command.extend([destination, remote_command])
    return command


def run_remote_pi_wechatbot_sender(
    message: str,
    *,
    target: str,
    remote_host: str,
    remote_user: str = "ubuntu",
    node_command: str = "node",
    wechatbot_module: str | None = None,
    storage_dir: str | None = None,
    ssh_command: str = "ssh",
    ssh_identity: str | None = None,
    account_id: str | None = None,
    timeout: int = 60,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send through the @wechatbot/wechatbot SDK used by the remote Pi agent."""

    timeout = _validate_positive_timeout(timeout)
    command = build_remote_pi_wechatbot_command(
        message,
        target=target,
        remote_host=remote_host,
        remote_user=remote_user,
        node_command=node_command,
        wechatbot_module=wechatbot_module,
        storage_dir=storage_dir,
        ssh_command=ssh_command,
        ssh_identity=ssh_identity,
        account_id=account_id,
        dry_run=dry_run,
        connect_timeout=min(15, timeout),
    )
    return _run_json_process(command, stdin_text="", timeout=timeout)
