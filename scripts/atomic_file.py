"""Small same-directory atomic text replacement for generated repository files."""
from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Replace a text file only after its complete contents are on disk.

    The temporary file lives beside the target, so ``os.replace`` stays on the
    same filesystem. Existing permission bits are retained; new files follow
    the process umask as with a normal ``open(..., "w")``.
    """
    target = Path(path).resolve()
    if not isinstance(text, str):
        raise TypeError("atomic_write_text requires str content")
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        prior_mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        prior_mode = None

    temp_path = None
    fd = None
    for _ in range(10):
        candidate = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            temp_path = candidate
            break
        except FileExistsError:
            continue
    if fd is None or temp_path is None:
        raise FileExistsError(f"无法为 {target} 创建唯一临时文件")

    try:
        if prior_mode is not None:
            os.chmod(temp_path, prior_mode)
        with os.fdopen(fd, "w", encoding=encoding) as output:
            fd = None  # ownership moved to the text stream
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, target)
    finally:
        if fd is not None:
            os.close(fd)
        if temp_path.exists():
            temp_path.unlink()
