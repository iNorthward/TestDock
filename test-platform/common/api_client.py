# -*- coding: utf-8 -*-
"""统一 HTTP 入口：项目认证 adapter + HTTP transport。

新增 *_cases.py / *_ops.py 一律经本模块发请求，禁止再本地定义
``_http`` / ``_headers`` / ``OPENER`` / ``ensure_tokens``。

用法::

    from api_client import cli, mgr, http, request_headers

    _, d = cli.get("/v1/items", params={"current": 1, "size": 10})
    _, d = mgr.post("/v1/items", {"title": "..."})

    # 未登录 / 坏 token / 错租户（E 系列）
    _, d = cli.get(path, with_token=False)
    _, d = cli.get(path, token="bad-token")
    _, d = cli.get(path, tenant=MGR_TENANT)  # 错位

会话会缓存本模块级 token；认证与请求头由项目注册的 adapter 提供。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional, Protocol

import env_config as ec

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DEFAULT_TIMEOUT = 30

# Module-level compatibility caches for the existing CLI/MGR helpers.
_CLI_TOKEN: Optional[str] = None
_MGR_TOKEN: Optional[str] = None
_CLI_USER: Optional[str] = None
_TOKEN_PROVIDER = None
_REQUEST_AUTH = None
_RESPONSE_DECODER = None


class TokenProvider(Protocol):
    """认证扩展点：根据用户名与租户提供访问 token。"""

    def get_token(self, username: str, tenant_id: str) -> str: ...


class RequestAuthProvider(Protocol):
    """Project-specific request headers and optional header validation."""

    def build_headers(self, token=None, **options) -> Mapping[str, str]: ...

    def validate_headers(self, headers: Mapping[str, str]) -> None: ...


class HttpTransport(Protocol):
    """HTTP transport contract; packs can inject an offline fake transport."""

    def request(self, method: str, url: str, headers: Mapping[str, str], *,
                data=None, timeout=DEFAULT_TIMEOUT): ...


class ResponseDecoder(Protocol):
    """The selected Adapter explicitly chooses JSON decoding for an operation."""
    def decode_json(self, text: str, *, method: str, url: str) -> Any: ...


def register_response_decoder(decoder: Optional[ResponseDecoder]):
    global _RESPONSE_DECODER
    previous = _RESPONSE_DECODER
    _RESPONSE_DECODER = decoder
    return previous


def register_token_provider(provider: Optional[TokenProvider]):
    """Register the selected pack's default token provider."""
    global _TOKEN_PROVIDER
    previous = _TOKEN_PROVIDER
    _TOKEN_PROVIDER = provider
    return previous


def register_request_auth(provider: Optional[RequestAuthProvider]):
    """Register the selected pack's request-header adapter."""
    global _REQUEST_AUTH
    previous = _REQUEST_AUTH
    _REQUEST_AUTH = provider
    return previous


def _registered_token_provider() -> TokenProvider:
    if _TOKEN_PROVIDER is None:
        raise RuntimeError("未注册 TokenProvider；请由项目 pack 提供 AuthAdapter")
    return _TOKEN_PROVIDER


def _registered_request_auth() -> RequestAuthProvider:
    if _REQUEST_AUTH is None:
        raise RuntimeError("未注册请求认证 adapter；请由项目 pack 提供 AuthAdapter")
    return _REQUEST_AUTH


class _TokenProviderProxy:
    """Resolve registration when called, including after session construction."""

    def get_token(self, username: str, tenant_id: str) -> str:
        return _registered_token_provider().get_token(username, tenant_id)


def http(
    method: str,
    url: str,
    headers: Mapping[str, str],
    data=None,
    timeout=DEFAULT_TIMEOUT,
    request_auth: Optional[RequestAuthProvider] = None,
    transport: Optional[HttpTransport] = None,
    response_decoder: Optional[ResponseDecoder] = None,
):
    """底层请求。返回 (http_status, JSON dict)；2xx 文件响应返回二进制元信息。"""
    from execution_jobs import checkpoint

    checkpoint()
    validator = request_auth or _REQUEST_AUTH
    if validator is not None:
        validator.validate_headers(headers)
    if transport is not None:
        return transport.request(
            method, url, dict(headers), data=data, timeout=timeout,
        )
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return _decode_response(r.status, r, method=method, url=url, decoder=response_decoder)
    except urllib.error.HTTPError as e:
        return _decode_response(e.code, e, method=method, url=url, decoder=response_decoder)
    except urllib.error.URLError as e:
        return 0, {"_transport_error": True, "_error": str(e.reason or e)}


