#!/usr/bin/env python3
"""Preview case rechecks from reviewed OpenAPI snapshots for the selected Pack."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path

if __package__:
    from .openapi_contract import HTTP_METHODS, OpenAPIContract, schema_issues
    from .validate_catalog import case_contract_bindings, valid_contract_source
else:
    from openapi_contract import HTTP_METHODS, OpenAPIContract, schema_issues
    from validate_catalog import case_contract_bindings, valid_contract_source


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("OpenAPI 含重复 JSON 键")
        result[name] = value
    return result


def _invalid_constant(_value):
    raise ValueError("OpenAPI 含非有限 JSON 数值")


def _comparison_value(value):
    if isinstance(value, dict):
        return (dict, tuple((key, _comparison_value(item)) for key, item in sorted(value.items())))
    if isinstance(value, list):
        return (list, tuple(_comparison_value(item) for item in value))
    # Python equality alone equates true and 1. Preserve raw JSON types, including
    # exact fractional bounds and enum values, for the selected Pack's comparison.
    return type(value), value


def _has_unexpanded_ref(value):
    if isinstance(value, dict):
        return "$ref" in value or any(_has_unexpanded_ref(child) for child in value.values())
    return isinstance(value, list) and any(_has_unexpanded_ref(child) for child in value)


def _operation_values(document):
    paths = document.get("paths") if isinstance(document, dict) else None
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI 缺少有效的 paths 对象")
    for path, item in paths.items():
        if not re.fullmatch(r"/[^\s?#]*", path) or not isinstance(item, dict):
            raise ValueError("OpenAPI path 或 path item 无效")
        if any(name.lower() in HTTP_METHODS and not isinstance(spec, dict)
               for name, spec in item.items()):
            raise ValueError("OpenAPI operation 须为对象")
    operations = {}
    for method, path, spec in OpenAPIContract(document).operations():
        key = method, path
        if key in operations:
            raise ValueError("OpenAPI 存在重复 method/path")
        fields = {name: value for name, value in spec.items() if name not in {
            "summary", "description", "tags", "externalDocs", "x-platform-path-methods",
        }}
        if schema_issues(fields) or _has_unexpanded_ref(fields):
            operations[key] = None
        else:
            operations[key] = _comparison_value(fields)
    return operations


def _snapshot(path):
    if path is None:
        return {"status": "unavailable", "reason": "not_configured"}, {}
    if not path.is_file():
        return {"status": "unavailable", "reason": "file_missing"}, {}
    try:
        raw = path.read_bytes()
    except OSError:
        return {"status": "unavailable", "reason": "file_unreadable"}, {}
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant, parse_float=Decimal)
        operations = _operation_values(document)
    except (ValueError, ArithmeticError, RecursionError):
        return {"status": "unavailable", "reason": "invalid_openapi"}, {}
    return {"status": "read", "sha256": hashlib.sha256(raw).hexdigest()}, operations


def _compare_source(source, before_path, after_path):
    before_meta, before = _snapshot(before_path)
    after_meta, after = _snapshot(after_path)
    comparable = before_meta["status"] == after_meta["status"] == "read"
    changes, unresolved = {}, set()
    keys = before.keys() | after.keys()
    if comparable:
        for key in keys:
            if (key in before and before[key] is None) or (key in after and after[key] is None):
                unresolved.add(key)
            if key not in before:
                changes[key] = "added"
            elif key not in after:
                changes[key] = "removed"
            elif key not in unresolved and before[key] != after[key]:
                changes[key] = "modified"
    report = {"source": source, "before": before_meta, "after": after_meta,
              "status": "complete" if comparable and not unresolved else "incomplete",
              "changes": [{"method": method, "path": path, "change": changes[method, path]}
                          for method, path in sorted(changes)],
              "unresolvedOperations": [{"method": method, "path": path}
                                       for method, path in sorted(unresolved)]}
    return report, keys, changes, unresolved, comparable


def _resolve_binding(binding, source_index, endpoint_sources, missing_pairs):
    # A declared URL may carry request query values. The contract key and
    # preview need only its path template, not those values.
    path = re.split(r"[?#]", binding["path"], maxsplit=1)[0]
    result = {**binding, "path": path}
    key = binding["method"], path
    source = binding["source"]
    if source is None:
        candidates = endpoint_sources.get(key, [])
        if len(candidates) > 1:
            return {**result, "status": "unresolved", "reason": "ambiguous_source",
                    "candidateSources": candidates}
        if missing_pairs:
            return {**result, "status": "unresolved", "reason": "snapshot_unavailable"}
        if not candidates:
            return {**result, "status": "unresolved", "reason": "operation_not_found"}
        source = candidates[0]
    if source not in source_index:
        return {**result, "status": "unresolved", "reason": "source_not_compared"}
    result["source"] = source
    keys, changes, unresolved, comparable = source_index[source]
    if not comparable:
        return {**result, "status": "unresolved", "reason": "snapshot_unavailable"}
    if key not in keys:
        return {**result, "status": "unresolved", "reason": "operation_not_found"}
    if key in changes:
        result["change"] = changes[key]
    if key in unresolved:
        return {**result, "status": "unresolved", "reason": "unresolved_openapi"}
    return {**result, "status": "matched"}


def _case_impact(case, runner_api, source_index, endpoint_sources, missing_pairs):
    roles = {}
    reasons = []
    for role, declarations in case_contract_bindings(case, runner_api).items():
        bindings = [_resolve_binding(binding, source_index, endpoint_sources, missing_pairs)
                    for binding in declarations["bindings"]]
        if not declarations["declared"]:
            status = "undeclared"
        elif declarations["errors"] or any(row["status"] == "unresolved" for row in bindings):
            status = "unresolved"
        else:
            status = "declared"
        roles[role] = {"status": status, "interfaces": bindings}
        if declarations["errors"]:
            roles[role]["errors"] = declarations["errors"]
        for row in bindings:
            if "change" in row:
                reason = {"role": role, **{field: row[field] for field in ("source", "method", "path", "change")}}
                if reason not in reasons:
                    reasons.append(reason)
    result = {"caseId": case["id"], "mode": case.get("mode"), "suite": case.get("suite"),
              "associations": roles, "retestReasons": reasons}
    if reasons or roles["primary"]["status"] != "declared" or roles["fixture"]["status"] == "unresolved":
        result["verification"] = {"ok": None, "status": "incomplete"}
    return result


def _annotate_history(report, history):
    fingerprints = history["contractFingerprints"]
    sources = {source["source"]: source for source in report["sources"]}
    for case in report["cases"]:
        if case["caseId"] not in history["cases"] or not case["retestReasons"]:
            continue
        evidence = []
        for source in sorted({reason["source"] for reason in case["retestReasons"]}):
            observed = fingerprints.get(source)
            if isinstance(observed, str) and re.fullmatch(r"[a-f0-9]{64}", observed):
                matches = [side for side in ("before", "after") if observed == sources[source][side].get("sha256")]
                evidence.append({"source": source, "sha256": observed, "matches": matches})
            else:
                evidence.append({"source": source, "sha256": None, "matches": []})
        case["history"] = {"runId": history["id"], "status": history["cases"][case["caseId"]],
                           "contractFingerprints": evidence}


def build_report(pack_id, modules, snapshots, *, history=None):
    reports, source_index, endpoint_sources = [], {}, {}
    for source, (before, after) in sorted(snapshots.items()):
        report, keys, changes, unresolved, comparable = _compare_source(source, before, after)
        reports.append(report)
        source_index[source] = keys, changes, unresolved, comparable
        for key in keys:
            endpoint_sources.setdefault(key, []).append(source)
    missing_pairs = any(not index[-1] for index in source_index.values())
    cases = [_case_impact(case, getattr(module, "RUNNER_API", {}), source_index, endpoint_sources, missing_pairs)
             for module in modules for case in module.CATALOG]
    cases.sort(key=lambda case: case["caseId"])
    unresolved_cases = [case["caseId"] for case in cases
                        if case["associations"]["primary"]["status"] != "declared"
                        or case["associations"]["fixture"]["status"] == "unresolved"]
    report = {
        "pack": pack_id, "scope": "contract-impact-preview", "businessCasesExecuted": 0,
        "analysisStatus": "incomplete" if unresolved_cases or not reports or
                          any(source["status"] == "incomplete" for source in reports) else "complete",
        "sources": reports, "cases": cases, "unresolvedCaseIds": unresolved_cases,
        "affectedCaseIds": [case["caseId"] for case in cases if case["retestReasons"]],
        "historyEvidence": {"status": "not_requested", "basis": "disk-at-execution",
                            "consumedSnapshotVerified": False},
    }
    if history is not None:
        if history["packId"] and history["packId"] != pack_id:
            report["historyEvidence"].update(status="unavailable", reason="different_pack")
        else:
            report["historyEvidence"].update(status="read", runId=history["id"],
                                             packEvidenceAvailable=history["packId"] == pack_id)
            _annotate_history(report, history)
    return report


def _paths_by_source(values):
    paths = {}
    for value in values:
        source, separator, path = value.partition("=")
        if not separator or not valid_contract_source(source) or not path.strip() or source in paths:
            raise ValueError("每组快照参数须为唯一的 SOURCE=PATH")
        paths[source] = Path(path).expanduser()
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", action="append", default=[], metavar="SOURCE=PATH")
    parser.add_argument("--after", action="append", default=[], metavar="SOURCE=PATH")
    parser.add_argument("--history-run", metavar="RUN_ID", help="可选，只读对照一次历史执行")
    args = parser.parse_args()
    try:
        before, after = _paths_by_source(args.before), _paths_by_source(args.after)
    except ValueError as exc:
        parser.error(str(exc))

    import catalog
    from platform_config import project_path

    pack = catalog.PACKS[0]
    configured = pack.get("execution_contract_sources", {})
    sources = before.keys() | after.keys()
    if not sources:
        snapshots = {"openapi": (project_path("PLATFORM_OAS_BASELINE"), project_path("PLATFORM_APIFOX_INDEX"))}
    else:
        snapshots = {source: (before.get(source), after.get(source) if source not in configured or source in after
                                                else project_path(configured[source])) for source in sources}

    history = None
    if args.history_run is not None:
        from run_history import read_run_evidence, validate_history_parameter

        try:
            run_id = validate_history_parameter(args.history_run, name="history-run", maximum=9223372036854775807)
        except ValueError as exc:
            parser.error(str(exc))
        try:
            history = read_run_evidence(run_id)
        except (sqlite3.Error, OSError, ValueError):
            pass
    report = build_report(pack["id"], catalog.MODULES, snapshots, history=history)
    if args.history_run is not None and history is None:
        report["historyEvidence"].update(status="unavailable", reason="history_not_readable_or_missing")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    incomplete_history = report["historyEvidence"].get("status") == "unavailable"
    return 0 if report["analysisStatus"] == "complete" and not incomplete_history else 2


if __name__ == "__main__":
    raise SystemExit(main())
