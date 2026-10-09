# -*- coding: utf-8 -*-
"""Read and update the selected pack's local identity and scenario pool.

The core only knows the pool schema. Identity values and role names belong to
the selected pack's data file.
"""
from __future__ import annotations

import json
import os
import tempfile
import hashlib
import fcntl
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import RLock

POOL_VERSION = 1
from resource_pool_config import valid_category, selected_definition
_LOCK = RLock()


class IdentityUnavailable(RuntimeError):
    """A requested pool role or scene is absent; the message is safe to show."""

    def __init__(self, category: str, role: str, *, reason: str | None = None):
        self.category = str(category or "identity")
        self.role = str(role or "unknown")
        self.reason = str(reason or "")
        message = f"账号池缺少 {self.category} 类角色：{self.role}"
        if self.reason:
            message = f"账号池 {self.category} 类角色 {self.role} {self.reason}"
        super().__init__(message)


class PoolConflict(RuntimeError):
    """A stale snapshot must not replace newer local identities or scenes."""


class PoolSnapshot(dict):
    """A dictionary plus private source/version metadata; metadata is never JSON."""


def _json_model_value(value):
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_model_value(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_json_model_value(item) for item in value)
    return value is None or type(value) in (str, int, float, bool)


def validate_pool(data: dict) -> dict:
    """Validate model fields without converting business JSON values."""
    if not isinstance(data, dict):
        raise ValueError("账号池顶层必须是对象")
    pool = deepcopy(data)
    if not _json_model_value(pool):
        raise ValueError("账号池只能保存 JSON 类型与字符串对象键；不能隐式转换")
    pool.setdefault("version", POOL_VERSION)
    if type(pool["version"]) is not int or pool["version"] != POOL_VERSION:
        raise ValueError("账号池 version 不受支持；请显式迁移，不能覆盖版本")
    seen_ids, seen_names = set(), set()
    for collection in ("identities", "scenarios", "fixtures"):
        rows = pool.setdefault(collection, [])
        if not isinstance(rows, list):
            raise ValueError(f"账号池 {collection} 必须是数组")
        for index, row in enumerate(rows):
            location = f"{collection}[{index}]"
            if not isinstance(row, dict):
                raise ValueError(f"账号池 {location} 必须是对象")
            identifier = row.get("id")
            if not isinstance(identifier, str) or not identifier.strip() or identifier != identifier.strip():
                raise ValueError(f"账号池 {location}.id 必须是稳定的非空字符串")
            if identifier in seen_ids:
                raise ValueError(f"账号池 {location}.id 重复")
            seen_ids.add(identifier)
            if row.get("status") is not None and row["status"] not in ("active", "disabled", "inactive", "deleted"):
                raise ValueError(f"账号池 {location}.status 必须是平台身份/场景状态；流程阶段状态请写 stages")
            if collection == "identities":
                if not valid_category(row.get("category")):
                    raise ValueError(f"账号池 {location}.category 无效")
                role, roles = row.get("role"), row.get("roles", [])
                if (role is not None and (not isinstance(role, str) or not role.strip() or role != role.strip())
                        or not isinstance(roles, list)
                        or any(not isinstance(r, str) or not r.strip() or r != r.strip() for r in roles)
                        or len(set(roles)) != len(roles) or not (role or roles)):
                    raise ValueError(f"账号池 {location} 需要有效的 role / roles")
                stages = row.get("stages", {})
                if not isinstance(stages, dict) or any(not isinstance(v, dict) for v in stages.values()):
                    raise ValueError(f"账号池 {location}.stages 必须是阶段名称到对象的映射")
            else:
                name = row.get("name")
                if not isinstance(name, str) or not name.strip() or name != name.strip():
                    raise ValueError(f"账号池 {location}.name 必须是非空字符串")
                if name in seen_names:
                    raise ValueError(f"账号池 {location}.name 与已有场景或夹具冲突")
                seen_names.add(name)
                if not isinstance(row.get("data", {}), dict) or not isinstance(row.get("identity_refs", {}), dict):
                    raise ValueError(f"账号池 {location}.data / identity_refs 必须是对象")
                if collection == "fixtures" and row.get("identity_refs"):
                    raise ValueError(f"账号池 {location} 负向夹具不得引用真实登录身份")
    identity_ids = {row["id"] for row in pool["identities"]}
    for index, scene in enumerate(pool["scenarios"]):
        for alias, value in scene.get("identity_refs", {}).items():
            ids = value if isinstance(value, list) else [value]
            if (not isinstance(alias, str) or not alias.strip() or not ids
                    or any(not isinstance(i, str) or i not in identity_ids for i in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError(f"账号池 scenarios[{index}].identity_refs 引用无效或不唯一")
    if not isinstance(pool.get("settings", {}), dict):
        raise ValueError("账号池 settings 必须是对象")
    password = pool.get("settings", {}).get("default_password")
    if password is not None and not isinstance(password, str):
        raise ValueError("账号池 settings.default_password 必须是字符串")
    try:
        json.dumps(pool, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("账号池只能保存有限数值与有效 JSON 类型") from exc
    return pool


def identity_active(row: dict) -> bool:
    return row.get("status") not in ("disabled", "inactive", "deleted")


def select_role(pool: dict, category: str, role: str):
    """An explicit primary wins; multiple candidates never silently pick first."""
    rows = [r for r in pool.get("identities", []) if identity_active(r) and r.get("category") == category]
    primary = [r for r in rows if r.get("role") == role]
    matches = primary or [r for r in rows if _has_role(r, role)]
    if len(matches) > 1:
        raise IdentityUnavailable(category, role, reason="匹配不唯一；请声明唯一主角色或使用场景身份引用")
    return matches[0] if matches else None


def distinct_role_ready(pool: dict, category: str, role: str, required: dict) -> bool:
    for rule in required.get("distinct_roles", []):
        if rule.get("category") != category or rule.get("role") != role:
            continue
        if rule.get("unique") and sum(1 for row in pool["identities"] if identity_active(row)
                                      and row.get("category") == category and _has_role(row, role)) != 1:
            return False
        own = select_role(pool, category, role)
        other = select_role(pool, rule.get("other_category", category), rule["other_role"])
        if not own or not other:
            return False
        for field in rule.get("fields", []):
            if own.get(field) in (None, "") or other.get(field) in (None, "") or str(own[field]) == str(other[field]):
                return False
    return True


def _signature(path: Path):
    template = path.with_name(f"{path.stem}.example{path.suffix}")
    source = path if path.is_file() else template
    return hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None


@contextmanager
def _write_lock(path: Path):
    """Serialize the panel and local CLIs around the same atomic file update."""
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_name(f".{path.name}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def empty_pool() -> dict:
    return {"version": POOL_VERSION, "identities": [], "scenarios": [], "fixtures": []}


def pool_path(pack_id: str | None = None) -> Path:
    """Resolve a pool path strictly inside a selected pack's declared data dir."""
    from pack_registry import load_packs

    packs = list(load_packs())
    selected = next((p for p in packs if pack_id and p.get("id") == pack_id), None)
    if pack_id and selected is None:
        raise ValueError("请求的账号池不属于当前项目 Pack；请使用该项目的独立进程")
    if selected is None:
        if len(packs) != 1:
            raise RuntimeError("账号池读取需要唯一的所选 pack")
        selected = packs[0]
    relative = str(selected.get("identity_pool_file") or "data/identity_pool.json")
    root = Path(selected["root"]).resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("pack identity_pool_file 必须位于 pack 数据目录内") from exc
    return target


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("账号池 JSON 存在重复对象键")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("账号池不允许 NaN 或 Infinity")


def _read_file(path: Path) -> dict:
    if not path.is_file():
        # A sanitized, tracked template provides shared identities and fixture
        # structure on fresh checkouts. The local file always wins when
        # present; each Pack declares its own version-control policy.
        template = path.with_name(f"{path.stem}.example{path.suffix}")
        if not template.is_file():
            return empty_pool()
        path = template
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant,
                          object_pairs_hook=_unique_json_object)
    except (OSError, ValueError) as exc:
        raise RuntimeError("账号池文件无法读取或不是有效 JSON") from exc
    return validate_pool(data)


def load_pool(*, pack_id: str | None = None) -> dict:
    with _LOCK:
        path = pool_path(pack_id)
        # Hash and read the same content, even if a CLI replaces it concurrently.
        for _ in range(3):
            signature = _signature(path)
            pool = PoolSnapshot(_read_file(path))
            if signature == _signature(path):
                pool.source_path = path.resolve()
                pool.signature = signature
                return pool
        raise PoolConflict("账号池正在更新，请重新读取后重试")


def default_password(*, pack_id: str | None = None) -> str | None:
    """Return the selected pack's configured business-account password, if any."""
    settings = load_pool(pack_id=pack_id).get("settings")
    if not isinstance(settings, dict):
        return None
    value = settings.get("default_password")
    return value if isinstance(value, str) and value.strip() else None


def identity_with_password(identity: dict, *, pack_id: str | None = None) -> dict:
    """Copy a business identity and resolve its explicit or pack-default password."""
    if not isinstance(identity, dict) or not identity_active(identity):
        raise IdentityUnavailable("identity", "login_identity")
    resolved = deepcopy(identity)
    password = resolved.get("password")
    if not isinstance(password, str) or not password.strip():
        password = None
    if not password and resolved.get("category") in requirements(pack_id)["password_default_categories"]:
        password = default_password(pack_id=pack_id)
        if password:
            resolved["password"] = password
    if not password:
        roles = resolved.get("roles") or [resolved.get("role") or "login_identity"]
        raise IdentityUnavailable(
            resolved.get("category") or "identity", roles[0],
            reason="缺少 password（可用池默认密码），无法登录",
        )
    return resolved


def _save_file(pool: dict, path: Path) -> None:
    encoded = json.dumps(validate_pool(pool), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".identity-pool-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def save_pool(data: dict, *, pack_id: str | None = None) -> None:
    pool = validate_pool(data)
    path = pool_path(pack_id)
    with _write_lock(path):
        if isinstance(data, PoolSnapshot) and (data.source_path != path.resolve() or data.signature != _signature(path)):
            raise PoolConflict("账号池快照已过期或属于其他项目，请重新读取；未覆盖新数据")
        _save_file(pool, path)


def view_snapshot(source: dict, view: dict) -> dict:
    """Preserve optimistic concurrency metadata in legacy stage/scenario views."""
    if not isinstance(source, PoolSnapshot):
        return view
    snapshot = PoolSnapshot(view)
    snapshot.source_path, snapshot.signature = source.source_path, source.signature
    return snapshot


def update_pool(mutator, *, pack_id: str | None = None, expected: dict | None = None):
    """Read, mutate, validate and replace under one process/thread lock."""
    path = pool_path(pack_id)
    with _write_lock(path):
        if isinstance(expected, PoolSnapshot) and (expected.source_path != path.resolve() or expected.signature != _signature(path)):
            raise PoolConflict("账号池视图已过期，请重新读取；未覆盖新数据")
        pool = _read_file(path)
        result = mutator(pool)
        _save_file(pool, path)
        return deepcopy(result)


def _has_role(record: dict, role: str) -> bool:
    roles = record.get("roles")
    return record.get("role") == role or isinstance(roles, list) and role in roles


def get_identity(category: str, role: str, *, required: bool = False, pack_id: str | None = None):
    """Return the role's primary identity before any shared role alias."""
    category, role = str(category or ""), str(role or "")
    pool = load_pool(pack_id=pack_id)
    match = select_role(pool, category, role)
    if match is not None and not distinct_role_ready(pool, category, role, requirements(pack_id)):
        raise IdentityUnavailable(category, role, reason="不满足 Pack 声明的角色隔离关系")
    if match is None and required:
        raise IdentityUnavailable(category, role)
    return deepcopy(match) if match is not None else None


def find_identity(*, username=None, uid=None, user_id=None, identity_id=None,
                  category=None, role=None, pack_id: str | None = None):
    """Every supplied selector must match the same active pool identity."""
    selectors = {k: v for k, v in {"id": identity_id, "uid": uid, "user_id": user_id,
                                   "username": username}.items() if v is not None and v != ""}
    if not selectors:
        return None
    if any(type(v) not in (str, int) for v in selectors.values()):
        raise IdentityUnavailable(category or "identity", role or "lookup", reason="查询条件类型无效")
    matches = []
    for row in load_pool(pack_id=pack_id)["identities"]:
        if not identity_active(row) or (category and row.get("category") != category) or (role and not _has_role(row, role)):
            continue
        if all(
            str(value) in {str(row[k]) for k in ("username", "email", "login_name", "uid") if row.get(k) is not None}
            if field == "username" else row.get(field) is not None and str(row[field]) == str(value)
            for field, value in selectors.items()
        ):
            matches.append(row)
    if len(matches) > 1:
        raise IdentityUnavailable(category or "identity", role or "lookup", reason="身份查询匹配不唯一；请限定 category 或身份 id")
    return deepcopy(matches[0]) if matches else None


def get_scenario(name: str, *, required: bool = False, pack_id: str | None = None):
    name = str(name or "")
    pool = load_pool(pack_id=pack_id)
    # Keep existing case fixture_scenario names working after their records are
    # classified into the dedicated fixture collection.
    matches = [
        row for row in [*pool["scenarios"], *pool["fixtures"]]
        if isinstance(row, dict) and row.get("name") == name and identity_active(row)
    ]
    if len(matches) > 1:
        raise IdentityUnavailable("scenario", name, reason="场景与夹具名称不唯一")
    match = matches[0] if matches else None
    if match is None and required:
        raise IdentityUnavailable("scenario", name)
    return deepcopy(match) if match else None


def scenario_value(name: str, field: str, *, required: bool = False, pack_id: str | None = None):
    scene = get_scenario(name, required=required, pack_id=pack_id)
    if scene is None:
        return None
    data = scene.get("data") if isinstance(scene.get("data"), dict) else {}
    value = data.get(field)
    if value in (None, "", []) and required:
        raise IdentityUnavailable("scenario", name, reason=f"缺少必需字段 {field}")
    return value


def _ref_ids(value) -> list:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return [item for item in value if item not in (None, "", [])]
    return [value]


def ensure_scenario_ready(name: str, *, pack_id: str | None = None) -> dict:
    """Require a declared scene and all fields/references that its pack needs."""
    pool = load_pool(pack_id=pack_id)
    scene = next((row for row in [*pool["scenarios"], *pool["fixtures"]]
                  if row.get("name") == name and identity_active(row)), None)
    if scene is None:
        raise IdentityUnavailable("scenario", name)
    required = requirements(pack_id)
    from identity_pool_audit_core import describe_pool_entry

    needs = describe_pool_entry(name, "scenario", required)["needs"]
    data = scene.get("data") if isinstance(scene.get("data"), dict) else {}
    refs = scene.get("identity_refs") if isinstance(scene.get("identity_refs"), dict) else {}
    missing_fields = [field for field in needs["fields"]
                      if data.get(field) in (None, "", [])]
    missing_refs = []
    identities = {row["id"]: row for row in pool["identities"] if identity_active(row)}
    for alias in needs["refs"]:
        ids = _ref_ids(refs.get(alias))
        if not ids or any(identity_id not in identities for identity_id in ids):
            missing_refs.append(alias)
    if missing_fields or missing_refs:
        raise IdentityUnavailable("scenario", name, reason="缺少字段或可用身份引用")
    return deepcopy(scene)


def case_scenario_value(case: dict, field: str = "id", *, required: bool = True,
                        pack_id: str | None = None):
    """Read a case's named pool scenario without exposing missing values."""
    name = str((case or {}).get("fixture_scenario") or "")
    if not name:
        raise IdentityUnavailable("scenario", "fixture_scenario")
    return scenario_value(name, field, required=required, pack_id=pack_id)


def fixture_value(case: dict, field: str, *, required: bool = True, pack_id: str | None = None):
    """Resolve a request fixture only from its declared identity-pool scenario."""
    value = case_scenario_value(case, field, required=required, pack_id=pack_id)
    return value


def scenario_identities(name: str, alias: str, *, required: bool = False, pack_id: str | None = None):
    """Resolve all members from one snapshot, never silently truncate a list."""
    pool = load_pool(pack_id=pack_id)
    scene = next((r for r in pool["scenarios"] if r.get("name") == name and identity_active(r)), None)
    refs = (scene or {}).get("identity_refs", {})
    ids = _ref_ids(refs.get(alias))
    index = {r["id"]: r for r in pool["identities"] if identity_active(r)}
    if not ids or any(i not in index for i in ids):
        if required or ids:
            raise IdentityUnavailable("scenario", name, reason=f"缺少可用身份引用 {alias}")
        return []
    return [deepcopy(index[i]) for i in ids]


def scenario_identity(name: str, alias: str, *, required: bool = False, pack_id: str | None = None):
    rows = scenario_identities(name, alias, required=required, pack_id=pack_id)
    if len(rows) > 1:
        raise IdentityUnavailable("scenario", name, reason=f"身份引用 {alias} 有多个成员；请使用 scenario_identities")
    return rows[0] if rows else None


def requirements(pack_id: str | None = None) -> dict:
    from pack_registry import load_packs

    packs = list(load_packs())
    pack = next((p for p in packs if p.get("id") == pack_id), None) if pack_id else None
    if pack is None:
        pack = packs[0] if len(packs) == 1 else None
    value = (pack or {}).get("identity_requirements") or {}
    definition = selected_definition(pack_id)
    ids = [c['id'] for c in definition['categories'] if c['storage'] == 'identities']
    return {
        **{key: list(value.get(key) or []) for key in ids},
        "_identity_categories": ids,
        "distinct_roles": list(value.get("distinct_roles") or []),
        "password_default_categories": list((pack or {}).get("identity_password_policy", {}).get("default_categories") or []),
        "scenario": list(value.get("scenario") or []),
        "scenario_fields": dict(value.get("scenario_fields") or {}),
        "scenario_refs": dict(value.get("scenario_refs") or {}),
        "notes": dict(value.get("notes") or (pack or {}).get("identity_pool_notes") or {}),
    }



def _safe_identity(row: dict, identities: list[dict] | None = None) -> dict:
    """Return display fields only; credentials never cross the panel API."""
    definition = selected_definition()
    display = next((c['display_fields'] for c in definition['categories'] if c['id'] == row.get('category')), [])
    safe = redact_secrets({key: row.get(key) for key in dict.fromkeys([
        'id', 'category', 'role', 'roles', 'username', 'login_name', 'email', 'status', 'created_at', 'updated_at', *display
    ]) if row.get(key) not in (None, '', [])}) | {'stages': sorted((row.get('stages') or {}).keys())}
    fields = definition['relationship_fields']
    related = [other for other in (identities or []) if other is not row and any(
        row.get(field) not in (None, '') and other.get(field) not in (None, '')
        and str(row[field]) == str(other[field]) for field in fields)]
    if related:
        safe["related_identities"] = [
            {"id": other.get("id"), "category": other.get("category"),
             "role": other.get("role"), "roles": other.get("roles") or []}
            for other in related
        ]
    return safe


def redact_secrets(value):
    """Remove credential fields from values returned to user-facing surfaces."""
    if isinstance(value, dict):
        return {
            key: redact_secrets(item)
            for key, item in value.items()
            if "".join(c for c in str(key).casefold() if c.isalnum()) not in {
                "password", "defaultpassword", "passwordhash", "paypassword", "passwd", "secret", "token",
                "credential", "apikey", "apisecret", "accesskey", "accesskeyid", "secretaccesskey",
                "clientsecret", "accesstoken", "refreshtoken", "authorization", "privatekey",
                "mfasecret", "otpsecret", "totpsecret",
            }
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def panel_snapshot(pack_id: str | None = None) -> dict:
    definition = selected_definition(pack_id)
    if not definition['enabled']:
        return {**definition, 'categories': {}, 'maintenance': {'summary': {}, 'findings': []}}
    pool = load_pool(pack_id=pack_id)
    required = requirements(pack_id)
    identities = [row for row in pool["identities"] if isinstance(row, dict)]
    scenarios = [row for row in pool["scenarios"] if isinstance(row, dict)]
    fixtures = [row for row in pool["fixtures"] if isinstance(row, dict)]
    categories = {}
    for category in [c["id"] for c in definition["categories"] if c["storage"] == "identities"]:
        rows = [row for row in identities if row.get("category") == category]
        roles = required.get(category, [])
        present = set()
        for role in roles:
            try:
                if select_role(pool, category, role) is not None and distinct_role_ready(pool, category, role, required):
                    present.add(role)
            except IdentityUnavailable:
                pass
        categories[category] = {
            "count": len(rows),
            "missing": [role for role in roles if role not in present],
            "identities": [_safe_identity(row, identities) for row in rows],
        }
    notes = required.get("notes") if isinstance(required.get("notes"), dict) else {}
    for category in [c["id"] for c in definition["categories"] if c["storage"] == "identities"]:
        categories[category]["notes"] = list(notes.get(category) or [])
    try:
        from identity_pool_audit_core import describe_pool_entry
    except Exception:
        describe_pool_entry = None

    def scene_description(name, collection):
        if describe_pool_entry:
            return describe_pool_entry(str(name or ""), collection, required)
        return {"title": str(name or "使用位置待配置"), "purpose": "使用位置待配置",
                "suite": "", "needs": {"fields": [], "refs": [], "preconditions": ["使用位置待配置"]}}

    def missing_reference_aliases(row):
        aliases = scene_description(row.get("name"), "scenario").get("needs", {}).get("refs", [])
        refs = row.get("identity_refs") if isinstance(row.get("identity_refs"), dict) else {}
        missing = []
        for alias in aliases:
            target = refs.get(alias)
            target_ids = target if isinstance(target, list) else [target]
            if not target_ids or any(not identity_id or identity_id not in identities_by_id or not identity_active(identities_by_id[identity_id]) for identity_id in target_ids):
                missing.append(alias)
        return missing

    scenario_by_name = {row.get("name"): row for row in [*scenarios, *fixtures]}
    identities_by_id = {identity.get("id"): identity for identity in identities if identity.get("id")}
    missing_scenarios = []
    for name in required["scenario"]:
        row = scenario_by_name.get(name)
        if not row or not identity_active(row):
            missing_scenarios.append(name)
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        refs = row.get("identity_refs") if isinstance(row.get("identity_refs"), dict) else {}
        missing_fields = [field for field in scene_description(name, "scenario").get("needs", {}).get("fields", [])
                          if data.get(field) in (None, "", [])]
        missing_refs = missing_reference_aliases(row)
        if missing_fields or missing_refs:
            missing_scenarios.append(name)
    categories["scenario"] = {
        "count": len(scenarios),
        "missing": missing_scenarios,
        "scenarios": [
            redact_secrets({
                "id": row.get("id"),
                "name": row.get("name"),
                "status": row.get("status") or "active",
                **scene_description(row.get("name"), "scenario"),
                "identity_refs": row.get("identity_refs") or {},
                "references": {
                    alias: (
                        [_safe_identity(identities_by_id.get(identity_id, {})) for identity_id in identity_ids]
                        if isinstance(identity_ids, list)
                        else _safe_identity(identities_by_id.get(identity_ids, {}))
                    )
                    for alias, identity_ids in (row.get("identity_refs") or {}).items()
                },
                "missing_references": missing_reference_aliases(row),
                "missing_fields": [
                    field for field in scene_description(row.get("name"), "scenario").get("needs", {}).get("fields", [])
                    if (row.get("data") if isinstance(row.get("data"), dict) else {}).get(field) in (None, "", [])
                ],
                "data_fields": sorted((row.get("data") or {}).keys()),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            })
            for row in scenarios
        ],
    }
    categories["fixture"] = {
        "count": len(fixtures),
        "missing": [],
        "fixtures": [
            redact_secrets({
                "id": row.get("id"),
                "name": row.get("name"),
                "status": row.get("status") or "active",
                **scene_description(row.get("name"), "fixture"),
                "missing_fields": [
                    field for field in scene_description(row.get("name"), "fixture").get("needs", {}).get("fields", [])
                    if (row.get("data") if isinstance(row.get("data"), dict) else {}).get(field) in (None, "", [])
                ],
                "data_fields": sorted((row.get("data") or {}).keys()),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            })
            for row in fixtures
        ],
    }
    try:
        from identity_pool_audit_core import audit_pack

        maintenance = audit_pack(pack_id=pack_id, pool=pool)
    except Exception as exc:
        maintenance = {
            "summary": {"missing_roles": 0, "missing_scenarios": 0, "missing_fixtures": 0,
                        "incomplete_entries": 0, "duplicate_names": 0, "files_scanned": 0},
            "findings": [{"type": "audit_unavailable", "name": type(exc).__name__,
                          "title": "账号池静态审计暂不可用", "purpose": "读取静态缺口与建议",
                          "needs": {"fields": [], "refs": [], "preconditions": ["检查审计错误后重试"]},
                          "suggestion": "运行 scripts/identity_pool_audit.py audit 检查静态缺口。"}],
        }
    visible = {}
    for category in definition['categories']:
        key = category['id'] if category['storage'] == 'identities' else ('scenario' if category['storage'] == 'scenarios' else 'fixture')
        visible[category['id']] = {**categories.get(key, {}), 'label': category['label'], 'storage': category['storage']}
    return {**definition, "version": pool.get("version", POOL_VERSION), "categories": visible,
            "settings": {"default_password_configured": bool((pool.get("settings") or {}).get("default_password"))},
            "maintenance": maintenance}


def upsert_identities(identities: list[dict], *, pack_id: str | None = None) -> list[dict]:
    """Register linked identities atomically; validation failure saves none of them."""
    if not isinstance(identities, list) or any(not isinstance(r, dict) or not r.get("id")
                                              or not valid_category(r.get("category")) for r in identities):
        raise ValueError("身份登记需要对象数组、稳定 id 和有效 category")
    from pack_registry import load_packs
    packs = [p for p in load_packs() if not pack_id or p.get("id") == pack_id]
    policy = (packs[0].get("identity_registration_policy") or {}) if len(packs) == 1 else {}
    definition = selected_definition(pack_id)
    allowed = {c['id'] for c in definition['categories'] if c['storage'] == 'identities'}
    if not definition['enabled'] or any(row['category'] not in allowed for row in identities):
        raise ValueError('身份分类未由当前 Pack 启用或声明')
    def mutate(pool):
        rows, saved = pool["identities"], []
        for identity in identities:
            restriction = policy.get('existing_only', {}).get(identity['category'])
            if restriction:
                roles = set([identity['role']] if identity.get('role') else []) | set(identity.get('roles') or [])
                existing = [r for r in rows if r.get('category') == identity['category']]
                match = next((r for r in existing if r['id'] == identity['id']), None)
                if (not match or roles != {restriction.get('role')}
                        or restriction.get('unique') and len(existing) != 1):
                    raise ValueError(restriction.get('message') or '只允许更新 Pack 声明的已有身份')
            current = next((r for r in rows if r["id"] == identity["id"]), None)
            if current is not None:
                if current["category"] != identity["category"]:
                    raise ValueError("同一身份 id 不允许改变 category")
                incoming = deepcopy(identity)
                if "stages" in incoming and isinstance(incoming["stages"], dict):
                    incoming["stages"] = {**current.get("stages", {}), **incoming["stages"]}
                if "roles" in incoming and isinstance(incoming["roles"], list):
                    incoming["roles"] = list(dict.fromkeys(current.get("roles", []) + incoming["roles"]))
                current.update(incoming)
            else:
                current = deepcopy(identity)
                rows.append(current)
            saved.append(current)
        return saved
    return update_pool(mutate, pack_id=pack_id)


def upsert_identity(identity: dict, *, pack_id: str | None = None) -> dict:
    return upsert_identities([identity], pack_id=pack_id)[0]


def upsert_scenario(scenario: dict, *, pack_id: str | None = None) -> dict:
    if not isinstance(scenario, dict) or not scenario.get("id") or not scenario.get("name"):
        raise ValueError("场景记录需要稳定 id 和 name")
    def mutate(pool):
        # A legacy fixture remains a fixture when edited through its case name.
        current = next((r for kind in ("scenarios", "fixtures") for r in pool[kind] if r["id"] == scenario["id"]), None)
        if current is not None:
            current.update(deepcopy(scenario))
            return current
        pool["scenarios"].append(deepcopy(scenario))
        return scenario
    return update_pool(mutate, pack_id=pack_id)
