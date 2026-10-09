# -*- coding: utf-8 -*-
"""Static identity-pool gap analysis shared by the CLI and read-only panel."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ISOLATED_ROLE_RE = re.compile(r"(?:^|_)(?:isolated|isolation)(?:_|$)|isolated", re.I)



def _literal(node):
    return node.value if isinstance(node, ast.Constant) else None


def _dict_values(node):
    if not isinstance(node, ast.Dict):
        return {}
    return {_literal(key): value for key, value in zip(node.keys, node.values) if _literal(key) is not None}


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _append_context(item: dict, *, case: str | None = None, suite: str | None = None) -> None:
    if case:
        item.setdefault("cases", set()).add(str(case))
    if suite:
        item.setdefault("suites", set()).add(str(suite))


def _pack_notes(requirements: dict) -> dict:
    notes = requirements.get("notes")
    return notes if isinstance(notes, dict) else {}


def _audit_table(requirements, name):
    value = _pack_notes(requirements).get(name)
    return value if isinstance(value, dict) else {}


def _metadata_override(requirements: dict, section: str, name: str) -> dict:
    table = _pack_notes(requirements).get(section)
    value = table.get(name) if isinstance(table, dict) else None
    if not isinstance(value, dict) and isinstance(table, dict):
        matches = [(prefix, candidate) for prefix, candidate in table.items()
                   if isinstance(prefix, str) and name.startswith(prefix + ".") and isinstance(candidate, dict)]
        if matches:
            value = max(matches, key=lambda item: len(item[0]))[1]
    return value if isinstance(value, dict) else {}


def _named_requirement(table, name):
    if not isinstance(table, dict):
        return []
    value = table.get(name)
    if value is None:
        matches = [(prefix, candidate) for prefix, candidate in table.items()
                   if isinstance(prefix, str) and name.startswith(prefix + ".")]
        if matches:
            value = max(matches, key=lambda item: len(item[0]))[1]
    return value or []


def _role_metadata(category: str, role: str, requirements: dict) -> dict:
    override = _metadata_override(requirements, "role_meta", role)
    category_label = _audit_table(requirements, "category_labels").get(category, category or "身份")
    label = str(override.get("title") or role or "未命名角色")
    purpose = str(override.get("purpose") or "使用位置待配置")
    needs = override.get("needs") if isinstance(override.get("needs"), dict) else {}
    fields = list(needs.get("fields") or [])
    refs = list(needs.get("refs") or [])
    preconditions = list(needs.get("preconditions") or [])
    if not preconditions and (purpose == "使用位置待配置" or not fields and not refs):
        preconditions = ["使用位置待配置"]
    return {
        "title": f"{category_label} · {label}",
        "purpose": purpose,
        "needs": {"fields": fields, "refs": refs, "preconditions": preconditions},
    }


def describe_role(category: str, role: str, requirements: dict) -> dict:
    """Return the shared title, purpose, and requirements for a role gap."""
    return _role_metadata(category, role, requirements)


def _fixture_title(name: str, requirements: dict) -> str:
    value = str(name or "")
    parts = value.split(".")
    group = parts[0] if parts else ""
    leaf = parts[-1] if parts else ""
    prefix_labels = _audit_table(requirements, "scenario_prefix_labels")
    group_labels = _audit_table(requirements, "scenario_group_labels")
    subject_labels = _audit_table(requirements, "fixture_subject_labels")
    group_label = next((label for prefix, label in sorted(prefix_labels.items(), key=lambda item: -len(item[0]))
                        if value.startswith(prefix + ".")), group_labels.get(group, group or "业务"))
    if leaf == "auth_probe":
        return f"{group_label} · 鉴权拒绝探针"
    if leaf.startswith("invalid_"):
        subject = subject_labels.get(leaf[len("invalid_"):], leaf[len("invalid_"):])
        return f"{group_label} · 无效{subject}"
    if leaf.startswith("missing_"):
        subject = subject_labels.get(leaf[len("missing_"):], leaf[len("missing_"):])
        return f"{group_label} · 缺失{subject}"
    subject = leaf.removeprefix("nonexistent_").removeprefix("nonexistent")
    subject = subject_labels.get(subject, subject or "数据")
    return f"{group_label} · 不存在的{subject}"


def _scene_metadata(name: str, collection: str, requirements: dict) -> dict:
    override = _metadata_override(requirements, "scenario_meta", name)
    fields_by_name = requirements.get("scenario_fields") or {}
    refs_by_name = requirements.get("scenario_refs") or {}
    fields = list(_named_requirement(fields_by_name, name))
    refs = list(_named_requirement(refs_by_name, name))
    override_needs = override.get("needs") if isinstance(override.get("needs"), dict) else {}
    fields = list(override_needs.get("fields") or fields)
    refs = list(override_needs.get("refs") or refs)
    preconditions = list(override_needs.get("preconditions") or override.get("preconditions") or [])
    if collection == "fixture":
        title = override.get("title") or _fixture_title(name, requirements)
        purpose = override.get("purpose") or "SAFE 负向查询夹具"
        if not fields:
            preconditions = preconditions or ["按 pack.identity_requirements.scenario_fields 声明查询字段；夹具不得引用登录身份。"]
        else:
            preconditions = preconditions or ["只用于负向/无效查询；不得充当真实身份或正向样本。"]
        refs = []
    else:
        group, _, leaf = name.partition(".")
        group_label = _audit_table(requirements, "scenario_group_labels").get(group, "业务")
        title = override.get("title") or f"{group_label}场景 · {leaf or name}"
        purpose = override.get("purpose") or "使用位置待配置"
        if not preconditions and purpose == "使用位置待配置":
            preconditions = ["使用位置待配置"]
        elif not preconditions:
            preconditions = ["核验样本仍存活，且满足对应场景前置条件。"]
    suite = str(override.get("suite") or "")
    return {
        "title": str(title or "使用位置待配置"),
        "purpose": str(purpose or "使用位置待配置"),
        "suite": suite,
        "cases": list(override.get("cases") or []),
        "needs": {"fields": fields, "refs": refs, "preconditions": preconditions},
    }


def describe_pool_entry(name: str, collection: str, requirements: dict) -> dict:
    """Return the shared title, purpose, and requirements used by audit and panel."""
    return _scene_metadata(name, collection, requirements)


def _needs_text(needs: dict) -> str:
    needs = needs if isinstance(needs, dict) else {}
    sections = []
    fields = [f"{str(field)} (`{field}`)" for field in needs.get("fields") or []]
    refs = [f"{str(ref)} (`{ref}`)" for ref in needs.get("refs") or []]
    preconditions = list(needs.get("preconditions") or [])
    if fields:
        sections.append("必填字段：" + "、".join(fields))
    if refs:
        sections.append("身份引用：" + "、".join(refs))
    if preconditions:
        sections.append("前置条件：" + "；".join(str(item) for item in preconditions))
    return "；".join(sections) or "使用位置待配置"


def scan_code(repo_root: Path, pack_root: Path | None = None) -> dict:
    """Find literal role and fixture declarations without importing test modules."""
    roots = [repo_root / "test-platform"]
    if pack_root and pack_root.exists():
        roots.append(pack_root)
    module_metadata = {}
    pack = {}
    if pack_root:
        try:
            pack = json.loads((pack_root / "pack.json").read_text(encoding="utf-8"))
            module_metadata = pack.get("module_metadata") or {}
            for relative in pack.get('case_dirs', []):
                candidate = (repo_root / relative).resolve()
                if candidate.is_relative_to(repo_root.resolve()):
                    roots.append(candidate)
        except (OSError, ValueError):
            module_metadata = {}
    files = sorted({p for base in roots if base.exists() for p in base.rglob("*.py")})
    roles: dict[tuple[str, str], dict] = {}
    scenarios: dict[str, set[str]] = defaultdict(set)
    scenario_context: dict[str, dict] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        source = _relative(path, repo_root)
        module_meta = module_metadata.get(path.stem) if isinstance(module_metadata, dict) else None
        suite = str(module_meta.get("label") or path.stem) if isinstance(module_meta, dict) else path.stem
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node.func)
                if name == "cli_user" and node.args:
                    role = _literal(node.args[0])
                    if isinstance(role, str):
                        category = pack.get('config_defaults', {}).get('PLATFORM_DEFAULT_CLI_CATEGORY') or pack.get('identity_alias_policy', {}).get('category')
                        if category:
                            _add_role(roles, category, role, source, "cli_user", suite=suite)
                elif name == "get_identity" and len(node.args) >= 2:
                    category, role = _literal(node.args[0]), _literal(node.args[1])
                    if isinstance(category, str) and isinstance(role, str):
                        _add_role(roles, category, role, source, "get_identity", suite=suite)
            if not isinstance(node, ast.Dict):
                continue
            values = _dict_values(node)
            case_id = _literal(values.get("id"))
            case_id = str(case_id) if isinstance(case_id, (str, int)) else None
            scenario = _literal(values.get("fixture_scenario"))
            if isinstance(scenario, str) and scenario:
                scenarios[scenario].add(source)
                context = scenario_context.setdefault(scenario, {"cases": set(), "suites": set()})
                _append_context(context, case=case_id, suite=suite)
            fixture_roles = values.get("fixture_roles")
            if isinstance(fixture_roles, (ast.List, ast.Tuple, ast.Set)):
                for item in fixture_roles.elts:
                    fields = _dict_values(item)
                    category = _literal(fields.get("category"))
                    role = _literal(fields.get("role"))
                    if isinstance(category, str) and isinstance(role, str):
                        _add_role(roles, category, role, source, "fixture_roles", case=case_id, suite=suite)
    return {
        "roles": roles,
        "scenarios": {name: sorted(paths) for name, paths in scenarios.items()},
        "scenario_context": {
            name: {key: sorted(value) for key, value in context.items()}
            for name, context in scenario_context.items()
        },
        "files_scanned": len(files),
    }


def _add_role(target, category, role, source, source_kind, *, case=None, suite=None):
    key = (category, role)
    item = target.setdefault(key, {"category": category, "role": role, "sources": set(), "source_kinds": set(),
                                   "cases": set(), "suites": set()})
    item["sources"].add(source)
    item["source_kinds"].add(source_kind)
    _append_context(item, case=case, suite=suite)


def _roles_in_pool(pool):
    present = set()
    for row in pool.get("identities", []):
        if not isinstance(row, dict):
            continue
        category = str(row.get("category") or "")
        if row.get("role"):
            present.add((category, str(row["role"])))
        for role in row.get("roles") or []:
            if role:
                present.add((category, str(role)))
    return present


_FIXTURE_FACTORIES = {}

def register_fixture_factory(pack_id, factory):
    if not isinstance(pack_id, str) or not pack_id or not callable(factory):
        raise TypeError('fixture factory 需要明确 Pack id 与可调用实现')
    if pack_id in _FIXTURE_FACTORIES and _FIXTURE_FACTORIES[pack_id] is not factory:
        raise RuntimeError('当前 Pack 的 fixture factory 已注册')
    _FIXTURE_FACTORIES[pack_id] = factory

def is_negative_fixture(name: str, requirements=None) -> bool:
    pattern = (requirements or {}).get('notes', {}).get('fixture_name_pattern')
    return bool(pattern and re.search(pattern, str(name or ''), re.I))


def _identity_index(pool):
    from identity_pool import identity_active

    return {str(row.get("id")): row for row in pool.get("identities", [])
            if isinstance(row, dict) and row.get("id") not in (None, "") and identity_active(row)}


def _satisfies_distinct_roles(category, role, rows, requirements):
    from identity_pool import IdentityUnavailable, distinct_role_ready, select_role

    pool = {"identities": rows}
    try:
        return select_role(pool, category, role) is not None and distinct_role_ready(pool, category, role, requirements)
    except IdentityUnavailable:
        return False


def build_audit(*, pack_id: str, pack: dict, pool: dict, code_scan: dict, pool_file: str) -> dict:
    from identity_pool import identity_active

    requirements = pack.get("identity_requirements") or {}
    present_roles = _roles_in_pool(pool)
    required_roles = {}
    from resource_pool_config import identity_categories
    for category in identity_categories(pack):
        for role in requirements.get(category) or []:
            _add_role(required_roles, category, str(role), str(Path("packs") / pack_id / "pack.json"), "identity_requirements")
    for key, value in code_scan.get("roles", {}).items():
        for source in value.get("sources", ()):
            for kind in value.get("source_kinds", ()):
                target = required_roles.setdefault(key, {"category": key[0], "role": key[1], "sources": set(),
                                                          "source_kinds": set(), "cases": set(), "suites": set()})
                target["sources"].add(source)
                target["source_kinds"].add(kind)
                target["cases"].update(value.get("cases", ()))
                target["suites"].update(value.get("suites", ()))

    missing_roles = []
    identity_rows = [row for row in pool.get("identities", []) if isinstance(row, dict)]
    for key in sorted(required_roles):
        category, role = key
        value = required_roles[key]
        key_is_present = key in present_roles and _satisfies_distinct_roles(
            category, role, identity_rows, requirements,
        )
        if key_is_present:
            continue
        action = _metadata_override(requirements, "role_meta", role).get("suggestion")
        if not action and ISOLATED_ROLE_RE.search(role):
            action = "先查池，再只读 SELECT 按用例前置条件核验身份；候选写池须用户确认。无合格候选时保持 incomplete，禁止伪造。"

        if not action:
            action = "先查池，再只读 SELECT 核验真实身份和所需字段；候选写池须用户确认，无法唯一核验时保持 incomplete。"
        meta = _role_metadata(category, role, requirements)
        missing_roles.append({
            "category": category, "role": role,
            **meta,
            "sources": sorted(value["sources"]),
            "source_kinds": sorted(value["source_kinds"]),
            "cases": sorted(value.get("cases", ())),
            "suites": sorted(value.get("suites", ())),
            "suggestion": action,
        })

    scenario_names = [row.get("name") for row in pool.get("scenarios", []) if isinstance(row, dict) and row.get("name")]
    fixture_names = [row.get("name") for row in pool.get("fixtures", []) if isinstance(row, dict) and row.get("name")]
    known_names = {row["name"] for collection in ("scenarios", "fixtures")
                   for row in pool.get(collection, [])
                   if isinstance(row, dict) and row.get("name") and identity_active(row)}
    requested_names = set(requirements.get("scenario") or []) | set(code_scan.get("scenarios", {}))
    missing_scenarios, missing_fixtures = [], []
    for name in sorted(requested_names - known_names):
        context = code_scan.get("scenario_context", {}).get(name, {})
        metadata = _scene_metadata(name, "fixture" if is_negative_fixture(name, requirements) else "scenario", requirements)
        item = {
            "name": name,
            **metadata,
            "sources": code_scan.get("scenarios", {}).get(name, []),
            "cases": sorted(set(context.get("cases") or []) | set(metadata.get("cases") or [])),
            "suites": sorted(set(context.get("suites") or []) | ({_scene_metadata(name, "scenario", requirements).get("suite")} if _scene_metadata(name, "scenario", requirements).get("suite") else set())),
            "fields": list((requirements.get("scenario_fields") or {}).get(name) or []),
        }
        if is_negative_fixture(name, requirements):
            item["suggestion"] = "先查池和 audit；按 pack.scenario_fields 生成稳定哨兵并登记到 fixtures。先 dry-run，实际写池须用户确认。" if item["fields"] else "先查池和 audit；补齐 pack.scenario_fields 后再建立负向 fixture，写池须用户确认。"
            missing_fixtures.append(item)
        else:
            item["suggestion"] = "先查池和 audit，再只读 SELECT 测试库核验唯一、存活的正向样本；候选写入 scenario.identity_refs + data 须用户确认。查无候选或无法核验时才保持 incomplete。"
            missing_scenarios.append(item)

    identity_index = _identity_index(pool)
    entries = {}
    for collection in ("scenarios", "fixtures"):
        for row in pool.get(collection, []):
            if isinstance(row, dict) and row.get("name"):
                entries.setdefault(row["name"], []).append((collection, row))
    incomplete = []
    for name, rows in sorted(entries.items()):
        for collection, row in rows:
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            needs = _scene_metadata(name, collection.rstrip("s"), requirements)["needs"]
            missing_fields = [field for field in needs["fields"] if data.get(field) in (None, "", [])]
            refs = row.get("identity_refs") if isinstance(row.get("identity_refs"), dict) else {}
            missing_refs = []
            for alias in needs["refs"]:
                value = refs.get(alias)
                ids = value if isinstance(value, list) else [value]
                if not ids or any(not identity_id or str(identity_id) not in identity_index for identity_id in ids):
                    missing_refs.append(alias)
            unavailable = not identity_active(row)
            if missing_fields or missing_refs or unavailable:
                context = code_scan.get("scenario_context", {}).get(name, {})
                incomplete.append({"name": name, "collection": collection,
                                   **_scene_metadata(name, collection.rstrip("s"), requirements),
                                   "missing_fields": missing_fields, "missing_refs": missing_refs,
                                   "status": row.get("status") or "active", "unavailable": unavailable,
                                   "sources": code_scan.get("scenarios", {}).get(name, []),
                                   "cases": list(context.get("cases") or []),
                                   "suites": sorted(set(context.get("suites") or [])),
                                   "suggestion": ("停用条目不能参与执行；先核对停用原因和用途，明确启用须用户确认。"
                                                  if unavailable else "先查池和 audit，再只读 SELECT 核验缺失字段与身份关系；候选写池须用户确认。无法核验或没有候选时保持 incomplete。")})

    name_counts = Counter(scenario_names + fixture_names)
    duplicate_names = []
    for name, count in sorted(name_counts.items()):
        if count <= 1:
            continue
        context = code_scan.get("scenario_context", {}).get(name, {})
        collection = "fixture" if all(kind == "fixtures" for kind, _ in entries.get(name, [])) else "scenario"
        duplicate_names.append({
            "name": name, "count": count,
            "collections": [kind for kind, row in entries.get(name, [])],
            **_scene_metadata(name, collection, requirements),
            "sources": code_scan.get("scenarios", {}).get(name, []),
            "cases": list(context.get("cases") or []),
            "suites": sorted(set(context.get("suites") or [])),
        })
    findings = []
    for row in missing_roles:
        findings.append({"type": "role", "name": "%s.%s" % (row["category"], row["role"]), **row})
    for row in missing_scenarios:
        findings.append({"type": "scenario", **row})
    for row in missing_fixtures:
        findings.append({"type": "fixture", **row})
    for row in incomplete:
        findings.append({"type": "incomplete_entry", "name": row["name"], **row})
    for row in duplicate_names:
        findings.append({"type": "duplicate_name", "suggestion": "为每个样本赋唯一 name，并同步 fixture_scenario 引用；先核验各样本用途，修改池前须用户确认。", **row})

    return {
        "pack_id": pack_id,
        "pool_file": pool_file,
        "summary": {
            "missing_roles": len(missing_roles),
            "missing_scenarios": len(missing_scenarios),
            "missing_fixtures": len(missing_fixtures),
            "incomplete_entries": len(incomplete),
            "duplicate_names": len(duplicate_names),
            "files_scanned": code_scan.get("files_scanned", 0),
        },
        "missing_roles": missing_roles,
        "missing_scenarios": missing_scenarios,
        "missing_fixtures": missing_fixtures,
        "incomplete_entries": incomplete,
        "duplicate_names": duplicate_names,
        "findings": findings,
    }




def plan_safe_changes(*, pack: dict, pool: dict, audit: dict, code_scan: dict) -> dict:
    """Build a strictly bounded plan: eligible cli_user aliases and clear negative fixtures."""
    changes = {"append_roles": [], "add_fixtures": []}
    rows = [row for row in pool.get("identities", []) if isinstance(row, dict)]
    alias = pack.get('identity_alias_policy') or {}
    default_clients = [row for row in rows if alias.get('category') and alias.get('role')
                       and row.get("category") == alias['category'] and
                       (row.get("role") == alias['role'] or alias['role'] in (row.get("roles") or [])) and
                       (row.get("username") or row.get("login_name") or row.get("email")) and
                       (row.get("password") or (pool.get("settings") or {}).get("default_password")) and
                       row.get("status") not in ("disabled", "inactive", "deleted")]
    default_client = default_clients[0] if len(default_clients) == 1 else None
    role_sources = code_scan.get("roles", {})
    for gap in audit.get("missing_roles", []):
        key = (gap["category"], gap["role"])
        source = role_sources.get(key, {})
        role = gap["role"]
        if (default_client and gap["category"] == alias.get("category") and alias.get("source_kind") in source.get("source_kinds", ())
                and not ISOLATED_ROLE_RE.search(role)):
            changes["append_roles"].append({"identity_id": default_client.get("id"), "role": role})

    requirement = pack.get("identity_requirements") or {}
    existing = {row.get("name") for row in pool.get("scenarios", []) + pool.get("fixtures", []) if isinstance(row, dict)}
    for gap in audit.get("missing_fixtures", []):
        name = gap["name"]
        fields = (requirement.get("scenario_fields") or {}).get(name) or []
        if not is_negative_fixture(name, requirement) or not fields or name in existing:
            continue
        if not all(isinstance(field, str) and field for field in fields):
            continue
        stable_id = "fixture-%s" % hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
        data = (requirement.get('fixture_templates') or {}).get(name)
        if data is None:
            factory = _FIXTURE_FACTORIES.get(pack.get('id'))
            if factory is not None:
                data = factory(name, fields)
        if not isinstance(data, dict) or any(field not in data for field in fields):
            continue
        changes["add_fixtures"].append({"id": stable_id, "name": name, "data": data})
    return changes


def apply_changes(pool: dict, changes: dict) -> tuple[dict, list[dict]]:
    """Apply only the operations encoded by plan_safe_changes; preserve all other data."""
    updated = json.loads(json.dumps(pool, ensure_ascii=False))
    applied = []
    identities = [row for row in updated.get("identities", []) if isinstance(row, dict)]
    for change in changes.get("append_roles", []):
        identity = next((row for row in identities if row.get("id") == change.get("identity_id")), None)
        if not identity:
            continue
        roles = list(identity.get("roles") or ([identity["role"]] if identity.get("role") else []))
        if change.get("role") not in roles:
            roles.append(change["role"])
            identity["roles"] = roles
            applied.append({"type": "append_role", "identity_id": identity.get("id"), "role": change["role"]})
    fixtures = updated.setdefault("fixtures", [])
    existing_names = {row.get("name") for row in fixtures + updated.get("scenarios", []) if isinstance(row, dict)}
    existing_ids = {row.get("id") for row in fixtures if isinstance(row, dict)}
    for fixture in changes.get("add_fixtures", []):
        if fixture.get("name") in existing_names or fixture.get("id") in existing_ids:
            continue
        fixtures.append(json.loads(json.dumps(fixture, ensure_ascii=False)))
        existing_names.add(fixture.get("name"))
        existing_ids.add(fixture.get("id"))
        applied.append({"type": "add_fixture", "name": fixture.get("name"), "fields": sorted((fixture.get("data") or {}).keys())})
    return updated, applied


def audit_pack(pack_id: str | None = None, *, pool: dict | None = None) -> dict:
    """Audit the selected registered pack without returning identity values."""
    from pack_registry import load_packs
    import identity_pool

    repo_root = Path(__file__).resolve().parents[1]
    packs = list(load_packs())
    selected = next((item for item in packs if pack_id and item.get("id") == pack_id), None)
    if selected is None:
        if len(packs) != 1:
            raise RuntimeError("账号池审计需要唯一的所选 pack")
        selected = packs[0]
    selected_id = str(selected.get("id") or pack_id or "")
    pack_root = Path(selected["root"]).resolve()
    pack_path = pack_root / "pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    if pool is None:
        pool = identity_pool.load_pool(pack_id=selected_id)
    relative_pool = str(selected.get("identity_pool_file") or "data/identity_pool.json")
    pool_file = _relative(pack_root / relative_pool, repo_root)
    scan = scan_code(repo_root, pack_root)
    return build_audit(pack_id=selected_id, pack=pack, pool=pool, code_scan=scan, pool_file=pool_file)


def audit_root(repo_root: Path, pack_id: str, *, pool: dict | None = None) -> tuple[dict, dict, dict, dict]:
    """Filesystem-backed entry point used by tests and the CLI wrapper."""
    repo_root = Path(repo_root)
    pack_root = repo_root / "packs" / pack_id
    pack = json.loads((pack_root / "pack.json").read_text(encoding="utf-8"))
    pool_path = pack_root / str(pack.get("identity_pool_file") or "data/identity_pool.json")
    if pool is None:
        if pool_path.exists():
            pool = json.loads(pool_path.read_text(encoding="utf-8"))
        else:
            example = pool_path.with_name(pool_path.stem + ".example" + pool_path.suffix)
            pool = json.loads(example.read_text(encoding="utf-8")) if example.exists() else {"identities": [], "scenarios": [], "fixtures": []}
    scan = scan_code(repo_root, pack_root)
    audit = build_audit(pack_id=pack_id, pack=pack, pool=pool,
                        code_scan=scan, pool_file=_relative(pool_path, repo_root))
    return audit, pack, pool, scan
