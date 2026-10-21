"""Reproducible, credential-free execution metadata for the selected Pack."""
import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

from identity_pool import pool_path, redact_secrets
from pack_registry import REPO_ROOT, case_source_files, load_packs
from platform_config import project_path


_RUNTIME_LOCK = threading.RLock()
_RUNTIME_SNAPSHOTS = {}


def file_fingerprint(path):
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_sources(pack):
    """Hash selected project code using the same declared ownership boundaries."""
    root = REPO_ROOT.resolve()
    directories = [Path(pack['root']), root / 'adapters' / pack['id']]
    directories.extend(root / Path(value) for value in pack.get('case_dirs', []))
    sources = {root / Path(value) for value in pack.get('adapter_files', [])}
    for directory in directories:
        for path in directory.rglob('*'):
            if (path.suffix in ('.py', '.js', '.cjs', '.mjs', '.html')
                    and 'data' not in path.relative_to(directory).parts):
                sources.add(path)
    for path in sources:
        if not path.resolve().is_relative_to(root):
            raise ValueError('执行版本指纹的项目源码须位于仓库内')
    return {path.resolve() for path in sources}


def _revision():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=3, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _snapshot_key(pack):
    # A fork must establish its own load boundary rather than inherit evidence.
    return (os.getpid(), str(REPO_ROOT.resolve()), pack['id'])


def _fingerprint(sources):
    digest = hashlib.sha256()
    for name, value in sorted(sources.items()):
        digest.update(name.encode())
        digest.update((value or 'missing').encode())
    return digest.hexdigest()


def _source_names(paths):
    root = REPO_ROOT.resolve()
    names = set()
    for path in paths:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError('执行版本指纹的项目源码须位于仓库内')
        names.add(str(resolved.relative_to(root)))
    return names


def capture_code_snapshot():
    """Capture declared sources before the catalog load/reload boundary.

    This is a source inventory, not a measurement of Python's loaded bytecode.
    Callers publish it only after the corresponding load succeeds.
    """
    pack = load_packs()[0]
    core = json.loads((REPO_ROOT / "resources/platform-core-files.json").read_text(encoding="utf-8"))
    sources = {REPO_ROOT / name for name in core}
    sources.update(_project_sources(pack))
    sources.add(pack["manifest_path"])
    sources.update((REPO_ROOT / "assets").glob("*.js"))
    values = {name: file_fingerprint(REPO_ROOT / name) for name in _source_names(sources)}
    cases = case_source_files([{**pack, 'case_dirs': pack.get('case_dirs', [])}])
    return {'key': _snapshot_key(pack), 'sources': values, 'revision': _revision(),
            'caseSources': _source_names(cases)}


def _changed_sources(before, after):
    return {name for name in before.keys() | after.keys()
            if name not in before or name not in after or before[name] != after[name]}


def initialize_runtime_snapshot(snapshot):
    """Bind the successful initial catalog load to its source snapshot.

    Sources changing across the load boundary make the evidence unknown. Core
    and helper snapshots remain fixed until a new process starts. The snapshot
    does not prove which optional files were imported or their bytecode bytes.
    """
    current = capture_code_snapshot()
    if snapshot['key'] != current['key']:
        raise ValueError('运行源码快照不属于当前进程、仓库及 Pack')
    state = {'sources': dict(snapshot['sources']), 'revision': snapshot['revision'],
             'caseSources': set(snapshot['caseSources']),
             'uncertainSources': _changed_sources(snapshot['sources'], current['sources'])}
    with _RUNTIME_LOCK:
        _RUNTIME_SNAPSHOTS[current['key']] = state


def refresh_case_runtime_snapshot(snapshot, case_paths):
    """Publish only the successfully reloaded case sources under catalog lock.

    A case reload does not reload its imported helpers, Adapter, or Core. Their
    original snapshots therefore stay in the runtime inventory even when the
    disk differs. Failed reloads must not call this function.
    """
    current = capture_code_snapshot()
    if snapshot['key'] != current['key']:
        raise ValueError('运行源码快照不属于当前进程、仓库及 Pack')
    names = _source_names(case_paths)
    with _RUNTIME_LOCK:
        state = _RUNTIME_SNAPSHOTS.get(current['key'])
        if state is None:
            return  # A reload cannot establish a missing initial load boundary.
        previous_names = state['caseSources']
        state['uncertainSources'].difference_update(previous_names | names)
        changed = _changed_sources(snapshot['sources'], current['sources']) & names
        # Newly discovered case files must have been present in the pre-load snapshot.
        changed.update(name for name in names if name not in snapshot['sources'])
        state['uncertainSources'].update(changed)
        for name in previous_names - names:
            state['sources'].pop(name, None)
        for name in names:
            state['sources'][name] = snapshot['sources'].get(name)
        state['caseSources'] = names


def runtime_source_status():
    """Safe source drift status without reading contracts or identity-pool data."""
    disk = capture_code_snapshot()
    with _RUNTIME_LOCK:
        state = _RUNTIME_SNAPSHOTS.get(disk['key'])
        runtime = dict(state['sources']) if state else None
        uncertain = sorted(state['uncertainSources']) if state else []
        case_names = set(state['caseSources']) if state else set()
    drift = sorted(_changed_sources(runtime, disk['sources'])) if runtime is not None else []
    return {'basis': 'catalog-load-source-snapshot', 'runtimeKnown': runtime is not None,
            'loadedBytecodeVerified': False, 'diskDriftSources': drift,
            'restartRequired': runtime is None or bool(uncertain or (set(drift) - case_names))}


def execution_metadata(options=None):
    pack = load_packs()[0]
    disk = capture_code_snapshot()
    with _RUNTIME_LOCK:
        state = _RUNTIME_SNAPSHOTS.get(disk['key'])
        runtime = dict(state['sources']) if state else None
        uncertain = sorted(state['uncertainSources']) if state else []
        revision = state['revision'] if state else None
        case_names = set(state['caseSources']) if state else set()
    runtime_fingerprint = _fingerprint(runtime) if runtime is not None else None
    drift = sorted(_changed_sources(runtime, disk['sources'])) if runtime is not None else []
    available = runtime is not None and not uncertain
    contracts = {}
    for label, config_key in pack.get("execution_contract_sources", {}).items():
        path = project_path(config_key)
        contracts[label] = file_fingerprint(path) if path else None
    return {"packId": pack["id"], "codeRevision": revision,
            "codeFingerprint": runtime_fingerprint if available else None,
            "diskCodeRevision": disk['revision'], "diskCodeFingerprint": _fingerprint(disk['sources']),
            "codeDrift": bool(drift) if runtime is not None else None,
            "codeEvidence": {
                "status": 'captured' if available else 'unknown',
                "basis": 'catalog-load-source-snapshot',
                "loadedBytecodeVerified": False,
                "sourceSnapshotFingerprint": runtime_fingerprint,
                "uncertainSources": uncertain, "diskDriftSources": drift,
                "restartRequired": bool(uncertain or (set(drift) - case_names)),
                "note": ('Source snapshots are bound to catalog initialization and successful case reloads. '
                         'They do not attest loaded bytecode; Core/helpers/Adapter need a process restart to refresh. '
                         'Without catalog initialization the runtime version is unknown.'),
            },
            "contractFingerprints": contracts, "identityPoolFingerprint": file_fingerprint(pool_path()),
            "contractEvidence": {"basis": 'disk-at-execution',
                                 "consumedSnapshotVerified": False,
                                 "note": 'Disk fingerprints do not prove which contract snapshot the runner consumed.'},
            "executionOptions": redact_secrets(options or {})}
