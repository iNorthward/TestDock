#!/usr/bin/env bash
# 停止 PLATFORM 本地服务：测试面板 (8801)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT/start.sh" stop
