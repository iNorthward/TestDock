#!/usr/bin/env python3
"""Select explicit offline unittest modules and Node tests from source changes."""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RegressionGroups(dict):
    """Explicit groups plus resource patterns that must never select zero tests."""
    def __init__(self, groups=(), *, required_sources=(), offline_environment=None):
        super().__init__(groups)
        self.required_sources = tuple(required_sources)
        self.offline_environment = dict(offline_environment or {})
        self.resolved_inputs = {}


def _patterns(values, label):
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip()
                                          or Path(value).is_absolute() or ".." in Path(value).parts
                                          for value in values):
        raise ValueError(f"{label} 须为仓库内的非空相对路径模式数组")
    return values


def read_groups(path, root=ROOT, *, pack_id=None):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("groups"),dict):
        raise ValueError("定向回归清单须声明 version=1 和 groups 对象")
    fixtures = data.get("offline_environment", {})
    if not isinstance(fixtures, dict) or any(
            not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or not isinstance(value, str) or not value.startswith("synthetic-offline-")
            or key.startswith(("PLATFORM_", "PYTHON", "NODE_")) or key in ("PATH", "HOME", "CODEX_HOME")
            for key, value in fixtures.items()):
        raise ValueError("离线环境夹具须使用显式 synthetic-offline- 值，且不能覆盖平台隔离配置")
    groups = RegressionGroups(data["groups"], required_sources=_patterns(data.get("required_sources", []), "required_sources"),
                              offline_environment=fixtures)
    for name, group in groups.items():
        if not isinstance(name,str) or not re.fullmatch(r"[a-z][a-z0-9_.-]*",name) or not isinstance(group,dict):
            raise ValueError("回归组名称与结构无效")
        for field in ("sources", "exclude_sources", "input_keys", "python", "javascript"):
            values=group.get(field,[])
            if not isinstance(values,list) or any(not isinstance(v,str) or not v.strip() for v in values):
                raise ValueError(f"{name}.{field} 须为非空字符串数组")
        if not group.get("python") and not group.get("javascript"):
            raise ValueError(f"回归组 {name} 未声明测试")
        for field in ("sources", "exclude_sources"):
            _patterns(group.get(field, []), name+"."+field)
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in group.get("input_keys", [])):
            raise ValueError("项目输入映射须使用显式配置键")
        for module in group.get("python",[]):
            if re.fullmatch(r"test_[a-zA-Z0-9_]+", module):
                path=(root/"tests"/(module+".py")).resolve()
                owner=root.resolve()/"tests"
            elif (pack_id and module.startswith(f"packs.{pack_id}.tests.")
                  and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*", module)
                  and re.fullmatch(r"test_[a-zA-Z0-9_]+", module.rsplit(".",1)[-1])):
                path=root.joinpath(*module.split(".")).with_suffix(".py").resolve()
                owner=root.resolve()/"packs"/pack_id/"tests"
            else:
                raise ValueError(f"回归测试模块无效或不属于当前 Pack: {module}")
            if owner not in path.parents or not path.is_file():
                raise ValueError(f"回归测试模块无效或缺失: {module}")
        for relative in group.get("javascript",[]):
            path=(root/relative).resolve()
            owners=[root.resolve()/"tests"]
            if pack_id: owners.append(root.resolve()/"packs"/pack_id/"tests")
            if (not any(owner in path.parents for owner in owners)
                    or path.suffix not in (".js", ".cjs", ".mjs") or not path.is_file()):
                raise ValueError(f"Node 测试路径无效或缺失: {relative}")
    return groups


def load_groups():
    sys.path[:0]=[str(ROOT),str(ROOT/"test-platform")]
    from pack_registry import load_packs
    groups=read_groups(ROOT/"resources/targeted-regression.json")
    for pack in load_packs():
        groups.pack_id=pack["id"]
        if pack.get("regression_manifest"):
            extra=read_groups(ROOT/pack["regression_manifest"], pack_id=pack["id"])
            if any(not name.startswith(pack["id"]+".") for name in extra):
                raise ValueError("Pack 回归组须以当前 Pack id 为前缀")
            if groups.keys() & extra.keys():
                raise ValueError("回归组名称重复")
            groups.update(extra)
            groups.required_sources += extra.required_sources
            if groups.offline_environment.keys() & extra.offline_environment.keys():
                raise ValueError("离线环境夹具配置键重复")
            groups.offline_environment.update(extra.offline_environment)
    # Resolve only the selected Pack's declared input paths. Core has no fixed
    # business OAS/schema names, and configured overrides receive the same gate.
    from platform_config import project_path
    for group in groups.values():
        for key in group.get("input_keys", []):
            path=project_path(key)
            if path is None:
                continue
            groups.resolved_inputs[key] = str(path.resolve())
            try:
                relative=path.resolve().relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                continue  # External inputs are not Git changes in this repository.
            group.setdefault("sources", []).append(relative)
            groups.required_sources += (relative,)
    return groups


