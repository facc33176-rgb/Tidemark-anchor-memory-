#!/usr/bin/env bash
# Start Anchor Memory with a key file kept OUTSIDE the repository.
# 启动脚本：钥匙放在仓库外面的 ~/.config/anchor-memory/anchor.env，别提交进 git。
set -euo pipefail
cd "$(dirname "$0")/../.."
ENV_FILE="${ANCHOR_ENV_FILE:-$HOME/.config/anchor-memory/anchor.env}"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
export ANCHOR_DATA_DIR="${ANCHOR_DATA_DIR:-$HOME/.local/share/anchor-memory}"
mkdir -p "$ANCHOR_DATA_DIR"
exec .venv/bin/python -m anchor_sse
