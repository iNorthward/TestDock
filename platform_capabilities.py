"""Read-only Pack capability adoption and evidence projection.

Declarations describe integration choices. Evidence describes one explicitly
executed scope; neither configuration nor imports prove runtime behaviour.
This module never imports a Pack, reads environment files, or probes services.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

import platform_protocol

ROOT = Path(__file__).resolve().parent
CAPABILITIES = (
    'http', 'resource_pool', 'database_pool', 'background_tasks',
    'request_deduplication', 'resource_mutex', 'ui_components',
    'evidence_handoff', 'diagnostics',
)
MODES = frozenset({'core', 'pack', 'disabled', 'unconfigured', 'unavailable'})
SCOPES = frozenset({'synthetic', 'mock', 'real_readonly', 'manual_review'})
_SOURCE_SUFFIXES = {'.py', '.js', '.mjs', '.cjs', '.html', '.css'}
_DECLARATION_FIELDS = (
    'capability_adoption', 'resource_pool', 'identity_requirements',
    'diagnostic_capabilities', 'adapter_files', 'bootstrap',
    'panel_extension', 'catalog_extension', 'panel_html', 'panel_services', 'platform_api',
)


def _id(manifest, pack_id=None):
    value = pack_id if pack_id is not None else manifest.get('id')
    if (not isinstance(value, str) or value in {'.', '..'}
            or not re.fullmatch('[A-Za-z0-9_.-]+', value)):
        raise ValueError('capability adoption requires an owned Pack id')
    return value


def _source(value, pack_id):
    if not isinstance(value, str):
        raise ValueError('capability sources must be public reference strings')
    kind, separator, reference = value.partition(':')
    if not separator:
        raise ValueError('capability source requires manifest:, config: or adapter:')
    if kind == 'manifest':
        if not re.fullmatch('[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*', reference):
            raise ValueError('manifest source must contain field names only')
    elif kind == 'config':
        if not re.fullmatch('[A-Z][A-Z0-9_]*', reference):
            raise ValueError('config source must contain an environment key name only')
    elif kind == 'adapter':
        path = PurePosixPath(reference)
        if (path.is_absolute() or '..' in path.parts or '\\' in reference
                or len(path.parts) < 3 or path.parts[:2] != ('packs', pack_id)
                or path.suffix not in _SOURCE_SUFFIXES or str(path) != reference):
            raise ValueError('adapter source must be a source file inside the selected Pack')
    else:
        raise ValueError('capability source requires manifest:, config: or adapter:')
    return value


def _derived(manifest, capability):
    """Use existing manifest settings as the single truth for enabled features."""
    if capability == 'resource_pool':
        if 'resource_pool' in manifest:
            raw = manifest['resource_pool']
            if not isinstance(raw, dict) or type(raw.get('enabled', False)) is not bool:
                raise ValueError('resource_pool.enabled must be boolean')
            return ('core' if raw.get('enabled', False) else 'disabled', ['manifest:resource_pool'])
        if manifest.get('identity_requirements'):
            # Match the existing legacy resource-pool rules without importing a Pack.
            requirements = manifest['identity_requirements']
            if not isinstance(requirements, dict):
                raise ValueError('identity_requirements must be an object')
            reserved = {'scenario', 'scenario_fields', 'scenario_refs', 'notes', 'distinct_roles', 'fixture_templates'}
            enabled = bool(requirements.get('scenario')) or any(
                key not in reserved and isinstance(value, list)
                and re.fullmatch('[a-z][a-z0-9_]*', key)
                for key, value in requirements.items())
            return ('core' if enabled else 'disabled', ['manifest:identity_requirements'])
    if capability == 'diagnostics' and 'diagnostic_capabilities' in manifest:
        values = manifest['diagnostic_capabilities']
        if (not isinstance(values, list) or any(not isinstance(value, str) or value not in {'contract', 'database', 'error_log'}
                for value in values) or len(set(values)) != len(values)):
            raise ValueError('diagnostic_capabilities must contain unique known names')
        return ('core' if values else 'disabled', ['manifest:diagnostic_capabilities'])
    return None


def validate_adoption(manifest):
    """Validate optional declarations, preserving legacy Pack compatibility."""
    if not isinstance(manifest, dict):
        raise ValueError('Pack manifest must be an object')
    if 'capability_adoption' not in manifest:
        return {}
    declaration = manifest['capability_adoption']
    pack_id = _id(manifest)
    if (not isinstance(declaration, dict) or set(declaration) != {'version', 'items'}
            or type(declaration.get('version')) is not int or declaration['version'] != 1
            or not isinstance(declaration.get('items'), dict)):
        raise ValueError('capability_adoption requires version=1 and an items object')
    items = declaration['items']
    if set(items) - set(CAPABILITIES):
        raise ValueError('capability_adoption contains unsupported capability ids')
    result = {}
    for capability, item in items.items():
        if (not isinstance(item, dict) or set(item) - {'mode', 'required', 'sources', 'reason', 'required_scope'}
                or type(item.get('required')) is not bool):
            raise ValueError('capability item requires a boolean required and supported fields only')
        derived = _derived(manifest, capability)
        mode = item.get('mode', derived[0] if derived else None)
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError('capability mode must be core/pack/disabled/unconfigured/unavailable')
        if derived and mode != derived[0]:
            raise ValueError('capability mode conflicts with existing manifest declaration')
        reason = item.get('reason', '')
        if not isinstance(reason, str) or len(reason) > 1000 or any(ord(c) < 32 for c in reason):
            raise ValueError('capability reason must be a public single-line description')
        if mode in {'pack', 'disabled', 'unconfigured', 'unavailable'} and not reason.strip():
            raise ValueError('dedicated, disabled or unavailable capabilities require a reason')
        sources = item.get('sources', [])
        if (not isinstance(sources, list) or any(not isinstance(value, str) for value in sources)
                or len(set(sources)) != len(sources)):
            raise ValueError('capability sources must contain unique references')
        sources = [_source(value, pack_id) for value in sources]
        if derived:
            sources = list(dict.fromkeys([*derived[1], *sources]))
        if mode in {'core', 'pack'} and not sources:
            raise ValueError('implemented capabilities require an explicit declaration source')
        required_scope = item.get('required_scope')
        if 'required_scope' in item and (not isinstance(required_scope, str) or required_scope not in SCOPES):
            raise ValueError('required_scope must be an exact supported evidence scope')
        result[capability] = {'mode': mode, 'required': item['required'],
                              'sources': sources, 'reason': reason.strip(), 'requiredScope': required_scope}
    return result


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _declaration_digest(manifest):
    return hashlib.sha256(_canonical({key: manifest.get(key) for key in _DECLARATION_FIELDS})).hexdigest()


def _manifest(manifest, root, pack_id):
    """Registered Packs include Path objects; bind the saved portable declaration."""
    path = root / 'packs' / pack_id / 'pack.json'
    if path.is_file():
        owner = root / 'packs' / pack_id
        if owner.resolve() != owner or not path.resolve().is_relative_to(owner):
            raise ValueError('capability manifest escapes selected Pack')
        raw = json.loads(path.read_bytes())
        if not isinstance(raw, dict) or raw.get('id', pack_id) != pack_id:
            raise ValueError('capability manifest belongs to another Pack')
        return raw
    raise ValueError('capability evidence requires the saved owned Pack manifest')


def _owned_source(root, pack_id, relative):
    _source('adapter:' + relative, pack_id)
    path = root / relative
    owner = root / 'packs' / pack_id
    if owner.resolve() != owner or not path.resolve().is_relative_to(owner):
        raise ValueError('adapter source escapes selected Pack')
    return path


def _module_source(root, pack_id, target):
    if not isinstance(target, str):
        return None
    module = target.partition(':')[0]
    parts = module.split('.')
    if len(parts) < 2 or parts[:2] != ['packs', pack_id] or any(not part.isidentifier() for part in parts):
        return None
    path = '/'.join(parts)
    if (root / (path + '.py')).is_file():
        return path + '.py'
    return path + '/__init__.py'


def _core_binding(root):
    """Hash registered Core and shared UI assets, without opening project data."""
    inventory = root / 'resources/platform-core-files.json'
    if not inventory.is_file():
        return {'coreSha256': hashlib.sha256(b'missing-core-inventory').hexdigest(),
                'coreInventorySha256': None, 'coreIntegrity': 'missing'}
    if inventory.resolve() != inventory:
        raise ValueError('Core inventory must be an owned regular file')
    inventory_bytes = inventory.read_bytes()
    files = json.loads(inventory_bytes)
    if (not isinstance(files, list) or any(not isinstance(value, str) for value in files)
            or len(set(files)) != len(files)):
        raise ValueError('Core inventory must contain unique source paths')
    selected = set(files)
    assets = root / 'assets'
    if assets.exists():
        if assets.resolve() != assets:
            raise ValueError('shared UI assets must be owned by this platform')
        selected.update(path.relative_to(root).as_posix() for path in assets.rglob('*')
                        if path.is_file() and path.suffix in {'.js', '.css', '.mjs', '.cjs'})
    digests = {}
    complete = True
    for relative in sorted(selected):
        pure = PurePosixPath(relative)
        if (pure.is_absolute() or '..' in pure.parts or '\\' in relative or str(pure) != relative
                or pure.suffix not in {'.py', '.js', '.css', '.mjs', '.cjs', '.html', '.json', '.sh'}
                or len(pure.parts) > 1 and pure.parts[0] not in {'test-platform', 'scripts', 'resources', 'assets'}):
            raise ValueError('Core inventory must reference platform source assets only')
        path = root / pure
        if path.resolve() != path:
            raise ValueError('Core source assets must not follow symlinks')
        if path.is_file():
            digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digests[relative] = None
            complete = False
    inventory_digest = hashlib.sha256(inventory_bytes).hexdigest()
    return {'coreSha256': hashlib.sha256(_canonical({'inventory': inventory_digest, 'files': digests})).hexdigest(),
            'coreInventorySha256': inventory_digest, 'coreIntegrity': 'complete' if complete else 'missing'}


def build_binding(manifest, *, root=ROOT, pack_id=None, capability=None):
    """Public context to include in a capability evidence report before execution."""
    root = Path(root).resolve()
    pack_id = _id(manifest, pack_id)
    raw = _manifest(manifest, root, pack_id)
    items = validate_adoption(raw)
    if capability is not None and capability not in items:
        raise ValueError('capability evidence requires an explicit adoption declaration')
    digest = _declaration_digest(raw)
    adapter_files = raw.get('adapter_files', [])
    if not isinstance(adapter_files, list) or any(not isinstance(value, str) for value in adapter_files):
        raise ValueError('adapter_files must contain owned source paths')
    sources = set(adapter_files)
    for field in ('bootstrap', 'panel_extension', 'catalog_extension'):
        path = _module_source(root, pack_id, raw.get(field))
        if path:
            sources.add(path)
    if raw.get('panel_html'):
        sources.add(raw['panel_html'])
    panel_services = raw.get('panel_services', {})
    if not isinstance(panel_services, dict):
        raise ValueError('panel_services must be an object')
    for target in panel_services.values():
        path = _module_source(root, pack_id, target)
        if path:
            sources.add(path)
    selected = [items[capability]] if capability else items.values()
    for item in selected:
        sources.update(value.partition(':')[2] for value in item['sources'] if value.startswith('adapter:'))
    fingerprints = {}
    for relative in sorted(sources):
        path = _owned_source(root, pack_id, relative)
        fingerprints[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return {'declarationSha256': digest, 'platformVersion': platform_protocol.API_VERSION,
            'adapterSha256': fingerprints, **_core_binding(root)}


def evidence_path(root, pack_id, value):
    """Evidence is owned by this Pack; never inspect another project's reports."""
    root = Path(root).resolve()
    if not isinstance(value, (str, Path)):
        raise ValueError('capability evidence path must be an owned report')
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    owner = root / 'artifacts' / pack_id
    if (owner.resolve() != owner or not path.resolve().is_relative_to(owner)
            or path.suffix != '.json'):
        raise ValueError('capability evidence must be a JSON report inside artifacts/<pack>/')
    return path.resolve()


