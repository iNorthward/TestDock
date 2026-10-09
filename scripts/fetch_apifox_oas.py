#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从配置的 Apifox 项目拉取最新 OpenAPI，写入 PLATFORM_APIFOX_INDEX 指向的文件。

对应巡检计划 §3 步骤 A。凭据（按优先级）：
  1. 环境变量 / .env.local：APIFOX_ACCESS_TOKEN、APIFOX_PROJECT_ID
  2. ~/.cursor/mcp.json 中与所选 Pack 项目 ID 一致的 apifox-mcp-server

注意：默认**不再**覆盖 PLATFORM_OAS_BASELINE 指向的本地文件——它是上一轮巡检的
基线快照，步骤 B 靠「基线 vs 在线」做 diff；若拉取时同步覆盖，diff 恒为 0。
基线由 run_apifox_inspection.py 在没有 actionable diff 时推进；有未处理契约差异时需人工传 --advance-baseline。

用法：
  python3 scripts/fetch_apifox_oas.py
  python3 scripts/fetch_apifox_oas.py --json
  python3 scripts/fetch_apifox_oas.py --sync-local   # 同时覆盖本地基线快照
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from atomic_file import atomic_write_text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from timezone_config import configure_timezone, now_project_iso  # noqa: E402
from project_inputs import project_input_path, project_input_setting  # noqa: E402