def _decode_response(status, response, *, method='', url='', decoder=None):
    raw = response.read()
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("Content-Type", ""))
    content_disposition = str(headers.get("Content-Disposition", ""))
    try:
        decoded = raw.decode("utf-8-sig")
        strategy = decoder or _RESPONSE_DECODER
        value = (strategy.decode_json(decoded, method=method, url=url) if strategy is not None
                 else json.loads(decoded)) if decoded.strip() else {}
        return status, value
    except (UnicodeDecodeError, json.JSONDecodeError):
        media_type = content_type.split(";", 1)[0].strip().lower()
        is_file = (
            "attachment" in content_disposition.lower()
            or media_type in {
                "application/octet-stream",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/zip",
                "application/pdf",
            }
        )
        if isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300 and is_file:
            return status, {
                "_binary": True,
                "_size": len(raw),
                "_content_type": content_type,
            }
        return status, {
            "_parse_error": True,
            "_size": len(raw),
            "_content_type": content_type,
        }


def request_headers(
    token=None,
    *,
    tenant=None,
    with_token=True,
    basic=None,
    content_type="application/json",
    extra=None,
):
    """Compatibility name; the selected adapter builds project request headers."""
    return _registered_request_auth().build_headers(
        token,
        tenant=tenant or ec.CLI_TENANT,
        with_token=with_token,
        basic=basic,
        content_type=content_type,
        extra=extra,
    )


def _abs_url(base: str, path_or_url: str, params=None) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
    else:
        path = path_or_url if path_or_url.startswith("/") else "/" + path_or_url
        url = base.rstrip("/") + path
    if params:
        qs = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}&{qs}" if "?" in url else f"{url}?{qs}"
    return url


