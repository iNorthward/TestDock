#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared input validation for OpenAPI candidate/report commands."""
from __future__ import annotations

import json
from pathlib import Path


from openapi_contract import HTTP_METHODS, OpenAPIContract


def iter_openapi_operations(document, prefix=""):
    return OpenAPIContract(document).operations(prefix)


def load_openapi_snapshot(path: Path | None) -> dict:
    """Load an OAS document and reject snapshots with no usable HTTP operations."""
    if path is None:
        raise ValueError("[incomplete] 未配置 PLATFORM_OAS_BASELINE；请在环境变量或所选 pack 中提供路径")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 OpenAPI 快照 {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"OpenAPI 快照根节点必须是对象: {path}")
    paths = data.get("paths")
    if not isinstance(paths, dict):
        raise ValueError(f"OpenAPI 快照缺少有效的 paths 对象: {path}")

    operation_count = sum(1 for _ in iter_openapi_operations(data))
    if operation_count == 0:
        raise ValueError(f"OpenAPI 快照没有有效 HTTP operation，拒绝生成空候选: {path}")
    return data


def load_coverage(path: Path | None, *, required: bool = False) -> dict[str, dict]:
    """Load endpoint coverage, optionally requiring the generated coverage cache."""
    if path is None:
        if required:
            raise ValueError("[incomplete] 所选 pack 未配置 PLATFORM_PACK_DATA_DIR，无法读取 coverage.json")
        return {}
    if not path.exists():
        if required:
            raise ValueError(
                f"--covered-only 需要 coverage 文件 {path}；请先运行 scripts/gen_apifox_coverage_wiki.py"
            )
        return {}
    if not path.is_file():
        raise ValueError(f"coverage 路径不是文件: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 coverage 文件 {path}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("paths"), list):
        raise ValueError(f"coverage 文件必须包含 paths 数组: {path}")

    coverage: dict[str, dict] = {}
    for index, item in enumerate(data["paths"]):
        if not isinstance(item, dict):
            raise ValueError(f"coverage paths[{index}] 必须是对象: {path}")
        method, endpoint_path = item.get("method"), item.get("path")
        if not isinstance(method, str) or not method.strip() or not isinstance(endpoint_path, str) or not endpoint_path.strip():
            raise ValueError(f"coverage paths[{index}] 缺少有效 method/path: {path}")
        method = method.strip().upper()
        endpoint_path = endpoint_path.strip()
        if method.lower() not in HTTP_METHODS or not endpoint_path.startswith("/"):
            raise ValueError(f"coverage paths[{index}] 的 method/path 格式无效: {path}")
        if not isinstance(item.get("covered"), bool):
            raise ValueError(f"coverage paths[{index}].covered 必须是布尔值: {path}")
        case_count = item.get("caseCount")
        cases = item.get("cases")
        if type(case_count) is not int or case_count < 0 or not isinstance(cases, list):
            raise ValueError(f"coverage paths[{index}] 的 caseCount/cases 格式无效: {path}")
        if any(not isinstance(case_id, str) for case_id in cases):
            raise ValueError(f"coverage paths[{index}].cases 必须只包含字符串: {path}")
        coverage[f"{method} {endpoint_path}"] = item
    return coverage
