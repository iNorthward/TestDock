"""Helpers for preserving manually maintained report sections."""
from __future__ import annotations

import re

START = "<!-- platform-hand:start -->"
END = "<!-- platform-hand:end -->"
PLACEHOLDER = "在标记内追加结论"
_AUTO_PLACEHOLDER = "本轮为自动巡检，修复项请在本节手工追加或关联 PR"


def _is_auto_placeholder(text: str) -> bool:
    """旧报告用下划线和中文括号包住自动占位句，不能当成人工结论。"""
    candidate = (text or "").strip()
    candidate = re.sub(r"^[_（）()\s]+|[_（）()\s]+$", "", candidate).strip()
    return candidate == _AUTO_PLACEHOLDER


def extract_hand_sections(text: str) -> str:
    blocks = re.findall(re.escape(START) + r"\s*(.*?)\s*" + re.escape(END), text or "", re.S)
    return "\n\n".join(block.strip() for block in blocks)


def wrap_hand_section(body: str) -> str:
    body = (body or "").strip() or PLACEHOLDER
    return f"{START}\n{body}\n{END}"


def preserve_hand_section(old: str | None, new: str, *, migrate_apifox: bool = False) -> str:
    body = extract_hand_sections(old or "")
    if _is_auto_placeholder(body):
        body = ""
    if not body and migrate_apifox and old:
        match = re.search(r"## 3\. 已修复\s*(.*?)(?=\n## |\Z)", old, re.S)
        candidate = (match.group(1) if match else "").strip()
        if not _is_auto_placeholder(candidate):
            body = candidate
    section = wrap_hand_section(body)
    if "<!-- platform-hand:start -->" in new:
        return re.sub(re.escape(START) + r".*?" + re.escape(END), section, new, count=1, flags=re.S)
    return new.rstrip() + "\n\n" + section + "\n"
