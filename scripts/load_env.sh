#!/usr/bin/env bash
# Source from the PLATFORM repository root to import .env.local without evaluating values.

if [[ -n "${BASH_VERSION:-}" ]]; then
  _PLATFORM_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
  _PLATFORM_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
_PLATFORM_ENV_FILE="${PLATFORM_ENV_FILE:-${_PLATFORM_REPO_ROOT}/.env.local}"
if [[ "$_PLATFORM_ENV_FILE" != /* ]]; then
  _PLATFORM_ENV_FILE="${_PLATFORM_REPO_ROOT}/${_PLATFORM_ENV_FILE}"
fi
_PLATFORM_CONFIGURED_TZ=""

_platform_trim() {
  local _platform_value="$1"
  _platform_value="${_platform_value#"${_platform_value%%[![:space:]]*}"}"
  _platform_value="${_platform_value%"${_platform_value##*[![:space:]]}"}"
  printf '%s' "$_platform_value"
}

_platform_unquote() {
  local _platform_value="$1"
  case "$_platform_value" in
    \"*\") _platform_value="${_platform_value#\"}"; _platform_value="${_platform_value%\"}" ;;
    \'*\') _platform_value="${_platform_value#\'}"; _platform_value="${_platform_value%\'}" ;;
  esac
  printf '%s' "$_platform_value"
}

if [[ -f "$_PLATFORM_ENV_FILE" ]]; then
  while IFS= read -r _platform_line || [[ -n "$_platform_line" ]]; do
    _platform_line="$(_platform_trim "$_platform_line")"
    case "$_platform_line" in
      ''|\#*) continue ;;
      *=*) ;;
      *) continue ;;
    esac
    _platform_key="$(_platform_trim "${_platform_line%%=*}")"
    _platform_value="$(_platform_unquote "$(_platform_trim "${_platform_line#*=}")")"
    case "$_platform_key" in
      [A-Za-z_]* ) ;;
      *) continue ;;
    esac
    case "$_platform_key" in
      *[!A-Za-z0-9_]* ) continue ;;
    esac
    # The selector is process-owned; a file cannot redirect later Python loads.
    [[ "$_platform_key" == "PLATFORM_ENV_FILE" ]] && continue
    # Match Python setdefault: explicit process values (including empty) win.
    if ! printenv "$_platform_key" >/dev/null 2>&1; then
      export "$_platform_key=$_platform_value"
    fi
    if [[ "$_platform_key" == "TZ" ]]; then
      _PLATFORM_CONFIGURED_TZ="$_platform_value"
    fi
  done < "$_PLATFORM_ENV_FILE"
fi

# PLATFORM uses China Standard Time even when the invoking shell has another TZ.
if [[ -n "$_PLATFORM_CONFIGURED_TZ" && "$_PLATFORM_CONFIGURED_TZ" != "Asia/Shanghai" ]]; then
  echo "PLATFORM requires TZ=Asia/Shanghai in the selected environment file" >&2
  return 1
fi
export TZ="Asia/Shanghai"
unset _PLATFORM_ENV_FILE _PLATFORM_REPO_ROOT _PLATFORM_CONFIGURED_TZ _platform_line _platform_key _platform_value
unset -f _platform_trim _platform_unquote
