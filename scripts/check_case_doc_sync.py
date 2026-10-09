#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档/代码用例 id 一致性校验。

对比 test-platform CATALOG 与所选 pack 配置的业务用例文档目录：
  1. 代码里有、文档里没有的用例 id（文档漏更新）
  2. 文档里出现、代码里不存在的用例 id（文档写错或用例已删）

用法：
  python3 scripts/check_case_doc_sync.py            # 全部模块
  python3 scripts/check_case_doc_sync.py --strict   # 有差异时 exit 1（供验收流程用）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TP = ROOT / "test-platform"
sys.path.insert(0, str(TP / "common"))
sys.path.insert(0, str(ROOT / "scripts"))

import importlib  # noqa: E402

from gen_apifox_coverage_wiki import discover_modules  # noqa: E402
from project_inputs import business_knowledge_subpath  # noqa: E402

DOC_DIR = business_knowledge_subpath("PLATFORM_CASE_DOC_SUBDIR")

# 用例 id 形如 NC-R01 / IV-LIVE-B01 / AGP-F03：全大写段 + 至少一段含数字
ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{0,5}(?:-[A-Z0-9]{1,8})+\b")


def _looks_like_case_id(token: str) -> bool:
    return any(ch.isdigit() for ch in token.split("-")[-1])


MANUAL_MARK = "仅供手工/历史记录使用"


def collect_code_ids() -> tuple[dict[str, dict], list[str]]:
    """{doc_wiki 相对路径: {"label": 模块名列表, "ids": set}}（多模块可共享一篇文档）"""
    docs: dict[str, dict] = {}
    undocumented: list[str] = []
    for mod_name, label, _code, doc_wiki in discover_modules():
        if not doc_wiki:
            undocumented.append(mod_name)
            continue
        mod = importlib.import_module(mod_name)
        ids = {c["id"] for c in getattr(mod, "CATALOG", [])}
        entry = docs.setdefault(doc_wiki, {"labels": [], "ids": set()})
        entry["labels"].append(label)
        entry["ids"] |= ids
    return docs, undocumented


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="有差异时非零退出")
    parser.add_argument("--allow-known", action="store_true", help="仅豁免 Pack 逐条声明的历史差异；新增缺口仍失败")
    args = parser.parse_args()
    known = {}
    if args.allow_known:
        from pack_registry import load_packs
        for pack in load_packs():
            known.update(pack.get("case_doc_exceptions", {}))

    if DOC_DIR is None:
        message = "[incomplete] 所选 pack 未配置业务知识库与 PLATFORM_CASE_DOC_SUBDIR；跳过用例文档同步检查"
        print(message)
        if args.strict:
            sys.exit(2)
        return

    docs, undocumented = collect_code_ids()
    if undocumented:
        print("[缺文档映射] " + "、".join(undocumented)
              + "（这些模块的用例未参与后续 id 对照）")
    all_code_ids = set()
    for entry in docs.values():
        all_code_ids |= entry["ids"]

    # 代码 id 的前缀集合，用于过滤文档中的其它全大写 token（如表名缩写）
    known_prefixes = {i.split("-")[0] for i in all_code_ids}

    problems = 0
    warnings = 0
    for doc_wiki, entry in sorted(docs.items()):
        doc_path = DOC_DIR / f"{Path(doc_wiki).name}.md"
        labels = " / ".join(entry["labels"])
        if not doc_path.exists():
            print(f"[缺文档] {labels}: {doc_path} 不存在")
            problems += 1
            continue
        text = doc_path.read_text(encoding="utf-8")
        in_doc = set(ID_RE.findall(text))
        missing = entry["ids"] - in_doc
        accepted = sorted(missing & known.keys())
        if accepted:
            print(f"[保留的历史差异] {doc_path.name}（{len(accepted)} 条；本次未处理）:")
            for identifier in accepted:
                print(f"    - {identifier}: {known[identifier]}")
        missing_in_doc = sorted(missing - known.keys())
        # 只报告与已知前缀同族、且不属于任何模块代码的 id（避免误报）
        stale_in_doc = sorted(
            t for t in in_doc
            if t.split("-")[0] in known_prefixes and t not in all_code_ids
        )
        if missing_in_doc:
            problems += 1
            print(f"[文档缺 id] {labels} → {doc_path.name}（{len(missing_in_doc)} 条）:")
            for i in missing_in_doc:
                print(f"    - {i}")
        if stale_in_doc:
            # 文档已声明这些编号是手工/历史记录时，只报条数，不把 id 再铺开
            warnings += 1
            if MANUAL_MARK in text:
                print(f"[已标注手工] {labels} → {doc_path.name}"
                      f"（{len(stale_in_doc)} 条文档独有 id，文档已声明不由自动化执行）")
            else:
                print(f"[提示·文档独有 id] {labels} → {doc_path.name}（{len(stale_in_doc)} 条，"
                      f"代码 CATALOG 中不存在）:")
                print("    " + ", ".join(stale_in_doc))

    total = len(all_code_ids)
    if problems == 0 and not undocumented:
        print(f"OK：{total} 条代码用例 id / {len(docs)} 篇文档未发现未豁免差异"
              + (f"（另有 {warnings} 处文档独有 id 提示）" if warnings else "。"))
    else:
        print(
            f"\n文档内容差异 {problems} 处；缺少文档映射 {len(undocumented)} 个模块 "
            f"（已登记代码用例 {total} 条 / 文档 {len(docs)} 篇），请同步文档。"
        )
        if args.strict:
            sys.exit(1)


if __name__ == "__main__":
    main()
