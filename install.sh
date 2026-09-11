#!/bin/sh
# Music Atlas 一键部署：定位 Python，然后执行内置的 `atlas setup`。
#
# 安装流程本身就是 `python atlas.py setup`，它会检查依赖、安装缺失依赖，
# 并注册 `atlas` 命令（~/.local/bin + 包装脚本）。安装完成后新开终端即可
# 直接使用 `atlas start`。
#
# 用法：
#   ./install.sh
#   ./install.sh --check-only
#   ./install.sh --skip-node-deps --skip-browser
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
echo "=== Music Atlas 安装程序 ==="
echo "仓库目录：$ROOT"

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON=$(command -v "$candidate")
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌ 找不到 Python。请安装 Python 3.10 或更高版本后重试。" >&2
    exit 1
fi
echo "使用解释器：$PYTHON"

cd "$ROOT"
set +e
"$PYTHON" atlas.py setup "$@"
STATUS=$?
set -e

if [ "$STATUS" -ne 0 ]; then
    echo "❌ 安装未完成（退出码 $STATUS）。" >&2
    exit "$STATUS"
fi

echo "✅ 安装完成。新开一个终端后可直接运行：atlas start"
exit 0
