#!/usr/bin/env bash
# 可选的服务器侧调度入口模板，非必需：工作流本身随时手动触发，
# 不绑定时间；仅在需要 cron 等外部调度时参考使用。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

exec "$PYTHON_BIN" "$ROOT/workflow.py" run \
  --input "$ROOT/input/web_favorites.json" \
  --declared-count-file "$ROOT/input/artist_distribution.json" \
  --platform apple_music \
  --playlist-id favorite-songs-web \
  --playlist-name '喜爱歌曲'