def _fact(report, pack_id, capability):
    if (not isinstance(report, dict) or report.get('pack') != pack_id
            or report.get('capability') != capability):
        raise ValueError('capability evidence belongs to another Pack or capability')
    status = report.get('status')
    if not isinstance(status, str) or status not in {'passed', 'failed', 'incomplete', 'not_run'}:
        raise ValueError('capability evidence requires an explicit verification status')
    expected = True if status == 'passed' else False if status == 'failed' else None
    if 'ok' not in report or report['ok'] is not expected:
        raise ValueError('capability evidence ok must agree with its verification status')
    scope = report.get('scope')
    if not isinstance(scope, str) or scope not in SCOPES:
        raise ValueError('capability evidence scope must be synthetic/mock/real_readonly/manual_review')
    verified_at = report.get('verifiedAt')
    if type(verified_at) is not int or verified_at <= 0:
        raise ValueError('capability evidence requires verifiedAt as positive Unix seconds')
    if report.get('realBusinessVerified') is True and scope != 'real_readonly':
        raise ValueError('non-business capability evidence cannot claim real business verification')
    if scope == 'real_readonly' and (report.get('realBusinessVerified') is not True or not _valid_case_ids(report.get('caseIds'))):
        raise ValueError('real_readonly capability evidence requires verified business facts and actual case ids')
    return {'status': status, 'ok': expected, 'scope': scope, 'verifiedAt': verified_at}


