"""Inherited Python guards for explicitly selected offline test processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys

_INSTALLED = False


def installed():
    return _INSTALLED


def read_context(path=None):
    return json.loads(Path(path or os.environ['PLATFORM_OFFLINE_CONTEXT']).read_text())


def record(context, kind, source):
    line = json.dumps({'kind': kind, 'source': source, 'pid': os.getpid()}) + '\n'
    descriptor = os.open(context['attempt_log'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line.encode())
    finally:
        os.close(descriptor)


def child_environment(environment, context):
    supplied = dict(os.environ if environment is None else environment)
    result = dict(supplied)
    for key in ('PLATFORM_OFFLINE_CONTEXT', 'PYTHONDONTWRITEBYTECODE'):
        result[key] = supplied.get(key) or os.environ[key]
    selected = read_context(result['PLATFORM_OFFLINE_CONTEXT'])
    result['PYTHONPATH'] = os.pathsep.join([selected['startup_dir'], selected['scripts'], supplied.get('PYTHONPATH', '')])
    result['NODE_OPTIONS'] = '--require ' + json.dumps(selected['node_guard'])
    # A child that supplies its own synthetic env may change Pack/config inputs,
    # but cannot accidentally return to the real data or environment selector.
    result['PLATFORM_ENV_FILE'] = '/dev/null'
    for key, name in (('PLATFORM_PACK_DATA_DIR', 'data'), ('PLATFORM_ARTIFACT_ROOT', 'artifacts')):
        value = supplied.get(key)
        if not value or any(Path(value).resolve().is_relative_to(Path(root)) for root in selected['protected_roots']):
            result[key] = str(Path(selected['directory']) / name)
    return result


def install():
    global _INSTALLED
    if _INSTALLED or not os.environ.get('PLATFORM_OFFLINE_CONTEXT'):
        return
    context = read_context()
    _INSTALLED = True
    code_root = Path(context['code_root'])
    sys.path[:0] = [str(code_root), str(code_root / 'test-platform'), str(code_root / 'test-platform/common')]
    def blocked(*args, **kwargs):
        record(context, 'network', 'python.socket')
        raise AssertionError('离线回归禁止网络连接（含子进程）')
    for method in ('connect', 'connect_ex', 'sendto', 'sendmsg'):
        if hasattr(socket.socket, method):
            setattr(socket.socket, method, blocked)
    for method in ('create_connection', 'getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'):
        setattr(socket, method, blocked)
    protected_roots = [Path(value).resolve() for value in context['protected_roots']]
    protected_reads = {Path(value).resolve() for value in context['protected_reads']}
    def audit(event, args):
        if event == 'socket.connect':
            blocked()
        if event != 'open' or not args or isinstance(args[0], int):
            return
        path = Path(os.fsdecode(args[0])).resolve()
        mode, flags = args[1], args[2]
        writing = (isinstance(mode, str) and any(letter in mode for letter in 'wax+')) or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if path in protected_reads or writing and any(path == root or root in path.parents for root in protected_roots):
            record(context, 'filesystem', 'python.open')
            raise PermissionError('离线回归禁止访问真实凭据/账号池或写入源码目录')
    sys.addaudithook(audit)
    original_popen = subprocess.Popen
    class OfflinePopen(original_popen):
        def __init__(self, *args, **kwargs):
            kwargs['env'] = child_environment(kwargs.get('env'), context)
            super().__init__(*args, **kwargs)
    subprocess.Popen = OfflinePopen
    # Preserve selected-Pack validation while redirecting only offline pool IO.
    import identity_pool
    original_pool_path = identity_pool.pool_path
    def pool_path(pack_id=None):
        original_pool_path(pack_id)
        return Path(context['directory']) / 'data/identity_pool.json'
    identity_pool.pool_path = pool_path
    import execution_guard
    execution_guard.lock_directory = lambda: Path(context['directory']) / 'data/locks'
