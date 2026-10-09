"""Reproducible, credential-free execution metadata for the selected Pack."""
import hashlib
import json
import subprocess
from pathlib import Path

from identity_pool import pool_path, redact_secrets
from pack_registry import REPO_ROOT, load_packs
from platform_config import project_path


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


def execution_metadata(options=None):
    pack = load_packs()[0]
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                                  capture_output=True, text=True, timeout=3, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = None
    core = json.loads((REPO_ROOT / "resources/platform-core-files.json").read_text(encoding="utf-8"))
    sources = {REPO_ROOT / name for name in core}
    sources.update(_project_sources(pack))
    sources.add(pack["manifest_path"])
    sources.update((REPO_ROOT / "assets").glob("*.js"))
    code = hashlib.sha256()
    for path in sorted({path.resolve() for path in sources}):
        code.update(str(path.relative_to(REPO_ROOT.resolve())).encode())
        code.update((file_fingerprint(path) or "missing").encode())
    contracts = {}
    for label, config_key in pack.get("execution_contract_sources", {}).items():
        path = project_path(config_key)
        contracts[label] = file_fingerprint(path) if path else None
    return {"packId": pack["id"], "codeRevision": revision, "codeFingerprint": code.hexdigest(),
            "contractFingerprints": contracts, "identityPoolFingerprint": file_fingerprint(pool_path()),
            "executionOptions": redact_secrets(options or {})}
