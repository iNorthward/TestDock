# -*- coding: utf-8 -*-
"""从被测项目 Java 枚举同步错误码索引。

源目录通过 `PLATFORM_LOG_ENUM_DIR` 或 `--enum-dir`
提供；源文件清单通过 `PLATFORM_LOG_ENUM_FILES` 提供。两项缺失时标记 incomplete 并跳过，
不探测兄弟仓。项目 Pack 在 manifest 中提供自己的文件清单。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from atomic_file import atomic_write_text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from platform_config import load_project_env, project_path, project_setting  # noqa: E402
from project_inputs import (  # noqa: E402
    pack_data_path,
    project_code_root,
    project_input_setting,
)
from timezone_config import now_project_iso  # noqa: E402

load_project_env()
OUT_FILE = pack_data_path("log_error_codes.json")

def configured_enum_dir() -> str:
    """Resolve an explicit enum path or derive it from the selected pack's code root."""
    path = project_path("PLATFORM_LOG_ENUM_DIR")
    if path is not None:
        return str(path)

    code_root = project_code_root()
    enum_subdir = str(project_input_setting("PLATFORM_LOG_ENUM_SUBDIR", default="") or "").strip()
    if code_root is None or not enum_subdir:
        return ""
    return str(code_root / enum_subdir)


def configured_enum_files() -> tuple[str, ...]:
    """Resolve the selected pack's file list or a neutral environment override."""
    raw = project_input_setting("PLATFORM_LOG_ENUM_FILES", default="")
    names = tuple(part.strip() for part in str(raw or "").split(",") if part.strip())
    if any(Path(name).name != name or "/" in name or "\\" in name or name in (".", "..")
           for name in names):
        raise ValueError("PLATFORM_LOG_ENUM_FILES 只接受逗号分隔的文件名")
    return names


_RE_ENTRY = re.compile(
    r"^\s*(?P<name>[A-Z][A-Z0-9_]*)\((?P<code>\d+),\s*\"(?P<message>(?:[^\"\\]|\\.)*)\"\)",
    re.M,
)


def _unescape_java(s: str) -> str:
    return (
        s.replace("\\\"", '"')
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\\\", "\\")
        .replace("%%", "%")
    )


def parse_enum_file(path: Path) -> dict[str, list[dict]]:
    """按错误码保留源枚举中的全部定义，不能让重复码被字典覆盖。"""
    text = path.read_text(encoding="utf-8")
    enum_name = path.stem
    out: dict[str, list[dict]] = {}
    for m in _RE_ENTRY.finditer(text):
        code = m.group("code")
        out.setdefault(code, []).append({
            "message": _unescape_java(m.group("message")),
            "enum": enum_name,
            "name": m.group("name"),
        })
    return out


def sync(enum_dir: Path, enum_files=None) -> dict:
    if not enum_dir.is_dir():
        raise FileNotFoundError(f"枚举目录不存在: {enum_dir}")
    enum_files = tuple(enum_files if enum_files is not None else configured_enum_files())
    if not enum_files:
        raise ValueError("[incomplete] 未配置 PLATFORM_LOG_ENUM_FILES；已跳过错误码同步")

    definitions_by_code: dict[str, list[dict]] = {}
    sources: list[str] = []
    for fname in enum_files:
        path = enum_dir / fname
        if not path.is_file():
            raise FileNotFoundError(f"缺少枚举文件: {path}")
        parsed = parse_enum_file(path)
        for code, definitions in parsed.items():
            definitions_by_code.setdefault(code, []).extend(definitions)
        sources.append(fname)

    codes: dict[str, dict] = {}
    for code, definitions in sorted(definitions_by_code.items(), key=lambda kv: int(kv[0])):
        if len(definitions) == 1:
            codes[code] = definitions[0]
            continue

        choices = "；".join(
            f"{item['enum']}.{item['name']}：{item['message']}" for item in definitions
        )
        codes[code] = {
            "message": f"重复错误码，无法唯一映射：{choices}",
            "enum": None,
            "name": None,
            "ambiguous": True,
            "definitions": definitions,
        }

    return {
        # The JSON is tracked; never serialize this machine's absolute path.
        "source_dir": "$PLATFORM_LOG_ENUM_DIR",
        "source_files": sources,
        "synced_at": now_project_iso(),
        "count": len(codes),
        "ambiguous_count": sum(1 for definitions in definitions_by_code.values() if len(definitions) > 1),
        "codes": dict(sorted(codes.items(), key=lambda kv: int(kv[0]))),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="同步被测项目错误码枚举")
    ap.add_argument(
        "--enum-dir",
        default=configured_enum_dir(),
        help="LogErrorCode 枚举 Java 源目录",
    )
    ap.add_argument(
        "--out",
        default=str(OUT_FILE) if OUT_FILE else "",
        help="输出 JSON 路径",
    )
    args = ap.parse_args()

    if not args.enum_dir:
        print("[incomplete] 未配置 PLATFORM_LOG_ENUM_DIR；已跳过错误码同步")
        return 2
    if not args.out:
        print("[incomplete] 所选 pack 未配置 PLATFORM_PACK_DATA_DIR；已跳过错误码索引写入")
        return 2
    try:
        enum_files = configured_enum_files()
        if not enum_files:
            print("[incomplete] 未配置 PLATFORM_LOG_ENUM_FILES；已跳过错误码同步")
            return 2
        payload = sync(Path(args.enum_dir), enum_files)
    except FileNotFoundError as exc:
        ap.error(str(exc))
    except ValueError as exc:
        ap.error(str(exc))
    out = Path(args.out)
    atomic_write_text(out, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"已写入 {out}（{payload['count']} 条，其中歧义码 {payload['ambiguous_count']} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
