"""A single synthetic, read-only example for trying the platform offline."""
from __future__ import annotations

from api_client import ApiSession
from examples.demo_pack.transport import MockTransport
from response_envelope import get_response_envelope

SUITES = [{
    "id": "demo-readonly",
    "name": "Demo · 离线只读样例",
    "parent": "demo",
    "endpoint": "GET /health (mock transport)",
    "account": "offline fixture",
    "hasCleanup": False,
}]

CATALOG = [{
    "id": "DEMO-R01",
    "suite": "demo-readonly",
    "group": "DEMO·只读",
    "desc": "通过 mock transport 读取本地 health fixture",
    "mode": "safe",
    "expect": "read",
    "api": [],
    "expected": "返回 status=ready；不访问网络、数据库或真实业务服务",
}]
CAT = {case["id"]: case for case in CATALOG}
RUNNER_API = {"read": []}


def _read_fixture(_case):
    transport = MockTransport()
    client = ApiSession(
        tenant="demo-tenant",
        default_user="demo-reader",
        base="https://demo.invalid",
        role="cli",
        transport=transport,
    )
    status, response = client.get("/health")
    envelope = get_response_envelope()
    data = envelope.data(response) if envelope and envelope.is_ok(response) else None
    if status != 200 or not isinstance(data, dict) or data.get("status") != "ready":
        detail = f"unexpected mock response: {response}"
        return {
            "ok": False,
            "status": "failed",
            "subs": [{"label": "GET /health", "ok": False, "detail": detail}],
        }
    return {
        "ok": True,
        "status": "passed",
        "subs": [{
            "label": "GET /health",
            "ok": True,
            "detail": f"via mock; source={data.get('source')}; calls={len(transport.calls)}",
        }],
    }


RUNNERS = {"read": _read_fixture}


def exec_case(case_id):
    if case_id != "DEMO-R01":
        return {"id": case_id, "ok": False, "error": "unknown demo case"}
    result = _read_fixture(CAT[case_id])
    result["id"] = case_id
    return result
