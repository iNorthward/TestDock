#!/usr/bin/env python3
"""Offline Core/Pack ownership checks, with an optional single demo mock run."""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_FILES = tuple(json.loads((ROOT / "resources/platform-core-files.json").read_text(encoding="utf-8")))
BUSINESS_PATHS = ("packs", "adapters")


def core_findings(root, *, core_files=CORE_FILES, business_modules=()):
    findings = []
    declared = {(root / relative).resolve() for relative in core_files}
    core_module_names = {Path(relative).stem for relative in core_files}
    search_roots = (root, root / "scripts", root / "test-platform", root / "test-platform/common")
    for relative in core_files:
        path = root / relative
        if not path.is_file():
            findings.append({"file": relative, "kind": "missing_core", "message": "Core 清单中的文件缺失"})
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        docstrings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)
                      and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}
        for node in ast.walk(tree):
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            for name in imports:
                if (name.split(".")[0] in {"packs", "adapters"}
                        or name.split(".")[0] in set(business_modules) - core_module_names):
                    findings.append({"file": relative, "line": node.lineno, "kind": "business_import",
                                     "message": f"Core 直接导入项目实现: {name}"})
                    break
            for name in imports:
                parts = name.split(".")
                candidates = [base.joinpath(*parts).with_suffix(".py") for base in search_roots]
                candidates += [base.joinpath(*parts, "__init__.py") for base in search_roots]
                local = next((candidate for candidate in candidates if candidate.is_file()), None)
                if local is not None and local.resolve() not in declared:
                    findings.append({"file": relative, "line": node.lineno,
                                     "kind": "unowned_core_dependency",
                                     "message": f"Core 依赖未登记源码: {name}"})
                    break
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings and any(
                        node.value.startswith(prefix + "/")
                        for prefix in BUSINESS_PATHS
                    )):
                findings.append({"file": relative, "line": node.lineno, "kind": "business_path",
                                 "message": "Core 固定引用项目资源路径"})
    return findings


def common_findings(root, *, core_files=CORE_FILES):
    """Core common is a declared inventory; business files must stay in Packs."""
    allowed = {str(Path(relative)) for relative in core_files}
    return [
        {"file": str(path.relative_to(root)), "kind": "unowned_common",
         "message": "common 包含未登记的文件；业务实现须放入 Pack，通用组件须登记 Core 归属"}
        for path in sorted((root / "test-platform/common").glob("*.py"))
        if str(path.relative_to(root)) not in allowed
    ]


def platform_findings(root, *, core_files=CORE_FILES):
    allowed = set(core_files)
    return [{"file": str(path.relative_to(root)), "kind": "unowned_platform",
             "message": "平台目录含未登记 Python 源码；项目实现须放入 Pack，通用组件须登记 Core"}
            for path in sorted((root / "test-platform").rglob("*.py"))
            if str(path.relative_to(root)) not in allowed and "common" not in path.relative_to(root).parts]


