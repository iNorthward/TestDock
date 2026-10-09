"""Discover and initialize project packs from portable JSON manifests."""
from __future__ import annotations

import importlib
import ast
from importlib.machinery import FileFinder, SourceFileLoader, ModuleSpec
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS_ROOT = REPO_ROOT / "packs"
CORE_FILES = tuple(json.loads((REPO_ROOT / "resources/platform-core-files.json").read_text(encoding="utf-8")))
# Extensions receive other shared capabilities through their explicit core context.
SHARED_SERVICE_MODULES = {"run_history", "identity_pool", "platform_config", "timezone_config"}
_PACKS = None
_CANDIDATES = None
_INITIALIZING = False
_BOOTSTRAP_FAILED = False
_PACK_SELECTOR = None


def _string_list(pack, field):
    value = pack.get(field, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise RuntimeError(f"pack {pack['id']} {field} 必须是非空字符串数组")
    return value


def _repo_path(pack, configured, field):
    if not isinstance(configured, str) or not configured.strip() or Path(configured).is_absolute():
        raise RuntimeError(f"pack {pack['id']} {field} 必须是仓库内的相对路径")
    path = (REPO_ROOT / configured).resolve()
    root = REPO_ROOT.resolve()
    if path != root and root not in path.parents:
        raise RuntimeError(f"pack {pack['id']} {field} 必须位于仓库目录内")
    packs_root = PACKS_ROOT.resolve()
    if packs_root in path.parents and path != pack['root'] and pack['root'] not in path.parents:
        raise RuntimeError(f"pack {pack['id']} {field} 不得引用其他 pack 的资源")
    return path


def _validate_target(pack, target, field):
    if not isinstance(target, str) or not re.fullmatch(
        r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", target, re.ASCII,
    ):
        raise RuntimeError(f"pack {pack['id']} {field} 必须写成 module:function")


def _owned_paths(pack):
    paths = [Path(pack.get("root") or PACKS_ROOT / pack["id"]).resolve()]
    for relative in pack.get("case_dirs", []):
        if isinstance(relative, Path):
            try:
                relative = relative.relative_to(REPO_ROOT.resolve()).as_posix()
            except ValueError as exc:
                raise RuntimeError(f"pack {pack['id']} case_dir 必须位于仓库目录内") from exc
        paths.append(_repo_path(pack, relative, "case_dir"))
    for relative in pack.get("adapter_files", []):
        source = _repo_path(pack, relative, "adapter_files")
        if not source.is_file() or source in {(REPO_ROOT / p).resolve() for p in CORE_FILES}:
            raise RuntimeError(f"pack {pack['id']} adapter_files 必须是项目源码文件，不能声明 Core 文件")
        paths.append(source)
    return paths


def _source_owned(pack, source, *, shared=False):
    source = Path(source).resolve()
    if shared:
        return source in {(REPO_ROOT / p).resolve() for p in CORE_FILES}
    if source in {(REPO_ROOT / p).resolve() for p in CORE_FILES}:
        return False
    return any(source == p or p.is_dir() and p in source.parents for p in _owned_paths(pack))


def _namespace_only(source):
    """Shared parent namespaces may contain a docstring/pass, never installers."""
    if Path(source).name != "__init__.py":
        return False
    tree = ast.parse(Path(source).read_text(encoding="utf-8"))
    return all(isinstance(n, ast.Pass) or isinstance(n, ast.Expr) and
               isinstance(n.value, ast.Constant) and isinstance(n.value.value, str) for n in tree.body)


def _source_spec(name, search):
    """FileFinder also resolves unimported namespace parents without sys.modules."""
    namespaces = []
    for directory in search:
        spec = FileFinder(str(directory), (SourceFileLoader, ['.py'])).find_spec(name)
        if spec is not None and spec.origin:
            return spec
        if spec is not None:
            namespaces.extend(spec.submodule_search_locations or [])
    if namespaces:
        spec = ModuleSpec(name, None, is_package=True)
        spec.submodule_search_locations = namespaces
        return spec
    return None


def _module_sources(pack, module_name, *, shared=False, optional=False):
    """Resolve package/module origins without executing any parent package."""
    if module_name.startswith("packs.") and module_name.split(".")[1] != pack['id']:
        raise RuntimeError(f"pack {pack['id']} 扩展入口不得引用其他 Pack: {module_name}")
    case_dirs = [str(p) for p in _owned_paths(pack)[1:] if p.is_dir()]
    search = [*case_dirs, str(REPO_ROOT), str(REPO_ROOT / "test-platform")]
    parts, sources = module_name.split("."), []
    for index in range(len(parts)):
        prefix = ".".join(parts[:index + 1])
        spec = _source_spec(prefix, search)
        if spec is None:
            if optional and (module_name.startswith(f"packs.{pack['id']}.") or index > 0 and sources):
                return []
            raise ModuleNotFoundError(f"pack {pack['id']} 声明的模块不存在: {module_name}", name=prefix)
        if spec.origin:
            source = Path(spec.origin).resolve()
            owned = _source_owned(pack, source, shared=shared)
            parent_namespace = index < len(parts) - 1 and REPO_ROOT.resolve() in source.parents and _namespace_only(source)
            if not owned and not parent_namespace:
                raise RuntimeError(f"pack {pack['id']} 扩展入口源码不属于当前项目: {module_name}")
            loaded = sys.modules.get(prefix)
            if loaded is not None and Path(getattr(loaded, "__file__", "") or ".").resolve() != source:
                raise RuntimeError(f"pack {pack['id']} 扩展入口已被其他来源模块占用: {prefix}")
            sources.append((prefix, source))
        elif prefix in sys.modules and sys.modules[prefix] is not None:
            locations = {Path(p).resolve() for p in getattr(sys.modules[prefix], "__path__", [])}
            if locations != {Path(p).resolve() for p in spec.submodule_search_locations or []}:
                raise RuntimeError(f"pack {pack['id']} 扩展入口父命名空间已被其他来源占用: {prefix}")
        search = list(spec.submodule_search_locations or [])
    if not sources or not _source_owned(pack, sources[-1][1], shared=shared):
        raise RuntimeError(f"pack {pack['id']} 扩展入口必须是当前项目的源码模块: {module_name}")
    return sources


def _import_owned(pack, module_name, *, shared=False, optional=False):
    sources = _module_sources(pack, module_name, shared=shared, optional=optional)
    if not sources:
        return None
    module = importlib.import_module(module_name)
    if Path(getattr(module, "__file__", "") or ".").resolve() != sources[-1][1]:
        raise RuntimeError(f"pack {pack['id']} 扩展入口加载来源发生变化: {module_name}")
    return module


def _manifest_identity(manifest, manifest_path):
    if not isinstance(manifest, dict):
        raise RuntimeError(f"pack manifest 顶层必须是对象: {manifest_path}")
    pack = dict(manifest)
    pack_id = pack.get("id", manifest_path.parent.name)
    if (not isinstance(pack_id, str) or pack_id in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", pack_id)):
        raise RuntimeError(f"pack manifest id 无效: {manifest_path}")
    pack.update(id=pack_id, manifest_path=manifest_path, root=manifest_path.parent.resolve())
    if PACKS_ROOT.resolve() not in pack['root'].parents:
        raise RuntimeError(f"pack {pack_id} manifest 必须位于 packs 目录内")
    if pack_id != manifest_path.parent.name:
        raise RuntimeError(f"pack {pack_id} id 必须与所在目录名一致")
    if "default" in pack and not isinstance(pack["default"], bool):
        raise RuntimeError(f"pack {pack_id} default 必须是 bool")
    return pack


DIAGNOSTIC_CAPABILITIES = {'contract', 'database', 'error_log'}

def diagnostic_capabilities(pack):
    values = pack.get('diagnostic_capabilities', [])
    if (not isinstance(values, list) or any(not isinstance(v, str) or v not in DIAGNOSTIC_CAPABILITIES for v in values)
            or len(set(values)) != len(values)):
        raise ValueError('diagnostic_capabilities 必须声明不重复的 contract/database/error_log 能力')
    return list(values)


def _validate_manifest(manifest, manifest_path):
    pack = _manifest_identity(manifest, manifest_path)
    pack_id = pack['id']
    from platform_protocol import compatibility
    compatibility(pack)
    diagnostic_capabilities(pack)
    catalog_policy=pack.get('catalog_policy',{})
    if (not isinstance(catalog_policy,dict) or set(catalog_policy)-{'group_tokens','require_readonly_baseline'}
            or not isinstance(catalog_policy.get('group_tokens',[]),list)
            or any(not isinstance(v,str) or not v.strip() for v in catalog_policy.get('group_tokens',[]))
            or type(catalog_policy.get('require_readonly_baseline',False)) is not bool):
        raise ValueError('catalog_policy 只能显式声明 group_tokens 与 require_readonly_baseline')
    execution_policy=pack.get('execution_policy',{})
    import math
    isolated=execution_policy.get('isolated_case_ids',[]) if isinstance(execution_policy,dict) else None
    timeout=execution_policy.get('case_timeout_seconds',30) if isinstance(execution_policy,dict) else None
    if (not isinstance(execution_policy,dict) or set(execution_policy)-{'isolated_case_ids','case_timeout_seconds'}
            or not isinstance(isolated,list) or any(not isinstance(v,str) or not v for v in isolated) or len(set(isolated))!=len(isolated)
            or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=3600):
        raise ValueError('execution_policy requires unique isolated_case_ids and finite timeout <=3600')
    display_name = pack.get("display_name", pack_id)
    if not isinstance(display_name, str) or not display_name.strip():
        raise RuntimeError(f"pack {pack_id} display_name 必须是非空字符串")
    pack["display_name"] = display_name.strip()
    if "default" in pack and not isinstance(pack["default"], bool):
        raise RuntimeError(f"pack {pack_id} default 必须是 bool")
    for field in ("case_dirs", "module_order_hint", "optional_panel_services", "adapter_files", "shared_core_services", "compatibility_files", "distribution_excludes", "required_environment"):
        _string_list(pack, field)
    if any(not re.fullmatch(r'[A-Z][A-Z0-9_]*',key) for key in pack.get('required_environment',[])):
        raise ValueError('required_environment must contain environment key names only')
    for relative in pack.get('compatibility_files', []):
        source = _repo_path(pack, relative, 'compatibility_files')
        if not source.is_file() or source in {(REPO_ROOT / path).resolve() for path in CORE_FILES} or source.name.startswith('.env'):
            raise RuntimeError(f"pack {pack_id} compatibility_files 不得声明 Core、凭据或缺失文件")
    for field in ("config_defaults", "panel_services", "module_metadata", "identity_requirements"):
        if field in pack and not isinstance(pack[field], dict):
            raise RuntimeError(f"pack {pack_id} {field} 必须是对象")
    for key, value in pack.get("config_defaults", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(value, str):
            raise RuntimeError(f"pack {pack_id} config_defaults 必须是环境变量名称到字符串的映射")
        if key in {"PLATFORM_PACKS", "PLATFORM_ENV_FILE"}:
            raise RuntimeError(f"pack {pack_id} config_defaults 不得声明运行时选择器 {key}")
    aliases = pack.get('config_aliases', {})
    if not isinstance(aliases, dict):
        raise RuntimeError(f"pack {pack_id} config_aliases 必须是对象")
    for key, values in aliases.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or key in {'PLATFORM_PACKS', 'PLATFORM_ENV_FILE'}:
            raise RuntimeError(f"pack {pack_id} config_aliases key 无效")
        values = _string_list({'id': pack_id, 'aliases': values}, 'aliases')
        if any(not re.fullmatch(r'[A-Z][A-Z0-9_]*', value) or value in {'PLATFORM_PACKS', 'PLATFORM_ENV_FILE'} for value in values):
            raise RuntimeError(f"pack {pack_id} config_aliases value 无效")
    contracts = pack.get("execution_contract_sources", {})
    if not isinstance(contracts, dict) or any(
        not isinstance(label, str) or not label.strip() or not isinstance(key, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        for label, key in contracts.items()
    ):
        raise RuntimeError(f"pack {pack_id} execution_contract_sources 须为名称到配置键的映射")
    exceptions = pack.get("case_doc_exceptions", {})
    if not isinstance(exceptions, dict) or any(not isinstance(key, str) or not key.strip()
            or not isinstance(reason, str) or not reason.strip() for key, reason in exceptions.items()):
        raise RuntimeError(f"pack {pack_id} case_doc_exceptions 须逐条声明案例编号与原因")
    if "regression_manifest" in pack:
        target = _repo_path(pack, pack["regression_manifest"], "regression_manifest")
        if not target.is_file() or pack['root'] not in target.parents:
            raise RuntimeError(f"pack {pack_id} regression_manifest 须位于当前 Pack 内")
    for alias, module in pack.get("panel_services", {}).items():
        if not alias.isidentifier() or not isinstance(module, str) or not all(
            part.isidentifier() for part in module.split(".")
        ):
            raise RuntimeError(f"pack {pack_id} panel_services 必须是名称到模块的映射")
    if set(pack.get("optional_panel_services", [])) - set(pack.get("panel_services", {})):
        raise RuntimeError(f"pack {pack_id} optional_panel_services 必须引用已声明的 service")
    checks = pack.get("optional_environment_checks", [])
    if not isinstance(checks, list):
        raise RuntimeError(f"pack {pack_id} optional_environment_checks 必须是数组")
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('name'), str) or not check['name'].strip():
            raise RuntimeError(f"pack {pack_id} 可选环境检查必须提供 name")
        variables = _string_list({'id': pack_id, 'variables': check.get('variables')}, 'variables')
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in variables):
            raise RuntimeError(f"pack {pack_id} 可选环境检查包含无效变量名称")
        if 'any_is_enough' in check and not isinstance(check['any_is_enough'], bool):
            raise RuntimeError(f"pack {pack_id} any_is_enough 必须是 bool")
    tools = pack.get("cli_tools", {})
    if not isinstance(tools, dict):
        raise RuntimeError(f"pack {pack_id} cli_tools 必须是对象")
    for alias, module in tools.items():
        if not isinstance(alias, str) or not alias.isidentifier() or not isinstance(module, str) or not all(
                part.isidentifier() for part in module.split('.')):
            raise RuntimeError(f"pack {pack_id} cli_tools 必须是名称到模块的映射")
        _module_sources(pack, module)
    distinct = pack.get('identity_requirements', {}).get('distinct_roles', [])
    if not isinstance(distinct, list):
        raise RuntimeError(f"pack {pack_id} distinct_roles 必须是数组")
    for rule in distinct:
        if not isinstance(rule, dict) or any(not isinstance(rule.get(key), str) or not rule[key].strip()
                                            for key in ('category', 'role', 'other_role')):
            raise RuntimeError(f"pack {pack_id} distinct_roles 必须声明 category / role / other_role")
        _string_list({'id': pack_id, 'fields': rule.get('fields')}, 'fields')
        if 'unique' in rule and not isinstance(rule['unique'], bool):
            raise RuntimeError(f"pack {pack_id} distinct_roles.unique 必须是 bool")
    for field in ("panel_extension", "catalog_extension"):
        targets = pack.get(field, [])
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list):
            raise RuntimeError(f"pack {pack_id} {field} 必须是 module:function 或数组")
        for target in targets:
            _validate_target(pack, target, field)
    if "bootstrap" in pack and not isinstance(pack["bootstrap"], str):
        raise RuntimeError(f"pack {pack_id} bootstrap 必须是字符串")
    if pack.get("bootstrap"):
        _validate_target(pack, pack["bootstrap"], "bootstrap")
    navigation = pack.get("navigation", [])
    node_ids = set()

    def check_navigation(nodes):
        if not isinstance(nodes, list):
            raise RuntimeError(f"pack {pack_id} navigation / children 必须是数组")
        for node in nodes:
            if not isinstance(node, dict) or any(
                not isinstance(node.get(key), str) or not node[key].strip() for key in ("id", "name")
            ):
                raise RuntimeError(f"pack {pack_id} navigation 节点必须提供 id / name")
            if node["id"] in node_ids:
                raise RuntimeError(f"pack {pack_id} navigation id 重复: {node['id']}")
            node_ids.add(node["id"])
            for field in ("suites", "parents"):
                if field in node:
                    _string_list({**node, "id": pack_id}, field)
            check_navigation(node.get("children", []))

    check_navigation(navigation)
    module_sources = {}
    for relative in pack.get("case_dirs", []):
        path = _repo_path(pack, relative, "case_dir")
        if not path.is_dir():
            raise RuntimeError(f"pack {pack_id} 的 case_dir 不存在: {relative}")
        for source in sorted(path.glob("*_cases.py")):
            _repo_path(pack, str(source.relative_to(REPO_ROOT.resolve())), "case module")
            if source.stem in module_sources:
                raise RuntimeError(f"pack {pack_id} case module 重复: {source.stem}")
            module_sources[source.stem] = source
    if pack.get("panel_html"):
        if not _repo_path(pack, pack["panel_html"], "panel_html").is_file():
            raise RuntimeError(f"pack {pack_id} panel_html 不存在")
    if "identity_pool_file" in pack:
        relative = pack["identity_pool_file"]
        if (not isinstance(relative, str) or not relative.strip() or Path(relative).is_absolute()
                or pack['root'] not in (pack['root'] / relative).resolve().parents):
            raise RuntimeError(f"pack {pack_id} identity_pool_file 必须位于 pack 目录内")
    _owned_paths(pack)
    shared = set(pack.get("shared_core_services", []))
    if shared - set(pack.get("panel_services", {})):
        raise RuntimeError(f"pack {pack_id} shared_core_services 必须引用已声明的 service")
    from resource_pool_config import resource_pool_definition, valid_category
    try:
        resource_pool_definition(pack)
    except ValueError as exc:
        raise RuntimeError(f"pack {pack_id}: {exc}") from exc
    pattern = pack.get('identity_requirements', {}).get('notes', {}).get('fixture_name_pattern')
    if pattern is not None:
        if not isinstance(pattern, str):
            raise RuntimeError(f'pack {pack_id} fixture_name_pattern 必须是字符串')
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RuntimeError(f'pack {pack_id} fixture_name_pattern 无效') from exc
    registration = pack.get('identity_registration_policy', {})
    if not isinstance(registration, dict) or not isinstance(registration.get('existing_only', {}), dict):
        raise RuntimeError(f'pack {pack_id} identity_registration_policy 无效')
    for category, rule in registration.get('existing_only', {}).items():
        if (not valid_category(category) or not isinstance(rule, dict)
                or not isinstance(rule.get('role'), str) or not rule['role'].strip()
                or type(rule.get('unique', False)) is not bool
                or not isinstance(rule.get('message', ''), str)):
            raise RuntimeError(f'pack {pack_id} existing_only 身份约束无效')
    alias = pack.get('identity_alias_policy', {})
    if not isinstance(alias, dict) or alias and (not valid_category(alias.get('category'))
            or not isinstance(alias.get('role'), str) or not alias['role'].strip()
            or not isinstance(alias.get('source_kind'), str) or not alias['source_kind'].strip()):
        raise RuntimeError(f'pack {pack_id} identity_alias_policy 无效')
    policy = pack.get("identity_password_policy", {})
    if not isinstance(policy, dict):
        raise RuntimeError(f"pack {pack_id} identity_password_policy 必须是对象")
    categories = _string_list({"id": pack_id, "default_categories": policy.get("default_categories", [])}, "default_categories")
    if any(not valid_category(category) for category in categories):
        raise RuntimeError(f"pack {pack_id} 密码策略类别无效")
    for field in ("bootstrap", "catalog_extension", "panel_extension"):
        targets = pack.get(field) or []
        for target in [targets] if isinstance(targets, str) else targets:
            _module_sources(pack, target.split(":")[0])
    for alias, module in pack.get("panel_services", {}).items():
        if alias in shared and module not in SHARED_SERVICE_MODULES:
            raise RuntimeError(f"pack {pack_id} 共享 service 不是已声明的 Core 能力")
        _module_sources(pack, module, shared=alias in shared, optional=alias in pack.get("optional_panel_services", []))
    return pack


def load_pack_tool_module(alias):
    """Resolve a capability from the selected Pack, never a project fallback."""
    pack = load_packs()[0]
    target = pack.get('cli_tools', {}).get(alias)
    if target is None:
        raise ValueError(f"[incomplete] Pack {pack['id']} 未提供工具: {alias}")
    return _import_owned(pack, target)


def _read_candidates(pack_id=None):
    candidates = []
    if not PACKS_ROOT.is_dir():
        return candidates
    paths = sorted(PACKS_ROOT.glob("*/pack.json")) if pack_id is None else [PACKS_ROOT / pack_id / "pack.json"]
    for manifest_path in paths:
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 pack manifest {manifest_path}: {exc}") from exc
        pack = _manifest_identity(manifest, manifest_path)
        if any(other['id'] == pack['id'] for other in candidates):
            raise RuntimeError(f"pack manifest id 重复: {pack['id']}")
        candidates.append(pack)
    return candidates


def _configured_pack_id():
    configured = os.environ.get("PLATFORM_PACKS", "").strip()
    if not configured:
        return None
    if "," in configured:
        raise RuntimeError("每个进程只能选择一个项目 Pack；多个项目请使用独立进程与环境文件")
    if configured in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", configured):
        raise RuntimeError("PLATFORM_PACKS 必须是有效的项目 Pack id")
    return configured


def panel_pack_summaries(packs):
    """Return the selected pack IDs and safe display names for panel headers."""
    return [
        {"id": str(pack["id"]), "name": str(pack.get("display_name") or pack["id"])}
        for pack in packs
    ]


def _select(candidates):
    ids = [pack['id'] for pack in candidates]
    if len(ids) != len(set(ids)):
        raise RuntimeError("pack manifest id 重复")
    configured = _configured_pack_id()
    if configured:
        by_id = {pack["id"]: pack for pack in candidates}
        if configured not in by_id:
            raise RuntimeError("PLATFORM_PACKS 指向未发现的 pack: " + configured)
        return [by_id[configured]]

    defaults = [pack for pack in candidates if pack.get("default") is True]
    if len(defaults) > 1:
        raise RuntimeError("只能有一个 default pack；请通过 PLATFORM_PACKS 显式选择")
    if defaults:
        return defaults
    if len(candidates) > 1:
        raise RuntimeError("发现多个 pack 且未标记唯一 default；请通过 PLATFORM_PACKS 显式选择")
    return candidates


def _prepare_pack(pack):
    pack = dict(pack)
    case_dirs = []
    for relative in pack.get("case_dirs") or []:
        path = _repo_path(pack, relative, "case_dir")
        if not path.is_dir():
            raise RuntimeError(f"pack {pack['id']} 的 case_dir 不存在: {relative}")
        case_dirs.append(path)
    pack["case_dirs"] = case_dirs
    pack["module_order_hint"] = list(pack.get("module_order_hint") or [])
    pack["navigation"] = list(pack.get("navigation") or [])
    for path in reversed(case_dirs):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    config_defaults = pack.get("config_defaults") or {}
    if config_defaults:
        if not isinstance(config_defaults, dict):
            raise RuntimeError(f"pack {pack['id']} config_defaults 必须是对象")
        from platform_config import register_project_defaults

        register_project_defaults(config_defaults)
    from platform_config import register_project_aliases
    register_project_aliases(pack.get('config_aliases', {}))

    # Core APIs must be importable before a Pack registers its adapters.
    for core_path in reversed([REPO_ROOT / 'test-platform' / 'common', REPO_ROOT / 'test-platform', REPO_ROOT]):
        if str(core_path) not in sys.path:
            sys.path.insert(0, str(core_path))

    bootstrap = str(pack.get("bootstrap") or "").strip()
    if bootstrap:
        module_name, separator, function_name = bootstrap.partition(":")
        if not separator or not module_name or not function_name:
            raise RuntimeError(f"pack {pack['id']} bootstrap 必须写成 module:function")
        module = _import_owned(pack, module_name)
        installer = getattr(module, function_name, None)
        if not callable(installer):
            raise RuntimeError(f"pack {pack['id']} bootstrap 不可调用: {bootstrap}")
        installer()
    return pack


def load_packs():
    """Return selected packs, installing their adapters before case discovery."""
    global _PACKS, _CANDIDATES, _INITIALIZING, _BOOTSTRAP_FAILED, _PACK_SELECTOR
    if _BOOTSTRAP_FAILED:
        raise RuntimeError("Pack 初始化曾失败，无法安全复用当前上下文；请修复后重启进程")
    if _PACKS is not None:
        configured = _configured_pack_id()
        same_default = (len(_PACKS) == 1 and _PACKS[0].get('default') is True
                        and configured in {None, _PACKS[0]['id']})
        if configured != _PACK_SELECTOR and not same_default:
            raise RuntimeError("项目 Pack 选择已变化；请重启进程后加载新项目")
        return tuple(_PACKS)
    if _INITIALIZING:
        raise RuntimeError("Pack bootstrap 不得递归加载项目上下文")
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from platform_config import load_project_env

    load_project_env()
    _PACK_SELECTOR = _configured_pack_id()
    _CANDIDATES = _read_candidates(_PACK_SELECTOR)
    selected = [_validate_manifest(pack, pack['manifest_path']) for pack in _select(_CANDIDATES)]
    _INITIALIZING = True
    try:
        _PACKS = [_prepare_pack(pack) for pack in selected]
    except Exception:
        _BOOTSTRAP_FAILED = True
        raise
    finally:
        _INITIALIZING = False
    return tuple(_PACKS)


def load_panel_services(packs=None):
    """Load pack-owned service modules exposed through the panel extension context."""
    packs = tuple(packs if packs is not None else load_packs())
    modules = {}
    for pack in packs:
        for service_name, module_name in (pack.get("panel_services") or {}).items():
            service_name = str(service_name)
            if not service_name.isidentifier():
                raise RuntimeError(f"pack {pack['id']} panel service 名称无效: {service_name}")
            if service_name in modules:
                raise RuntimeError(f"多个 pack 重复提供 panel service: {service_name}")
            try:
                shared = service_name in pack.get("shared_core_services", [])
                if shared and module_name not in SHARED_SERVICE_MODULES:
                    raise RuntimeError("共享 service 不是已声明的 Core 能力")
                module = _import_owned(pack, str(module_name), shared=shared, optional=service_name in pack.get("optional_panel_services", []))
                if module is not None:
                    modules[service_name] = module
            except ModuleNotFoundError as exc:
                optional = service_name in (pack.get("optional_panel_services") or [])
                # Only an absent declared module is optional. A broken dependency
                # inside a present module must remain a visible startup error.
                absent_target = str(module_name) == exc.name or str(module_name).startswith(f"{exc.name}.")
                if not optional or not absent_target:
                    raise
    return modules


def load_panel_extensions(packs=None, *, services=None, core=None):
    """Instantiate optional panel extensions using the stable context/dispatch contract.

    Each manifest target is ``module:factory``. The factory receives a mapping with
    ``pack_id``, ``pack_root``, ``services`` (only that pack's declared services),
    and the explicitly supplied ``core`` capabilities. It returns an object whose
    ``dispatch(method, handler, path, query, body)`` returns bool and may expose
    ``startup()`` for pack-owned initialization.
    """
    packs = tuple(packs if packs is not None else load_packs())
    services = dict(services or {})
    core = dict(core or {})
    extensions = []
    for pack in packs:
        targets = pack.get("panel_extension") or []
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list):
            raise RuntimeError(f"pack {pack['id']} panel_extension 必须是 module:factory 或数组")
        pack_services = {
            name: services[name]
            for name in (pack.get("panel_services") or {})
            if name in services
        }
        for target in targets:
            module_name, separator, factory_name = str(target).partition(":")
            if not separator or not module_name or not factory_name:
                raise RuntimeError(f"pack {pack['id']} panel_extension 必须写成 module:factory")
            module = _import_owned(pack, module_name)
            factory = getattr(module, factory_name, None)
            if not callable(factory):
                raise RuntimeError(f"pack {pack['id']} panel extension factory 不可调用: {target}")
            context = {
                "pack_id": pack["id"],
                "pack_root": pack["root"],
                "services": pack_services,
                "core": dict(core),
            }
            extension = factory(context)
            if not callable(getattr(extension, "dispatch", None)):
                raise RuntimeError(f"pack {pack['id']} panel extension 缺少 dispatch(): {target}")
            extensions.append((pack["id"], extension))
    return tuple(extensions)


