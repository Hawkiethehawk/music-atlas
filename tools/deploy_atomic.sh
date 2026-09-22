#!/usr/bin/env bash
set -euo pipefail

APP_NAME="music-atlas"
SOURCE_ROOT="${2:-/home/ubuntu/apps/music-atlas}"
RELEASE_ROOT="/home/ubuntu/apps/${APP_NAME}-releases"
CURRENT_LINK="/home/ubuntu/apps/${APP_NAME}-current"
SHARED_RUNTIME="/home/ubuntu/apps/${APP_NAME}/runtime"
SHARED_INPUT="/home/ubuntu/apps/${APP_NAME}/input"
ACTION="${1:-deploy}"

require_safe_paths() {
  SOURCE_ROOT="$(readlink -f "$SOURCE_ROOT")"
  [[ "$SOURCE_ROOT" == /home/ubuntu/apps/music-atlas ]] || {
    echo "拒绝未知源目录：$SOURCE_ROOT" >&2
    exit 2
  }
  [[ "$RELEASE_ROOT" == /home/ubuntu/apps/music-atlas-releases ]]
  [[ "$CURRENT_LINK" == /home/ubuntu/apps/music-atlas-current ]]
}

wait_for_health() {
  local attempt
  for attempt in {1..30}; do
    if sudo systemctl is-active --quiet music-atlas.service \
      && curl --fail --silent --max-time 3 http://127.0.0.1:8420/api/health >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

deploy() {
  local stamp release temporary previous
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  release="$RELEASE_ROOT/$stamp"
  temporary="$RELEASE_ROOT/.${stamp}.tmp"
  previous="$SOURCE_ROOT"
  [[ ! -e "$release" && ! -e "$temporary" ]] || {
    echo "发布目录已存在：$release" >&2
    exit 3
  }
  if [[ -L "$CURRENT_LINK" ]]; then
    previous="$(readlink -f "$CURRENT_LINK")"
  fi
  mkdir -p "$RELEASE_ROOT" "$SHARED_RUNTIME" "$SHARED_INPUT"
  mkdir "$temporary"
  rsync -a --delete \
    --exclude '.git/' --exclude 'runtime/' --exclude 'input/' \
    --exclude 'web/node_modules/' \
    --exclude '__pycache__/' --exclude '*.pyc' --exclude 'atlas-review-overlay*.tar' \
    "$SOURCE_ROOT/" "$temporary/"
  ln -s "$SHARED_RUNTIME" "$temporary/runtime"
  ln -s "$SHARED_INPUT" "$temporary/input"
  printf '%s\n' "$stamp" > "$temporary/.release-id"
  mv "$temporary" "$release"
  ln -sfn "$release" "${CURRENT_LINK}.new"
  mv -Tf "${CURRENT_LINK}.new" "$CURRENT_LINK"
  if ! sudo systemctl restart music-atlas.service || ! wait_for_health; then
    ln -sfn "$previous" "${CURRENT_LINK}.new"
    mv -Tf "${CURRENT_LINK}.new" "$CURRENT_LINK"
    sudo systemctl restart music-atlas.service || true
    echo "服务启动或健康检查失败，已回滚到：$previous" >&2
    exit 4
  fi
  echo "$release"
}

rollback() {
  local target="${2:-}"
  [[ -n "$target" ]] || { echo "用法：$0 rollback <release-id>" >&2; exit 2; }
  [[ "$target" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || { echo "发布 ID 格式无效" >&2; exit 2; }
  target="$RELEASE_ROOT/$target"
  [[ -d "$target" ]] || { echo "发布不存在：$target" >&2; exit 2; }
  ln -sfn "$target" "${CURRENT_LINK}.new"
  mv -Tf "${CURRENT_LINK}.new" "$CURRENT_LINK"
  sudo systemctl restart music-atlas.service
  wait_for_health
  echo "$target"
}

require_safe_paths
case "$ACTION" in
  deploy) deploy ;;
  rollback) rollback "$@" ;;
  *) echo "用法：$0 deploy [source-root] | rollback <release-id>" >&2; exit 2 ;;
esac
