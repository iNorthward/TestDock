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
import re
import socket
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from weakref import WeakKeyDictionary
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional, Protocol

import env_config as ec

class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if _origin(request.full_url) != _origin(new_url):
            raise urllib.error.URLError("跨源重定向已拒绝，防止认证信息泄漏")
        return super().redirect_request(request, response, code, message, headers, new_url)


def _origin(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("请求 URL 须为无凭据的 HTTP(S) 地址")
    return parsed.scheme.lower(), parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), SameOriginRedirect())
DEFAULT_TIMEOUT = 30


class NoPreparedRedirect(urllib.request.HTTPRedirectHandler):
    """A prepared target cannot change after signing, even within one origin."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


PREPARED_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoPreparedRedirect())
_RAW_BODY_UNSET = object()


class _JsonNull:
    def __repr__(self):
        return "JSON_NULL"


JSON_NULL = _JsonNull()
"""Explicit JSON null. Existing data=None continues to mean no body."""


def _prepared_headers(headers, body):
    if not isinstance(headers, Mapping):
        raise TypeError("准备请求的 headers 须为映射")
    result, names = {}, set()
    for name, value in headers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("准备请求包含非法 header 名称")
        if not isinstance(value, str) or any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value):
            raise ValueError("准备请求包含非法 header 值")
        lower = name.lower()
        if lower in names:
            raise ValueError("准备请求包含重复 header")
        if lower == "transfer-encoding":
            raise ValueError("固定正文准备请求不支持 Transfer-Encoding")
        if lower == "connection":
            # urllib's HTTP handler unconditionally sends Connection: close.
            # Freeze that exact value before signing for every transport.
            if value.lower() != "close":
                raise ValueError("准备请求 Connection 只允许 close")
            value = "close"
        if (lower == "content-length" and body is not _RAW_BODY_UNSET
                and value != str(len(body) if body is not None else 0)):
            raise ValueError("Content-Length 与准备正文不一致")
        names.add(lower)
        result[name] = value
    if "connection" not in names:
        result["Connection"] = "close"
    return MappingProxyType(result)


@dataclass(frozen=True)
class PreparedRequest:
    """Immutable HTTP intent; repr deliberately omits URL, headers and body."""

    method: str
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    body: Optional[bytes] = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.method, str) or not re.fullmatch(r"[A-Z]+", self.method):
            raise ValueError("准备请求 method 须为大写 HTTP 方法")
        # urllib removes controls/whitespace and URL wrappers. Reject these
        # before the signer can observe a target different from the wire target.
        if not isinstance(self.url, str) or any(ord(char) <= 32 or ord(char) >= 127 for char in self.url):
            raise ValueError("准备请求 URL 须为已编码、无空白的 HTTP(S) 地址")
        _origin(self.url)
        if urllib.parse.urlsplit(self.url).fragment or "#" in self.url:
            raise ValueError("准备请求 URL 不允许 fragment")
        parsed = urllib.parse.urlsplit(self.url)
        if not parsed.path:
            # HTTP transmits '/' for an empty path. Expose that target before
            # signing instead of leaving the default transport to add it.
            object.__setattr__(self, "url", urllib.parse.urlunsplit(parsed._replace(path="/")))
        if self.body is not None and not isinstance(self.body, bytes):
            raise TypeError("准备请求 body 须为 bytes 或 None")
        object.__setattr__(self, "headers", _prepared_headers(self.headers, self.body))

    @property
    def path(self):
        return urllib.parse.urlsplit(self.url).path or "/"

    @property
    def query(self):
        return urllib.parse.urlsplit(self.url).query

# Adapter registrations shared by the selected Pack; adapters own token caches.
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


class RequestEncoder(Protocol):
    """Encode one supplied JSON value; explicit JSON_NULL is passed as None."""

    def encode(self, data, *, method: str, url: str) -> bytes: ...


class JsonRequestEncoder:
    """Ordinary JSON only; exact numeric strategies belong to the Adapter."""

    def encode(self, data, *, method: str, url: str) -> bytes:
        return json.dumps(data, allow_nan=False).encode("utf-8")


class RequestSigner(Protocol):
    """Read frozen intent and return additional signature headers only."""

    def sign(self, request: PreparedRequest) -> Mapping[str, str]: ...


class PreparedHttpTransport(Protocol):
    """Consume exact bytes without encoding, redirects or implicit retries.

    Like legacy fake transports, an injected transport returns a decoded
    (http_status, body) pair and owns its response decoding strategy.
    """

    def request_prepared(self, request: PreparedRequest, *, timeout=DEFAULT_TIMEOUT): ...


def _preparation_enabled(data, raw_body, request_encoder, request_signer, prepared_transport):
    return (data is JSON_NULL or raw_body is not _RAW_BODY_UNSET or request_encoder is not None
            or request_signer is not None or prepared_transport is not None)


def _validate_transport(transport, prepared_transport, prepared):
    if prepared and transport is not None:
        raise ValueError("准备请求不能使用旧 transport；请显式实现 prepared_transport.request_prepared")
    if prepared_transport is not None and not callable(getattr(prepared_transport, "request_prepared", None)):
        raise TypeError("prepared_transport 须实现 request_prepared")


def prepare_request(method, url, headers, *, data=None, raw_body=_RAW_BODY_UNSET,
                    request_encoder=None, request_signer=None):
    """Encode once, freeze, then append signature headers without changing intent.

    data=None has no body; data=JSON_NULL encodes null; raw_body=b'' is an
    explicit empty body. Raw bytes bypass the JSON encoder entirely.
    """
    if raw_body is not _RAW_BODY_UNSET and data is not None:
        raise ValueError("data 与 raw_body 不能同时提供")
    # Validate the target and base headers before calling any Adapter hook.
    if not isinstance(method, str):
        raise TypeError("准备请求 method 须为字符串")
    target = PreparedRequest(method.upper(), url, {}, None)
    _prepared_headers(headers, _RAW_BODY_UNSET)
    if raw_body is not _RAW_BODY_UNSET:
        body = raw_body
    elif data is None:
        body = None
    else:
        encoder = request_encoder if request_encoder is not None else JsonRequestEncoder()
        body = encoder.encode(None if data is JSON_NULL else data, method=target.method, url=target.url)
        if not isinstance(body, bytes):
            raise TypeError("RequestEncoder.encode 必须返回 bytes")
    request = PreparedRequest(target.method, target.url, headers, body)
    if request_signer is not None:
        additional = request_signer.sign(request)
        if not isinstance(additional, Mapping):
            raise TypeError("RequestSigner.sign 必须返回 header 映射")
        names = {name.lower() for name in request.headers}
        if any(isinstance(name, str) and name.lower() in names for name in additional):
            raise ValueError("签名头不能覆盖准备请求的已有 header")
        request = PreparedRequest(request.method, request.url, {**request.headers, **additional}, request.body)
    return request


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
    validate_auth=True,
    *,
    raw_body=_RAW_BODY_UNSET,
    request_encoder: Optional[RequestEncoder] = None,
    request_signer: Optional[RequestSigner] = None,
    prepared_transport: Optional[PreparedHttpTransport] = None,
):
    """底层请求。返回 (http_status, JSON dict)；2xx 文件响应返回二进制元信息。"""
    from execution_jobs import checkpoint

    checkpoint()
    prepared = _preparation_enabled(data, raw_body, request_encoder, request_signer, prepared_transport)
    _validate_transport(transport, prepared_transport, prepared)
    if prepared:
        request = prepare_request(method, url, headers, data=data, raw_body=raw_body,
                                  request_encoder=request_encoder, request_signer=request_signer)
        validator = request_auth or _REQUEST_AUTH
        if validate_auth and validator is not None:
            validator.validate_headers(request.headers)
        checkpoint()
        if prepared_transport is not None:
            return prepared_transport.request_prepared(request, timeout=timeout)
        req = urllib.request.Request(request.url, data=request.body, headers=dict(request.headers), method=request.method)
        if req.full_url != request.url or req.get_method() != request.method or req.data != request.body:
            raise ValueError("传输改变了准备请求")
        return _send_http(req, PREPARED_OPENER, timeout=timeout, response_decoder=response_decoder,
                          hide_transport_details=True)
    validator = request_auth or _REQUEST_AUTH
    if validate_auth and validator is not None:
        validator.validate_headers(headers)
    if transport is not None:
        return transport.request(
            method, url, dict(headers), data=data, timeout=timeout,
        )
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    return _send_http(req, OPENER, timeout=timeout, response_decoder=response_decoder)


def _send_http(req, opener, *, timeout, response_decoder, hide_transport_details=False):
    method, url = req.get_method(), req.full_url
    try:
        with opener.open(req, timeout=timeout) as r:
            return _decode_response(r.status, r, method=method, url=url, decoder=response_decoder)
    except urllib.error.HTTPError as e:
        try:
            with e:
                return _decode_response(e.code, e, method=method, url=url, decoder=response_decoder)
        except (TimeoutError, socket.timeout):
            return 0, {"_transport_error": True, "_error": "HTTP 请求超时"}
    except (TimeoutError, socket.timeout) as e:
        return 0, {"_transport_error": True, "_error": "HTTP 请求超时"}
    except urllib.error.URLError as e:
        detail = "HTTP 连接失败；未自动重发" if hide_transport_details else str(e.reason or e)
        return 0, {"_transport_error": True, "_error": detail}


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


# One module-owned variable avoids retaining a variable for each short-lived
# session. Weak keys let disposed sessions leave a long-running context too.
_SESSION_TOKENS = ContextVar("session-tokens", default=None)


class ApiSession:
    """CLI 或 MGR 一侧的请求会话。"""

    def __init__(
        self,
        *,
        tenant: str = "",
        default_user: str = "",
        base: str | None = None,
        role: str = "cli",
        token_provider: Optional[TokenProvider] = None,
        request_auth: Optional[RequestAuthProvider] = None,
        transport: Optional[HttpTransport] = None,
        response_decoder: Optional[ResponseDecoder] = None,
        request_encoder: Optional[RequestEncoder] = None,
        request_signer: Optional[RequestSigner] = None,
        prepared_transport: Optional[PreparedHttpTransport] = None,
    ):
        _validate_transport(transport, prepared_transport,
                            _preparation_enabled(None, _RAW_BODY_UNSET, request_encoder, request_signer, prepared_transport))
        self.tenant = tenant
        # Pool role references are lazy; truth-testing them reads project data
        # during adapter bootstrap and can recursively initialize the pack.
        self.default_user = default_user if default_user is not None else ""
        self.base = (base or ec.API).rstrip("/")
        self.role = role
        self.token_provider = token_provider or _TokenProviderProxy()
        self.request_auth = request_auth
        self.transport = transport
        self.response_decoder = response_decoder
        self.request_encoder = request_encoder
        self.request_signer = request_signer
        self.prepared_transport = prepared_transport

    def _token_state(self):
        states = _SESSION_TOKENS.get()
        return states.get(self, (None, None)) if states is not None else (None, None)

    def _set_token_state(self, token, user):
        # Copy on write: copied execution contexts must not mutate each other.
        states = WeakKeyDictionary(_SESSION_TOKENS.get() or {})
        states[self] = (token, user)
        _SESSION_TOKENS.set(states)

    @property
    def _token(self):
        return self._token_state()[0]

    @_token.setter
    def _token(self, value):
        self._set_token_state(value, self._token_state()[1])

    @property
    def _token_user(self):
        return self._token_state()[1]

    @_token_user.setter
    def _token_user(self, value):
        self._set_token_state(self._token_state()[0], value)

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
        allow_external=False,
        raw_body=_RAW_BODY_UNSET,
    ):
        """发请求。with_token=True 时自动 ensure_token（可用 token= 覆盖）。"""
        from execution_jobs import checkpoint

        checkpoint()
        prepared = _preparation_enabled(data, raw_body, self.request_encoder, self.request_signer, self.prepared_transport)
        _validate_transport(self.transport, self.prepared_transport,
                            prepared)
        url = _abs_url(self.base, path_or_url, params)
        if prepared:
            if not isinstance(method, str):
                raise TypeError("准备请求 method 须为字符串")
            url = PreparedRequest(method.upper(), url, {}, None).url
        external = _origin(url) != _origin(self.base)
        if external:
            if not allow_external:
                raise ValueError("跨源 URL 已拒绝；显式 allow_external=True 才可发送无认证请求")
            if extra_headers:
                raise ValueError("跨源请求禁止自定义请求头")
            return http(method, url, {"Content-Type": "application/json"}, data=data,
                        timeout=timeout, transport=self.transport, response_decoder=self.response_decoder,
                        validate_auth=False, raw_body=raw_body, request_encoder=self.request_encoder,
                        prepared_transport=self.prepared_transport)
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
        return http(
            method, url, hdrs, data=data, timeout=timeout,
            request_auth=self.request_auth, transport=self.transport,
            response_decoder=self.response_decoder,
            raw_body=raw_body, request_encoder=self.request_encoder,
            request_signer=self.request_signer, prepared_transport=self.prepared_transport,
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
    """兼容命名会话。默认不带登录用户。"""
    return ApiSession(
        tenant=ec.CLI_TENANT,
        default_user="",
        base=base or ec.NODE_API,
        role="node",
    )


# ---------------------------------------------------------------------------
# 兼容命名会话按调用上下文懒加载，避免并发案例串号。
# ---------------------------------------------------------------------------
class _ContextSession:
    def __init__(self, factory):
        self.factory = factory
        self.context = ContextVar("named-session", default=None)

    def current(self):
        session = self.context.get()
        if session is None:
            session = self.factory()
            self.context.set(session)
        return session

    def __getattr__(self, name):
        return getattr(self.current(), name)


cli = _ContextSession(cli_session)
mgr = _ContextSession(mgr_session)
node = _ContextSession(node_session)


def configure_cli(username=None, *, role=None):
    """切换默认 CLI 账号（只能指向账号池中的身份）。"""
    session = cli_session(username, role=role)
    cli.context.set(session)
    return session


def ensure_tokens(*, cli_user=None, cli_role=None, need_cli=True, need_mgr=True):
    """兼容旧用例的 ensure_tokens()。

    返回 (cli_token_or_None, mgr_token_or_None)。
    """
    session = configure_cli(cli_user, role=cli_role) if need_cli else None
    return (session.ensure_token() if session else None,
            mgr.ensure_token() if need_mgr else None)


def cli_token(username=None, *, role=None) -> str:
    session = configure_cli(username, role=role) if username or role else cli.current()
    return session.ensure_token(username)


def mgr_token(username=None) -> str:
    if username:
        mgr.context.set(mgr_session(username))
    return mgr.ensure_token(username)