def _valid_case_ids(values):
    return (isinstance(values, list) and bool(values)
            and all(isinstance(value, str) and value and value == value.strip() for value in values)
            and len(set(values)) == len(values))


def _real_authorization(report, root, pack_id):
    """Reuse existing scope and real_readonly task proofs, never invent a new grant."""
    if report['scope'] != 'real_readonly':
        return None
    path = root / 'data' / pack_id / 'onboarding-tasks.json'
    owner = root / 'data' / pack_id
    if owner.resolve() != owner or not path.resolve().is_relative_to(owner):
        raise ValueError('real read-only evidence task state escapes selected Pack')
    data = json.loads(path.read_bytes())
    if (not isinstance(data, dict) or data.get('pack') != pack_id
            or type(data.get('schemaVersion')) is not int or data['schemaVersion'] != 1
            or not isinstance(data.get('tasks'), list)):
        raise ValueError('existing read-only task evidence is required')
    rows = data['tasks']
    selected = [row for row in rows if isinstance(row, dict) and row.get('id') in {'scope', 'real_readonly'}]
    if len(selected) != 2 or {row['id'] for row in selected} != {'scope', 'real_readonly'}:
        raise ValueError('existing scope and real_readonly tasks are required')
    facts = {}
    digests = {}
    task_rows = {row['id']: row for row in selected}
    for task in ('scope', 'real_readonly'):
        row = task_rows[task]
        if row.get('status') != 'passed':
            raise ValueError('scope and real_readonly tasks must already have passed evidence')
        target = evidence_path(root, pack_id, row.get('evidencePath'))
        raw = target.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != row.get('evidenceSha256'):
            raise ValueError('existing read-only task evidence changed')
        fact = json.loads(raw)
        if not isinstance(fact, dict) or fact.get('pack') != pack_id or not _valid_case_ids(fact.get('caseIds')):
            raise ValueError('existing read-only task evidence belongs to another Pack or has invalid case ids')
        if task == 'scope' and fact.get('authorizedReadOnly') is not True:
            raise ValueError('explicit read-only scope authorization is required')
        if task == 'real_readonly' and (fact.get('ok') is not True or fact.get('scope') != 'real_readonly'
                or fact.get('realBusinessVerified') is not True):
            raise ValueError('existing real_readonly task does not prove real acceptance')
        facts[task] = fact
        digests[task] = digest
    if (task_rows['real_readonly'].get('scopeEvidenceSha256') != digests['scope']
            or report.get('scopeEvidenceSha256') != digests['scope']
            or not set(facts['real_readonly']['caseIds']) <= set(facts['scope']['caseIds'])
            or not set(report['caseIds']) <= set(facts['real_readonly']['caseIds'])):
        raise ValueError('capability case ids must remain within current authorized real read-only evidence')
    return {'scopeEvidenceSha256': digests['scope'], 'realEvidenceSha256': digests['real_readonly'],
            'caseIds': report['caseIds']}


