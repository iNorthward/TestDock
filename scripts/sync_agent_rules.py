#!/usr/bin/env python3
"""Cursor / Claude Code 共享 Agent 规则 — 双向同步。

唯一维护源：agent-rules/*.md（统一 frontmatter）
生成物（勿手改，改 agent-rules 或在 Cursor/Claude 侧改完后跑 sync）：
  - .cursor/rules/*.mdc
  - .claude/rules/*.md

用法：
  python3 scripts/sync_agent_rules.py              # 合并三处差异 → 写回 canonical → 生成两端
  python3 scripts/sync_agent_rules.py --check        # 本地一致性检查：未同步则 exit 1
  python3 scripts/sync_agent_rules.py --prefer cursor  # 冲突时以 Cursor 为准
  python3 scripts/sync_agent_rules.py --prefer claude
  python3 scripts/sync_agent_rules.py --prefer src   # 冲突时以 agent-rules 为准（默认）

在 Cursor 或 Claude 里新增/修改规则后，在仓库根目录执行本脚本即可两边对齐。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "agent-rules"
CURSOR_DIR = ROOT / ".cursor" / "rules"
CLAUDE_DIR = ROOT / ".claude" / "rules"
RULE_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class Rule:
    description: str
    always_apply: bool
    paths: tuple[str, ...]
    body: str

    def fingerprint(self) -> str:
        """用于三方合并；不含 description（Claude 生成物不携带该字段）。"""
        payload = (
            self.always_apply,
            self.paths,
            _normalize_body(self.body),
        )
        return hashlib.sha256(repr(payload).encode()).hexdigest()


def _normalize_body(text: str) -> str:
    return text.replace("\r\n", "\n").strip() + "\n"


def _parse_simple_yaml(text: str) -> dict:
    data: dict[str, object] = {}
    key: str | None = None
    list_items: list[str] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and key is not None:
            list_items.append(line[4:].strip().strip('"').strip("'"))
            continue
        if list_items and key is not None:
            data[key] = list_items
            list_items = []
            key = None
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if not v:
            key = k
            list_items = []
            continue
        if v.lower() in ("true", "false"):
            data[k] = v.lower() == "true"
        else:
            data[k] = v.strip('"').strip("'")

    if list_items and key is not None:
        data[key] = list_items
    return data


def _split_paths(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        text = str(value).strip()
        if not text:
            return []
        # 含 {a,b,c} 的 glob 整段保留，勿按逗号拆（如 **/*.{html,css,tsx}）
        if "{" in text and "}" in text:
            items = [text]
        else:
            items = re.split(r",\s*", text)
    out: list[str] = []
    for item in items:
        p = str(item).strip().strip('"').strip("'")
        if p:
            out.append(p)
    return out


def _as_rule(
    *,
    description: str = "",
    always_apply: bool = False,
    paths: list[str] | None = None,
    body: str = "",
) -> Rule:
    paths = paths or []
    # always_apply 且无 paths → 全局规则
    if always_apply:
        paths = []
    return Rule(
        description=description,
        always_apply=always_apply,
        paths=tuple(sorted(set(paths))),
        body=_normalize_body(body),
    )


def parse_src(text: str) -> Rule:
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("agent-rules: missing frontmatter")
    meta = _parse_simple_yaml(m.group(1))
    body = text[m.end() :]
    always = bool(meta.get("always_apply") or meta.get("alwaysApply"))
    paths = _split_paths(meta.get("paths") or meta.get("globs"))
    desc = str(meta.get("description") or "")
    return _as_rule(description=desc, always_apply=always, paths=paths, body=body)


def parse_cursor(text: str) -> Rule:
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("cursor rule: missing frontmatter")
    meta = _parse_simple_yaml(m.group(1))
    body = text[m.end() :]
    always = bool(meta.get("alwaysApply"))
    paths = _split_paths(meta.get("globs"))
    if always:
        paths = []
    desc = str(meta.get("description") or "")
    return _as_rule(description=desc, always_apply=always, paths=paths, body=body)


def parse_claude(text: str) -> Rule:
    m = FRONTMATTER_RE.match(text)
    if m:
        meta = _parse_simple_yaml(m.group(1))
        body = text[m.end() :]
        paths = _split_paths(meta.get("paths"))
        desc = str(meta.get("description") or "")
        return _as_rule(description=desc, always_apply=not paths, paths=paths, body=body)
    return _as_rule(always_apply=True, body=text)


def render_src(rule: Rule) -> str:
    lines = ["---"]
    if rule.description:
        lines.append(f"description: {rule.description}")
    lines.append(f"always_apply: {'true' if rule.always_apply else 'false'}")
    if rule.paths:
        lines.append("paths:")
        for p in rule.paths:
            lines.append(f'  - "{p}"')
    lines.append("---")
    lines.append("")
    lines.append(rule.body.rstrip())
    lines.append("")
    return "\n".join(lines)


def render_cursor(rule: Rule) -> str:
    lines = ["---"]
    if rule.description:
        lines.append(f"description: {rule.description}")
    lines.append(f"alwaysApply: {'true' if rule.always_apply else 'false'}")
    if rule.paths:
        if len(rule.paths) == 1:
            lines.append(f"globs: {rule.paths[0]}")
        else:
            lines.append("globs:")
            for p in rule.paths:
                lines.append(f'  - "{p}"')
    lines.append("---")
    lines.append("")
    lines.append(rule.body.rstrip())
    lines.append("")
    return "\n".join(lines)


def render_claude(rule: Rule) -> str:
    if rule.always_apply or not rule.paths:
        return rule.body
    lines = ["---", "paths:"]
    for p in rule.paths:
        lines.append(f'  - "{p}"')
    lines.append("---")
    lines.append("")
    lines.append(rule.body.rstrip())
    lines.append("")
    return "\n".join(lines)


def _read_rule(path: Path, kind: str) -> Rule | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if kind == "src":
        return parse_src(text)
    if kind == "cursor":
        return parse_cursor(text)
    return parse_claude(text)


def _pick_canonical(
    stem: str,
    src: Rule | None,
    cursor: Rule | None,
    claude: Rule | None,
    prefer: str,
) -> Rule:
    present = {k: v for k, v in (("src", src), ("cursor", cursor), ("claude", claude)) if v}
    if not present:
        raise ValueError(f"{stem}: no rule content")

    fps = {k: v.fingerprint() for k, v in present.items()}
    if len(set(fps.values())) == 1:
        return next(iter(present.values()))

    if len(present) == 1:
        return next(iter(present.values()))

    # 两方一致、第三方不同 → 以「被改的那一方」为准（真正的双向同步）
    keys = list(present.keys())
    for minority in keys:
        others = [k for k in keys if k != minority]
        if len(others) < 2:
            continue
        base_fp = fps[others[0]]
        if all(fps[o] == base_fp for o in others[1:]) and fps[minority] != base_fp:
            print(
                f"imported {stem} from {minority} "
                f"({', '.join(others)} unchanged)",
                file=sys.stderr,
            )
            return present[minority]

    if prefer in present:
        chosen = present[prefer]
        others = [k for k in present if k != prefer and fps[k] != fps[prefer]]
        if others:
            print(
                f"resolved {stem}: --prefer {prefer} "
                f"(conflict with {', '.join(others)})",
                file=sys.stderr,
            )
        return chosen

    names = ", ".join(sorted(present))
    raise SystemExit(
        f"conflict: agent-rules/{stem}.md differs between sources ({names}).\n"
        f"  Fix manually or rerun with --prefer src|cursor|claude"
    )


def _collect_stems() -> set[str]:
    stems: set[str] = set()
    for d, ext in ((SRC_DIR, ".md"), (CURSOR_DIR, ".mdc"), (CLAUDE_DIR, ".md")):
        if d.is_dir():
            for p in d.glob(f"*{ext}"):
                if _is_rule_file(p):
                    stems.add(p.stem)
    return stems


def delete_rule(stem: str) -> list[Path]:
    """Explicitly remove one rule from canonical and both generated locations.

    Ordinary sync is intentionally bidirectional so it can import edits made in
    Cursor or Claude. A missing file therefore cannot also mean deletion.
    """
    if not RULE_STEM_RE.fullmatch(stem) or stem.lower() == "readme":
        raise ValueError("rule name must be a plain file stem (letters, digits, '-' or '_')")

    targets = (
        SRC_DIR / f"{stem}.md",
        CURSOR_DIR / f"{stem}.mdc",
        CLAUDE_DIR / f"{stem}.md",
    )
    present = [path for path in targets if path.exists() or path.is_symlink()]
    for path in present:
        if not path.is_file() and not path.is_symlink():
            raise ValueError(f"refusing to remove non-file rule path: {path}")

    removed = []
    for path in present:
        path.unlink()
        removed.append(path)
    return removed


def _is_rule_file(path: Path) -> bool:
    return path.name.lower() != "readme.md" and not path.name.startswith((".", "_"))


def _bootstrap_src_from_cursor() -> bool:
    """首次迁移：agent-rules 无规则文件时从 .cursor/rules 导入。"""
    if any(p for p in SRC_DIR.glob("*.md") if _is_rule_file(p)):
        return False
    if not CURSOR_DIR.is_dir() or not any(CURSOR_DIR.glob("*.mdc")):
        return False
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    for mdc in sorted(CURSOR_DIR.glob("*.mdc")):
        rule = parse_cursor(mdc.read_text(encoding="utf-8"))
        out = SRC_DIR / f"{mdc.stem}.md"
        out.write_text(render_src(rule), encoding="utf-8")
        print(f"bootstrapped {out.relative_to(ROOT)} from cursor")
    return True


def sync(*, check: bool = False, prefer: str = "src") -> int:
    if not check:
        _bootstrap_src_from_cursor()
        SRC_DIR.mkdir(parents=True, exist_ok=True)
        CURSOR_DIR.mkdir(parents=True, exist_ok=True)
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)

    stems = _collect_stems()
    canonical: dict[str, Rule] = {}
    changed = False

    for stem in sorted(stems):
        src_path = SRC_DIR / f"{stem}.md"
        cursor_path = CURSOR_DIR / f"{stem}.mdc"
        claude_path = CLAUDE_DIR / f"{stem}.md"

        src = _read_rule(src_path, "src") if src_path.exists() else None
        cursor = _read_rule(cursor_path, "cursor") if cursor_path.exists() else None
        claude = _read_rule(claude_path, "claude") if claude_path.exists() else None

        if not any((src, cursor, claude)):
            continue

        try:
            rule = _pick_canonical(stem, src, cursor, claude, prefer)
        except SystemExit as exc:
            if check:
                print(str(exc), file=sys.stderr)
                return 1
            raise

        # description 仅 canonical / Cursor 使用；合并时保留已有说明
        if not rule.description:
            for candidate in (src, cursor):
                if candidate and candidate.description:
                    rule = Rule(
                        description=candidate.description,
                        always_apply=rule.always_apply,
                        paths=rule.paths,
                        body=rule.body,
                    )
                    break

        canonical[stem] = rule

        expected_src = render_src(rule)
        expected_cursor = render_cursor(rule)
        expected_claude = render_claude(rule)

        current_src = src_path.read_text(encoding="utf-8") if src_path.exists() else None
        current_cursor = cursor_path.read_text(encoding="utf-8") if cursor_path.exists() else None
        current_claude = claude_path.read_text(encoding="utf-8") if claude_path.exists() else None

        if (
            current_src != expected_src
            or current_cursor != expected_cursor
            or current_claude != expected_claude
        ):
            changed = True
            if check:
                if current_src != expected_src:
                    print(f"out of sync: {src_path.relative_to(ROOT)}")
                if current_cursor != expected_cursor:
                    print(f"out of sync: {cursor_path.relative_to(ROOT)}")
                if current_claude != expected_claude:
                    print(f"out of sync: {claude_path.relative_to(ROOT)}")
            else:
                if current_src != expected_src:
                    src_path.write_text(expected_src, encoding="utf-8")
                    print(f"wrote {src_path.relative_to(ROOT)}")
                if current_cursor != expected_cursor:
                    cursor_path.write_text(expected_cursor, encoding="utf-8")
                    print(f"wrote {cursor_path.relative_to(ROOT)}")
                if current_claude != expected_claude:
                    claude_path.write_text(expected_claude, encoding="utf-8")
                    print(f"wrote {claude_path.relative_to(ROOT)}")

    # 删除已无 canonical 的生成物
    for cursor_path in sorted(CURSOR_DIR.glob("*.mdc")):
        if not _is_rule_file(cursor_path):
            continue
        if cursor_path.stem not in canonical:
            changed = True
            if check:
                print(f"stale: {cursor_path.relative_to(ROOT)} (no agent-rules/{cursor_path.stem}.md)")
            else:
                cursor_path.unlink()
                print(f"removed stale {cursor_path.relative_to(ROOT)}")

    for claude_path in sorted(CLAUDE_DIR.glob("*.md")):
        if not _is_rule_file(claude_path):
            continue
        if claude_path.stem not in canonical:
            changed = True
            if check:
                print(f"stale: {claude_path.relative_to(ROOT)} (no agent-rules/{claude_path.stem}.md)")
            else:
                claude_path.unlink()
                print(f"removed stale {claude_path.relative_to(ROOT)}")

    if check:
        if changed:
            print("run: python3 scripts/sync_agent_rules.py", file=sys.stderr)
            return 1
        print("agent rules in sync (agent-rules / cursor / claude)")
        return 0

    if not changed:
        print("agent rules already up to date")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check",
        action="store_true",
        help="仅检查三处是否一致（不写文件）",
    )
    actions.add_argument(
        "--delete",
        action="append",
        metavar="RULE",
        help="显式删除指定规则及两端生成物；可重复指定多个规则名",
    )
    parser.add_argument(
        "--prefer",
        choices=("src", "cursor", "claude"),
        default="src",
        help="三方内容冲突时的优先级（默认 agent-rules）",
    )
    args = parser.parse_args()
    if args.delete:
        for stem in args.delete:
            removed = delete_rule(stem)
            if removed:
                for path in removed:
                    print(f"removed {path.relative_to(ROOT)}")
            else:
                print(f"rule {stem} already absent")
        return
    raise SystemExit(sync(check=args.check, prefer=args.prefer))


if __name__ == "__main__":
    main()
