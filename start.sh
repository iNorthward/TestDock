#!/usr/bin/env bash
# 启动 / 停止 PLATFORM 本地服务：测试面板 (8801)
# 启动：./start.sh  停止：./stop.sh  状态：./start.sh status
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Read local variables without evaluating credential values as shell code.
# Python uses the same PLATFORM_ENV_FILE selector; this exports TZ before startup logs.
# shellcheck source=scripts/load_env.sh
source "$ROOT/scripts/load_env.sh"

LAUNCH_CONFIG="$(python3 "$ROOT/scripts/platform_launch_config.py")"
read -r PACK_ID TEST_PORT <<< "$LAUNCH_CONFIG"
RUN_DIR="$(python3 "$ROOT/scripts/platform_launch_config.py" --run-dir)"
mkdir -p "$RUN_DIR"

port_pids() {
  lsof -i ":$1" -sTCP:LISTEN -t 2>/dev/null || true
}

wait_up() {
  local port=$1
  for _ in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:${port}/" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

wait_down() {
  local port=$1
  for _ in $(seq 1 20); do
    if [[ -z "$(port_pids "$port")" ]]; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

process_command() {
  ps -p "$1" -o command= 2>/dev/null || true
}

is_our_process() {
  local pid=$1 script=$2 command
  command="$(process_command "$pid")"
  [[ -n "$command" && "$command" == *"$ROOT/$script"* ]] || return 1
  if [[ "$command" == *"--pack=${PACK_ID} --port=${TEST_PORT}" ]]; then
    return 0
  fi
  # One-time compatibility for the previously managed default server.
  if [[ "$RUN_DIR" == "$ROOT/.run" && "$command" != *"--pack="* &&
        -f "$RUN_DIR/test-platform.pid" && "$(cat "$RUN_DIR/test-platform.pid")" == "$pid" ]]; then
    return 0
  fi
  return 1
}

# double-fork + setsid：子进程脱离 Cursor/IDE 终端会话，避免 agent 命令结束后被一并回收
daemonize_python() {
  local script=$1 logfile=$2
  python3 - "$script" "$logfile" "$PACK_ID" "$TEST_PORT" <<'PY'
import os, sys
script, log, pack, port = sys.argv[1:]
if os.fork() > 0:
    raise SystemExit(0)
os.setsid()
if os.fork() > 0:
    raise SystemExit(0)
lf = open(log, "a", buffering=1)
os.dup2(lf.fileno(), 1)
os.dup2(lf.fileno(), 2)
os.chdir(os.path.dirname(script))
os.execvp("python3", ["python3", script, f"--pack={pack}", f"--port={port}"])
PY
}

start_one() {
  local name=$1 port=$2 script=$3 pidfile=$4 logfile=$5
  local existing pid actual
  pid=""

  existing="$(port_pids "$port")"
  if [[ -n "$existing" ]]; then
    echo "[$name] 端口 $port 已被占用 (pid ${existing//$'\n'/ })，正在停止本仓库服务…"
    stop_one "$name" "$port" "$script" "$pidfile" quiet
    if ! wait_down "$port"; then
      for pid in $(port_pids "$port"); do
        if is_our_process "$pid" "$script"; then
          kill -9 "$pid" 2>/dev/null || true
        else
          echo "[$name] 端口 $port 仍被其他进程占用 (pid $pid): $(process_command "$pid")" >&2
          return 1
        fi
      done
      wait_down "$port" || {
        echo "[$name] 无法释放端口 $port" >&2
        return 1
      }
    fi
  fi

  printf '===== %s start =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$logfile"
  daemonize_python "$ROOT/$script" "$logfile"

  if wait_up "$port"; then
    actual="$(port_pids "$port")"
    if [[ -n "$actual" ]]; then
      echo "${actual%%$'\n'*}" >"$pidfile"
      pid="${actual%%$'\n'*}"
    fi
    echo "[$name] 已启动 http://localhost:$port (pid $pid)"
  else
    echo "[$name] 启动失败，请查看日志: $logfile" >&2
    if [[ -s "$logfile" ]]; then
      tail -5 "$logfile" >&2
    fi
    return 1
  fi
}

stop_one() {
  local name=$1 port=$2 script=$3 pidfile=$4
  local quiet=${5:-}
  local pids killed=0 pid saved command

  if [[ -f "$pidfile" ]]; then
    saved="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -z "$saved" ]] || ! is_our_process "$saved" "$script"; then
      rm -f "$pidfile"
      saved=""
    fi
  fi

  pids="$(port_pids "$port")"
  for pid in $pids; do
    if ! is_our_process "$pid" "$script"; then
      command="$(process_command "$pid")"
      echo "[$name] 端口 $port 被其他进程占用 (pid $pid): ${command:-<命令行不可读>}" >&2
      return 1
    fi
  done

  if [[ -f "$pidfile" ]]; then
    if [[ -n "$saved" ]] && is_our_process "$saved" "$script"; then
      if kill "$saved" 2>/dev/null; then killed=1; fi
    fi
    rm -f "$pidfile"
  fi

  for pid in $pids; do
    if is_our_process "$pid" "$script"; then
      kill "$pid" 2>/dev/null && killed=1
    fi
  done

  if [[ "$killed" -eq 1 ]] && ! wait_down "$port"; then
    for pid in $(port_pids "$port"); do
      if is_our_process "$pid" "$script"; then
        kill -9 "$pid" 2>/dev/null || true
      else
        echo "[$name] 端口 $port 进程身份发生变化 (pid $pid): $(process_command "$pid")" >&2
        return 1
      fi
    done
  fi

  if [[ "$killed" -eq 1 ]]; then
    if [[ -z "$quiet" ]]; then
      echo "[$name] 已停止 (端口 $port)"
    fi
  elif [[ -z "$quiet" ]]; then
    echo "[$name] 未在运行 (端口 $port)"
  fi
}

status_one() {
  local name=$1 port=$2 pidfile=$3
  local pids code stale=""
  pids="$(port_pids "$port")"
  if [[ -f "$pidfile" ]]; then
    local saved
    saved="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "$saved" && -z "$(ps -p "$saved" -o pid= 2>/dev/null)" ]]; then
      stale="（pid 文件已过期）"
      rm -f "$pidfile"
    fi
  fi
  if [[ -n "$pids" ]]; then
    for pid in $pids; do
      if ! is_our_process "$pid" "test-platform/server.py"; then
        echo "[${PACK_ID}] 端口 $port 被其他项目或进程占用；当前项目未运行" >&2
        return 1
      fi
    done
    if curl -sf "http://127.0.0.1:${port}/" >/dev/null 2>&1; then
      code="OK"
    else
      code="无响应"
    fi
    echo "[$name] 运行中 — http://localhost:$port ($code, pid ${pids//$'\n'/ })$stale"
  else
    echo "[$name] 未运行 (端口 $port)$stale"
  fi
}

cmd_start() {
  start_one "$PACK_ID" "$TEST_PORT" "test-platform/server.py" \
    "$RUN_DIR/test-platform.pid" "$RUN_DIR/test-platform.log"
  echo
  echo "测试面板: http://localhost:$TEST_PORT"
  echo "测试洞察: http://localhost:$TEST_PORT/insights"
}

cmd_stop() {
  stop_one "$PACK_ID" "$TEST_PORT" "test-platform/server.py" "$RUN_DIR/test-platform.pid"
}

cmd_status() {
  status_one "$PACK_ID" "$TEST_PORT" "$RUN_DIR/test-platform.pid"
}

case "${1:-start}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  *)
    echo "用法: $0 {start|stop|restart|status}" >&2
    exit 1
    ;;
esac