configure_timezone()
APIFOX_INDEX = project_input_path("PLATFORM_APIFOX_INDEX", required=False)
LOCAL_OAS = project_input_path("PLATFORM_OAS_BASELINE", required=False)
API_VERSION = "2024-03-28"
_OPENAPI_METHODS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})
_AUTH_SCHEME_VALUE = re.compile(
    r"(?i)^(?P<scheme>[A-Za-z][A-Za-z0-9_-]*)\s+(?P<credential>\S+)$"
)
_AUTH_PLACEHOLDER = re.compile(
    r"(?i)^(?:YOUR_(?:TOKEN|CREDENTIALS|CREDENTIAL)_HERE|\{\{[^{}]+\}\}|<[^<>]+>)$"
)
_BEARER_JWT = re.compile(
    r"(?i)(?P<prefix>bearer\s+)?eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)


def _load_env_local():
    from platform_config import load_project_env

    load_project_env()


def _credentials_from_cursor_mcp() -> tuple[str | None, str | None]:
    mcp = Path.home() / ".cursor" / "mcp.json"
    if not mcp.is_file():
        return None, None
    try:
        data = json.loads(mcp.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    servers = data.get("mcpServers") or {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        cmd = str(cfg.get("command") or "")
        if "apifox-mcp-server" not in cmd and "apifox" not in name.lower():
            continue
        env = cfg.get("env") or {}
        token = env.get("APIFOX_ACCESS_TOKEN")
        project = None
        for arg in cfg.get("args") or []:
            if isinstance(arg, str) and arg.startswith("--project-id="):
                project = arg.split("=", 1)[1]
        if token:
            return str(token), str(project) if project else None
    return None, None


def resolve_credentials() -> tuple[str, str]:
    import os
    _load_env_local()
    token = (os.environ.get("APIFOX_ACCESS_TOKEN") or os.environ.get("APIFOX_TOKEN") or "").strip()
    project = str(project_input_setting("APIFOX_PROJECT_ID", default="") or "").strip()
    if not project:
        raise RuntimeError("[incomplete] 未配置 APIFOX_PROJECT_ID；请在环境变量或所选 pack 中提供")
    if not token:
        mcp_token, mcp_project = _credentials_from_cursor_mcp()
        if mcp_token:
            if mcp_project != project:
                raise RuntimeError(
                    "[incomplete] Cursor MCP 的 Apifox 项目与所选 Pack 不一致或未声明；"
                    "请为当前项目显式配置 APIFOX_ACCESS_TOKEN"
                )
            token = mcp_token
    if not token:
        raise RuntimeError(
            "缺少 Apifox 凭据：请在 .env.local 配置 APIFOX_ACCESS_TOKEN，"
            "或在 Cursor MCP（apifox-mcp-server）中配置同一令牌"
        )
    return token, project


def _api_base() -> str:
    base = str(project_input_setting("PLATFORM_APIFOX_API_BASE", default="") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("[incomplete] 未配置 PLATFORM_APIFOX_API_BASE；请在环境变量或所选 pack 中提供")
    return base


def _require_path(path: Path | None, key: str) -> Path:
    if path is None:
        raise RuntimeError(f"[incomplete] 未配置 {key}；请在环境变量或所选 pack 中提供路径")
    return path


def _export_body() -> dict:
    return {
        "scope": {"type": "ALL", "excludedByTags": []},
        "options": {
            "includeApifoxExtensionProperties": False,
            "addFoldersToTags": False,
        },
        "oasVersion": "3.1",
        "exportFormat": "JSON",
    }


def _curl_config_quote(value: str) -> str:
    """Quote one curl config value; credentials are passed through stdin."""
    safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
    safe = safe.replace("\r", "").replace("\n", "")
    return f'"{safe}"'


def fetch_openapi_curl(token: str, project_id: str) -> dict:
    if not shutil.which("curl"):
        raise RuntimeError("需要 curl 命令以从 Apifox 拉取 OAS")
    url = f"{_api_base()}/v1/projects/{project_id}/export-openapi?locale=zh-CN"
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = tmp.name
    try:
        cmd = [
            "curl", "--disable", "--config", "-", "-sS", "-f",
            "-X", "POST", url,
            "-H", f"X-Apifox-Api-Version: {API_VERSION}",
            "-H", "Content-Type: application/json",
            "-H", "Accept: application/json",
            "-d", json.dumps(_export_body(), ensure_ascii=False),
            "-o", out_path,
        ]
        curl_config = f"header = {_curl_config_quote(f'Authorization: Bearer {token}')}\n"
        proc = subprocess.run(
            cmd, input=curl_config, capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"curl 拉取失败: {(proc.stderr or proc.stdout or 'unknown')[:400]}")
        raw = json.loads(Path(out_path).read_text(encoding="utf-8"))
    finally:
        Path(out_path).unlink(missing_ok=True)
    return _unwrap_oas(raw)


def fetch_openapi_urllib(token: str, project_id: str) -> dict:
    url = f"{_api_base()}/v1/projects/{project_id}/export-openapi?locale=zh-CN"
    body = json.dumps(_export_body()).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Apifox-Api-Version": API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"Apifox API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Apifox API 网络错误: {e.reason}") from e
    return _unwrap_oas(raw)


def _unwrap_oas(raw: dict) -> dict:
    if isinstance(raw, dict):
        candidate = raw if "paths" in raw else raw.get("data")
        if isinstance(candidate, dict) and isinstance(candidate.get("paths"), dict):
            return candidate
    raise RuntimeError("Apifox 导出格式异常：响应中无 paths")


def _operation_count(oas: dict) -> int:
    """Count valid HTTP operations; an empty export must never replace a snapshot."""
    paths = oas.get("paths") if isinstance(oas, dict) else None
    if not isinstance(paths, dict):
        raise RuntimeError("Apifox 导出格式异常：paths 不是对象")
    return sum(
        1 for path_item in paths.values()
        if isinstance(path_item, dict)
        for method, spec in path_item.items()
        if isinstance(method, str) and method.lower() in _OPENAPI_METHODS
        and isinstance(spec, dict)
    )


def _require_operations(oas: dict) -> int:
    operation_count = _operation_count(oas)
    if operation_count == 0:
        raise RuntimeError("Apifox 导出没有任何 HTTP operation，保留现有快照")
    return operation_count


def redact_auth_header_examples(oas: dict) -> int:
    """Remove embedded credentials from OpenAPI auth-header examples/defaults.

    Apifox exports may copy bearer tokens or Basic credentials into operation
    headers. Keep the authentication scheme visible while preventing those
    credentials from being persisted in either downloaded OAS snapshot.
    Returns the number of credential occurrences replaced.
    """
    redacted = 0

    def is_auth_header_name(name: str) -> bool:
        normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).lower()
        parts = set(re.split(r"[^a-z0-9]+", normalized))
        return bool(parts & {
            "auth", "authorization", "token", "credential", "credentials", "secret", "key",
        })

    def scrub_example(value):
        nonlocal redacted
        if isinstance(value, dict):
            for key, child in list(value.items()):
                value[key] = scrub_example(child)
            return value
        if isinstance(value, list):
            return [scrub_example(child) for child in value]
        if not isinstance(value, str):
            return value

        auth_value = _AUTH_SCHEME_VALUE.fullmatch(value.strip())
        if auth_value:
            scheme = auth_value.group("scheme")
            credential = auth_value.group("credential")
            if not _AUTH_PLACEHOLDER.fullmatch(credential):
                redacted += 1
                placeholder = "YOUR_TOKEN_HERE" if scheme.lower() == "bearer" else "YOUR_CREDENTIALS_HERE"
                return f"{scheme} {placeholder}"

        def replace(match):
            nonlocal redacted
            redacted += 1
            return f"{match.group('prefix') or ''}YOUR_TOKEN_HERE"

        return _BEARER_JWT.sub(replace, value)

    def visit(node):
        if isinstance(node, dict):
            name = str(node.get("name") or "").strip()
            location = str(node.get("in") or "").strip().lower()
            if location == "header" and is_auth_header_name(name):
                for key in ("example", "examples", "schema"):
                    if key in node:
                        node[key] = scrub_example(node[key])
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(oas)
    return redacted


def fetch_openapi(token: str, project_id: str) -> dict:
    try:
        return fetch_openapi_curl(token, project_id)
    except Exception as curl_err:
        try:
            return fetch_openapi_urllib(token, project_id)
        except Exception:
            raise curl_err


def sync_local_snapshot(oas: dict | None = None) -> None:
    """把在线索引推进为本地基线快照（巡检报告落盘后调用）。"""
    local_oas = _require_path(LOCAL_OAS, "PLATFORM_OAS_BASELINE")
    if oas is None:
        index = _require_path(APIFOX_INDEX, "PLATFORM_APIFOX_INDEX")
        oas = json.loads(index.read_text(encoding="utf-8"))
    _require_operations(oas)
    redact_auth_header_examples(oas)
    atomic_write_text(local_oas, json.dumps(oas, ensure_ascii=False, indent=1))


def save_index(oas: dict, sync_local: bool = False) -> dict:
    apifox_index = _require_path(APIFOX_INDEX, "PLATFORM_APIFOX_INDEX")
    operation_count = _require_operations(oas)
    redacted_count = redact_auth_header_examples(oas)
    info = dict(oas.get("info") or {})
    info["x-download-time"] = now_project_iso(timespec="milliseconds")
    oas["info"] = info
    apifox_index.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(apifox_index, json.dumps(oas, ensure_ascii=False, separators=(",", ":")))
    # 基线快照默认保留（步骤 B 需要「上一轮基线 vs 本次在线」的 diff），
    # 仅显式要求时才同步覆盖。
    if sync_local:
        sync_local_snapshot(oas)
    paths = oas.get("paths") or {}
    return {
        "path": "PLATFORM_APIFOX_INDEX",
        "pathCount": len(paths),
        "operationCount": operation_count,
        "downloadTime": info["x-download-time"],
        "redactedAuthExamples": redacted_count,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--sync-local", action="store_true",
                        help="同时把在线契约覆盖到本地基线快照（默认保留基线供 diff）")
    args = parser.parse_args()

    out = {"ok": True}
    try:
        token, project_id = resolve_credentials()
        out["projectId"] = project_id
        oas = fetch_openapi(token, project_id)
        out["fetch"] = save_index(oas, sync_local=args.sync_local)
    except Exception as e:
        out = {"ok": False, "error": str(e)}

    print(json.dumps(out, ensure_ascii=False, indent=None if args.json else 2))
    sys.exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
