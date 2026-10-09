#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态校验 SQL 表引用是否与所选 pack 的 schema 文件一致。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from project_inputs import init_project_pack  # noqa: E402

PACKS = init_project_pack(ROOT)

from platform_config import project_path  # noqa: E402

SCHEMA = project_path("PLATFORM_SCHEMA_JSON")
SCAN_DIRS = (ROOT / "test-platform", ROOT / "scripts",
             *(pack["root"] for pack in PACKS),
             *(ROOT / "adapters" / pack["id"] for pack in PACKS),
             *(ROOT / relative for pack in PACKS for relative in pack.get("case_dirs", [])))
TABLE_RE = re.compile(
    r"(?:FROM|JOIN|INTO|UPDATE)\s+(?:[a-z_][a-z0-9_]*\.)?`?([a-z_][a-z0-9_]*)`?",
    re.IGNORECASE,
)
CTE_RE = re.compile(r"(?:WITH|,)\s+`?([a-z_][a-z0-9_]*)`?\s+AS\s*\(", re.IGNORECASE)
TABLE_CONST_RE = re.compile(
    r"\b(?:TABLE|_TABLE|REPAIR_TABLE|FIAT_TABLE|AUDIT_TABLE|CHANNEL_TABLE|STRATEGY_TABLE)\s*=\s*[\"']([a-z_][a-z0-9_]*)[\"']",
)


def load_tables() -> set[str]:
    if SCHEMA is None:
        raise RuntimeError("未配置 PLATFORM_SCHEMA_JSON，schema 引用检查不完整")
    data = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return {t["name"] for t in data["tables"]}


def schema_prefixes(tables: set[str]) -> set[str]:
    """Infer project table namespaces from the selected schema, not project names."""
    return {table.split("_", 1)[0].lower() for table in tables if "_" in table}


def iter_py_files():
    seen = set()
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        for fp in base.rglob("*.py"):
            if "__pycache__" in fp.parts:
                continue
            fp = fp.resolve()
            if fp not in seen:
                seen.add(fp)
                yield fp


def main() -> int:
    if SCHEMA is None:
        print("[incomplete] 未配置 PLATFORM_SCHEMA_JSON；已跳过 schema 引用检查", file=sys.stderr)
        return 2
    if not SCHEMA.exists():
        print(f"ERROR: schema not found: {SCHEMA}", file=sys.stderr)
        return 1
    known = load_tables()
    prefixes = schema_prefixes(known)
    if not prefixes:
        print("[incomplete] 配置 schema 未提供可识别的表命名空间；无法限定未知表检查", file=sys.stderr)
        return 2
    missing: dict[str, set[str]] = {}
    for fp in iter_py_files():
        text = fp.read_text(encoding="utf-8", errors="ignore")
        rel = str(fp.relative_to(ROOT))
        ctes = {name.lower() for name in CTE_RE.findall(text)}
        refs = set()
        for match in TABLE_RE.finditer(text):
            # Ignore Python ``from module import name`` statements, which share
            # the SQL keyword but are not table references.
            if re.match(r"\s+import\b", text[match.end():], re.IGNORECASE):
                continue
            refs.add(match.group(1))
        refs |= set(TABLE_CONST_RE.findall(text))
        for t in refs:
            if t.lower() in ctes:
                continue
            if not any(t.lower().startswith(prefix + "_") for prefix in prefixes):
                continue
            if t not in known:
                missing.setdefault(t, set()).add(rel)
    if not missing:
        print("OK: 未发现引用所选 schema 表命名空间中不存在的表")
        return 0
    print(f"ERROR: {len(missing)} 个未知表引用")
    for table in sorted(missing):
        files = sorted(missing[table])
        print(f"\n  {table}")
        for f in files[:10]:
            print(f"    - {f}")
        if len(files) > 10:
            print(f"    ... +{len(files) - 10} files")
    return 1


if __name__ == "__main__":
    sys.exit(main())
