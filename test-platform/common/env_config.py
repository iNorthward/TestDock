# -*- coding: utf-8 -*-
"""测试平台统一连接配置与 pack 身份池访问。

环境常量（域名、租户）可用环境变量覆盖；登录身份只从 pack 账号池读取。

取值在访问时再读 project_setting，避免在 pack/adapter 注册默认值之前
把空字符串冻进模块常量。

用法：
    from env_config import CLI_TENANT, MGR_TENANT, MGR_USER, DEFAULT_CLI_USER
    import env_config as ec  # ec.API 始终按当前环境/默认值解析
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from platform_config import load_project_env, project_setting  # noqa: E402

load_project_env()

_LAZY_SETTINGS = {
    "AUTH_BASE": ("PLATFORM_AUTH_BASE", (), ""),
    "API": ("PLATFORM_API_BASE", (), ""),
    "NODE_API": ("PLATFORM_NODE_API_BASE", (), ""),
    "CLI_TENANT": ("PLATFORM_CLI_TENANT", (), ""),
    "MGR_TENANT": ("PLATFORM_MGR_TENANT", (), ""),
}

class _PoolRoleReference(str):
    def __new__(cls, category, role):
        underlying = f"__pool_role__:{category}:{role}"
        obj = super().__new__(cls, underlying)
        obj.category = str(category)
        obj.role = str(role)
        return obj

    def _identity(self):
        from identity_pool import get_identity

        return get_identity(self.category, self.role)

    @property
    def value(self):
        identity = self._identity()
        if identity:
            return str(identity.get("username") or identity.get("login_name") or identity.get("email") or "")
        return ""

    @property
    def label(self):
        return self.value or f"（未配置 {self.category} 角色 {self.role}）"

    def __str__(self):
        return self.label

    def __bool__(self):
        return bool(self.value)

    def __format__(self, spec):
        return format(self.label, spec)

    def __repr__(self):
        return repr(self.label)


def default_role(kind):
    category = project_setting("PLATFORM_DEFAULT_MGR_CATEGORY" if kind == "mgr" else "PLATFORM_DEFAULT_CLI_CATEGORY", default="") or ""
    key = "PLATFORM_DEFAULT_MGR_ROLE" if kind == "mgr" else "PLATFORM_DEFAULT_CLI_ROLE"
    return _PoolRoleReference(category, project_setting(key, default="") or "")


def _default_cli_user() -> str:
    return default_role("cli")


def _resolve(name: str):
    if name == "DEFAULT_CLI_USER":
        return _default_cli_user()
    if name == "MGR_USER":
        return default_role("mgr")
    spec = _LAZY_SETTINGS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    key, aliases, default = spec
    value = project_setting(key, aliases=aliases, default=default)
    return value


def __getattr__(name: str):
    return _resolve(name)


def __dir__():
    return sorted(set(globals()) | set(_LAZY_SETTINGS) | {"DEFAULT_CLI_USER"})


def cli_user(role=None, default=None):
    """按 pack 身份角色返回带角色元数据的登录名；缺身份时仍保留角色引用。"""
    return _PoolRoleReference(project_setting("PLATFORM_DEFAULT_CLI_CATEGORY", default="") or "", role) if role else default_role("cli")


def pool_role(category, role):
    """Return a lazy pool reference so import-time constants follow pool edits."""
    return _PoolRoleReference(category, role)


def cli_account_label(role=None, tenant=None):
    """Suite 展示用：只展示所选池角色的登录名。"""
    u = cli_user(role)
    t = tenant or _resolve("CLI_TENANT")
    return f"{u or '（未配置 CLI 账号）'}（{t}）"