def changed_files(*, staged=False):
    command=["git","diff","--no-renames","--name-only","-z"]
    command+= ["--cached"] if staged else ["HEAD"]
    result=subprocess.run(command,cwd=ROOT,check=True,capture_output=True)
    names=result.stdout.decode().split("\0")
    if not staged:
        result=subprocess.run(["git","ls-files","--others","--exclude-standard","-z"],cwd=ROOT,check=True,capture_output=True)
        names+=result.stdout.decode().split("\0")
    return sorted(set(filter(None,names)))


def build_plan(groups, files, selected=(), root=ROOT):
    unknown_groups=set(selected)-groups.keys()
    if unknown_groups:
        raise ValueError("未知回归组: "+", ".join(sorted(unknown_groups)))
    chosen=set(selected)
    python, javascript, unknown, mapping = set(),set(),[],{}
    for relative in files:
        matches=[name for name,group in groups.items()
                 if any(fnmatch.fnmatchcase(relative,p) for p in group.get("sources",[]))
                 and not any(fnmatch.fnmatchcase(relative,p) for p in group.get("exclude_sources",[]))]
        mapping[relative]=matches
        chosen.update(matches)
        path=root/relative
        if relative.startswith("tests/test_") and path.suffix==".py":
            if path.is_file(): python.add(path.stem)
        elif (getattr(groups,"pack_id",None) and Path(relative).parts[:3]==("packs",groups.pack_id,"tests")
              and path.name.startswith("test_") and path.suffix==".py"):
            if path.is_file(): python.add(".".join(path.relative_to(root).with_suffix("").parts))
        elif relative.startswith("tests/test_") and path.suffix in (".js", ".cjs", ".mjs"):
            if path.is_file(): javascript.add(relative)
        elif not matches and (path.suffix in (".py",".js",".cjs",".mjs",".sh",".html")
                              or any(fnmatch.fnmatchcase(relative,p) for p in getattr(groups,"required_sources",()))):
            unknown.append(relative)
    for name in chosen:
        python.update(groups[name].get("python",[]))
        javascript.update(groups[name].get("javascript",[]))
    return {"groups":sorted(chosen),"python":sorted(python),"javascript":sorted(javascript),
            "unmappedSources":unknown,"sourceMapping":mapping,"businessCasesExecuted":0}


def execute_plan(plan, *, fixtures=None, inputs=None):
    if plan["unmappedSources"]:
        raise ValueError("新增/变更源码缺回归映射: "+", ".join(plan["unmappedSources"]))
    from offline_process import execute
    return execute(ROOT, plan, fixtures=fixtures, inputs=inputs)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed",action="store_true",help="根据工作区、暂存区与未跟踪源码选择测试（默认）")
    parser.add_argument("--staged",action="store_true",help="只根据暂存改动选择测试")
    parser.add_argument("--group",action="append",default=[],help="指定清单中的回归组，可重复")
    parser.add_argument("--file",action="append",default=[],help="预览或测试指定源码影响，可重复")
    parser.add_argument("--plan",action="store_true",help="只输出选择计划，不运行测试")
    parser.add_argument("--report",type=Path,help="保存 JSON 结果（建议 docs/reports 或临时目录）")
    args=parser.parse_args()
    try:
        groups=load_groups()
        files=args.file or (changed_files(staged=args.staged) if args.changed or args.staged or not args.group else [])
        plan=build_plan(groups,files,args.group)
        if plan["unmappedSources"]: raise ValueError("源码缺回归映射: "+", ".join(plan["unmappedSources"]))
        output=plan if args.plan else execute_plan(plan, fixtures=groups.offline_environment, inputs=groups.resolved_inputs)
        if args.report:
            args.report.parent.mkdir(parents=True,exist_ok=True)
            args.report.write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        displayed=output if args.plan else {key:value for key,value in output.items() if key not in ("sourceMapping","python","javascript")}
        print(json.dumps(displayed,ensure_ascii=False,indent=2))
        return 0 if args.plan or output["ok"] else 1
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        print(str(exc),file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