def demo_smoke(root):
    code = '''
import json, socket, sys, tempfile
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root), str(root / "test-platform")]
def forbidden(*args, **kwargs):
    raise AssertionError("offline demo attempted a business socket")
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
import server, identity_pool
assert [p["id"] for p in server.cat.PACKS] == ["demo_pack"]
assert [c["id"] for c in server.cat._ALL] == ["DEMO-R01"]
assert server.rh.DB_PATH == root / "packs/demo_pack/data/run_history.db"
assert not server.PACK_SERVICES and not server.PANEL_EXTENSIONS
from response_base_rules import validate_response_data
assert validate_response_data({"records": [{"userId": 1}]}) == (True, "")
from json_wire_types import _PROJECT_WIRE_RULES
assert not _PROJECT_WIRE_RULES
result = server.cat.exec_case("DEMO-R01")
assert result["status"] == "passed", result
with tempfile.TemporaryDirectory() as td:
    server.rh.DB_PATH = Path(td) / "history.db"
    run_id = server.rh.record_case(result, source="isolation-smoke")
    assert server.rh.run_detail(run_id)["status"] == "passed"
for name in ("foreign-project", "unknown-project"):
    try:
        identity_pool.pool_path(name)
    except ValueError:
        pass
    else:
        raise AssertionError("foreign identity pool accepted")
assert not [name for name in sys.modules if name.startswith("packs.") and not name.startswith("packs.demo_pack")]
print(json.dumps({"case": "DEMO-R01", "status": "passed", "external_connections": 0, "history": "temporary"}))
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(root)], cwd=root, timeout=30,
        env={"PATH": os.environ["PATH"], "PLATFORM_ENV_FILE": "/dev/null", "PLATFORM_PACKS": "demo_pack"},
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError("demo 隔离验证失败:\n" + result.stderr)
    return json.loads(result.stdout)


def build_report(*, with_demo=False):
    sys.path[:0] = [str(ROOT), str(ROOT / "test-platform")]
    import pack_registry

    findings, packs, business_modules, owners, data_roots = [], [], set(), {}, {}
    try:
        candidates = pack_registry._read_candidates()
    except RuntimeError as exc:
        return {"ok": False, "findings": [{"kind": "manifest", "message": str(exc)}], "packs": []}
    core_paths = {(ROOT / relative).resolve() for relative in CORE_FILES}
    for candidate in candidates:
        try:
            pack = pack_registry._validate_manifest(candidate, candidate['manifest_path'])
        except RuntimeError as exc:
            findings.append({"pack": candidate['id'], "kind": "manifest", "message": str(exc)})
            continue
        source_count = 0
        business_modules.update(path.stem for path in pack['root'].rglob('*.py')
                                if path.stem != '__init__' and path.resolve() not in core_paths)
        for relative in pack.get('case_dirs', []):
            folder = (ROOT / relative).resolve()
            business_modules.update(path.stem for path in folder.glob('*.py')
                                    if path.resolve() not in core_paths and path.stem != '__init__')
            for source in folder.glob('*_cases.py'):
                source_count += 1
                if source.resolve() in owners:
                    findings.append({'kind': 'shared_case_source', 'pack': pack['id'],
                                     'message': f"案例源码同时归属 {owners[source.resolve()]} 和 {pack['id']}"})
                owners[source.resolve()] = pack['id']
        configured = pack.get('config_defaults', {}).get('PLATFORM_PACK_DATA_DIR')
        data_root = (ROOT / configured).resolve() if configured else pack['root'] / 'data'
        for owned_root, owner in data_roots.items():
            if data_root == owned_root or data_root in owned_root.parents or owned_root in data_root.parents:
                findings.append({'kind': 'shared_data_root', 'pack': pack['id'],
                                 'message': f"项目数据根同时归属 {owner} 和 {pack['id']}"})
        data_roots[data_root] = pack['id']
        packs.append({'id': pack['id'], 'case_modules': source_count})
    findings.extend(core_findings(ROOT, business_modules=business_modules))
    findings.extend(common_findings(ROOT))
    findings.extend(platform_findings(ROOT))
    report = {'ok': not findings, 'core_files': len(CORE_FILES), 'packs': packs, 'findings': findings,
              'common_files': len(list((ROOT / 'test-platform/common').glob('*.py'))),
              'scope': '声明的 Core 源码、Pack manifest 与资源归属；不执行业务案例'}
    if with_demo and report['ok']:
        report['demo'] = demo_smoke(ROOT)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo-smoke', action='store_true', help='额外执行一条 demo mock 案例与临时历史库验证')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    report = build_report(with_demo=args.demo_smoke)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"平台隔离检查：{'通过' if report['ok'] else '失败'}")
        for finding in report['findings']:
            print(f"- {finding.get('file', finding.get('pack', 'manifest'))}: {finding['message']}")
        if report.get('demo'):
            print('DEMO-R01 与临时 SQLite：通过；外部连接 0')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