def _references(report, root, pack_id):
    references = report.get('evidenceRefs', [])
    if not isinstance(references, list):
        raise ValueError('capability evidenceRefs must be a list of owned report digests')
    result = []
    seen = set()
    for reference in references:
        if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
                or not isinstance(reference.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', reference['sha256'])):
            raise ValueError('capability evidenceRef requires a public report path and SHA-256')
        target = evidence_path(root, pack_id, reference['path'])
        relative = target.relative_to(root).as_posix()
        if relative in seen:
            raise ValueError('capability evidenceRefs must not repeat reports')
        seen.add(relative)
        if hashlib.sha256(target.read_bytes()).hexdigest() != reference['sha256']:
            raise ValueError('capability referenced evidence changed')
        result.append({'path': relative, 'sha256': reference['sha256']})
    return result


def prepare_evidence(manifest, capability, path, *, root=ROOT, pack_id=None, recorded_at):
    """Validate an explicit report and return its small, immutable receipt."""
    root = Path(root).resolve()
    pack_id = _id(manifest, pack_id)
    target = evidence_path(root, pack_id, path)
    raw = target.read_bytes()
    report = json.loads(raw)
    _fact(report, pack_id, capability)
    references = _references(report, root, pack_id)
    authorization = _real_authorization(report, root, pack_id)
    binding = build_binding(manifest, root=root, pack_id=pack_id, capability=capability)
    if (_canonical(report.get('binding')) != _canonical(binding)
            or any(value is None for value in binding['adapterSha256'].values())
            or binding['coreIntegrity'] != 'complete'):
        raise ValueError('capability evidence must match the current declaration, platform and Adapter sources')
    return {'evidencePath': target.relative_to(root).as_posix(),
            'evidenceSha256': hashlib.sha256(raw).hexdigest(),
            'recordedAt': recorded_at, 'binding': binding, 'evidenceRefs': references,
            'readOnlyAuthorization': authorization}


def _receipts(root, pack_id):
    owner = root / 'data' / pack_id
    path = owner / 'onboarding-tasks.json'
    if owner.resolve() != owner or not path.resolve().is_relative_to(owner):
        raise ValueError('capability task state escapes selected Pack')
    if not path.is_file():
        return {}
    data = json.loads(path.read_bytes())
    if (not isinstance(data, dict) or data.get('pack') != pack_id
            or type(data.get('schemaVersion')) is not int or data['schemaVersion'] != 1):
        raise ValueError('capability task state belongs to another Pack')
    return data.get('capabilityEvidence', {})


def _verification(manifest, root, pack_id, capability, receipt):
    empty = {'status': 'not_run', 'ok': None, 'scope': None, 'verifiedAt': None,
             'evidence': None, 'nextAction': 'Execute and record scoped capability evidence explicitly'}
    if receipt is None:
        return empty
    fields = {'evidencePath', 'evidenceSha256', 'recordedAt', 'binding'}
    if (not isinstance(receipt, dict) or not fields <= set(receipt)
            or set(receipt) - fields - {'evidenceRefs', 'readOnlyAuthorization'}
            or not isinstance(receipt.get('evidenceSha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', receipt['evidenceSha256'])
            or type(receipt.get('recordedAt')) is not int or receipt['recordedAt'] <= 0
            or not isinstance(receipt.get('binding'), dict)):
        raise ValueError('capability evidence receipt is malformed')
    target = evidence_path(root, pack_id, receipt['evidencePath'])
    projection = {'path': target.relative_to(root).as_posix(), 'sha256': receipt['evidenceSha256'],
                  'recordedAt': receipt['recordedAt']}
    stale = {**empty, 'status': 'stale', 'evidence': projection,
             'nextAction': 'Repeat verification against the current declaration, platform and Adapter sources'}
    try:
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt['evidenceSha256']:
            return stale
        report = json.loads(raw)
        fact = _fact(report, pack_id, capability)
        references = _references(report, root, pack_id)
        if references != receipt.get('evidenceRefs', []):
            return stale
        authorization = _real_authorization(report, root, pack_id)
        if authorization != receipt.get('readOnlyAuthorization'):
            return stale
        binding = build_binding(manifest, root=root, pack_id=pack_id, capability=capability)
        if _canonical(receipt['binding']) != _canonical(binding) or _canonical(report.get('binding')) != _canonical(binding):
            return stale
    except (OSError, ValueError, TypeError):
        return stale
    return {**fact, 'evidence': projection, 'evidenceRefs': references, 'binding': binding,
            'readOnlyAuthorization': authorization, 'nextAction': ''}


def adoption_report(manifest, *, root=ROOT, pack_id=None, capability_evidence=None):
    """One safe JSON projection for doctor, tasks, API and delivery reports."""
    root = Path(root).resolve()
    pack_id = _id(manifest, pack_id)
    raw = _manifest(manifest, root, pack_id)
    items = validate_adoption(raw)
    receipts = _receipts(root, pack_id) if capability_evidence is None else capability_evidence
    if not isinstance(receipts, dict) or set(receipts) - set(CAPABILITIES):
        raise ValueError('capability evidence receipts must use known capability ids')
    rows = []
    status = 'ready'
    for capability in CAPABILITIES:
        explicit = capability in items
        derived = _derived(raw, capability)
        item = items.get(capability, {'mode': derived[0] if derived else 'undeclared',
                                     'required': None, 'requiredScope': None,
                                     'sources': derived[1] if derived else [], 'reason': ''})
        fact = _verification(raw, root, pack_id, capability, receipts.get(capability))
        row_status = 'ready'
        if fact['status'] == 'failed':
            row_status = 'failed'
        elif item['required'] is True and (
                item['mode'] not in {'core', 'pack'} or fact['status'] != 'passed'
                or item['requiredScope'] is not None and fact['scope'] != item['requiredScope']):
            row_status = 'incomplete'
        if row_status == 'failed' or status != 'failed' and row_status == 'incomplete':
            status = row_status
        rows.append({'id': capability, 'declared': explicit, **item, 'verification': fact,
                     'needsMigration': not explicit, 'status': row_status,
                     'nextAction': 'Satisfy the declared capability and exact required_scope with current evidence'
                     if row_status == 'incomplete' else ''})
    # Legacy Packs can explicitly own repository-level adapters under the old
    # registry contract. Enforce the new binding rules only after adoption or
    # evidence is opted into; undeclared choices still remain unverified.
    if items or receipts:
        build_binding(raw, root=root, pack_id=pack_id)
    return {'schemaVersion': 1, 'pack': pack_id, 'basis': 'saved_manifest', 'platformVersion': platform_protocol.API_VERSION,
            'declarationSha256': _declaration_digest(raw),
            'declared': 'capability_adoption' in raw, 'needsMigration': len(items) != len(CAPABILITIES),
            'items': rows, 'status': status, 'ok': True if status == 'ready' else False if status == 'failed' else None,
            'realBusinessVerified': False,
            'nextAction': 'Declare missing capability choices; run scoped checks explicitly' if len(items) != len(CAPABILITIES) else ''}
