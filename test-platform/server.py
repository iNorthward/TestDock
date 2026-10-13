# -*- coding: utf-8 -*-
"""测试执行面板后端（标准库）。提供通用 catalog / runner API，并挂载所选 pack 扩展。
启动：python3 test-platform/server.py  （PLATFORM_ENV_FILE 选择环境文件，缺省 .env.local）
按所选 pack 的用例与适配器配置补充外部依赖。

改 *_cases.py / catalog.py 后 server 会自动热重载；浏览器轮询 /api/catalog/revision 同步目录。
"""
import json
import argparse
import mimetypes
import subprocess
import sys
import threading
import logging
import math
import time
import os
import secrets
import signal
import socket
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))
from timezone_config import configure_timezone  # noqa: E402
from platform_config import panel_port  # noqa: E402

configure_timezone()

import catalog as cat  # noqa: E402
from pack_registry import (dispatch_panel_extensions, load_panel_extensions,
                           load_panel_services, panel_html_path,
                           panel_pack_summaries)  # noqa: E402
from project_paths import business_knowledge_subpath, pack_data_path  # noqa: E402

PACK_SERVICES = load_panel_services(cat.PACKS)
PANEL_EXTENSIONS = ()
PANEL_HTML = panel_html_path(cat.PACKS)

import run_history as rh  # noqa: E402
import execution_jobs as jobs  # noqa: E402
from execution_provenance import execution_metadata  # noqa: E402
from identity_pool import redact_secrets  # noqa: E402

from http_security import authorize, RequestRejected

PORT = panel_port()
CATALOG_REVISION = 0
_CATALOG_WATCH_INTERVAL = 1.0
_CATALOG_LOCK = cat.REGISTRY_LOCK
INSPECT_REPORT_DIR = business_knowledge_subpath("PLATFORM_APIFOX_REPORT_SUBDIR")
SCHEMA_REPORT_DIR = business_knowledge_subpath("PLATFORM_SCHEMA_REPORT_SUBDIR")
PACK_DATA_DIR = pack_data_path()


