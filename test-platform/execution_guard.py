"""Nonblocking, reentrant POSIX resource guards shared by panel and local CLIs."""
import errno
import fcntl
import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar

from project_paths import runtime_data_path

_HELD = ContextVar("execution_resource_leases", default=None)


class ExecutionBusy(RuntimeError):
    pass


def lock_directory():
    return runtime_data_path(".execution-locks")


def guarded():
    return any(lease[2] and lease[3] == os.getpid() for lease in (_HELD.get() or {}).values())


@contextmanager
def resource_guard(*, exclusive=(), shared=()):
    modes = {}
    for keys, mode in ((shared, fcntl.LOCK_SH), (exclusive, fcntl.LOCK_EX)):
        if not isinstance(keys, (list, tuple)) or any(not isinstance(k, str) or not k.strip() for k in keys):
            raise ValueError("执行资源必须是非空字符串列表")
        for key in keys:
            modes[key] = mode
    # Context copies may delegate work to joined workers. The lease is shared,
    # while additions remain local to each context. Released leases cannot be
    # reused by a child that outlives its parent.
    held = {key: lease for key, lease in (_HELD.get() or {}).items() if lease[2] and lease[3] == os.getpid()}
    token = _HELD.set(held)
    acquired = []
    try:
        for key, mode in sorted(modes.items()):
            if key in held:
                if held[key][1] == fcntl.LOCK_SH and mode == fcntl.LOCK_EX:
                    raise ExecutionBusy("当前执行不能把共享资源升级为独占；未开始新写操作")
                continue
            directory = lock_directory()
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = hashlib.sha256(key.encode("utf-8")).hexdigest()+".lock"
            fd = os.open(directory / name, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ExecutionBusy("所需资源正在被另一执行使用；未开始本次写操作") from None
                raise
            held[key] = [fd, mode, True, os.getpid()]
            acquired.append(key)
        yield
    finally:
        cleanup_error = None
        try:
            for key in reversed(acquired):
                lease = held.pop(key)
                fd = lease[0]
                lease[2] = False
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
                except OSError as exc:
                    cleanup_error = cleanup_error or exc
        finally:
            _HELD.reset(token)
        if cleanup_error is not None:
            raise cleanup_error
