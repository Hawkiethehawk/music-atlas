#!/usr/bin/env bash
set -euo pipefail

exec "${HERMES_PYTHON:-/home/ubuntu/.hermes/hermes-agent/venv/bin/python}" -c \
  'import sys; from hermes_cli.oneshot import run_oneshot; raise SystemExit(run_oneshot(sys.stdin.read()))'