class ApiSession:
    """CLI 或 MGR 一侧的请求会话。"""

    def __init__(
        self,
        *,
        tenant: str,
        default_user: str = "",
        base: str | None = None,
        role: str = "cli",
        token_provider: Optional[TokenProvider] = None,
        request_auth: Optional[RequestAuthProvider] = None,
        transport: Optional[HttpTransport] = None,
        response_decoder: Optional[ResponseDecoder] = None,
    ):
        self.tenant = tenant
        # Pool role references are lazy; truth-testing them reads project data
        # during adapter bootstrap and can recursively initialize the pack.
        self.default_user = default_user if default_user is not None else ""
        self.base = (base or ec.API).rstrip("/")
        self.role = role  # "cli" | "mgr" | "node"
        self.token_provider = token_provider or _TokenProviderProxy()
        self.request_auth = request_auth
        self.transport = transport
        self.response_decoder = response_decoder
        self._token: Optional[str] = None
        self._token_user: Optional[str] = None

    def resolve_user(self, username=None) -> str:
        explicit_reference = hasattr(username, "category") and hasattr(username, "role")
        reference = username if explicit_reference or username else self.default_user
        if hasattr(reference, "category") and hasattr(reference, "role"):
            return reference
        u = (reference or "").strip()
        if not u and self.role == "mgr":
            u = ec.MGR_USER
        return u

    def get_token(self, username=None) -> str:
        user = self.resolve_user(username)
        if not (hasattr(user, "category") and hasattr(user, "role")) and not user:
            from identity_pool import IdentityUnavailable

            reference = ec.default_role(self.role)
            category, role = reference.category, reference.role
            raise IdentityUnavailable(category, role)
        return self.token_provider.get_token(user, self.tenant)

    def ensure_token(self, username=None) -> str:
        user = self.resolve_user(username)
        # Delegate refresh/cache policy to the adapter; this session only retains
        # the token needed to build the next request's headers.
        # 同时避免先请求其他 username 后，不传 username 时误复用他人的 token。
        resolved_token = self.get_token(user)
        self._token = resolved_token
        self._token_user = user
        return resolved_token

    def clear_token(self):
        self._token = None
        self._token_user = None

    def headers(
        self,
        token=None,
        *,
        with_token=True,
        tenant=None,
        extra=None,
    ):
        tok = None
        if with_token:
            tok = token if token is not None else self._token
        auth = self.request_auth or _registered_request_auth()
        return auth.build_headers(
            tok,
            tenant=tenant or self.tenant,
            with_token=with_token and bool(tok),
            extra=extra,
        )

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        data=None,
        params=None,
        token=None,
        with_token=True,
        tenant=None,
        username=None,
        timeout=DEFAULT_TIMEOUT,
        extra_headers=None,
    ):
        """发请求。with_token=True 时自动 ensure_token（可用 token= 覆盖）。"""
        from execution_jobs import checkpoint

        checkpoint()
        if with_token and token is None:
            user = self.resolve_user(username)
            has_role_reference = hasattr(user, "category") and hasattr(user, "role")
            if has_role_reference or user:
                # The shared compatibility cache may be refreshed by another
                # request. Keep this authentication result in the request itself.
                token = self.ensure_token(username)
            else:
                from identity_pool import IdentityUnavailable

                reference = ec.default_role(self.role)
                category, role = reference.category, reference.role
                raise IdentityUnavailable(category, role)
        hdrs = self.headers(
            token=token,
            with_token=with_token,
            tenant=tenant,
            extra=extra_headers,
        )
        url = _abs_url(self.base, path_or_url, params)
        return http(
            method, url, hdrs, data=data, timeout=timeout,
            request_auth=self.request_auth, transport=self.transport,
            response_decoder=self.response_decoder,
        )

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, data=None, **kw):
        return self.request("POST", path, data=data, **kw)

    def put(self, path, data=None, **kw):
        return self.request("PUT", path, data=data, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


def cli_session(username=None, *, role=None, base=None) -> ApiSession:
    """CLI 会话。username 优先；否则按 pack 角色取池身份。"""
    user = (username if hasattr(username, "category") and hasattr(username, "role")
            else (username or "").strip() or ec.cli_user(role))
    return ApiSession(tenant=ec.CLI_TENANT, default_user=user, base=base, role="cli")


def mgr_session(username=None, *, base=None) -> ApiSession:
    """MGR 会话。管理端账号与租户来自项目配置。"""
    user = (username or "").strip() or ec.MGR_USER
    return ApiSession(tenant=ec.MGR_TENANT, default_user=user, base=base, role="mgr")


def node_session(*, base=None) -> ApiSession:
    """trade-node 会话（回调等常 @SkipToken）。默认不带登录用户。"""
    return ApiSession(
        tenant=ec.CLI_TENANT,
        default_user="",
        base=base or ec.NODE_API,
        role="node",
    )


# ---------------------------------------------------------------------------
# 模块级单例 —— 多数用例文件共用，避免每个文件再 ensure_tokens
# ---------------------------------------------------------------------------
cli = cli_session()
mgr = mgr_session()
node = node_session()


def configure_cli(username=None, *, role=None):
    """切换默认 CLI 账号（只能指向账号池中的身份）。"""
    global cli, _CLI_TOKEN, _CLI_USER
    user = (username if hasattr(username, "category") and hasattr(username, "role")
            else (username or "").strip() or ec.cli_user(role))
    cli = cli_session(user)
    _CLI_TOKEN = None
    _CLI_USER = user or None
    return cli


def ensure_tokens(*, cli_user=None, cli_role=None, need_cli=True, need_mgr=True):
    """兼容旧用例的 ensure_tokens()。

    返回 (cli_token_or_None, mgr_token_or_None)。
    """
    global _CLI_TOKEN, _MGR_TOKEN, _CLI_USER
    if need_cli:
        user = (cli_user if hasattr(cli_user, "category") and hasattr(cli_user, "role")
                else (cli_user or "").strip() or ec.cli_user(cli_role))
        has_role_reference = hasattr(user, "category") and hasattr(user, "role")
        if user or has_role_reference:
            if user != _CLI_USER:
                configure_cli(user)
            _CLI_TOKEN = cli.ensure_token(user)
            _CLI_USER = user
        else:
            _CLI_TOKEN = None
    if need_mgr:
        _MGR_TOKEN = mgr.ensure_token()
    return _CLI_TOKEN, _MGR_TOKEN


def cli_token(username=None, *, role=None) -> str:
    if username or role:
        configure_cli(username, role=role)
    return cli.ensure_token(username)


def mgr_token(username=None) -> str:
    if username:
        global mgr
        mgr = mgr_session(username)
    return mgr.ensure_token(username)
