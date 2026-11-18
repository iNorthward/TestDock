#!/usr/bin/env python3
"""Run a two-stage synthetic import and keep the evidence of each outcome."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts"), str(ROOT / "test-platform"), str(ROOT / "test-platform/common")]

from atomic_file import atomic_write_text
from execution_jobs import JobCancelled, JobStore, checkpoint, control_context, request_fingerprint

KIND = "synthetic-import"
INPUT = '{"items":["one","two"]}\n'
DETAIL_BUDGET = 256


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def start_round(root):
    run_id = uuid.uuid4().hex
    directory = root / run_id
    directory.mkdir(mode=0o700)
    source = directory / "input.json"
    source.write_text(INPUT, encoding="utf-8")
    plan = {"runId": run_id, "requestId": run_id, "inputSha256": sha256(source),
            "phases": [{"id": "IMPORT", "dependsOn": []}, {"id": "VERIFY-INPUT", "dependsOn": []}]}
    write_json(directory / "plan.json", plan)
    return directory, plan


def import_once(directory, plan, scenario):
    """Initial authorized synthetic submission; the resume path only reads."""
    jobs = JobStore(directory / "execution_jobs.db")
    job, created = jobs.create(kind=KIND, request_key=plan["requestId"], fingerprint=request_fingerprint(plan))
    if not created or not jobs.start(job["id"]):
        return
    if scenario == "cancelled":
        jobs.cancel(job["id"])
    try:
        with control_context(job["id"], store=jobs):
            checkpoint()
            source = directory / "input.json"
            if sha256(source) != plan["inputSha256"]:
                jobs.finish(job["id"], result={"ok": None, "status": "incomplete", "reason": "synthetic input changed"})
            elif scenario == "failed":
                error = "synthetic import rejected before any write"
                (directory / "failure.log").write_text(error + "\n", encoding="utf-8")
                detail = ("synthetic diagnostic line\n" * 30).encode()
                (directory / "details.log").write_bytes(detail[:DETAIL_BUDGET])
                jobs.finish(job["id"], result={"ok": False, "error": error})
            else:
                # Exclusive create stands in for one synthetic business write.
                with (directory / "import.json").open("x", encoding="utf-8") as output:
                    output.write(source.read_text(encoding="utf-8"))
                if scenario != "lost_response":
                    jobs.finish(job["id"], result={"ok": True, "sha256": sha256(directory / "import.json")})
                # Simulate the caller losing the response before a terminal
                # receipt became available. A pending receipt remains queryable.
    except JobCancelled:
        jobs.finish(job["id"], result={"ok": None, "status": "incomplete"}, status="cancelled")


def result(case_id, ok, reason):
    status = "passed" if ok is True else "failed" if ok is False else "incomplete"
    return {"id": case_id, "ok": ok, "status": status,
            "subs": [{"label": reason, "ok": ok, "status": status}]}


def summarize(directory, plan, *, verify_input, cleanup=None, input_path=None):
    jobs = JobStore(directory / "execution_jobs.db")
    receipt = jobs.request_receipt(plan["requestId"], kind=KIND)
    status = receipt["status"] if receipt else None
    import_ok = True if status == "done" else False if status == "failed" else None
    phases = [result("IMPORT", import_ok, status or "receipt missing")]
    source = input_path or directory / "input.json"
    input_matches = source.is_file() and sha256(source) == plan["inputSha256"]
    plan_path = directory / "plan.json"
    try:
        plan_matches = json.loads(plan_path.read_text(encoding="utf-8")) == plan
    except (OSError, ValueError):
        plan_matches = False
    current = plan_matches and input_matches
    phases.append(result("VERIFY-INPUT", True if verify_input and current else None,
                         "plan and input verified" if verify_input and current
                         else "plan or input changed" if verify_input else "not run"))
    if any(phase["ok"] is False for phase in phases):
        ok = False
    else:
        cleanup_verified = cleanup is not None and cleanup["ok"] is True
        ok = True if current and cleanup_verified and all(phase["ok"] is True for phase in phases) else None
    report = result("SYNTHETIC-RUN", ok, "execution scope")
    report.update(runId=plan["runId"], planSha256=request_fingerprint(plan), inputSha256=plan["inputSha256"],
                  evidenceStatus="current" if current else "stale", receipt=receipt, phases=phases,
                  cleanup=cleanup or {"ok": None, "status": "incomplete"},
                  evidence={name: sha256(directory / name) for name in ("import.json", "failure.log", "details.log")
                            if (directory / name).is_file()},
                  detailLogTruncated=(directory / "details.log").is_file())
    return report


def cleanup_import(directory, *, interrupt=False):
    path = directory / "import.json"
    intent = directory / "cleanup-intent.json"
    if not intent.exists():
        write_json(intent, {"ok": None, "status": "incomplete", "ownedArtifact": path.name})
    removed = path.exists()
    path.unlink(missing_ok=True)
    if interrupt:
        return None
    verified = not path.exists()
    receipt = {"ok": True if verified else None, "status": "passed" if verified else "incomplete",
               "removedThisAttempt": removed, "verifiedAbsent": verified}
    first_receipt = directory / "cleanup.json"
    write_json(directory / "cleanup-repeat.json" if first_receipt.exists() else first_receipt, receipt)
    return receipt


def run_demo(root):
    root = Path(root).resolve()
    root.mkdir(mode=0o700)
    # This standalone synthetic process owns both roots. No private env is loaded.
    os.environ.update(PLATFORM_ENV_FILE=os.devnull, PLATFORM_PACKS="demo_pack",
                      PLATFORM_PACK_DATA_DIR=str(root / "history"), PLATFORM_ARTIFACT_ROOT=str(root))
    import run_history

    report = {"scope": "synthetic", "realBusinessVerified": False, "runs": {}}
    for scenario in ("success", "failed", "cancelled", "lost_response"):
        directory, plan = start_round(root)
        import_once(directory, plan, scenario)
        first = summarize(directory, plan, verify_input=scenario == "success")
        write_json(directory / "first-report.json", first)
        cleanup = None
        if scenario == "success":
            # Simulate interruption after removing the owned scratch file, but
            # before collecting a receipt. A retry checks the actual state.
            cleanup_import(directory, interrupt=True)
            interrupted = summarize(directory, plan, verify_input=True)
            write_json(directory / "cleanup-interrupted-report.json", interrupted)
            cleanup = cleanup_import(directory)
            cleanup_import(directory)
        # Opening a fresh JobStore in summarize queries the original request,
        # and the input review can proceed independently of an unknown write.
        final = summarize(directory, plan, verify_input=scenario in ("success", "lost_response"), cleanup=cleanup)
        if scenario == "success":
            final["evidence"] = first["evidence"]
        final["historyId"] = run_history.record_run(
            {"mode": "safe", "cases": final["phases"]}, source="synthetic-task-evidence",
            metadata={"packId": "demo_pack", "runId": plan["runId"], "planSha256": final["planSha256"]},
        )
        write_json(directory / "report.json", final)
        report["runs"][scenario] = final
        if scenario == "lost_response":
            # Inspect the same round with a different input without replacing its
            # original plan, report or source evidence.
            changed = directory / "changed-input.json"
            changed.write_text('{"items":[]}\n', encoding="utf-8")
            drift = summarize(directory, plan, verify_input=False, input_path=changed)
            write_json(directory / "evidence-with-drift.json", drift)
            report["changedInputEvidenceStatus"] = drift["evidenceStatus"]
    report["ok"] = (
        {scenario: run["status"] for scenario, run in report["runs"].items()}
        == {"success": "passed", "failed": "failed", "cancelled": "incomplete", "lost_response": "incomplete"}
        and report["changedInputEvidenceStatus"] == "stale"
    )
    write_json(root / "summary.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory for isolated synthetic evidence")
    args = parser.parse_args()
    report = run_demo(args.output)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