def _path_label(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def _list_inspect_reports():
    if INSPECT_REPORT_DIR is None or not INSPECT_REPORT_DIR.is_dir():
        return []
    items = []
    for path in sorted(INSPECT_REPORT_DIR.glob("*.md"), reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({
            "file": path.name,
            "path": _path_label(path),
            "mtime": int(stat.st_mtime),
        })
    return items


def _read_inspect_report(name):
    if INSPECT_REPORT_DIR is None or not name or ".." in name or "/" in name or "\\" in name:
        return None
    if not name.endswith(".md"):
        return None
    path = (INSPECT_REPORT_DIR / name).resolve()
    try:
        path.relative_to(INSPECT_REPORT_DIR.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    stat = path.stat()
    return {
        "file": name,
        "path": _path_label(path),
        "mtime": int(stat.st_mtime),
        "content": path.read_text(encoding="utf-8"),
    }


def _list_schema_reports():
    if SCHEMA_REPORT_DIR is None or not SCHEMA_REPORT_DIR.is_dir():
        return []
    items = []
    for path in sorted(SCHEMA_REPORT_DIR.glob("*.md"), reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({
            "file": path.name,
            "path": _path_label(path),
            "mtime": int(stat.st_mtime),
        })
    return items


def _read_schema_report(name):
    if SCHEMA_REPORT_DIR is None or not name or ".." in name or "/" in name or "\\" in name:
        return None
    if not name.endswith(".md"):
        return None
    path = (SCHEMA_REPORT_DIR / name).resolve()
    try:
        path.relative_to(SCHEMA_REPORT_DIR.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    stat = path.stat()
    return {
        "file": name,
        "path": _path_label(path),
        "mtime": int(stat.st_mtime),
        "content": path.read_text(encoding="utf-8"),
    }


def _enrich_schema_patrol_result(result, *, write_report=False):
    """补全 Schema 巡检的影响分析与 Agent 提示。"""
    if not isinstance(result, dict):
        return result
    scripts_dir = REPO / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        import db_schema_impact_audit as dsia  # noqa: E402
        impact_out = dsia.run_audit(result, write_report=write_report and bool(result.get("has_changes")))
        result["impact"] = impact_out.get("impact")
        result["diffDigest"] = impact_out.get("diffDigest")
        result["undigested"] = impact_out.get("undigested", False)
        result["agentPrompt"] = impact_out.get("agentPrompt") or ""
        if impact_out.get("reportPath"):
            result["impactReportPath"] = impact_out["reportPath"]
        result.pop("impactError", None)
    except Exception as e:
        result["impactError"] = str(e)
    return result


def sync_case_module_aliases():
    """catalog.reload_registry 后刷新 pack services 与可选面板扩展。"""
    global PACK_SERVICES, PANEL_EXTENSIONS
    PACK_SERVICES = load_panel_services(cat.PACKS)
    PANEL_EXTENSIONS = _build_panel_extensions()


def _case_source_files():
    yield from cat.case_source_files(cat.PACKS)
    catalog_py = ROOT / "catalog.py"
    if catalog_py.is_file():
        yield catalog_py


def _catalog_fingerprints():
    fingerprints = {}
    for path in _case_source_files():
        try:
            fingerprints[str(path)] = path.stat().st_mtime_ns
        except OSError:
            pass
    return fingerprints


_CATALOG_LOADED_FINGERPRINTS = _catalog_fingerprints()


class CatalogReloadBusy(RuntimeError):
    pass


def reload_catalog_registry(reason="manual"):
    """热重载用例模块并 bump revision（供 HTTP / 文件监听共用）。"""
    global CATALOG_REVISION, _CATALOG_LOADED_FINGERPRINTS
    with _CATALOG_LOCK.try_write() as acquired:
        if not acquired:
            raise CatalogReloadBusy("执行中或目录正在重载，请稍后重试")
        fingerprints = _catalog_fingerprints()
        info = cat.reload_registry()
        sync_case_module_aliases()
        CATALOG_REVISION += 1
        _CATALOG_LOADED_FINGERPRINTS = fingerprints
        info["revision"] = CATALOG_REVISION
        info["reason"] = reason
        return info


def reload_catalog_if_idle():
    """Return None while execution is active; the watcher retries next poll."""
    with _CATALOG_LOCK.try_write() as acquired:
        if not acquired:
            return None
        return reload_catalog_registry(reason="watch")


def catalog_revision_payload():
    return {
        "revision": CATALOG_REVISION,
        "cases": len(cat._ALL),
        "suites": len(cat.SUITES),
        "reloadPending": _catalog_fingerprints() != _CATALOG_LOADED_FINGERPRINTS,
    }


def run_catalog_validation():
    """调用 scripts/validate_catalog.py 的静态体检（不执行用例）。"""
    scripts_dir = REPO / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import validate_catalog as vc
    errors, warnings, stats = vc.check_structure()
    source_errors, source_warnings = vc.check_source_antipatterns()
    errors.extend(source_errors)
    warnings.extend(source_warnings)
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
        "revision": CATALOG_REVISION,
    }


def _poll_catalog_reload(state):
    new_fp = _catalog_fingerprints()
    if new_fp == state["fingerprints"]:
        return None
    info = reload_catalog_if_idle()
    if info is not None:
        state["fingerprints"] = new_fp
    return info


def _start_catalog_watcher():
    """后台轮询 *_cases.py mtime，变更时自动 reload（无需手点刷新）。"""
    state = {"fingerprints": _catalog_fingerprints()}

    def loop():
        while True:
            time.sleep(_CATALOG_WATCH_INTERVAL)
            try:
                info = _poll_catalog_reload(state)
                if info is not None:
                    print("[catalog] auto-reload revision=%s cases=%s" % (
                        info.get("revision"), info.get("cases")))
            except Exception as exception:
                logging.getLogger(__name__).error("目录监听失败：%s", redact_secrets(str(exception)))

    threading.Thread(target=loop, name="catalog-watcher", daemon=True).start()


_MAX_JSON_INT = 9007199254740991  # JS Number.MAX_SAFE_INTEGER
def _json_safe(obj):
    """将超过 JS 安全整数范围的 int 序列化为 str，避免前端 ID 精度丢失。"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, int) and abs(obj) > _MAX_JSON_INT:
        return str(obj)
    return obj


def jdump(o):
    return json.dumps(_json_safe(o), ensure_ascii=False, default=str).encode("utf-8")


def _save_schema_patrol_result(result):
    """持久化 schema 巡检（diff）结果。"""
    if not isinstance(result, dict) or not result.get("database"):
        return
    try:
        cache_file = PACK_DATA_DIR / "db_schema_patrol_cache.json" if PACK_DATA_DIR else None
        if cache_file is None:
            return
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception as e:
        logging.getLogger(__name__).error("数据库巡检缓存失败：%s", redact_secrets(str(e)))


def _schema_patrol_sse(handler, max_workers=16):
    """SSE 流式 schema 巡检：读库结构并与本地缓存 diff。"""
    scripts_dir = REPO / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import sync_db_schema as sds

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()

    def progress_callback(current, total, table_name):
        msg = json.dumps({
            "type": "progress",
            "current": current,
            "total": total,
            "table": table_name,
            "percent": round(current / total * 100) if total else 0,
        }, ensure_ascii=False)
        handler.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
        handler.wfile.flush()

    result = sds.patrol_schema_with_progress(
        progress_callback=progress_callback,
        max_workers=max_workers,
    )
    result["timestamp"] = int(time.time())
    _enrich_schema_patrol_result(result, write_report=True)
    _save_schema_patrol_result(result)
    msg = json.dumps({"type": "complete", "result": result}, ensure_ascii=False, default=str)
    handler.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
    handler.wfile.flush()


def _schema_sync_sse(handler, max_workers=16):
    """SSE 流式更新 schema 本地缓存。"""
    scripts_dir = REPO / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import sync_db_schema as sds

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()

    def progress_callback(current, total, table_name):
        msg = json.dumps({
            "type": "progress",
            "current": current,
            "total": total,
            "table": table_name,
            "percent": round(current / total * 100) if total else 0,
        }, ensure_ascii=False)
        handler.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
        handler.wfile.flush()

    result = sds.sync_schema_with_progress(
        progress_callback=progress_callback,
        max_workers=max_workers,
    )
    result["timestamp"] = int(time.time())
    _save_schema_patrol_result({
        "ok": True,
        "database": result.get("database"),
        "total_tables": result.get("total_tables", 0),
        "cached_tables": result.get("total_tables", 0),
        "cache_exists": True,
        "cache_file": result.get("cache_file", "PLATFORM_SCHEMA_JSON"),
        "has_changes": False,
        "diff": {
            "new_tables": [],
            "removed_tables": [],
            "modified_tables": [],
            "unchanged_tables": [],
        },
        "timestamp": result["timestamp"],
        "synced_at": result["timestamp"],
    })
    msg = json.dumps({"type": "complete", "result": result}, ensure_ascii=False, default=str)
    handler.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
    handler.wfile.flush()


def _build_panel_extensions():
    """Build optional pack routers with a bounded core capability map."""
    core = {
        "catalog_revision": lambda: CATALOG_REVISION,
        "catalog_section": cat.catalog_section,
        "reload_catalog_registry": reload_catalog_registry,
        "repo_root": REPO,
    }
    return load_panel_extensions(cat.PACKS, services=PACK_SERVICES, core=core)


PANEL_EXTENSIONS = _build_panel_extensions()


def _panel_execution_options(data):
    """Collect optional pack execution settings under their owning pack IDs."""
    options = {}
    for pack_id, extension in PANEL_EXTENSIONS:
        provider = getattr(extension, "execution_options", None)
        if not callable(provider):
            continue
        pack_options = provider(data)
        if pack_options is None:
            continue
        if not isinstance(pack_options, dict):
            raise TypeError(f"pack {pack_id} execution_options() 必须返回对象")
        options[pack_id] = pack_options
    return options


class PanelHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler):
        self.session_token = secrets.token_urlsafe(32)
        self._lifecycle_condition = threading.Condition()
        self._active_mutations = 0
        self._stopping = False
        self._shutdown_thread = None
        self._shutdown_error = None
        super().__init__(address, handler)

    @contextmanager
    def mutation_slot(self):
        with self._lifecycle_condition:
            allowed = not self._stopping
            if allowed:
                self._active_mutations += 1
        try:
            yield allowed
        finally:
            if allowed:
                with self._lifecycle_condition:
                    self._active_mutations -= 1
                    self._lifecycle_condition.notify_all()

    def request_shutdown(self):
        with self._lifecycle_condition:
            if self._shutdown_thread is not None and self._shutdown_thread.is_alive():
                return
            self._stopping = True
            self._shutdown_error = None
            self._shutdown_thread = threading.Thread(
                target=self._drain_shutdown, name="panel-shutdown", daemon=False)
            self._shutdown_thread.start()

    def _drain_shutdown(self):
        try:
            jobs.begin_shutdown()
            with self._lifecycle_condition:
                while self._active_mutations:
                    self._lifecycle_condition.wait(.1)
            jobs.wait_for_shutdown()
        except Exception as exc:
            # A shutdown failure must not pretend that business cleanup finished.
            self._shutdown_error = redact_secrets(str(exc))
            logging.getLogger(__name__).error("停止清理失败，服务保留运行以便核验：%s", self._shutdown_error)
            return
        self.shutdown()

    def wait_for_shutdown(self):
        if self._shutdown_thread is not None:
            self._shutdown_thread.join()


class H(BaseHTTPRequestHandler):
    io_timeout_seconds = 10

    def setup(self):
        super().setup()
        # An incomplete body or a blocked response must not hold shutdown forever.
        self.connection.settimeout(self.io_timeout_seconds)

    def log_message(self, format_string, *args):
        # Do not log query strings, headers, credentials or request bodies.
        logging.getLogger(__name__).info("request=%s method=%s status=%s",
                                         getattr(self, "request_id", "-"), self.command,
                                         args[1] if len(args) > 1 else "-")

    def _authorize(self, *, mutation=False):
        self.request_id = uuid.uuid4().hex
        path = urlparse(self.path).path
        try:
            authorize(self.headers, port=self.server.server_port,
                      token=self.server.session_token, api=path.startswith("/api/"),
                      session=path == "/api/session", mutation=mutation)
        except RequestRejected as exc:
            self.close_connection = True
            self._json(exc.status, {"ok": False, "error": str(exc)})
            return False
        return True

    def _diagnostic_allowed(self, path):
        from pack_registry import diagnostic_capabilities
        capability = ("contract" if path.startswith("/api/apifox/") or path == "/api/coverage"
                      else "database" if path.startswith("/api/db-schema/") else None)
        if capability and capability not in diagnostic_capabilities(cat.PACKS[0]):
            self._json(404, {"ok": None, "status": "incomplete", "error": "当前 Pack 未声明该诊断能力"})
            return False
        return True

    def _html(self, path):
        page = path.read_text(encoding="utf-8")
        # Load before inline scripts and any other fetch wrappers.
        page = page.replace("<head>", '<head><script src="/assets/security-client.js"></script>', 1)
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload, no_cache=True):
        known_stop = code == 503 and payload.get("status") == "incomplete" and payload.get("ok") is None
        if code >= 500 and not known_stop:
            request_id = getattr(self, "request_id", uuid.uuid4().hex)
            logging.getLogger(__name__).error("request=%s internal_error=%r", request_id, redact_secrets(payload.get("error")))
            payload = {"ok": False, "error": "服务内部错误，请查看服务日志", "requestId": request_id}
        b = jdump(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if no_cache:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _qs_flag(self, qs, key):
        return (qs.get(key) or ["0"])[0].lower() in ("1", "true", "yes")

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            raw = self.rfile.read(n)
        except (TimeoutError, socket.timeout) as exc:
            self.close_connection = True
            raise RequestRejected(408, "请求体读取超时，操作未开始") from exc
        if len(raw) != n:
            raise ValueError("请求体不完整")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("请求 JSON 不允许重复键")
                result[key] = value
            return result
        def finite_number(text):
            value = float(text)
            if not math.isfinite(value):
                raise ValueError("请求 JSON 数字须为有限值")
            return value
        def reject_constant(text):
            raise ValueError("请求 JSON 不允许非有限数字")
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_float=finite_number, parse_constant=reject_constant)

    def _serve_repo_asset(self, rel, *, asset_root=None):
        rel = rel.lstrip("/")
        if not rel or ".." in rel.split("/"):
            self.send_error(403)
            return
        asset_root = Path(asset_root or REPO / "assets").resolve()
        fp = (asset_root / rel).resolve()
        if not fp.is_relative_to(asset_root):
            self.send_error(403)
            return
        if not fp.is_file():
            self.send_error(404)
            return
        suffix = fp.suffix.lower()
        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        if suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif suffix == ".svg":
            ctype = "image/svg+xml"
        b = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._authorize():
            return
        try:
            self._dispatch_GET()
        except (BrokenPipeError, ConnectionResetError):
            logging.getLogger(__name__).info("request=%s disconnected", self.request_id)
        except Exception:
            logging.getLogger(__name__).exception("request=%s GET failed", self.request_id)
            self._json(500, {"error": "GET handler failed"})

    def _dispatch_GET(self):
        p = urlparse(self.path)
        qs = parse_qs(p.query)
        if p.path == "/api/session":
            self._json(200, {"ok": True, "token": self.server.session_token})
            return
        if p.path in ("/api/catalog/reload", "/api/db-schema/sync-stream", "/api/db-schema/patrol-stream") or (p.path == "/api/catalog" and self._qs_flag(qs, "reload")):
            self._json(405, {"ok": False, "error": "此操作须使用 POST"})
            return
        if not self._diagnostic_allowed(p.path):
            return
        if p.path == "/api/document":
            relative = (qs.get("path") or [""])[0]
            document = (REPO / relative).resolve()
            roots = [REPO / "docs", REPO / "examples" / "adapter_recipes"]
            if document.suffix != ".md" or not any(document.is_relative_to(root.resolve()) for root in roots):
                self._json(403, {"ok": False, "error": "文档路径不允许访问"})
                return
            try:
                self._json(200, {"ok": True, "data": {"content": document.read_text(encoding="utf-8")}})
            except OSError:
                self._json(404, {"ok": False, "error": "文档不存在或不可读"})
            return
        if p.path == "/whitepaper" or (unquote(p.path).endswith(".md") and p.path.startswith(("/docs/", "/examples/adapter_recipes/"))):
            self._html((ROOT / "document.html"))
            return
        if p.path == "/api/whitepaper":
            document = REPO / "docs" / "architecture" / "TEST_PLATFORM_ARCHITECTURE.md"
            try:
                self._json(200, {"ok": True, "data": {"content": document.read_text(encoding="utf-8")}})
            except OSError:
                self._json(404, {"ok": False, "error": "平台白皮书文件不可读"})
            return
        if p.path == "/api/platform":
            from resource_pool_config import selected_definition
            from pack_registry import diagnostic_capabilities
            from platform_protocol import API_VERSION, FEATURES, compatibility
            self._json(200, {'ok': True, 'data': {
                'pack': cat.PACKS[0]['id'], 'platformApiVersion': API_VERSION, 'platformFeatures': sorted(FEATURES),
                'compatibility': compatibility(cat.PACKS[0]), 'resourcePool': selected_definition(),
                'diagnostics': diagnostic_capabilities(cat.PACKS[0]),
                'name': cat.PACKS[0].get('display_name', cat.PACKS[0]['id'])}})
            return
        if p.path == "/api/execution/queue":
            self._json(200, {'ok':True,'data':jobs.get_executor().snapshot()})
            return
        if p.path == "/api/execution/job":
            try:
                job = jobs.get_store().get((qs.get("id") or [None])[0])
                self._json(200 if job else 404, {"ok": bool(job), "data": job, "error": None if job else "任务不存在"})
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if dispatch_panel_extensions(PANEL_EXTENSIONS, "GET", self, p.path, qs):
            return
        if p.path.startswith("/assets/"):
            self._serve_repo_asset(p.path[len("/assets/"):])
        elif p.path.startswith("/pack-assets/"):
            self._serve_repo_asset(p.path[len("/pack-assets/"):], asset_root=cat.PACKS[0]["root"] / "assets")
        elif p.path in ("/", "/index.html"):
            self._html(PANEL_HTML)
        elif p.path == "/api/history":
            try:
                limit = rh.validate_history_parameter(
                    (qs.get("limit") or ["50"])[0], name="limit",
                    maximum=rh.MAX_HISTORY_LIMIT,
                )
                self._json(200, {"ok": True, "data": rh.recent_runs(limit)})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/history/compare":
            try:
                from history_compare import compare_runs
                first=(qs.get('before') or [''])[0];second=(qs.get('after') or [''])[0]
                if not first.isascii() or not first.isdecimal() or not second.isascii() or not second.isdecimal() or not 0<int(first)<2**63 or not 0<int(second)<2**63:
                    raise ValueError('before/after must be positive run IDs')
                before,after=rh.run_detail(first),rh.run_detail(second)
                if before is None or after is None:
                    self._json(404, {'ok':False,'error':'history run not found'})
                else:self._json(200, {'ok':True,'data':compare_runs(before,after)})
            except ValueError as exc:self._json(400, {'ok':False,'error':str(exc)})
            except Exception as exc:self._json(500, {'ok':False,'error':str(exc)})
        elif p.path == "/api/history/run":
            try:
                detail = rh.run_detail((qs.get("id") or ["0"])[0])
                if detail is None:
                    self._json(404, {"ok": False, "error": "run 不存在"})
                else:
                    self._json(200, {"ok": True, "data": detail})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/history/case":
            try:
                cid = (qs.get("id") or [""])[0]
                limit = rh.validate_history_parameter(
                    (qs.get("limit") or ["20"])[0], name="limit",
                    maximum=rh.MAX_HISTORY_LIMIT,
                )
                self._json(200, {"ok": True, "data": rh.case_history(cid, limit)})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/history/summary":
            try:
                days = rh.validate_history_parameter(
                    (qs.get("days") or ["30"])[0], name="days",
                    maximum=rh.MAX_SUMMARY_DAYS,
                )
                self._json(200, {"ok": True, "data": rh.summary(days)})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/coverage":
            try:
                cov_file = PACK_DATA_DIR / "coverage.json" if PACK_DATA_DIR else None
                if cov_file is None:
                    self._json(404, {"ok": False, "error": "[incomplete] 所选 pack 未配置 PLATFORM_PACK_DATA_DIR"})
                    return
                if cov_file.exists():
                    self._json(200, {"ok": True, "data": json.loads(cov_file.read_text(encoding="utf-8"))})
                else:
                    self._json(404, {"ok": False, "error": "coverage.json 不存在，请先运行 scripts/gen_apifox_coverage_wiki.py"})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/identity-pool":
            try:
                from identity_pool import panel_snapshot

                self._json(200, {"ok": True, "data": panel_snapshot()})
            except Exception as e:
                self._json(500, {
                    "ok": False,
                    "error": f"账号池状态暂不可用（{type(e).__name__}）",
                })
        elif p.path == "/api/env-preflight":
            try:
                scripts_dir = REPO / "scripts"
                if str(scripts_dir) not in sys.path:
                    sys.path.insert(0, str(scripts_dir))
                from check_env import build_preflight_report

                self._json(200, {"ok": True, "data": build_preflight_report()})
            except Exception as e:
                self._json(500, {
                    "ok": False,
                    "error": f"环境预检暂不可用（{type(e).__name__}）",
                })
        elif p.path == "/api/insights/actions":
            try:
                from insights_actions import build_action_sections

                warnings = []
                cov_file = PACK_DATA_DIR / "coverage.json" if PACK_DATA_DIR else None
                coverage = json.loads(cov_file.read_text(encoding="utf-8")) if cov_file and cov_file.exists() else {}
                if not coverage:
                    warnings.append("覆盖索引不可用；配置 PLATFORM_PACK_DATA_DIR 后运行 python3 scripts/gen_apifox_coverage_wiki.py 生成。")

                scorecard = {}
                scripts_dir = REPO / "scripts"
                if str(scripts_dir) not in sys.path:
                    sys.path.insert(0, str(scripts_dir))
                try:
                    from coverage_scorecard import build_scorecard

                    scorecard = build_scorecard("", False)
                except Exception as exc:
                    warnings.append(
                        f"候选评分卡不可用（{type(exc).__name__}）；可运行 python3 scripts/coverage_scorecard.py 排查。"
                    )

                issue_runs = []
                run_details = {}
                try:
                    recent = rh.recent_runs(50)
                    issue_runs = [run for run in recent if run.get("status") in ("failed", "skipped", "incomplete")
                                  or any(int(run.get(key) or 0) for key in ("failed", "skipped", "incomplete"))]
                    for run in issue_runs[:20]:
                        detail = rh.run_detail(run.get("id"))
                        if detail:
                            run_details[int(run["id"])] = detail
                except Exception as exc:
                    warnings.append(f"运行历史暂不可用（{type(exc).__name__}）；请到「回归历史」查看服务状态。")
                data = build_action_sections(coverage, scorecard, issue_runs, run_details)
                data["warnings"] = warnings
                self._json(200, {"ok": True, "data": data})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/apifox/reports":
            try:
                self._json(200, {"ok": True, "data": _list_inspect_reports()})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/apifox/report":
            try:
                name = (qs.get("file") or qs.get("name") or [""])[0]
                data = _read_inspect_report(name)
                if data is None:
                    self._json(404, {"ok": False, "error": "报告不存在"})
                else:
                    self._json(200, {"ok": True, "data": data})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/db-schema/reports":
            try:
                self._json(200, {"ok": True, "data": _list_schema_reports()})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/db-schema/report":
            try:
                name = (qs.get("file") or qs.get("name") or [""])[0]
                data = _read_schema_report(name)
                if data is None:
                    self._json(404, {"ok": False, "error": "报告不存在"})
                else:
                    self._json(200, {"ok": True, "data": data})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path.startswith("/api/error-log/"):
            self._json(404, {
                "ok": False,
                "error": "[incomplete] 当前 pack 未提供可选异常日志审计",
            })
        elif p.path in ("/insights", "/insights.html"):
            self._html((ROOT / "insights.html"))
        elif p.path == "/api/flaky":
            try:
                window = rh.validate_history_parameter(
                    (qs.get("window") or ["10"])[0], name="window",
                    maximum=rh.MAX_HISTORY_WINDOW,
                )
                self._json(200, {"ok": True, "data": rh.flaky_cases(window)})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/case-badges":
            try:
                window = rh.validate_history_parameter(
                    (qs.get("window") or ["10"])[0], name="window",
                    maximum=rh.MAX_HISTORY_WINDOW,
                )
                self._json(200, {"ok": True, "data": rh.case_badges(window)})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/validate":
            try:
                self._json(200, {"ok": True, "data": run_catalog_validation()})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/db-schema/patrol/cache":
            try:
                cache_file = PACK_DATA_DIR / "db_schema_patrol_cache.json" if PACK_DATA_DIR else None
                if cache_file and cache_file.exists():
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    self._json(200, {"ok": True, "data": data})
                else:
                    self._json(200, {"ok": True, "data": None})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif p.path == "/api/catalog/revision":
            self._json(200, {"ok": True, "data": catalog_revision_payload()})
        elif p.path == "/api/catalog":
            lite = self._qs_flag(qs, "lite")
            payload = cat.catalog(lite=lite)
            payload["packs"] = panel_pack_summaries(cat.PACKS)
            payload["catalogRevision"] = CATALOG_REVISION
            self._json(200, {"ok": True, "data": payload})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._authorize(mutation=True):
            return
        try:
            with self.server.mutation_slot() as allowed:
                if not allowed:
                    self._json(503, {"ok": None, "status": "incomplete", "error": "服务正在等待任务清理，请稍后重试"})
                    return
                self._dispatch_POST()
        except (BrokenPipeError, ConnectionResetError):
            logging.getLogger(__name__).info("request=%s disconnected", self.request_id)

    def _dispatch_POST(self):
        try:
            path = urlparse(self.path).path
            if not self._diagnostic_allowed(path):
                return
            data = self._body()
            if not isinstance(data, dict):
                self._json(400, {"ok": False, "error": "请求 JSON 须为对象"})
                return
            if dispatch_panel_extensions(PANEL_EXTENSIONS, "POST", self, path, {}, data):
                return
            if path == "/api/execution/cancel":
                job = jobs.get_store().cancel(data.get("id"))
                self._json(200 if job else 404, {"ok": bool(job), "data": job, "error": None if job else "任务不存在"})
            elif path == "/api/catalog/reload":
                try:
                    info = reload_catalog_registry(reason="http-post")
                except CatalogReloadBusy as exc:
                    self._json(409, {"ok": None, "status": "incomplete", "error": str(exc)})
                else:
                    self._json(200, {"ok": True, "data": info})
            elif path == "/api/run":
                result = _run_panel_request(data, batch=True)
                self._json(200, {"ok": True, "data": result})
            elif path == "/api/run-case":
                result = _run_panel_request(data, batch=False)
                self._json(200, {"ok": True, "data": result})
            elif path in ("/api/db-schema/sync-stream", "/api/db-schema/patrol-stream"):
                try:
                    (_schema_sync_sse if path.endswith("/sync-stream") else _schema_patrol_sse)(self)
                except (BrokenPipeError, ConnectionResetError):
                    logging.getLogger(__name__).info("schema stream disconnected")
                except Exception:
                    logging.getLogger(__name__).exception("schema stream failed")
                    self.wfile.write('data: {"type":"error","error":"巡检失败，请查看服务日志"}\n\n'.encode('utf-8'))
                    self.wfile.flush()
                finally:
                    self.close_connection = True
            elif path == "/api/db-schema/sync":
                try:
                    scripts_dir = REPO / "scripts"
                    if str(scripts_dir) not in sys.path:
                        sys.path.insert(0, str(scripts_dir))
                    import sync_db_schema as sds

                    result = sds.sync_schema(dry_run=False, json_output=False)
                    result["timestamp"] = int(time.time())
                    self._json(200, result)
                except Exception as e:
                    logging.getLogger(__name__).error("数据库结构同步失败：%s", redact_secrets(str(e)))
                    self._json(500, {"ok": False, "error": str(e)})
            elif path == "/api/apifox/sync":
                script = REPO / "scripts" / "run_apifox_inspection.py"
                if not script.is_file():
                    script = REPO / "scripts" / "sync_apifox_integrated.py"
                if not script.is_file():
                    self._json(500, {"ok": False, "error": "缺少 scripts/run_apifox_inspection.py"})
                else:
                    cmd = [sys.executable, str(script), "--json"]
                    if data.get("skipFetch"):
                        cmd.append("--skip-fetch")
                    if data.get("skipSafe"):
                        cmd.append("--skip-safe")
                        cmd.append("--skip-safe-reason=ui")
                    p = subprocess.run(
                        cmd,
                        cwd=str(REPO),
                        capture_output=True,
                        text=True,
                        timeout=900,
                    )
                    try:
                        payload = json.loads(p.stdout or "{}")
                    except json.JSONDecodeError:
                        payload = {"ok": False, "error": (p.stderr or p.stdout or "sync 脚本无输出")[:500]}
                    if p.returncode != 0 and payload.get("ok") is not False:
                        payload["ok"] = False
                        payload.setdefault("error", (p.stderr or "sync 失败")[:500])
                    self._json(200 if payload.get("ok") else 500, payload)
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except jobs.RequestConflict as e:
            self._json(409, {"ok": False, "error": str(e)})
        except RequestRejected as e:
            self._json(e.status, {"ok": None, "status": "incomplete", "error": str(e)})
        except ValueError as e:
            self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})


@cat.registry_read
def _validate_panel_execution(data, *, batch):
    mode = data.get("mode", "safe")
    if mode not in ("safe", "live"):
        raise ValueError("mode 必须是 safe 或 live")
    if batch:
        suite = data.get("suite")
        if suite is not None and (not isinstance(suite, str) or suite not in {s["id"] for s in cat.SUITES}):
            raise ValueError("suite 不存在")
        live = mode == "live" or any(
            case.get("requires_write_opt_in") or case.get("requires_unscoped_write_opt_in")
            for case in cat.selected_cases(mode, suite)
        )
        confirmation = suite or "all"
    else:
        case_id = data.get("id")
        if not isinstance(case_id, str) or case_id not in cat._CAT:
            raise ValueError("case id 不存在")
        case = cat._CAT[case_id]
        live = case.get("mode") == "live" or bool(case.get("requires_write_opt_in") or case.get("requires_unscoped_write_opt_in"))
        confirmation = case_id
    if live and (os.environ.get("PLATFORM_ALLOW_LIVE") != "1" or data.get("confirmLive") != confirmation):
        raise ValueError("LIVE 须设置 PLATFORM_ALLOW_LIVE=1 并以 confirmLive 确认 case ID / suite（全量为 all）")


def _run_panel_request(data, *, batch):
    _validate_panel_execution(data, batch=batch)
    options = _panel_execution_options(data)
    with cat.REGISTRY_LOCK.read():
        metadata = execution_metadata(options)

    def execute():
        with cat.REGISTRY_LOCK.read():
            return execute_locked()

    def execute_locked():
        _validate_panel_execution(data, batch=batch)
        captured_metadata = execution_metadata(options)
        t0 = time.monotonic()
        result = (cat.run_all(data.get("mode", "safe"), data.get("suite"), execution_options=options)
                  if batch else cat.exec_case(data.get("id"), execution_options=options))
        result = dict(result)
        if not (result.get("ok") is False or result.get("status") == "failed" or result.get("error")):
            try:
                jobs.checkpoint()
            except (jobs.JobCancelled, jobs.JobTimedOut) as exc:
                result = jobs.stopped_result(result, exc)
        result["executionMetadata"] = captured_metadata
        try:
            if batch:
                result["runId"] = rh.record_run(result, source="panel", duration_ms=int((time.monotonic()-t0)*1000))
            else:
                case_meta = cat._CAT.get(data.get("id"))
                if not case_meta:
                    return result
                if result.get("error"):
                    result["ok"] = False
                result.setdefault("suite", case_meta.get("suite"))
                result["runId"] = rh.record_case(result, source="panel", mode=case_meta.get("mode", "safe"),
                                                duration_ms=int((time.monotonic()-t0)*1000))
            result["historySaved"] = True
        except Exception as exc:
            result["historySaved"] = False
            result["historyError"] = "历史保存失败，请查看服务日志"
            logging.getLogger(__name__).error("案例/批次 %s 历史保存失败：%s", data.get("id") or data.get("suite"), redact_secrets(str(exc)))
        return result

    if data.get('background') is True:
        identifier=jobs.start_background_job(kind='catalog-batch' if batch else 'catalog-case',request_id=data.get('requestId'),
            payload={key:value for key,value in data.items() if key!='requestId'},operation=lambda _id,_store:execute(),metadata=metadata,
            timeout_seconds=data.get('timeoutSeconds',1200))
        job=jobs.get_store().get(identifier)
        return {'ok':None,'status':'incomplete','pending':job['status'] in jobs.ACTIVE,'executionId':identifier,'executionStatus':job['status'],'result':job.get('result')}
    return jobs.run_idempotent(kind="catalog-batch" if batch else "catalog-case", request_id=data.get("requestId"),
                               payload={key: value for key, value in data.items() if key != "requestId"},
                               operation=execute, metadata=metadata, timeout_seconds=data.get("timeoutSeconds",1200))


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_secrets(super().format(record))


def main():
    parser = argparse.ArgumentParser(description="TestDock 当前项目面板")
    parser.add_argument('--pack', default=cat.PACKS[0]['id'])
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()
    if args.pack != cat.PACKS[0]['id'] or args.port != PORT:
        parser.error("启动标签与所选项目 / PLATFORM_PANEL_PORT 不一致")
    logging.basicConfig(level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    store = jobs.get_store()
    recovered, pruned = store.recover(), store.prune()
    logging.getLogger(__name__).info("任务存储就绪：中断 %d，清理 %d", recovered, pruned)
    _start_catalog_watcher()
    for _pack_id, extension in PANEL_EXTENSIONS:
        startup = getattr(extension, "startup", None)
        if callable(startup):
            startup()
    httpd = PanelHTTPServer(("127.0.0.1", PORT), H)
    httpd.daemon_threads = True
    previous_handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    for number in previous_handlers:
        signal.signal(number, lambda _number, _frame: httpd.request_shutdown())
    print("测试面板已启动: http://localhost:%d" % PORT)
    print("用例目录: 监听 *_cases.py 变更并自动热重载 (revision 轮询 /api/catalog/revision)")
    try:
        httpd.serve_forever(poll_interval=.1)
    finally:
        httpd.request_shutdown()
        httpd.wait_for_shutdown()
        httpd.server_close()
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    main()
