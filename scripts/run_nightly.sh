#!/usr/bin/env bash
# 定时回归入口：加载 .env.local 后跑全量 SAFE 并落库/通知。
# 挂 cron 示例（crontab -e）：
#   30 2 * * * $PLATFORM_ROOT/scripts/run_nightly.sh >> $PLATFORM_ROOT/.run/nightly.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/load_env.sh
source "$ROOT/scripts/load_env.sh"

mkdir -p "$ROOT/.run"
echo "===== $(date '+%Y-%m-%d %H:%M:%S') nightly regression ====="
python3 scripts/nightly_regression.py "$@"
