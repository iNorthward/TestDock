"""Recoverable two-file output commits for local tools (POSIX).

Renames are atomic per file, not across files. Cooperative writers hold both
locks; a durable journal allows rollback after a process interruption.
"""
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class OutputPair:
    def __init__(self, first, second, *, check=False):
        supplied = [Path(first), Path(second)]
        for path in supplied:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError('输出路径须为普通文件，不允许目录或符号链接')
            if not path.parent.is_dir():
                raise ValueError('输出路径的父目录不存在')
        self.paths = [path.resolve() for path in supplied]
        if (self.paths[0] == self.paths[1]
                or all(path.exists() for path in self.paths) and os.path.samefile(*self.paths)):
            raise ValueError('两份输出路径指向同一文件')
        self.journal = self.paths[0].with_name('.' + self.paths[0].name + '.platform-sync.json')
        self.check = check
        self.recovered = False
        self.stack = ExitStack()

    def __enter__(self):
        lock_root = Path(tempfile.gettempdir()) / ('platform-output-locks-' + str(os.getuid()))
        lock_root.mkdir(mode=0o700, exist_ok=True)
        try:
            for path in sorted(self.paths):
                key = hashlib.sha256(str(path).encode()).hexdigest()
                descriptor = os.open(lock_root / key, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                stream = self.stack.enter_context(os.fdopen(descriptor, 'a+b'))
                fcntl.flock(stream, fcntl.LOCK_EX)
            if self.journal.is_symlink():
                raise ValueError('[incomplete] 输出事务记录不允许符号链接')
            if self.journal.exists():
                if self.check:
                    raise ValueError('[incomplete] 存在未完成的输出事务，请先运行 --recover')
                self._recover()
                self.recovered = True
            self.original = [path.read_bytes() if path.exists() else None for path in self.paths]
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        self.stack.close()

    def _stage(self, path, content, suffix):
        descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.platform-sync.', suffix=suffix, dir=path.parent)
        staged = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(staged, stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644)
            return staged
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def _write_journal(self, data):
        staged = self._stage(self.journal, (json.dumps(data, ensure_ascii=False) + '\n').encode(), '.journal')
        try:
            os.replace(staged, self.journal)
            sync_directory(self.journal.parent)
        finally:
            staged.unlink(missing_ok=True)

    def _validate_records(self, data):
        if (not isinstance(data, dict) or data.get('version') != 1
                or data.get('phase') not in ('prepared', 'committed')
                or not isinstance(data.get('files'), list) or len(data['files']) != 2):
            raise ValueError('[incomplete] 输出事务恢复记录无效')
        for record, path in zip(data['files'], self.paths):
            if not isinstance(record, dict) or record.get('path') != str(path):
                raise ValueError('[incomplete] 恢复记录的输出路径与当前配置不一致')
            for field in ('old_sha256', 'new_sha256'):
                value = record.get(field)
                if field == 'old_sha256' and field in record and value is None:
                    continue
                if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
                    raise ValueError('[incomplete] 恢复记录的内容指纹无效')
            if (record['old_sha256'] is None) != (record.get('backup') is None):
                raise ValueError('[incomplete] 恢复记录的原始文件与备份不一致')
            for field in ('staged', 'backup'):
                value = record.get(field)
                if field == 'backup' and value is None:
                    continue
                if not isinstance(value, str):
                    raise ValueError('[incomplete] 恢复临时文件路径无效')
                artifact = Path(value)
                if (artifact.parent != path.parent or artifact.is_symlink()
                        or not artifact.name.startswith('.' + path.name + '.platform-sync.')
                        or not artifact.name.endswith('.new' if field == 'staged' else '.bak')):
                    raise ValueError('[incomplete] 恢复临时文件路径越界')
        return data['files']

    def _cleanup(self, records):
        for record in records:
            for field in ('staged', 'backup'):
                if record.get(field):
                    Path(record[field]).unlink(missing_ok=True)
        self.journal.unlink(missing_ok=True)
        for parent in {path.parent for path in self.paths}:
            sync_directory(parent)

    def _recover(self):
        if self.journal.is_symlink():
            raise ValueError('[incomplete] 输出事务记录不允许符号链接')
        data = json.loads(self.journal.read_text())
        records = self._validate_records(data)
        # Validate the whole recovery before touching either output.
        for record, path in zip(records, self.paths):
            current = fingerprint(path)
            if data['phase'] == 'committed':
                if current != record['new_sha256']:
                    raise ValueError('[incomplete] 已提交输出被外部修改，停止自动恢复')
            elif current not in (record['old_sha256'], record['new_sha256']):
                raise ValueError('[incomplete] 输出被外部修改，停止自动恢复')
            if (data['phase'] == 'prepared' and current != record['old_sha256']
                    and record['old_sha256'] is not None
                    and (not record['backup'] or fingerprint(Path(record['backup'])) != record['old_sha256'])):
                raise ValueError('[incomplete] 原始备份缺失或损坏，停止恢复')
        if data['phase'] == 'prepared':
            for record, path in zip(records, self.paths):
                if fingerprint(path) == record['old_sha256']:
                    continue
                if record['old_sha256'] is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(record['backup'], path)
                sync_directory(path.parent)
        self._cleanup(records)

    def write(self, contents):
        if self.check or len(contents) != 2 or any(not isinstance(content, bytes) for content in contents):
            raise ValueError('只读检查不能写入，输出必须恰好两份')
        records = []
        try:
            for path, original, content in zip(self.paths, self.original, contents):
                if (path.read_bytes() if path.exists() else None) != original:
                    raise ValueError('输出在计算期间被外部修改，停止提交')
                record = {'path': str(path), 'staged': None, 'backup': None,
                          'old_sha256': hashlib.sha256(original).hexdigest() if original is not None else None,
                          'new_sha256': hashlib.sha256(content).hexdigest()}
                records.append(record)
                record['staged'] = str(self._stage(path, content, '.new'))
                if original is not None:
                    record['backup'] = str(self._stage(path, original, '.bak'))
            data = {'version': 1, 'phase': 'prepared', 'files': records}
            self._write_journal(data)
            for record, path in zip(records, self.paths):
                os.replace(record['staged'], path)
                sync_directory(path.parent)
            data['phase'] = 'committed'
            self._write_journal(data)
            self._cleanup(records)
        except BaseException:
            if self.journal.exists():
                try:
                    self._recover()
                except BaseException as recovery_error:
                    raise OSError('输出恢复未完成；保留事务记录：' + str(self.journal)) from recovery_error
            else:
                for record in records:
                    for field in ('staged', 'backup'):
                        if record.get(field):
                            Path(record[field]).unlink(missing_ok=True)
            raise
