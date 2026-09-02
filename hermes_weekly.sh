#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

exec "$PYTHON_BIN" "$ROOT/workflow.py" run \
  --input "$ROOT/input/web_favorites.json" \
  --declared-count-file "$ROOT/input/artist_distribution.json" \
  --platform apple_music \
  --playlist-id favorite-songs-web \
  --playlist-name '喜爱歌曲'