def load_catalog_extensions(packs=None):
    """Instantiate optional catalog hooks for enrichment, sections, and execution setup.

    The returned object may implement any of: ``enrich_catalog(data, lite=...)``,
    ``catalog_section(section_id, force_refresh=...)``, ``before_suite(suite, options)``,
    ``before_case(case, module, options)``, ``case_context(case, module, options)``
    (a context manager restored on success/error), and ``after_reload()``. Missing hooks are
    ignored so a pack can implement only the behavior it needs.
    """
    packs = tuple(packs if packs is not None else load_packs())
    extensions = []
    for pack in packs:
        targets = pack.get("catalog_extension") or []
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list):
            raise RuntimeError(f"pack {pack['id']} catalog_extension 必须是 module:factory 或数组")
        for target in targets:
            module_name, separator, factory_name = str(target).partition(":")
            if not separator or not module_name or not factory_name:
                raise RuntimeError(f"pack {pack['id']} catalog_extension 必须写成 module:factory")
            module = _import_owned(pack, module_name)
            factory = getattr(module, factory_name, None)
            if not callable(factory):
                raise RuntimeError(f"pack {pack['id']} catalog extension factory 不可调用: {target}")
            extension = factory({"pack_id": pack["id"], "pack_root": pack["root"]})
            extensions.append((pack["id"], extension))
    return tuple(extensions)


def dispatch_panel_extensions(extensions, method, handler, path, query, body=None) -> bool:
    """Dispatch a request to selected pack extensions; bool true means response sent."""
    for pack_id, extension in extensions:
        handled = extension.dispatch(method, handler, path, query, body)
        if not isinstance(handled, bool):
            raise RuntimeError(f"pack {pack_id} panel extension dispatch() 必须返回 bool")
        if handled:
            return True
    return False


def panel_html_path(packs=None):
    """Resolve the selected pack's optional panel entry, or the core default."""
    packs = tuple(packs if packs is not None else load_packs())
    for pack in packs:
        configured = str(pack.get("panel_html") or "").strip()
        if not configured:
            continue
        path = _repo_path(pack, configured, "panel_html")
        if path != REPO_ROOT and REPO_ROOT not in path.parents:
            raise RuntimeError(f"pack {pack['id']} panel_html 必须位于仓库目录内")
        if not path.is_file():
            raise RuntimeError(f"pack {pack['id']} panel_html 不存在: {configured}")
        return path
    return REPO_ROOT / "test-platform" / "panel.html"


def case_source_files(packs=None):
    """List discovered case modules for watcher fingerprints."""
    packs = tuple(packs if packs is not None else load_packs())
    files = []
    for pack in packs:
        for folder in pack["case_dirs"]:
            folder = Path(folder)
            relative = str(folder.relative_to(REPO_ROOT.resolve())) if folder.is_absolute() else str(folder)
            folder = _repo_path(pack, relative, "case_dir")
            for source in sorted(folder.glob("*_cases.py")):
                _repo_path(pack, str(source.relative_to(REPO_ROOT.resolve())), "case module")
                files.append(source)
    return tuple(files)


def case_module_name(source):
    """Use a Pack package name when its source has a valid Python namespace."""
    relative = Path(source).resolve().relative_to(REPO_ROOT.resolve()).with_suffix("")
    if relative.parts[0] == "packs" and all(part.isidentifier() for part in relative.parts):
        return ".".join(relative.parts)
    return Path(source).stem
