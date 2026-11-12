"""A frozen clean prefix, existing checkpoint stages, and explicit review gates.

Archived comparison reports are diagnostics only. Exact accepted same-run
prefixes or first-stage cell replacements retain their bound evidence and costs.
"""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import json
import tempfile
import os

from . import accepted_review, batch, checkpoint, comparison, profile_authority, review_pipeline, settlement
from .common import asset_bindings, code_bindings, digest, encoded, load, now, records, write_private, file_digest, file_manifest, verify_manifest, parallel_map
from .accounting import report_costs
from .completion_audit import inspect_ledger
from .ledger import Ledger
from .reporting import write_reports
from .runner import run as run_native
from ._method.scoring import POOL_DEPTH

FIRST_RUN = 'unavailable-first-run-v1'
PROFILE = 'clean-staged-run-v1'
STAGE_PROFILE = 'clean-stage-v1'
STAGE_COUNTS = (10, 20, 30, 40, 100)
LEAN_PROFILE = 'clean-staged-run-v2'
LEAN_STAGE = 'clean-stage-v2'
LEAN_GATE = 'clean-stage-gate-evidence-v2'
LEAN_VERIFICATION = 'complete-byte-closure-at-boundary-v1'
RECOVERED_STAGE = 'completed-native-stage-continuation-v1'
RECOVERY_COMPLETION = 'completed-native-recovery-validation-v1'
NATIVE_REBIND = 'completed-native-authority-rebind-v1'
RECOVERED_SEED = 'completed-recovery-seed-transition-v1'
INTEGRATION_FILES = frozenset({'clean_run.py', 'review_pipeline.py'})


def _evidence_files(roots):
    """Closed benchmark artifacts only; never scan a credential/runtime home."""
    paths = set()
    excluded = {'codex-runtime', 'claude-runtime', '__pycache__', 'venv'}
    for root in map(Path, roots):
        if root.is_symlink() or not root.is_dir():
            raise ValueError('closed evidence root must be an existing directory')
        pending = [root]
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if entry.name.startswith('.') or entry.name in excluded:
                        continue
                    if entry.is_symlink():
                        raise ValueError('closed evidence contains a symlink')
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        paths.add(os.path.abspath(entry.path))
                    else:
                        raise ValueError('closed evidence must contain regular files only')
    return paths


def _case_refs(packages, outcomes):
    wanted = {o['case_id']: o['packet_sha256'] for o in outcomes}
    refs = {}
    for path in map(Path, packages):
        for row in load(path)['cases']:
            cid = row['case_id']
            if cid not in wanted:
                continue
            ref = dict(path=str((path.parent / row['path']).resolve()), sha256=row['sha256'])
            if file_digest(ref['path']) != ref['sha256']:
                raise ValueError('source case changed')
            own = load(ref['path'])
            if own['compiled']['packet_sha256'] == wanted[cid]:
                refs[cid] = ref
    if set(refs) != set(wanted):
        raise ValueError('verified source cases are incomplete')
    return refs


def _file_references(value):
    """Explicit absolute artifact references, not native JSON field paths."""
    refs = []
    if isinstance(value, dict):
        if (isinstance(value.get('path'), str) and Path(value['path']).is_absolute()
                and isinstance(value.get('sha256'), str) and len(value['sha256']) == 64):
            refs.append(dict(path=value['path'], sha256=value['sha256']))
        for child in value.values(): refs.extend(_file_references(child))
    elif isinstance(value, list):
        for child in value: refs.extend(_file_references(child))
    return refs


def _same_choices(old, new, *, allowed=None):
    # This transition may change orchestration only, never a date/grade/prompt rule.
    allowed = {'common.py', 'clean_run.py', 'settlement.py', 'accepted_review.py', 'operator.py', 'cli.py'} if allowed is None else set(allowed)
    a, b = deepcopy(old['profile_bindings']), deepcopy(new['profile_bindings'])
    ac, bc = a.pop('semantic_code'), b.pop('semantic_code')
    if a != b or any(ac.get(k) != bc.get(k) for k in set(ac) | set(bc) if k not in allowed):
        raise ValueError('lean continuation changed a method choice or grading code')


def _historical_native_authority(native):
    """Authenticate accepted historical bytes without allowing them to dispatch."""
    cfg = native['configuration']; authority = cfg.get('live_profile_authority', {})
    if authority.get('profile') != profile_authority.METHOD_PROFILE:
        raise ValueError('native integration requires an independently accepted method')
    path = Path(authority['package_path']); method = load(path); receipt = load(path.parent/'approval.json')
    if (digest(method) != authority['package_sha256'] or digest(receipt) != authority['approval_sha256']
            or receipt.get('approved') is not True or receipt.get('accept_method') is not True
            or not receipt.get('approved_by') or not receipt.get('approved_at')
            or receipt.get('package_sha256') != digest(method)
            or method.get('profile') != profile_authority.METHOD_PROFILE
            or method['profile_bindings'] != authority['profile_bindings']
            or method['mode'] != authority['mode'] or native['mode'] != authority['mode']
            or method['method_options'] != authority['method_options']
            or profile_authority.method_options(cfg) != authority['method_options']
            or authority['profile_bindings']['semantic_code'] != native['code']
            or authority['profile_bindings']['assets'] != native['assets']
            or authority['profile_bindings']['reviewers'] != cfg['reviewers']):
        raise ValueError('historical native method/receipt differs')
    return authority


def _recovery_completion(reference):
    """Read the source runtime's bound completion facts; no old code executes."""
    from .interruption import CREDIT_PROFILE, CREDIT_FILES, KEY
    receipt = _read_bound(reference)
    if (receipt.get('profile') != RECOVERY_COMPLETION or receipt.get('validated') is not True
            or receipt.get('live_calls') != 0):
        raise ValueError('completed native validation receipt required')
    _verify_bound(receipt['producer'])
    if receipt['producer_sha256'] != receipt['producer']['sha256']:
        raise ValueError('completed native receipt producer differs')
    native = _read_bound(receipt['native_package']); source = _read_bound(receipt['source_native'])
    report = _read_bound(receipt['report']); audit = _read_bound(receipt['audit'])
    approval = _read_bound(receipt['owner_approval']); _verify_bound(receipt['ledger'])
    authority = _historical_native_authority(native); old = _historical_native_authority(source)
    proof = native['configuration'].get(KEY, {})
    snapshot = proof.get('source_snapshot', {})
    if (proof.get('profile') != CREDIT_PROFILE or proof.get('source_package_sha256') != digest(source)
            or proof.get('source_code_sha256') != digest(source['code'])
            or proof.get('target_code_sha256') != digest(native['code'])
            or snapshot != receipt['source_snapshot'] or snapshot != native['configuration'].get('reuse_snapshot')
            or digest(proof.get('attempts')) != receipt['named_attempts_sha256']
            or receipt['runtime_code'] != native['code'] or receipt['runtime_code_sha256'] != digest(native['code'])
            or receipt['method_sha256'] != authority['package_sha256']
            or receipt['method_receipt_sha256'] != authority['approval_sha256']
            or approval.get('decision') != 'approved' or approval.get('package_sha256') != digest(native)
            or not approval.get('approved_by') or not approval.get('approved_at')
            or report.get('package_sha256') != digest(native) or report['execution']['state'] != 'completed'
            or report['source_versions'] != dict(code=native['code'], assets=native['assets'])
            or audit.get('unclassified') or audit['source_ledger_sha256'] != receipt['ledger']['sha256']
            or audit['code'] != native['code'] or audit['assets'] != native['assets']
            or Path(audit['source_ledger']).resolve() != Path(receipt['ledger']['path']).resolve()
            or Path(native['configuration']['run_directory']).resolve() != Path(receipt['report']['path']).parent
            or Path(receipt['ledger']['path']).resolve() != Path(native['configuration']['run_directory']).resolve()/'attempts.jsonl'):
        raise ValueError('completed recovery receipt does not bind its native lineage')
    events = list(records(receipt['ledger']['path']))
    starts = {e['attempt_id'] for e in events if e['event']=='started'}
    if (starts != {e['attempt_id'] for e in events if e['event']=='finished'}
            or report['execution']['retained_started_attempts'] != len(starts)):
        raise ValueError('completed recovery report does not cover a drained ledger')
    if file_digest(snapshot['manifest_path']) != snapshot['sha256']:
        raise ValueError('native recovery snapshot changed')
    snapshot_path = Path(snapshot['manifest_path']); snapshot_meta = load(snapshot_path)
    if (snapshot_meta['source_package_sha256'] != digest(source)
            or Path(receipt['source_native']['path']).resolve() != snapshot_path.parent/'source-package.json'):
        raise ValueError('completed recovery names another source snapshot')
    for key in ('questions', 'question_bindings', 'prompt_profiles', 'scope', 'providers', 'primary', 'assets', 'mode', 'search_requests'):
        if native[key] != source[key]: raise ValueError('completed recovery changed frozen work: '+key)
    changed = {k for k in source['code'].keys() | native['code'].keys() if source['code'].get(k) != native['code'].get(k)}
    if not changed <= CREDIT_FILES:
        raise ValueError('completed recovery changed code outside its owner set')
    _same_choices(old, authority, allowed=CREDIT_FILES)
    mutable = {'run_directory', 'reuse_snapshot', KEY, 'live_profile_authority', 'method_acceptance_package'}
    if ({k:v for k,v in native['configuration'].items() if k not in mutable}
            != {k:v for k,v in source['configuration'].items() if k not in mutable}):
        raise ValueError('completed recovery changed settings or funding')
    return receipt, native, source


def native_authority_rebind(completion, accepted_profile):
    """Declare the sole historical-to-current authority change for review input."""
    _, native, _ = _recovery_completion(completion)
    old = _historical_native_authority(native)
    new = profile_authority.read(accepted_profile, native['configuration']['reviewers'])
    _same_choices(old, new, allowed=INTEGRATION_FILES)
    if old['mode'] != new['mode'] or old['method_options'] != new['method_options']:
        raise ValueError('native integration changed mode or method options')
    return dict(profile=NATIVE_REBIND, completion=deepcopy(completion),
        source_package_sha256=digest(native), old_method_sha256=old['package_sha256'],
        old_receipt_sha256=old['approval_sha256'], new_authority=new)


def verify_native_authority_rebind(source, record):
    if (record.get('profile') != NATIVE_REBIND or record.get('source_package_sha256') != digest(source)
            or record != native_authority_rebind(record['completion'], record['new_authority']['package_path'])):
        raise ValueError('completed native authority rebind differs')
    return record['new_authority']


def _recovered_stage_package(source_manifest, completion, directory, accepted_profile, *, created_at=None):
    """Authenticate one held stage and derive its subscription-only finish."""
    source_manifest = Path(source_manifest).resolve(); root = Path(directory).resolve()
    old = load(source_manifest); owner_path = source_manifest.parent/'approval.json'
    _approval(old, load(owner_path))
    if old.get('profile') != LEAN_PROFILE or len(old['stages']) != 1:
        raise ValueError('native continuation requires a single held lean stage')
    receipt, native, source_native = _recovery_completion(completion)
    if (root.is_relative_to(source_manifest.parent) or source_manifest.parent.is_relative_to(root)
            or root.is_relative_to(Path(native['configuration']['run_directory']))
            or root.is_relative_to(Path(completion['path']).parent)):
        raise ValueError('native continuation must preserve its source roots')
    manifest = deepcopy(old['stages'][0]); source_root = source_manifest.parent/'stage-1'
    checkpoint_path = source_root/'checkpoint.json'; checkpoint_data = load(checkpoint_path)
    source_native_path = source_root/'native/package.json'; original = load(source_native_path)
    held_path = Path(original['configuration']['run_directory'])/'report.json'; held = load(held_path)
    if (digest(original) != digest(source_native) or checkpoint_data['native_package_sha256'] != digest(original)
            or checkpoint_data['clean_package_sha256'] != digest(old)
            or Path(checkpoint_data['clean_manifest_path']).resolve() != source_manifest
            or held['package_sha256'] != digest(original) or held['execution']['state'] == 'completed'
            or (source_root/'stage-review.json').exists()
            or manifest['questions'] != native['questions'] or old['assets'] != native['assets']
            or old['mode'] != native['mode'] or old['code'] != source_native['code']
            or old['accepted_profile'] != source_native['configuration']['live_profile_authority']):
        raise ValueError('held lifecycle and recovered native do not share an approved stage')
    authority = profile_authority.read(accepted_profile, native['configuration']['reviewers'])
    transition = _recovered_seed_transition(old['verified_seed'], authority, completion)
    prefix, _ = _verified_prefix(old['verified_seed'], authority, check_files=False, load_evidence=False, transition=transition)
    order, admission = batch.frozen_order(old['selection_path']); offset = len(prefix['question_ids'])
    if (prefix['question_ids'] != [q['question_id'] for q in order[:offset]]
            or manifest['questions'] != order[offset:offset+manifest['count']]
            or manifest['cumulative'] != offset+manifest['count']
            or any(old[k] != v for k,v in admission.items())
            or checkpoint_data['carried_usd'] != prefix['costs']['conservative_total_usd']
            or Decimal(native['configuration']['execution_policy']['spending_ceiling_usd'])
                != Decimal(old['shared_ceiling_usd'])-Decimal(checkpoint_data['carried_usd'])):
        raise ValueError('recovered stage changed bank, prefix, task scope or funding')
    manifest['native_prototype'] = deepcopy(native)
    return dict(profile=RECOVERED_STAGE, created_at=created_at or now(), output_directory=str(root),
        code=code_bindings(), assets=asset_bindings(), mode=old['mode'], accepted_profile=authority,
        source_lifecycle=_bound_file(source_manifest), source_approval=_bound_file(owner_path),
        source_checkpoint=_bound_file(checkpoint_path), source_held_report=_bound_file(held_path),
        completed_recovery=deepcopy(completion), native_rebind=native_authority_rebind(completion, accepted_profile),
        completion_producer=deepcopy(receipt['producer']), producer_sha256=receipt['producer_sha256'],
        verified_seed=deepcopy(old['verified_seed']), seed_transition=transition,
        template=deepcopy(receipt['native_package']), archived_reference=deepcopy(old['archived_reference']),
        **(dict(archive_comparison=FIRST_RUN) if old['archived_reference'] is None else {}),
        shared_ceiling_usd=old['shared_ceiling_usd'], separate_diagnostic_usd=load(owner_path).get('separate_diagnostic_usd','0'),
        stages=[manifest], incremental_api_calls=0,
        scope='Existing ordinary/accepted subscription reviews, aggregate/report and independent gate only. '
            'No native calls, searches, Pro calls, new questions or Brave recovery; preserve source and retained outcomes.')


def prepare_recovered_stage(source_manifest, completion, directory, *, accepted_profile):
    root = Path(directory).resolve()
    if root.exists(): raise ValueError('native continuation requires a new sibling directory')
    package = _recovered_stage_package(source_manifest, completion, root, accepted_profile)
    _verified_prefix(package['verified_seed'],package['accepted_profile'],load_evidence=False,transition=package['seed_transition'])
    write_private(root/'continuation.json', package)
    write_private(root/'approval-template.json',dict(decision='NOT_APPROVED', package_sha256=digest(package),
        approved_by='', approved_at='', shared_ceiling_usd=package['shared_ceiling_usd'], scope=package['scope']))
    return package


def _verify_recovered_stage(path, package):
    if package.get('profile') != RECOVERED_STAGE or package['output_directory'] != str(path.parent):
        raise ValueError('recovered stage profile/path differs')
    expected = _recovered_stage_package(package['source_lifecycle']['path'], package['completed_recovery'],
        path.parent, package['accepted_profile']['package_path'], created_at=package['created_at'])
    if expected != package:
        raise ValueError('recovered stage source, method, scope or bounds changed')
    return package, _read_bound(package['template'])


def _recovered_stage_evidence(package):
    receipt, native, source = _recovery_completion(package['completed_recovery'])
    snapshot_path = Path(receipt['source_snapshot']['manifest_path']); snapshot = load(snapshot_path)
    expected = [dict(path=str(snapshot_path.parent/r['path']), sha256=r['sha256']) for r in snapshot['files']]
    expected += [dict(path=str(snapshot_path), sha256=receipt['source_snapshot']['sha256'])]
    expected += [receipt[k] for k in ('native_package','source_native','report','ledger','owner_approval','audit','producer')]
    expected += [package[k] for k in ('source_lifecycle','source_approval','source_checkpoint','source_held_report','completed_recovery')]
    for authority in (source['configuration']['live_profile_authority'], native['configuration']['live_profile_authority'], package['accepted_profile']):
        method = Path(authority['package_path'])
        expected += [dict(path=str(method),sha256=authority['package_sha256']),
            dict(path=str(method.parent/'approval.json'),sha256=authority['approval_sha256'])]
    for restoration_ref in receipt.get('provenance_restorations', []):
        restoration = _read_bound(restoration_ref)
        if (restoration.get('package_sha256') != digest(native) or restoration.get('new_searches') != 0
                or restoration.get('new_judgments') != 0):
            raise ValueError('native provenance restoration changed calls or source')
        expected.append(restoration_ref)
        for row in restoration['records']:
            expected += [dict(path=row[key],sha256=row['sha256']) for key in ('source','target')]
    unique = {}
    for ref in expected:
        path = str(Path(ref['path']).resolve()); normalized = dict(path=path,sha256=ref['sha256'])
        if path in unique and unique[path] != normalized:
            raise ValueError('recovered evidence has conflicting file bindings')
        unique[path] = normalized
    expected = [unique[k] for k in sorted(unique)]
    verify_manifest(expected)
    roots = [Path(receipt['native_package']['path']).parent, snapshot_path.parent,
        Path(package['completed_recovery']['path']).parent]
    return roots, expected


def _run_recovered_stage(path, approval, *, fixture_call=None):
    package, native = _verify_recovered_stage(path, load(path)); _approval(package, approval)
    if package['mode']=='live' and fixture_call is not None:
        raise ValueError('live recovered continuation requires frozen subscription transports')
    with Ledger(path.parent/'coordinator',dict(package_sha256=digest(package),mode=package['mode'])).locked():
        _save_same(path.parent/'approval.json',approval)
        root = path.parent/'stage-1'
        if (root/'gate-evidence.json').exists():
            gate = _check_lean_gate(path,package,1)
            if not (root/'stage-review.json').exists():
                return dict(state='awaiting_stage_review',stage_index=1,evidence=gate,
                    gate_sha256=file_digest(root/'gate-evidence.json'),report=str(_accepted_root(root)/'report/report.html'))
            review = load(root/'stage-review.json'); _validate_review(review,gate)
            if review['gate_sha256'] != file_digest(root/'gate-evidence.json'):
                raise ValueError('recovered stage verdict binds different gate bytes')
            if review['decision'] != 'continue': return dict(state='stopped_after_review',stage_index=1)
            prefix = _write_lean_prefix(root,gate)
            result = dict(state='completed',questions=package['stages'][0]['cumulative'],prefix=prefix,
                report=str(_accepted_root(root)/'report/report.html'))
            _save_same(path.parent/'closure.json',result); return result
        prior,_ = _verified_prefix(package['verified_seed'],package['accepted_profile'],
            load_evidence=False,transition=package['seed_transition'])
        stage = dict(profile=LEAN_STAGE,clean_manifest_path=str(path),clean_package_sha256=digest(package),
            index=1,accepted_profile=package['accepted_profile'],parent_report=_read_bound(prior['snapshot'])['report'],
            carried_usd=prior['costs']['conservative_total_usd'],native_package_sha256=digest(native),
            expected_differences=package['stages'][0]['expected_differences'])
        if package.get('archive_comparison') == FIRST_RUN:
            stage['archive_comparison'] = FIRST_RUN
        _save_same(root/'checkpoint.json',stage)
        roots, expected = _recovered_stage_evidence(package)
        return _finish_lean_native(path,package,package['stages'][0],stage,package['verified_seed'],prior,native,approval,
            fixture_call=fixture_call,native_package_path=package['template']['path'],
            authority_rebind=package['native_rebind'],extra_evidence_roots=roots,retained_bindings=expected)


def _closed_index_checks(roots):
    """Authenticate saved indexes/attempt bytes, without replaying judgments."""
    expected = {}
    packages = [p for root in map(Path, roots) for p in root.rglob('package.json') if 'codex-runtime' not in p.parts]
    native_by_sha = {}
    for path in packages:
        p = load(path)
        if 'configuration' in p:
            native_by_sha[file_digest(path)] = p
    for root in map(Path, roots):
        for ledger in root.rglob('attempts.jsonl'):
            if 'codex-runtime' in ledger.parts:
                continue
            events = list(records(ledger))
            starts = {e['attempt_id']: e for e in events if e['event'] == 'started'}
            ends = {e['attempt_id']: e for e in events if e['event'] == 'finished'}
            if starts.keys() != ends.keys():
                raise ValueError('bootstrap has unfinished saved attempts')
            for aid, end in ends.items():
                expected[str((ledger.parent/'attempts'/aid/'request.json').resolve())] = starts[aid]['request_sha256']
                result_file = (ledger.parent/end['result_path']).resolve()
                expected[str(result_file)] = end['result_sha256']
                raw_file = result_file.parent/'response.bin'
                if raw_file.exists():
                    raw = load(result_file)
                    sha = raw.get('raw_sha256')
                    retained = raw.get('retained_sha256', sha)
                    if sha and retained != sha and raw.get('credential_echo_redacted') is not True:
                        raise ValueError('unexplained raw evidence hash difference')
                    if retained is None and isinstance(raw.get('raw_text'), str):
                        retained = digest(raw['raw_text'].encode())
                    if not retained:
                        raise ValueError('raw response lacks a retained byte binding')
                    expected[str(raw_file)] = retained
        for path in [p for p in packages if p.is_relative_to(root)]:
            p = load(path)
            for row in p.get('cases', []):
                case_file = (path.parent/row['path']).resolve()
                if file_digest(case_file) != row['sha256']:
                    raise ValueError('closed case index changed')
                expected[str(case_file)] = row['sha256']
                native = native_by_sha.get(p.get('source_package_sha256'))
                if native is not None:
                    compiled_file = Path(native['configuration']['run_directory'])/'compiled'/(row['case_id']+'.json')
                    expected[str(compiled_file.resolve())] = digest(load(case_file)['compiled'])
        for path in root.rglob('native-completion-audit.json'):
            audit = load(path)
            if audit['unclassified']:
                raise ValueError('bootstrap native completion is unclassified')
            run = Path(audit['source_ledger']).parent
            expected[str((run/'attempts.jsonl').resolve())] = audit['source_ledger_sha256']
            for row in audit['records']:
                for name, sha in row.get('files', {}).items():
                    expected[str((run/'attempts'/row['attempt_id']/name).resolve())] = sha
    verify_manifest([dict(path=p, sha256=h) for p, h in sorted(expected.items())])


def bootstrap_checkpoint(gate_path, review_path, directory, *, evidence_roots,
                         case_packages, native_package, accepted_profile):
    """One-time migration of a fully audited/reviewed gate, with no old replay.

    Exact new method and execution approval will pin the resulting closure.
    The saved indexes are checked, never replaced with hashes of altered bytes.
    """
    from collections.abc import Mapping
    gate_path, review_path = Path(gate_path).resolve(), Path(review_path).resolve()
    gate, review = load(gate_path), load(review_path)
    _validate_review(review, gate)
    if review['decision'] != 'continue':
        raise ValueError('bootstrap needs an independent continue verdict')
    for key in ('report', 'comparison', 'audit', 'ordinary_closure', 'accepted_closure'):
        _verify_bound(gate[key])
    if review.get('source_review'):
        _verify_bound(review['source_review'])
    report_path = Path(gate['report']['path'])
    report = load(report_path)
    native = load(native_package)
    stage_root = Path(native_package).resolve().parent.parent
    stage = load(stage_root/'checkpoint.json')
    if (stage['index'] != gate['stage_index'] or stage['clean_package_sha256'] != gate['clean_package_sha256']
            or report_path.resolve() != (_accepted_root(stage_root)/'report/report.json').resolve()
            or Path(native['configuration']['run_directory']).resolve() != stage_root/'native/run'):
        raise ValueError('bootstrap gate and native stage scope differ')
    if native['mode'] == 'live':
        if not review.get('source_review') or file_digest(gate_path) not in Path(review['source_review']['path']).read_text():
            raise ValueError('live bootstrap needs the independent exact gate-file verdict')
    source_manifest = _read_bound(dict(path=stage['clean_manifest_path'], sha256=gate['clean_package_sha256']))
    _approval(source_manifest, load(Path(stage['clean_manifest_path']).parent/'approval.json'))
    if (stage['native_package_sha256'] != digest(native)
            or load(stage_root/'review/run/closure.json')['package_sha256'] != file_digest(stage_root/'review/package.json')):
        raise ValueError('bootstrap stage package anchors differ')
    anchored_packages = {str((stage_root/'review/package.json').resolve())}
    source_refs = source_manifest.get('recollection_source')
    if source_refs:
        for key in ('report', 'disposition', 'receipt'):
            _verify_bound(source_refs[key])
        for key in ('native_packages', 'review_packages', 'publication_packages'):
            for ref in source_refs[key]: _verify_bound(ref)
        for ref in _file_references(load(source_refs['disposition']['path'])):
            _verify_bound(ref)
        anchored_packages.update(str(Path(r['path']).resolve()) for r in source_refs['review_packages'])
    if {str(Path(p).resolve()) for p in case_packages} != anchored_packages:
        raise ValueError('bootstrap cases lack the reviewed source anchors')
    roots = [Path(r).resolve() for r in evidence_roots]
    required = [stage_root, _accepted_root(stage_root), *[Path(p).parent for p in case_packages]]
    if source_refs:
        required.extend(Path(r['path']).parent for key in ('native_packages', 'publication_packages') for r in source_refs[key])
    if any(not any(p.resolve().is_relative_to(r) for r in roots) for p in required):
        raise ValueError('bootstrap closure omits a required evidence root')
    authority = profile_authority.read(accepted_profile, native['configuration']['reviewers'])
    _same_choices(report['active_review_profile'], authority)
    _closed_index_checks(evidence_roots)
    refs = _case_refs(case_packages, report['outcomes'])
    class Cases(Mapping):
        def __iter__(self): return iter(refs)
        def __len__(self): return len(refs)
        def __getitem__(self, cid): return load(_verify_bound(refs[cid]))
    # Rebind execution authority only. Saved report/outcome/source bytes stay put.
    native = deepcopy(native)
    native['configuration']['live_profile_authority'] = authority
    native['configuration']['method_acceptance_package'] = authority['package_path']
    native['configuration'].pop('search_provider_ids', None)
    basis = [_bound_file(gate_path), _bound_file(review_path)]
    if review.get('source_review'): basis.append(review['source_review'])
    # Existing gate + indexed byte proofs authenticate this one-time migration.
    # Verify each source case's method/packet before minting sealed scoring facts.
    source_cases = Cases()
    for cid in source_cases:
        case = source_cases[cid]
        profile_authority.verify_case_method(authority, case['compiled'], case['provider'])
        from .judging import verify_compiled
        verify_compiled(case['compiled'])
    trusted = settlement.VerifiedCases({cid: None for cid in refs}, refs, authority)
    snapshot = settlement.snapshot_parent(report_path, trusted, native, directory,
        case_refs=refs, basis=basis)
    files = _evidence_files(evidence_roots) | {gate_path, review_path, Path(snapshot['path']),
        Path(native_package), *map(Path, case_packages)}
    files.update((Path(stage['clean_manifest_path']), Path(stage['clean_manifest_path']).parent/'approval.json'))
    if source_refs:
        files.update(Path(source_refs[key]['path']) for key in ('report', 'disposition', 'receipt'))
        files.update(Path(r['path']) for key in ('native_packages', 'review_packages', 'publication_packages') for r in source_refs[key])
        disposition = load(source_refs['disposition']['path'])
        files.update(Path(r['path']) for r in _file_references(disposition))
        if disposition.get('method'):
            files.add(Path(disposition['method']['path']).parent/'approval.json')
    files.update(Path(r['path']) for r in basis)
    # Include the independently accepted source method and receipt as anchors.
    old_method = Path(report['active_review_profile']['package_path'])
    files.update((old_method, old_method.parent/'approval.json'))
    closure = dict(profile='verified-evidence-closure-v1', files=file_manifest(files))
    root = Path(directory).resolve()
    write_private(root/'evidence-closure.json', closure)
    result = dict(profile='verified-clean-prefix-v1', snapshot=snapshot,
        gate=_bound_file(gate_path), review=_bound_file(review_path),
        closure=_bound_file(root/'evidence-closure.json'),
        question_ids=[q['question_id'] for q in report['questions']], costs=report['phase_costs'])
    write_private(root/'prefix.json', result)
    return _bound_file(root/'prefix.json')


SEED_TRANSITION_FILES = frozenset({'common.py','clean_run.py','settlement.py','accepted_review.py',
    'operator.py','cli.py','phase_report.py','comparison.py','checkpoint.py'})


def _seed_transition(ref, authority):
    """Declare a compatible transition for one reviewed seed, never later gates."""
    return _seed_transition_fields(ref, authority, SEED_TRANSITION_FILES)


def _seed_transition_fields(ref, authority, allowed):
    prefix = _read_bound(ref); snap = _read_bound(prefix['snapshot'])
    old = snap['native']['configuration']['live_profile_authority']
    method_path = Path(old['package_path']); method = load(method_path); receipt = load(method_path.parent/'approval.json')
    if (old['package_sha256'] != digest(method) or old['approval_sha256'] != digest(receipt)
            or method['profile_bindings'] != old['profile_bindings'] or method['mode'] != old['mode']
            or method['method_options'] != old['method_options'] or snap['code'] != old['profile_bindings']['semantic_code']
            or snap['assets'] != old['profile_bindings']['assets'] or snap['assets'] != asset_bindings()
            or receipt.get('approved') is not True or receipt.get('accept_method') is not True
            or not receipt.get('approved_by') or not receipt.get('approved_at')
            or receipt.get('package_sha256') != digest(method)
            or old['mode'] != authority['mode'] or old['method_options'] != authority['method_options']
            or authority['profile_bindings']['semantic_code'] != code_bindings()):
        raise ValueError('reviewed seed source method or receipt differs')
    _same_choices(old, authority, allowed=allowed)
    new_code=authority['profile_bindings']['semantic_code']; old_code=old['profile_bindings']['semantic_code']
    return dict(profile='compatible-reviewed-seed-v1', prefix=deepcopy(ref),
        old_method_sha256=old['package_sha256'],old_receipt_sha256=old['approval_sha256'],
        new_method_sha256=authority['package_sha256'],
        changed_code_files=sorted(k for k in old_code.keys()|new_code.keys() if old_code.get(k)!=new_code.get(k)))


def _recovered_seed_transition(ref, authority, completion):
    from .interruption import CREDIT_FILES
    _, native, source = _recovery_completion(completion)
    snap = _read_bound(_read_bound(ref)['snapshot'])
    if (snap['code'] != source['code'] or snap['assets'] != source['assets']
            or snap['native']['configuration']['live_profile_authority'] != source['configuration']['live_profile_authority']):
        raise ValueError('recovered prefix does not belong to the original native method')
    native_authority_rebind(completion, authority['package_path'])
    result = _seed_transition_fields(ref, authority, CREDIT_FILES | INTEGRATION_FILES)
    result.update(profile=RECOVERED_SEED, completed_recovery=deepcopy(completion),
        recovery_package_sha256=digest(native))
    return result


def _verified_prefix(ref, authority, *, check_files=True, load_evidence=True, transition=None):
    p = _read_bound(ref)
    closure = _read_bound(p['closure'])
    if p['profile'] != 'verified-clean-prefix-v1' or closure['profile'] != 'verified-evidence-closure-v1':
        raise ValueError('unknown verified prefix')
    if check_files: verify_manifest(closure['files'])
    gate, review = _read_bound(p['gate']), _read_bound(p['review'])
    _validate_review(review, gate)
    if review.get('source_review'): _verify_bound(review['source_review'])
    snap = _read_bound(p['snapshot'])
    compatible = transition is not None
    if compatible:
        expected = (_recovered_seed_transition(ref, authority, transition['completed_recovery'])
            if transition.get('profile') == RECOVERED_SEED else _seed_transition(ref,authority))
        if transition.get('prefix') != ref or transition != expected:
            raise ValueError('seed transition does not bind this exact reviewed prefix')
    if (review['decision'] != 'continue' or gate['report'] != snap['report']
            or (not compatible and snap['code'] != code_bindings()) or snap['assets'] != asset_bindings()
            or (not compatible and snap['native']['configuration']['live_profile_authority'] != authority)):
        raise ValueError('verified prefix has no bound continuation verdict')
    if gate['profile'] == LEAN_GATE and (p['snapshot'] != gate['snapshot'] or p['closure'] != gate['evidence_closure']):
        raise ValueError('verified prefix snapshot/closure differs from reviewed gate')
    if p['question_ids'] != gate['question_ids'] or p['costs'] != gate['costs']:
        raise ValueError('verified prefix scope or cost differs from its gate')
    if p['costs'] != snap['costs'] or p['question_ids'] != snap['question_ids']:
        raise ValueError('verified prefix scope/cost differs from sealed facts')
    if not load_evidence:
        _verify_bound(p['snapshot'])
        return p, None
    if compatible:
        report = _read_bound(gate['report'])
        ids={o['case_id'] for o in report['outcomes']}
        if set(snap['facts']) != ids or set(snap['case_refs']) != ids or digest(report['outcomes']) != snap['outcomes_sha256']:
            raise ValueError('transition seed outcomes/scoring facts differ')
        native=deepcopy(snap['native']); native['configuration']['live_profile_authority']=authority
        native['configuration']['method_acceptance_package']=authority['package_path']
        evidence=(report,settlement.VerifiedCases(snap['facts'],snap['full_cases'],authority),native)
    else:
        evidence = settlement.read_snapshot(p['snapshot'], gate['report']['path'], authority=authority)
    if (p['question_ids'] != [q['question_id'] for q in evidence[0]['questions']]
            or p['costs'] != evidence[0]['phase_costs']):
        raise ValueError('verified prefix scope or cost differs')
    return p, evidence


def rebind_held_checkpoint(gate_path, checklist_path, directory, *, accepted_profile):
    """Bind a held gate to compatible orchestration code, never invent a verdict."""
    gate_path, checklist_path = Path(gate_path).resolve(), Path(checklist_path).resolve()
    if (gate_path.parent/'stage-review.json').exists():
        raise ValueError('quota repair requires a held gate without a recorded verdict')
    gate, checklist = load(gate_path), load(checklist_path)
    if (gate.get('profile') != LEAN_GATE or checklist.get('gate_sha256') != file_digest(gate_path)
            or not checklist.get('reviewed_by') or not checklist.get('reviewed_at')
            or not isinstance(checklist.get('inspection'), dict)
            or any(not checklist['inspection'].get(k) for k in
                ('model_inputs', 'date_source_and_quotes', 'changed_and_unchanged_cases', 'ranking_movements'))):
        raise ValueError('held repair requires an independently bound checkpoint checklist')
    for key in ('report', 'comparison', 'audit', 'ordinary_closure', 'accepted_closure', 'snapshot', 'evidence_closure'):
        _verify_bound(gate[key])
    closure = _read_bound(gate['evidence_closure'])
    if closure.get('profile') != 'verified-evidence-closure-v1':
        raise ValueError('held gate lacks a complete evidence closure')
    verify_manifest(closure['files'])
    meta = _read_bound(gate['snapshot']); native = deepcopy(meta['native'])
    old = native['configuration']['live_profile_authority']
    stage = load(gate_path.parent/'checkpoint.json')
    manifest_path = Path(stage['clean_manifest_path'])
    manifest = _read_bound(dict(path=str(manifest_path), sha256=gate['clean_package_sha256']))
    _approval(manifest, load(manifest_path.parent/'approval.json'))
    method_path = Path(old['package_path']); method = load(method_path); receipt = load(method_path.parent/'approval.json')
    if (meta.get('profile') != settlement.SNAPSHOT_PROFILE or meta['report'] != gate['report']
            or meta['costs'] != gate['costs'] or meta['question_ids'] != gate['question_ids']
            or meta['code'] != manifest['code'] or meta['code'] != old['profile_bindings']['semantic_code']
            or meta['assets'] != manifest['assets'] or manifest['accepted_profile'] != old
            or gate['accepted_profile_sha256'] != digest(old) or stage['index'] != gate['stage_index']
            or old['package_sha256'] != digest(method) or old['approval_sha256'] != digest(receipt)
            or receipt.get('approved') is not True or receipt.get('accept_method') is not True
            or receipt.get('package_sha256') != digest(method)):
        raise ValueError('held source gate/method/snapshot anchors differ')
    authority = profile_authority.read(accepted_profile, native['configuration']['reviewers'])
    _same_choices(old, authority)
    if old['mode'] != authority['mode'] or old['method_options'] != authority['method_options']:
        raise ValueError('held repair changed method options or mode')
    native['configuration']['live_profile_authority'] = authority
    native['configuration']['method_acceptance_package'] = authority['package_path']
    native['quota_repair_source'] = dict(gate=_bound_file(gate_path), closure=gate['evidence_closure'])
    trusted = settlement.VerifiedCases(meta['facts'], meta['full_cases'], authority)
    basis = [_bound_file(gate_path), gate['snapshot'], gate['evidence_closure'], _bound_file(checklist_path),
             _bound_file(manifest_path), _bound_file(manifest_path.parent/'approval.json'),
             _bound_file(method_path), _bound_file(method_path.parent/'approval.json')]
    return settlement.snapshot_parent(gate['report']['path'], trusted, native, directory,
        case_refs=meta['case_refs'], basis=basis)


QUOTA_REPAIR = 'quota-third-repair-checkpoint-v1'


def prepare_quota_repair(gate_path, checklist_path, directory, *, accepted_profile, maximum_tasks):
    """No-call exact missing-third package, including bounded normal follow-up."""
    root = Path(directory).resolve(); gate_path = Path(gate_path).resolve()
    if root.exists() or root.is_relative_to(gate_path.parent.parent):
        raise ValueError('quota repair must use a new directory outside the source lifecycle')
    snapshot = rebind_held_checkpoint(gate_path, checklist_path, root/'source', accepted_profile=accepted_profile)
    gate = load(gate_path)
    evidence = settlement.read_snapshot(snapshot, gate['report']['path'])
    child = settlement.prepare_settlement(gate['report']['path'], root/'thirds',
        accepted_profile=accepted_profile, source_evidence=evidence, verified_parent=snapshot,
        quota_thirds=True, maximum_tasks=maximum_tasks)
    source_stage = load(gate_path.parent/'checkpoint.json')
    source_manifest = load(source_stage['clean_manifest_path'])
    p = dict(profile=QUOTA_REPAIR, created_at=now(), mode=child['mode'], code=code_bindings(), assets=asset_bindings(),
        output_directory=str(root), source_gate=_bound_file(gate_path), source_checklist=_bound_file(checklist_path),
        source_snapshot=snapshot, accepted_profile=child['accepted_profile'],
        thirds=_bound_file(root/'thirds/package.json'), maximum_followup_tasks=2*len(child['roster']),
        archived_reference=source_manifest['archived_reference'], expected_differences=[*gate['expected_differences'],
            dict(category='quota_rejected_third_recovery',
                bindings=dict(source_gate_sha256=file_digest(gate_path), case_ids=[r['case_id'] for r in child['roster']]),
                expected='Previously missing votes may resolve unknowns; unselected outcomes and prior costs remain unchanged.',
                inspect=['no-inference rejection proof and unchanged blind packet', 'new vote, normal settlement and bounded follow-up', 'shared scores and unchanged outcomes'],
                limit='No favorable grade or ranking target; a valid unknown vote is terminal.')],
        scope='Only proven unobtained quota thirds still unknown, then normal reviews limited to those cases. No searches or Pro calls; successful judgments and costs retained. Any repair hold returns to the owner before a replacement package; do not resend successful repair votes.')
    write_private(root/'quota-repair.json', p)
    write_private(root/'approval-template.json', dict(decision='NOT_APPROVED', package_sha256=digest(p), approved_by='', approved_at=''))
    return p


def verify_quota_repair(path):
    path = Path(path).resolve(); p = load(path)
    if (p.get('profile') != QUOTA_REPAIR or p['code'] != code_bindings() or p['assets'] != asset_bindings()
            or p['output_directory'] != str(path.parent)):
        raise ValueError('quota repair code, assets or destination changed')
    gate = _read_bound(p['source_gate']); checklist = _read_bound(p['source_checklist'])
    child = _read_bound(p['thirds'])
    if (checklist.get('gate_sha256') != p['source_gate']['sha256'] or child.get('quota_thirds') != settlement.QUOTA_THIRDS
            or child['verified_parent'] != p['source_snapshot'] or child['accepted_profile'] != p['accepted_profile']
            or p['mode'] != child['mode'] or child['parent_report_path'] != gate['report']['path']
            or p['maximum_followup_tasks'] != 2*len(child['roster'])
            or Path(p['thirds']['path']).resolve() != path.parent/'thirds/package.json'):
        raise ValueError('quota repair source, method or declared child differs')
    evidence = settlement.verify(child, path.parent/'thirds')
    return p, child, evidence


def run_quota_repair(path, approval, *, call=None):
    """Obtain missing votes, apply ordinary policy parity, then await a new verdict."""
    path = Path(path).resolve(); root = path.parent
    p, child, evidence = verify_quota_repair(path); _approval(p, approval)
    if p['mode'] == 'live' and call is not None:
        raise ValueError('live quota repair requires frozen transports')
    with Ledger(root/'coordinator', dict(package_sha256=digest(p), mode=p['mode'])).locked():
        _save_same(root/'approval.json', approval)
        if (root/'gate-evidence.json').exists():
            gate = verify_quota_repair_gate(path, p)
            return dict(state='awaiting_stage_review', gate_sha256=file_digest(root/'gate-evidence.json'),
                report=str(Path(gate['report']['path']).with_suffix('.html')), evidence=gate)
        source_gate = _read_bound(p['source_gate'])
        verify_manifest(_read_bound(source_gate['evidence_closure'])['files'])
        receipt = dict(approved=True, package_sha256=digest(child), parent_quota_repair_sha256=digest(p),
            approved_by=approval['approved_by'], approved_at=approval['approved_at'])
        _save_same(root/'thirds/approval.json', receipt)
        third_closure = root/'thirds/run/closure.json'
        if third_closure.exists() and load(third_closure)['execution']['state'] == 'completed':
            third = settlement.load_parent(root/'thirds/run/report.json')[0]
        else:
            third = settlement.run_settlement(root/'thirds/package.json', receipt, call=call)
        if third['execution']['state'] != 'completed':
            return dict(state=third['execution']['state'], stage='quota_thirds')
        meta = _read_bound(p['source_snapshot']); _, cases, native = evidence
        snapshot_path = root/'third-snapshot/verified-parent.json'
        if not snapshot_path.exists():
            settlement.snapshot_parent(root/'thirds/run/report.json', cases, native, snapshot_path.parent,
                case_refs=meta['case_refs'], basis=[p['source_snapshot'], p['thirds'], _bound_file(root/'thirds/run/closure.json')])
        snapshot = _bound_file(snapshot_path)
        current = settlement.read_snapshot(snapshot, root/'thirds/run/report.json', authority=p['accepted_profile'])
        allowed = {r['case_id'] for r in child['roster']}
        selected = settlement.selected(*current[:2], current[2]['configuration']['reviewers'], authority=p['accepted_profile'])
        if not {r['case_id'] for r in selected} <= allowed:
            raise ValueError('quota follow-up would re-review an unchanged case')
        follow_path = root/'accepted/review-checkpoint.json'
        if not follow_path.exists():
            accepted_review.prepare(root/'thirds/run/report.json', p['accepted_profile']['package_path'], root/'accepted',
                allow_inference=True, maximum_tasks=p['maximum_followup_tasks'], mode=p['mode'], verified_parent=snapshot)
        follow = load(follow_path)
        if (follow['parent_report_sha256'] != file_digest(root/'thirds/run/report.json')
                or follow['accepted_profile'] != p['accepted_profile'] or follow['review_tasks'] != 2*len(selected)):
            raise ValueError('quota follow-up derivation changed')
        follow_receipt = dict(approved=True, package_sha256=digest(follow), parent_quota_repair_sha256=digest(p),
            approved_by=approval['approved_by'], approved_at=approval['approved_at'])
        final = accepted_review.run(follow_path, follow_receipt, call=call)
        if load(root/'accepted/closure.json')['execution']['state'] != 'completed':
            return dict(state='completed_with_holds', stage='quota_followup')
        before = {o['case_id']: o for o in evidence[0]['outcomes']}
        if any(o != before[o['case_id']] for o in final['outcomes'] if o['case_id'] not in allowed):
            raise ValueError('quota repair changed an unselected outcome')
        final_path = root/'accepted/report/report.json'
        final_snapshot_path = root/'final/verified-parent.json'
        if not final_snapshot_path.exists():
            settlement.snapshot_parent(final_path, cases, native, final_snapshot_path.parent,
                case_refs=meta['case_refs'], basis=[p['source_snapshot'], _bound_file(root/'accepted/closure.json')])
        source_gate = _read_bound(p['source_gate'])
        diagnostic = _comparison(p['archived_reference'], final, final_path)
        diagnostic['expected_differences'] = p['expected_differences']
        _save_same(root/'comparison.json', diagnostic)
        previous = _read_bound(source_gate['evidence_closure'])['files']
        _closed_index_checks([root])
        files = _evidence_files([root]) - {str(root/name) for name in
            ('evidence-closure.json', 'gate-evidence.json', 'stage-review.json', 'prefix.json')}
        files.update(Path(r['path']) for r in (p['source_gate'], p['source_checklist'], source_gate['evidence_closure']))
        method_path = Path(p['accepted_profile']['package_path'])
        files.update((method_path, method_path.parent/'approval.json'))
        merged = {r['path']: r for r in previous}
        for ref in file_manifest(files):
            if ref['path'] in merged and merged[ref['path']] != ref:
                raise ValueError('quota repair would replace historical evidence')
            merged[ref['path']] = ref
        _save_same(root/'evidence-closure.json', dict(profile='verified-evidence-closure-v1', files=[merged[k] for k in sorted(merged)]))
        gate = dict(source_gate, clean_package_sha256=digest(p), report=_bound_file(final_path),
            comparison=_bound_file(root/'comparison.json'), snapshot=_bound_file(final_snapshot_path),
            ordinary_closure=_bound_file(root/'thirds/run/closure.json'), accepted_closure=_bound_file(root/'accepted/closure.json'),
            evidence_closure=_bound_file(root/'evidence-closure.json'), accepted_profile_sha256=digest(p['accepted_profile']),
            repair_source_gate=p['source_gate'], expected_differences=p['expected_differences'])
        _save_same(root/'gate-evidence.json', gate)
        return dict(state='awaiting_stage_review', gate_sha256=file_digest(root/'gate-evidence.json'),
            report=str(final_path.with_suffix('.html')), evidence=gate)


def record_quota_repair_review(path, receipt):
    path = Path(path).resolve(); p, _, _ = verify_quota_repair(path)
    _approval(p, load(path.parent/'approval.json'))
    gate = verify_quota_repair_gate(path, p)
    if (receipt.get('gate_sha256') != file_digest(path.parent/'gate-evidence.json')
            or gate['clean_package_sha256'] != digest(p) or gate.get('repair_source_gate') != p['source_gate']):
        raise ValueError('quota review must bind the exact repaired gate')
    _validate_review(receipt, gate)
    if receipt.get('source_review'): _verify_bound(receipt['source_review'])
    if p['mode'] == 'live' and (not receipt.get('source_review')
            or receipt['gate_sha256'] not in Path(receipt['source_review']['path']).read_text()):
        raise ValueError('live quota review requires an independent exact gate-file verdict')
    _save_same(path.parent/'stage-review.json', receipt)
    _write_lean_prefix(path.parent, gate)
    return receipt


def verify_quota_repair_gate(path, package):
    root = Path(path).resolve().parent
    source = _read_bound(package['source_gate']); gate = load(root/'gate-evidence.json')
    expected = dict(source, clean_package_sha256=digest(package), report=_bound_file(root/'accepted/report/report.json'),
        comparison=_bound_file(root/'comparison.json'), snapshot=_bound_file(root/'final/verified-parent.json'),
        ordinary_closure=_bound_file(root/'thirds/run/closure.json'), accepted_closure=_bound_file(root/'accepted/closure.json'),
        evidence_closure=_bound_file(root/'evidence-closure.json'), accepted_profile_sha256=digest(package['accepted_profile']),
        repair_source_gate=package['source_gate'], expected_differences=package['expected_differences'])
    if gate != expected:
        raise ValueError('quota repair gate differs from its declared output and source')
    meta, _ = settlement.verify_snapshot(gate['snapshot'], gate['report']['path'], authority=package['accepted_profile'])
    if meta['report'] != gate['report'] or meta['costs'] != gate['costs'] or meta['question_ids'] != gate['question_ids']:
        raise ValueError('quota repair gate scope or costs differ')
    verify_manifest(_read_bound(gate['evidence_closure'])['files'])
    return gate


def _verify_lean(path, p, *, historical=False):
    _verify_archive_declaration(p)
    if (p['profile'] != LEAN_PROFILE or p.get('verification_policy') != LEAN_VERIFICATION or p['output_directory'] != str(path.parent)
            or p['mode'] not in {'live', 'fixture'} or p['assets'] != asset_bindings()
            or not historical and p['code'] != code_bindings()):
        raise ValueError('lean lifecycle code/assets changed')
    template = _read_bound(p['template'])
    authority = profile_authority.read(p['accepted_profile']['package_path'], template['configuration']['reviewers'])
    if authority != p['accepted_profile'] or authority.get('mode') != p['mode']:
        raise ValueError('lean method acceptance changed')
    prefix = _read_bound(p['verified_seed'])
    if p.get('seed_transition') is not None and p['seed_transition'] != _seed_transition(p['verified_seed'],authority):
        raise ValueError('declared seed transition changed')
    order, admission = batch.frozen_order(p['selection_path'])
    offset = len(prefix['question_ids'])
    if prefix['question_ids'] != [q['question_id'] for q in order[:offset]]:
        raise ValueError('lean seed differs from frozen prefix')
    if any(p[k] != v for k, v in admission.items()):
        raise ValueError('lean bank admission changed')
    for i, stage in enumerate(p['stages'], 1):
        n = stage['count']
        native = stage['native_prototype']
        if (type(n) is not int or n <= 0 or stage['index'] != i
                or stage['cumulative'] != offset+n or stage['questions'] != order[offset:offset+n]
                or len(stage['questions']) != n or native['questions'] != stage['questions']
                or native['code'] != p['code'] or native['assets'] != p['assets']
                or native['configuration'].get('search_provider_ids') != p.get('search_provider_ids')
                or native['configuration']['live_profile_authority'] != authority):
            raise ValueError('lean stage scope or prototype changed')
        offset += n
    if not p['stages'] or p['capacity'] != capacity(p):
        raise ValueError('lean lifecycle capacity changed')
    _review_capability(template)
    return p, template


def _check_lean_gate(path, p, index, *, check_files=True):
    root = path.parent / ('stage-'+str(index))
    gate = load(root/'gate-evidence.json')
    if (gate['profile'] != LEAN_GATE or gate['clean_package_sha256'] != digest(p)
            or gate['stage_index'] != index or gate['accepted_profile_sha256'] != digest(p['accepted_profile'])):
        raise ValueError('lean gate lifecycle or method differs')
    prior = (_read_bound(p['verified_seed']) if index == 1
        else load(path.parent/('stage-'+str(index-1))/'prefix.json'))
    if gate['previous_gate'] != prior['gate'] or gate['previous_review'] != prior['review']:
        raise ValueError('lean gate chain changed')
    review = _read_bound(prior['review'])
    _validate_review(review, _read_bound(prior['gate']))
    if review['decision'] != 'continue':
        raise ValueError('lean previous gate has no continue verdict')
    if check_files:
        for key in ('report', 'comparison', 'audit', 'ordinary_closure', 'accepted_closure', 'snapshot'):
            _verify_bound(gate[key])
    if check_files:
        closure = _read_bound(gate['evidence_closure'])
        verify_manifest(closure['files'])
        snap, _ = settlement.verify_snapshot(gate['snapshot'], gate['report']['path'], authority=p['accepted_profile'])
        if gate['costs'] != snap['costs'] or gate['question_ids'] != snap['question_ids']:
            raise ValueError('lean gate cost/scope differs from sealed report facts')
    else:
        _verify_bound(gate['evidence_closure'])
    return gate


def _record_lean_review(path, receipt):
    p, _ = verify(path)
    _approval(p, load(path.parent/'approval.json'))
    i = receipt.get('stage_index')
    if type(i) is not int or not 1 <= i <= len(p['stages']):
        raise ValueError('lean stage review index differs')
    gate = _check_lean_gate(path, p, i)
    if receipt.get('gate_sha256') != file_digest(path.parent/('stage-'+str(i))/'gate-evidence.json'):
        raise ValueError('review must bind the exact gate file bytes')
    _validate_review(receipt, gate)
    if receipt.get('source_review'): _verify_bound(receipt['source_review'])
    root = path.parent/('stage-'+str(i))
    _save_same(root/'stage-review.json', receipt)
    _write_lean_prefix(root, gate)
    return receipt


def _write_lean_prefix(root, gate):
    prefix = dict(profile='verified-clean-prefix-v1', snapshot=gate['snapshot'],
        gate=_bound_file(root/'gate-evidence.json'), review=_bound_file(root/'stage-review.json'),
        closure=gate['evidence_closure'], question_ids=gate['question_ids'], costs=gate['costs'])
    _save_same(root/'prefix.json', prefix)
    return _bound_file(root/'prefix.json')


def _seal_lean_gate(path, p, stage, prior, final, snapshot, *, extra_evidence_roots=(), retained_bindings=()):
    root = path.parent/('stage-'+str(stage['index']))
    report_path = _accepted_root(root)/'report/report.json'
    diagnostic = _comparison(p['archived_reference'], final, report_path)
    diagnostic['expected_differences'] = stage['expected_differences']
    _save_same(root/'comparison.json', diagnostic)
    previous_files = _read_bound(prior['closure'])['files']
    new_files = _evidence_files([root, _accepted_root(root), *extra_evidence_roots])
    # The manifest and gate cannot contain their own hashes. Both are pinned by
    # the external verdict/next approved seed instead.
    new_files -= {str(root/name) for name in ('evidence-closure.json', 'gate-evidence.json', 'stage-review.json', 'prefix.json')}
    new_files.update(Path(r['path']) for r in (prior['gate'], prior['review'], prior['closure'], p['template'],
        _bound_file(path), _bound_file(path.parent/'approval.json')))
    merged = {r['path']: r for r in previous_files}
    for ref in retained_bindings:
        if ref['path'] in merged and merged[ref['path']] != ref:
            raise ValueError('recovery evidence conflicts with historical bindings')
        merged[ref['path']] = ref
    for ref in file_manifest(new_files):
        if ref['path'] in merged and merged[ref['path']] != ref:
            raise ValueError('new gate would replace a historical evidence binding')
        merged[ref['path']] = ref
    _save_same(root/'evidence-closure.json', dict(profile='verified-evidence-closure-v1',
        files=[merged[k] for k in sorted(merged)]))
    gate = dict(profile=LEAN_GATE, clean_package_sha256=digest(p), stage_index=stage['index'],
        report=_bound_file(report_path), comparison=_bound_file(root/'comparison.json'),
        audit=_bound_file(root/'native-completion-audit.json'),
        ordinary_closure=_bound_file(root/'review/run/closure.json'),
        accepted_closure=_bound_file(_accepted_root(root)/'closure.json'), snapshot=snapshot,
        evidence_closure=_bound_file(root/'evidence-closure.json'), previous_gate=prior['gate'], previous_review=prior['review'],
        question_ids=[q['question_id'] for q in final['questions']], unclassified_native_completions=0,
        undeclared_duplicate_tasks=0, costs=final['phase_costs'], accepted_profile_sha256=digest(p['accepted_profile']),
        expected_differences=stage['expected_differences'])
    _save_same(root/'gate-evidence.json', gate)
    return gate


def _finish_lean_native(path, p, manifest, stage, previous, prior, native, approval, *,
                        fixture_call=None, native_package_path=None, authority_rebind=None,
                        extra_evidence_roots=(), retained_bindings=()):
    """The one post-native review/assembly path; this never dispatches native work."""
    root = path.parent/('stage-'+str(manifest['index']))
    native_package_path = Path(native_package_path) if native_package_path else root/'native/package.json'
    native_file = Path(native['configuration']['run_directory'])/'report.json'
    prior_facts = _read_bound(prior['snapshot'])
    retained_ids = set(prior_facts['case_refs'])
    retained_sha = prior_facts['outcomes_sha256']
    snapshot_native = native
    if authority_rebind is not None:
        authority = verify_native_authority_rebind(native, authority_rebind)
        if authority != p['accepted_profile']:
            raise ValueError('recovered review authority differs from continuation')
        snapshot_native = deepcopy(native)
        snapshot_native['configuration']['live_profile_authority'] = authority
        snapshot_native['completed_native_rebind'] = deepcopy(authority_rebind)
    if not (root/'native-completion-audit.json').exists():
        write_private(root/'native-completion-audit.json', _audit(native))
    review_path = root/'review/package.json'
    if not review_path.exists():
        options = dict(authority_rebind=authority_rebind) if authority_rebind is not None else {}
        child = review_pipeline.prepare_review(native_package_path, [q['question_id'] for q in manifest['questions']], root/'review', **options)
        if p['mode'] == 'fixture':
            child['mode'] = 'fixture'; write_private(review_path, child, replace=True)
    child = load(review_path)
    if (child['source_package_sha256'] != digest(native) or child['source_report_sha256'] != file_digest(native_file)
            or child['questions'] != manifest['questions'] or len(child['jobs']) > manifest['ordinary_review_task_bound']
            or child.get('live_profile_authority') != p['accepted_profile']
            or child.get('native_authority_rebind') != authority_rebind):
        raise ValueError('lean ordinary review derivation differs')
    review_receipt = dict(approved=True, package_sha256=digest(child), parent_clean_run_sha256=digest(p),
        approved_by=approval['approved_by'], approved_at=approval['approved_at'])
    _save_same(root/'review/approval.json', review_receipt)
    ordinary = review_pipeline.run_review(review_path, approval=review_receipt, call=fixture_call)
    if ordinary['execution']['state'] != 'completed':
        return dict(state=ordinary['execution']['state'], stage='review', stage_index=manifest['index'])
    ordinary = None
    snapshot_path = root/'assembled/verified-parent.json'
    if snapshot_path.exists():
        snapshot = _bound_file(snapshot_path)
        meta, _ = settlement.verify_snapshot(snapshot, root/'report/report.json', authority=p['accepted_profile'])
        trusted = settlement.VerifiedCases(meta['facts'], meta['full_cases'], p['accepted_profile'])
        refs = meta['case_refs']
    else:
        _, evidence = _verified_prefix(previous, p['accepted_profile'], check_files=False, transition=p.get('seed_transition') if previous==p['verified_seed'] else None)
        parent, parent_cases, _ = evidence
        child_component = checkpoint.completed_review(review_path)
        child_refs = {r['case_id']: dict(path=str((review_path.parent/r['path']).resolve()), sha256=r['sha256'])
            for r in child_component[1]['cases']}
        full_ids = {o['case_id'] for o in child_component[2]['outcomes'] if o['grade'] is None}
        small = settlement.VerifiedCases({cid: settlement.scoring_fact(c) for cid, c in child_component[3].items()},
            {cid: child_refs[cid] for cid in full_ids}, p['accepted_profile'])
        child_component = (*child_component[:3], small)
        components = [batch.parent_component(stage['parent_report']['path'], evidence), child_component]
        report = _combine(components, stage, native)
        comparison._index(report)
        write_reports(root/'report', report)
        write_private(root/'closure.json', dict(checkpoint_sha256=digest(stage), report_sha256=file_digest(root/'report/report.json'),
            execution='completed', costs=report['phase_costs'], native_report_sha256=file_digest(native_file),
            native_audit_sha256=file_digest(root/'native-completion-audit.json'),
            review_closure_sha256=file_digest(root/'review/run/closure.json')), replace=True)
        cases = {cid: c for component in components for cid, c in component[3].items()}
        refs = dict(prior_facts['case_refs'])
        refs.update(child_refs)
        trusted = settlement.VerifiedCases(cases, {}, p['accepted_profile'])
        # Drop decoded historical/current review reports before sealing
        # and running the next review. No cache retains their raw cases.
        parent = parent_cases = evidence = components = child_component = None
        snapshot = settlement.snapshot_parent(root/'report/report.json', trusted, snapshot_native, root/'assembled', case_refs=refs,
            basis=[_bound_file(root/'closure.json'), _bound_file(root/'native-completion-audit.json'),
                _bound_file(root/'review/run/closure.json')])
        report = cases = None
        meta = load(_verify_bound(snapshot))
        trusted = settlement.VerifiedCases(meta['facts'], meta['full_cases'], p['accepted_profile'])
    accepted_path = _accepted_root(root)/'review-checkpoint.json'
    if not accepted_path.exists():
        accepted_review.prepare(root/'report/report.json', p['accepted_profile']['package_path'], _accepted_root(root),
            allow_inference=True, maximum_tasks=manifest['accepted_review_task_bound'], mode=p['mode'], verified_parent=snapshot)
    accepted = load(accepted_path)
    accepted_receipt = dict(approved=True, package_sha256=digest(accepted), parent_clean_run_sha256=digest(p),
        approved_by=approval['approved_by'], approved_at=approval['approved_at'])
    final = accepted_review.run(accepted_path, accepted_receipt, call=fixture_call)
    if load(_accepted_root(root)/'closure.json')['execution']['state'] != 'completed':
        return dict(state='incomplete', stage='accepted_review', stage_index=manifest['index'])
    if digest([o for o in final['outcomes'] if o['case_id'] in retained_ids]) != retained_sha:
        raise ValueError('lean continuation changed retained outcomes')
    final_snapshot_path = root/'accepted/verified-parent.json'
    if final_snapshot_path.exists():
        final_snapshot = _bound_file(final_snapshot_path)
        settlement.read_snapshot(final_snapshot, _accepted_root(root)/'report/report.json', authority=p['accepted_profile'])
    else:
        final_snapshot = settlement.snapshot_parent(_accepted_root(root)/'report/report.json', trusted, snapshot_native,
            root/'accepted', case_refs=refs, basis=[_bound_file(_accepted_root(root)/'closure.json')])
    _closed_index_checks([root, _accepted_root(root), *extra_evidence_roots])
    gate = _seal_lean_gate(path, p, stage, prior, final, final_snapshot,
        extra_evidence_roots=extra_evidence_roots, retained_bindings=retained_bindings)
    return dict(state='awaiting_stage_review', stage_index=manifest['index'], evidence=gate,
        gate_sha256=file_digest(root/'gate-evidence.json'),
        report=str(_accepted_root(root)/'report/report.html'))


def _run_lean(path, approval, *, env_file=None, fixture_call=None):
    p, template = verify(path)
    _approval(p, approval)
    if p['mode'] == 'live' and fixture_call is not None:
        raise ValueError('live lean run requires frozen transports')
    with Ledger(path.parent/'coordinator', dict(package_sha256=digest(p), mode=p['mode'])).locked():
        _save_same(path.parent/'approval.json', approval)
        previous = p['verified_seed']
        for manifest in p['stages']:
            root = path.parent/('stage-'+str(manifest['index']))
            if (root/'gate-evidence.json').exists():
                gate = _check_lean_gate(path, p, manifest['index'], check_files=False)
                if not (root/'stage-review.json').exists():
                    return dict(state='awaiting_stage_review', stage_index=manifest['index'], evidence=gate,
                        gate_sha256=file_digest(root/'gate-evidence.json'),
                        report=str(_accepted_root(root)/'report/report.html'))
                review = load(root/'stage-review.json')
                _validate_review(review, gate)
                if review.get('gate_sha256') != file_digest(root/'gate-evidence.json'):
                    raise ValueError('recorded review gate bytes changed')
                if review.get('source_review'): _verify_bound(review['source_review'])
                if review['decision'] == 'stop':
                    return dict(state='stopped_after_review', stage_index=manifest['index'])
                previous = _write_lean_prefix(root, gate)
                continue
            prior, _ = _verified_prefix(previous, p['accepted_profile'], load_evidence=False, transition=p.get('seed_transition') if previous==p['verified_seed'] else None)
            carried = prior['costs']['conservative_total_usd']
            if Decimal(carried) >= Decimal(p['shared_ceiling_usd']):
                return dict(state='stopped_at_ceiling', stage_index=manifest['index'], carried_usd=carried)
            cfg, native = _native(template, manifest['questions'], root, p['shared_ceiling_usd'], carried, p['accepted_profile'], p['mode'], search_provider_ids=p.get('search_provider_ids'))
            stage = dict(profile=LEAN_STAGE, clean_manifest_path=str(path), clean_package_sha256=digest(p),
                index=manifest['index'], accepted_profile=p['accepted_profile'], parent_report=load(prior['snapshot']['path'])['report'],
                carried_usd=carried, native_package_sha256=digest(native), expected_differences=manifest['expected_differences'])
            if p.get('archive_comparison') == FIRST_RUN:
                stage['archive_comparison'] = FIRST_RUN
            for dest, value in ((root/'checkpoint.json', stage), (root/'native/config.json', cfg), (root/'native/package.json', native)):
                _save_same(dest, value)
            receipt = dict(approval, package_sha256=digest(native), parent_clean_run_sha256=digest(p))
            _save_same(root/'native/approval.json', receipt)
            native_file = root/'native/run/report.json'
            result = load(native_file) if native_file.exists() else None
            if result is None or result['execution']['state'] != 'completed':
                result = run_native(native, approval=receipt, env_file=env_file)
            if result['package_sha256'] != digest(native): raise ValueError('lean native identity changed')
            if result['execution']['state'] != 'completed':
                return dict(state=result['execution']['state'], stage='native', stage_index=manifest['index'], execution=result['execution'])
            result = None
            return _finish_lean_native(path, p, manifest, stage, previous, prior, native, approval,
                fixture_call=fixture_call)
        closure = dict(state='completed', clean_package_sha256=digest(p), questions=p['stages'][-1]['cumulative'],
            report=load(load(previous['path'])['snapshot']['path'])['report'])
        if not (path.parent/'closure.json').exists():
            verify_manifest(_read_bound(_read_bound(previous)['closure'])['files'])
        _save_same(path.parent/'closure.json', closure)
        return closure


def _bound_file(path):
    path = Path(path).resolve()
    return dict(path=str(path), sha256=file_digest(path))


def _verify_bound(ref):
    path = Path(ref['path'])
    if file_digest(path) != ref['sha256']:
        raise ValueError('clean run bound file changed')
    return path


def _read_bound(ref):
    return load(_verify_bound(ref))


def _archive_prefix(archive, order):
    """Closed archive may end before the new scope, but must match the frozen prefix."""
    rows=archive['questions'];by_id={q['question_id']:q for q in rows}
    prefix=order[:len(rows)]
    if (not rows or len(rows)!=len(by_id) or len(rows)>len(order)
            or set(by_id)!={q['question_id'] for q in prefix}
            or any(by_id[q['question_id']]!=q for q in prefix)):
        raise ValueError('archived diagnostic scope differs from frozen clean prefix')
    return by_id


def _archive_reference(reference):
    if reference is None:
        return None
    if 'report' in reference:
        return reference
    return dict(report=dict(path=reference['report_path'], sha256=reference['report_sha256']),
        closure=dict(path=reference['closure_path'], sha256=reference['closure_sha256']),
        proof_files=reference.get('proof_files', []),
        **({'configuration_replacements': reference['configuration_replacements']}
           if 'configuration_replacements' in reference else {}))


def _verify_archive_declaration(package):
    if ((package['archived_reference'] is None and package.get('archive_comparison') != FIRST_RUN)
            or (package['archived_reference'] is not None and 'archive_comparison' in package)):
        raise ValueError('archive comparison declaration changed')


def _archive_scope(reference, order, configurations):
    if reference is None:
        return {}
    archive = archived_report(reference)
    questions = _archive_prefix(archive, order)
    comparison.configuration_pairs({c['configuration_id'] for c in archive['collection']},
        configurations, reference.get('configuration_replacements'))
    return questions


def _comparison(reference, report, report_path):
    if reference is None:
        return dict(profile=FIRST_RUN, state='unavailable',
            reason='No archived benchmark supplied; this is a first run.',
            current_question_ids=[q['question_id'] for q in report['questions']],
            sources=dict(current=_bound_file(report_path), archived=None))
    result = comparison.compare_reports(archived_report(reference), report,
        replacements=reference.get('configuration_replacements'))
    result['sources'] = dict(current=_bound_file(report_path), archived=reference['report'])
    return result


def archived_report(reference):
    """Authenticate closed diagnostic bytes without replaying old executables.

    A copied accepted-review report keeps its underlying package identity. Its
    wrapper closure therefore requires the separately bound wrapper and receipt.
    None of these historical objects authorizes new dispatch or method choices.
    """
    reference = _archive_reference(reference)
    report = _read_bound(reference['report'])
    receipt = _read_bound(reference['approval']) if reference.get('approval') else None
    closure = _read_bound(reference['closure'])
    sha = reference['report']['sha256']
    execution = closure.get('execution')
    closed = execution == 'completed' or isinstance(execution, dict) and execution.get('state') == 'completed'
    closure_identity = closure.get('package_sha256', closure.get('checkpoint_sha256'))
    identity = report.get('package_sha256')
    wrapper_ref = reference.get('package')
    if wrapper_ref is not None:
        wrapper = _read_bound(wrapper_ref)
        identity = digest(wrapper)
        if (wrapper.get('profile') != accepted_review.PROFILE or receipt is None
                or wrapper.get('output_directory') != str(Path(wrapper_ref['path']).resolve().parent)
                or closure.get('source_report_sha256') != wrapper.get('parent_report_sha256')
                or closure.get('accepted_profile_sha256') != digest(wrapper.get('accepted_profile'))):
            raise ValueError('archived diagnostic wrapper binding differs')
    files = closure.get('report_files', {})
    if (not isinstance(identity, str) or not identity or not closed or report.get('execution', {}).get('state') != 'completed'
            or receipt is not None and (not (receipt.get('approved') is True or receipt.get('decision') == 'approved')
                or not receipt.get('approved_by') or not receipt.get('approved_at')
                or receipt.get('package_sha256') != identity)
            or not (closure.get('report_sha256') == sha or files.get('report.json') == sha)
            or closure_identity != identity):
        raise ValueError('archived diagnostic closure/receipt differs')
    for ref in reference.get('proof_files', []):
        _verify_bound(ref)
    comparison._index(report)
    return report


def _native(template, questions, root, ceiling, carried, authority, mode, *, search_provider_ids=None):
    return batch.native_plan(template, questions, root, ceiling, carried, authority=authority, mode=mode,
        search_provider_ids=search_provider_ids)


def _recollection(reference, authority):
    if reference is None:return None
    from .date_report import recollection_source
    return recollection_source(reference,authority)


def _recollection_ids(authority):
    selected=authority['method_options'].get('provider_date_profiles',{})
    return [cid for cid in authority['profile_bindings']['provider_ids']if cid in selected]


def _review_capability(template):
    cfg = template['configuration']
    reviewers = cfg['reviewers']
    if (reviewers['codex'].get('backend') != 'codex-subscription'
            or reviewers['codex'].get('transport') != 'app-server-stdio-v1'
            or reviewers['claude'].get('backend') != 'claude-subscription'
            or cfg.get('execution_stage') not in (None, 'benchmark')):
        raise ValueError('clean lifecycle requires isolated Codex and Claude subscription reviews and native benchmark judging')


def seed_evidence(report_path, archived_reference, authority):
    """The existing checkpoint review contract applied to the repaired prefix."""
    report,_,native=settlement.load_parent(report_path)
    if report.get('mode')!='date-profile-report-v1' or report.get('active_review_profile')!=authority:
        raise ValueError('clean seed requires an accepted checker-only repaired report')
    package=load(Path(report_path).resolve().parent.parent/'package.json')
    if not package.get('repair_disposition') or package.get('accepted_profile')!=authority or package['jobs']:
        raise ValueError('clean seed lacks an exact zero-call repair disposition')
    if native['configuration'].get('live_profile_authority')!=authority:
        raise ValueError('clean seed template method differs')
    diagnostic=(_comparison(None, report, report_path) if archived_reference is None else
        comparison.compare_reports(archived_report(archived_reference),report,
            replacements=archived_reference.get('configuration_replacements')))
    return dict(profile='clean-repaired-prefix-gate-v1',stage_index=0,report=_bound_file(report_path),
        comparison_sha256=digest(diagnostic),accepted_profile_sha256=digest(authority),
        question_ids=[q['question_id']for q in report['questions']],costs=report['phase_costs'])


def _seed(seed, archived_reference, authority):
    if seed is None:return None
    report_path=_verify_bound(seed['report']);review=_read_bound(seed['review'])
    evidence=seed_evidence(report_path,archived_reference,authority)
    _validate_review(review,evidence)
    report=load(report_path)
    return dict(report=report,reference=seed['report'],review=review,evidence=evidence)


def prepare(template_package, selection_path, directory, *, ceiling, accepted_profile,
            archived_reference=None, stage_counts=STAGE_COUNTS, mode='live', build_identity=None, seed_report=None, seed_review=None,
            recollection_source=None, verified_prefix=None, search_provider_ids=None, rebind_seed=False, created_at=None):
    """Freeze the complete predictable lifecycle. This makes no external calls."""
    root = Path(directory).resolve()
    if mode not in ('live', 'fixture'):
        raise ValueError('clean run mode differs')
    counts = list(stage_counts)
    if not counts or any(type(n) is not int or n <= 0 for n in counts):
        raise ValueError('explicit positive clean stage counts required')
    ceiling = Decimal(ceiling)
    if not ceiling.is_finite() or ceiling <= 0:
        raise ValueError('positive clean cumulative ceiling required')
    template = load(template_package)
    _review_capability(template)
    authority = profile_authority.read(accepted_profile, template['configuration']['reviewers'])
    if authority.get('profile') != profile_authority.METHOD_PROFILE or authority.get('mode') != mode:
        raise ValueError('clean run requires independent exact new-method owner acceptance in its execution mode')
    order, admission = batch.frozen_order(selection_path)
    if (seed_report is None)!=(seed_review is None):raise ValueError('repaired prefix requires its independent review')
    seed_ref=dict(report=_bound_file(seed_report),review=_bound_file(seed_review))if seed_report is not None else None
    seed=_seed(seed_ref,_archive_reference(archived_reference),authority)
    transition = _seed_transition(verified_prefix,authority) if rebind_seed and verified_prefix is not None else None
    if rebind_seed and verified_prefix is None:
        raise ValueError('seed transition requires an explicit reviewed prefix')
    if verified_prefix is not None:
        if seed is not None or recollection_source is not None:
            raise ValueError('verified prefix cannot mix migration strategies')
        prefix, evidence = _verified_prefix(verified_prefix, authority, transition=transition)
        seed = dict(report=evidence[0], reference=load(prefix['snapshot']['path'])['report'])
    replacement=_recollection(recollection_source,authority)
    if search_provider_ids is not None:
        roster=[provider['configuration_id'] for provider in template['providers']]
        if (not isinstance(search_provider_ids,list) or not search_provider_ids
                or search_provider_ids != [cid for cid in roster if cid in search_provider_ids]
                or replacement is not None):
            raise ValueError('declared deferred search scope must be an ordered subset without recollection')
    if replacement and (seed or template['configuration'].get('search_provider_ids')
            or replacement[0]['questions']!=order[:counts[0]]):
        raise ValueError('recollection must replace the exact first stage without a completed seed or narrowed template')
    offset=len(seed['report']['questions'])if seed else 0
    if seed and seed['report']['questions']!=order[:offset]:raise ValueError('repaired prefix differs from frozen bank')
    if seed and {r['configuration_id']for r in seed['report']['collection']}!={r['configuration_id']for r in template['providers']}:
        raise ValueError('repaired prefix provider scope differs')
    if sum(counts)+offset > len(order):
        raise ValueError('clean stages exceed frozen bank')
    archived_reference = _archive_reference(archived_reference)
    configs = {p['configuration_id'] for p in template['providers']}
    _archive_scope(archived_reference, order, configs)
    # Copy only the reviewed configuration template. Its old run path and any
    # execution history are neither loaded nor authority for clean dispatch.
    template_ref = _bound_file(template_package)
    stages, cumulative_slots = [], offset*len(configs)*POOL_DEPTH
    for i, count in enumerate(counts, 1):
        stage_root = root / ('stage-' + str(i))
        questions = order[offset:offset + count]
        search_ids=_recollection_ids(authority)if replacement and i==1 else search_provider_ids
        _, native = _native(template, questions, stage_root, str(ceiling), '0', authority, mode,search_provider_ids=search_ids)
        cumulative_slots += len(questions)*len(configs)*POOL_DEPTH if replacement and i==1 else native['scope']['result_slots']
        stages.append(dict(index=i, count=count, cumulative=offset + count,
            questions=questions, native_prototype=native,
            expected_differences=expected_differences(i, native, authority, archived_reference, deferred=search_provider_ids is not None),
            ordinary_review_task_bound=3 * native['scope']['result_slots'],
            accepted_review_task_bound=2 * cumulative_slots))
        offset += count
    p = dict(profile=PROFILE, created_at=now() if created_at is None else created_at, mode=mode, output_directory=str(root),
        code=code_bindings(), assets=asset_bindings(), build_identity=build_identity,
        template=template_ref, accepted_profile=authority, archived_reference=deepcopy(archived_reference),
        shared_ceiling_usd=str(ceiling), stages=stages, **admission,
        native_derivation='prototype with allowance=shared ceiling minus verified earlier clean native costs; all other bytes fixed',
        review_derivation='existing complete-native ordinary review then accepted unknown review, bounded by frozen result slots',
        review_accounting='Ordinary and accepted reviews use only the bound subscriptions; subscription usage/quota is outside the API ceiling. Native ledgers own API costs and holds.',
        stage_gate='External explicit review binds exact report, comparison and invariant evidence. Material method change stops future stages.',
        stop='Stop after every checkpoint for review. No questions after the declared frozen prefix are authorized.',
        historical_limits='Archived closed bytes are diagnostic only. No historical capture, vote, cost or authority import; complete historic judgment replay is not claimed.')
    if archived_reference is None:
        p['archive_comparison'] = FIRST_RUN
        p['historical_limits'] = 'First run: archive comparison is unavailable. No historical evidence or authority is imported.'
    if transition is not None:
        p['seed_transition']=transition
    if search_provider_ids is not None:
        p['search_provider_ids']=list(search_provider_ids)
    if seed_ref is not None:
        p['completed_seed']=seed_ref
        p['historical_limits']='Archived results remain diagnostic only. Only the explicitly accepted repaired prefix carries same-run evidence and costs; it authorizes no repeated calls.'
    if verified_prefix is not None:
        p.update(profile=LEAN_PROFILE, verified_seed=deepcopy(verified_prefix), verification_policy=LEAN_VERIFICATION,
            historical_limits='Only the reviewed, hash-bound same-run prefix carries outcomes and costs. No historical replay on record/resume.')
    if replacement:
        p['recollection_source']=deepcopy(recollection_source)
        p['historical_limits']='The accepted repaired first-stage evidence is reused only for unchanged-provider cells; original costs and holds carry once. Archived comparison history remains diagnostic. Changed cells receive fresh normal votes.'
    p['capacity'] = capacity(p)
    _save_same(root / 'clean-run.json', p)
    _save_same(root / 'approval-template.json', dict(decision='NOT_APPROVED', package_sha256=digest(p),
        approved_by='', approved_at='', shared_ceiling_usd=p['shared_ceiling_usd']))
    review_path = root / 'APPROVAL-REVIEW.md'
    review_bytes = approval_review(p).encode()
    if review_path.is_symlink() or review_path.exists() and review_path.read_bytes() != review_bytes:
        raise ValueError('clean approval review changed')
    if not review_path.exists():
        write_private(review_path, review_bytes)
    return p


def expected_differences(stage_index, native, authority, archived_reference, *, deferred=False):
    """Predeclare inspectable effects of actual choices, without a score forecast.

    The archived method is not replayed. Except for new collection/votes, these
    are potential effects to investigate, not claims that every choice changed.
    """
    binding = authority['profile_bindings']
    options = authority['method_options']
    trust = binding['date_trust']
    return [dict(category='new_collection_and_votes',
        expected='Every selected cell gets new collection and fresh normal votes; returned evidence, coverage and grades may change or remain the same.',
        bindings=dict(stage_index=stage_index, new_question_ids=[q['question_id'] for q in native['questions']],
            **(dict(searched_configurations=deepcopy(native['configuration']['search_provider_ids']),
                deferred_search_configurations=[p['configuration_id'] for p in native['providers'] if p['configuration_id'] not in native['configuration']['search_provider_ids']],
                deferred_status='not_collected; no provider-quality inference') if deferred else
                dict(recollected_configurations=deepcopy(native['configuration']['search_provider_ids']),
                retained_configurations=[p['configuration_id']for p in native['providers']if p['configuration_id']not in native['configuration']['search_provider_ids']],
                retained_outcomes='Unchanged byte-for-byte; shared scores/ranks may move; source costs/holds carried once.')
                if 'search_provider_ids'in native['configuration']else {}),
            archived_report_sha256=archived_reference['report']['sha256'] if archived_reference is not None else None,
            **({'configuration_replacements': deepcopy(archived_reference['configuration_replacements'])}
               if archived_reference is not None and archived_reference.get('configuration_replacements') else {})),
        inspect=['request and capture timestamps', 'returned evidence and empty/failed cells', 'fresh vote and review provenance'],
        limit='Later collection is not the archived snapshot, a causal filter experiment or a requirement to return equivalent evidence.'),
        dict(category='accepted_model_inputs',
            expected='New judgments use the selected blind input and review profiles; input representation and date interpretation may affect grades without predicting their direction.',
            bindings=dict(method_package_sha256=authority['package_sha256'], method_approval_sha256=authority['approval_sha256'],
                judge_input_profile=binding['judge_input_profile'], review_profiles=profile_authority.profiles(authority)),
            inspect=['actual model-facing prompt/input/schema', 'retained own source meaning and exact quote spans', 'changed and unchanged judgments'],
            limit='The archived method is not replayed here. Profile acceptance and unchanged outputs do not prove judgment accuracy.'),
        dict(category='provider_request_choices',
            expected='Requests use the frozen provider configurations, selected profiles and question date constraints; any retrieval difference needs traceable request/evidence support.',
            bindings=dict(provider_configurations=deepcopy(binding['provider_configurations']),
                request_profiles=deepcopy(options.get('provider_request_profiles', {})),
                **(dict(date_profiles=deepcopy(options['provider_date_profiles']))if options.get('provider_date_profiles')else {}),
                projection_profiles=deepcopy(options.get('provider_projection_profiles', {})),
                search_requests_sha256=digest(native['search_requests']),
                date_capabilities_sha256=digest(native.get('date_capabilities', {}))),
            inspect=['exact per-cell requests and documented date controls', 'returned native fields and capture provenance', 'score/rank movement on the same question scope'],
            limit='No claim that every request differs from the archive, that filters improve scores, or that ranking agreement is required.'),
        dict(category='owner_selected_date_trust',
            expected='Only exact owner-selected native fields are admitted to the existing occurrence review; admission and date handling may explain date-related grade transitions.',
            bindings=dict(date_trust_package_sha256=trust['package_sha256'], date_trust_receipt_sha256=trust['receipt_sha256'],
                eligible_fields={cid: deepcopy(row['eligible_fields']) for cid, row in sorted(trust['selections'].items())}),
            inspect=['native date meanings versus inference admission', 'own evidence, conflicts and exact date quotes', 'date-related grade transitions and remaining unknowns'],
            limit='Admission is not publication-date truth or an automatic grade. Unknowns and score changes alone do not fail a stage.')]


def capacity(p):
    """Derived logical task caps, not a quota forecast or a new execution policy."""
    rows = []
    cumulative_slots = 0
    for stage in p['stages']:
        native = stage['native_prototype']
        scope = native['scope']
        slots = scope['result_slots']
        cumulative_slots += slots
        rows.append(dict(stage_index=stage['index'], new_questions=stage['count'],
            cumulative_questions=stage['cumulative'], providers=len(native['providers']),
            search_cells=scope['search_cells'], result_slots=slots,
            native_tasks=dict(pro_api=scope['primary_tasks'], sol_subscription=scope['second_tasks'],
                conditional_claude_subscription=scope['conditional_third_tasks']),
            ordinary_review=dict(total=stage['ordinary_review_task_bound'], codex=slots, claude=2*slots),
            accepted_review=dict(total=stage['accepted_review_task_bound'], codex=stage['accepted_review_task_bound']//2, claude=stage['accepted_review_task_bound']//2),
            max_attempts_per_task=native['configuration']['execution_policy']['max_attempts_per_task'],
            native_empty_evidence_baseline=deepcopy(native['reservation_forecast'])))
    return dict(profile='clean-lifecycle-capacity-v1', stages=rows,
        limits='Logical task caps from frozen result slots and existing review routes. Quota use is context dependent; pauses and multiple quota windows may be needed. Available capacity and completion date are not established.')


def approval_review(p):
    """A review view of clean-run.json; it owns no settings or run status."""
    authority = p['accepted_profile']
    binding = authority['profile_bindings']
    options = authority.get('method_options', {})
    trust = binding.get('date_trust', {})
    lines = ['# Clean staged run approval review', '',
        'NOT APPROVED. Preparation makes no provider or model calls. This is a derived view of clean-run.json.', '',
        f'- Exact lifecycle package SHA: `{digest(p)}`.',
        f'- Mode: `{p["mode"]}`; cumulative new-run API ceiling: ${p["shared_ceiling_usd"]}.',
        f'- Exact method package: `{authority["package_path"]}`; SHA `{authority["package_sha256"]}`; owner receipt `{authority["approval_sha256"]}`.',
        f'- Date-trust package: `{options.get("date_trust_package")}`; package SHA `{trust.get("package_sha256")}`; owner receipt `{trust.get("receipt_sha256")}`.',
        f'- Selected request profiles: `{json.dumps(options.get("provider_request_profiles", {}), sort_keys=True)}`; other requests use the frozen catalog defaults.',
        (f'- Archived report: `{p["archived_reference"]["report"]["path"]}`; SHA `{p["archived_reference"]["report"]["sha256"]}`. Diagnostic only; no old captures, votes, costs or method authority are imported.' if p["archived_reference"] is not None else '- Archive comparison: unavailable. First run with no archived benchmark.'), '',
        f'- Explicit current-to-archived configuration replacements: `{json.dumps((p["archived_reference"] or {}).get("configuration_replacements", {}), sort_keys=True)}`. Changed modes are a disclosed potential explanation, not a causal result.', '',
        '## Declared method and checkpoint inspection', '',
        f'- Every selected cell gets new collection and fresh normal votes. Model inputs use `{binding.get("judge_input_profile")}`.',
        f'- Date/checker/text/selection profiles: `{json.dumps(profile_authority.profiles(authority), sort_keys=True)}`.',
        f'- Exact owner-selected date fields: `{json.dumps(trust.get("selections", {}), sort_keys=True)}`.',
        '- Inspect these declared choices, selected native request/date mappings and actual new capture dates as possible explanations for movement. They do not predict favorable scores or establish causal improvement.',
        '- At every checkpoint inspect actual model inputs, date/source meaning and quote evidence; inspect changed and unchanged cases; compare same-question rankings, scores, coverage, empty/failed cells and unknowns.',
        '- Unknown shares and rank changes have no automatic pass threshold. Unexplained material differences or invariant failures stop continuation. A material method change requires a new decision about affected earlier work.', '',
        '## Frozen stages and logical task caps', '',
        '| Stage | New / cumulative questions | Providers / search cells | Pro / Sol / conditional Claude | Ordinary review Codex / Claude (total) | Accepted review Codex / Claude (total) | Native API baseline incl. retries |',
        '|---|---|---|---|---|---|---|']
    baseline = Decimal(0)
    for row in p['capacity']['stages']:
        n, ordinary, accepted = row['native_tasks'], row['ordinary_review'], row['accepted_review']
        lines.append(f'| {row["stage_index"]} | {row["new_questions"]} / {row["cumulative_questions"]} | {row["providers"]} / {row["search_cells"]} | {n["pro_api"]} / {n["sol_subscription"]} / {n["conditional_claude_subscription"]} | {ordinary["codex"]} / {ordinary["claude"]} ({ordinary["total"]}) | {accepted["codex"]} / {accepted["claude"]} ({accepted["total"]}) | ${row["native_empty_evidence_baseline"]["baseline_total_usd"]} |')
        baseline += Decimal(row['native_empty_evidence_baseline']['baseline_total_usd'])
    lines.extend(['', '## Predeclared expected differences by stage', '',
        'These exact manifest declarations are fixed before collection. They identify effects to inspect, not favorable predictions, numerical gates or proof that every setting differs from the archive.'])
    for stage in p['stages']:
        lines.extend(['', f'### Stage {stage["index"]}', '', '```json',
            json.dumps(stage['expected_differences'], ensure_ascii=False, sort_keys=True, indent=2), '```'])
    prototype = p['stages'][0]['native_prototype']
    policy = prototype['configuration']['execution_policy']
    lines.extend(['', '## Exact dispatch identities', '',
        '| Configuration | Method and endpoint |', '|---|---|'])
    for provider in prototype['providers']:
        lines.append(f'| {provider["configuration_id"]} | `{provider["method"]} {provider["endpoint"]}` |')
    lines.extend(['', f'- Pro API model: `{prototype["primary"]["model"]}`.',
        f'- Subscription reviewers: `{json.dumps(prototype["configuration"]["reviewers"], sort_keys=True)}`.',
        f'- Maximum attempts per task: `{policy["max_attempts_per_task"]}`; exact retry policy: `{json.dumps(policy["retry_policy"], sort_keys=True)}`.',
        f'- Automatic recovery/uncertainty policy: `{json.dumps(prototype["configuration"].get("automatic_recovery"), sort_keys=True)}`. Absence does not grant automatic resends.',
        '', '## API estimates, subscriptions and recovery', '',
        f'- Sum of native empty-evidence baseline estimates, including their declared retry estimates: ${baseline}. This is not a worst-case bound, observed usage, an invoice or a new spending approval.',
        '- Pro API and provider searches count toward the shared new-run ceiling. Completed native usage and uncertain charge holds come from retained new-run ledgers; earlier clean costs reduce the next native allowance. Archived costs remain separate diagnostic history.',
        '- Sol/Claude ordinary and accepted reviews use only the bound subscriptions. Subscription usage and quota are outside the API ceiling. There is no automatic API fallback.',
        '- Task counts above are logical caps, not guaranteed quota availability. Context size, reviews, retries and reset evidence can change consumption. Quota pauses and multiple windows may be needed; no available capacity or completion date is promised.',
        '- Each native prototype freezes its exact retry, attempt, backoff, uncertainty, stop/reset and private evidence-retention rules. Raw inputs, outputs, failures and holds are retained. Successful tasks are reused on resume.', '',
        '## One lifecycle approval and separate checkpoint verdicts', '',
        '- A single exact owner execution receipt covers only the frozen stages and their declared evidence-derived conditional review/budget branches. This document and the template grant no execution authority.',
        '- Each checkpoint still requires an independently supplied review verdict binding the exact report, comparison, audits and closure evidence before continuation. The pipeline never creates its own passing verdict.',
        '- Complete approval-template.json only after the method/trust choices and the full ceiling, endpoints, reviewer identities, recovery and scope have been explicitly approved. Uncovered changes require a new decision.'])
    if p.get('completed_seed'):
        lines+=['', '## Accepted repaired prefix', '',
            f"- Prefix report: `{p['completed_seed']['report']['path']}`; hash `{p['completed_seed']['report']['sha256']}`.",
            '- Its exact independent checkpoint review is bound in completed_seed. Its completed costs and uncertainty holds count once inside the same shared ceiling; only remaining declared questions receive new calls.']
    if p.get('recollection_source'):
        source=load(_verify_bound(p['recollection_source']['report']))
        lines += ['', '## Accepted first-stage cell replacement', '',
            f"- Original costs/holds ${source['phase_costs']['conservative_total_usd']} reduce the first native allowance and remain inside the same shared ceiling, once.",
            '- Only the selected start-control providers are recollected in the first stage. Other accepted outcomes remain byte-identical. All later stages collect the full method roster.',
            '- The first stage rebuilds all provider scores and stops for independent review. No numeric improvement, loss or churn threshold is selected.']
    return '\n'.join(lines) + '\n'


def verify(path, *, historical=False):
    path = Path(path).resolve()
    p = load(path)
    if p.get('profile') == RECOVERED_STAGE:
        return _verify_recovered_stage(path,p)
    if p.get('profile') == LEAN_PROFILE:
        return _verify_lean(path, p, historical=historical)
    if (p['profile'] != PROFILE or p['output_directory'] != str(path.parent)
            or p['mode'] not in ('live', 'fixture')
            or not historical and (p['code'] != code_bindings() or p['assets'] != asset_bindings())
            or not all(asset_bindings().get(k) == v for k, v in p['assets'].items())):
        raise ValueError('clean lifecycle bindings changed')
    template = _read_bound(p['template'])
    _review_capability(template)
    authority = profile_authority.read(p['accepted_profile']['package_path'], template['configuration']['reviewers'])
    order, admission = batch.frozen_order(p['selection_path'])
    if (authority != p['accepted_profile'] or authority.get('profile') != profile_authority.METHOD_PROFILE
            or authority.get('mode') != p['mode'] or any(p[k] != v for k, v in admission.items())):
        raise ValueError('clean method or bank changed')
    _verify_archive_declaration(p)
    aq = _archive_scope(p['archived_reference'], order, {c['configuration_id'] for c in template['providers']})
    seed=_seed(p.get('completed_seed'),p['archived_reference'],authority)
    replacement=_recollection(p.get('recollection_source'),authority)
    if replacement and (seed or template['configuration'].get('search_provider_ids')
            or replacement[0]['questions']!=order[:p['stages'][0]['count']]):
        raise ValueError('recollection source differs from the exact first stage')
    offset=len(seed['report']['questions'])if seed else 0
    if seed and seed['report']['questions']!=order[:offset]:raise ValueError('repaired prefix differs from frozen bank')
    if seed and {r['configuration_id']for r in seed['report']['collection']}!={r['configuration_id']for r in template['providers']}:
        raise ValueError('repaired prefix provider scope differs')
    slots=offset*len(template['providers'])*POOL_DEPTH
    for i, stage in enumerate(p['stages'], 1):
        count = stage['count']
        if type(count) is not int or count <= 0:
            raise ValueError('clean stage count changed')
        questions = order[offset:offset + count]
        search_ids=_recollection_ids(authority)if replacement and i==1 else p.get('search_provider_ids')
        _, expected = _native(template, questions, path.parent / ('stage-' + str(i)), p['shared_ceiling_usd'], '0', authority, p['mode'],search_provider_ids=search_ids)
        if historical:
            expected.update(code=stage['native_prototype']['code'], assets=stage['native_prototype']['assets'])
        slots += len(questions)*len(template['providers'])*POOL_DEPTH if replacement and i==1 else expected['scope']['result_slots']
        if (len(questions) != count or stage['index'] != i or stage['cumulative'] != offset + count
                or stage['questions'] != questions or any(aq[q['question_id']] != q for q in questions if q['question_id'] in aq)
                or stage['native_prototype'] != expected
                or stage.get('expected_differences') != expected_differences(i, expected, authority, p['archived_reference'], deferred=p.get('search_provider_ids') is not None)
                or stage['ordinary_review_task_bound'] != 3 * expected['scope']['result_slots']
                or stage['accepted_review_task_bound'] != 2 * slots):
            raise ValueError('clean stage manifest/order/bounds changed')
        offset += count
    if not p['stages']:
        raise ValueError('clean lifecycle has no stages')
    if p.get('capacity') != capacity(p):
        raise ValueError('clean lifecycle capacity differs from frozen prototypes')
    return p, template


def _approval(p, approval):
    if (approval.get('decision') != 'approved' or approval.get('package_sha256') != digest(p)
            or not approval.get('approved_by') or not approval.get('approved_at')):
        raise ValueError('exact clean lifecycle owner approval required')


def _save_same(path, value):
    if Path(path).exists():
        if load(path) != value:
            raise ValueError('clean immutable derived receipt/package changed')
    else:
        write_private(path, value)


def _components(stage_root, stage):
    result = []
    if stage.get('recollection_source'):
        source,cases,_=_recollection(stage['recollection_source'],stage['accepted_profile'])
        replaced=set(_recollection_ids(stage['accepted_profile']))
        source=deepcopy(source)
        source['outcomes']=[o for o in source['outcomes']if o['configuration_id']not in replaced]
        ids={o['case_id']for o in source['outcomes']}
        source['collection']=[r for r in source['collection']if r['configuration_id']not in replaced]
        for owner in ('publication_review','review_pipeline'):
            source[owner]['records']=[r for r in source[owner]['records']if r['case_id']in ids]
        source['review_pipeline']['final_unknowns']=sum(o['grade']is None for o in source['outcomes'])
        result.append((dict(recollection_source=deepcopy(stage['recollection_source']),
            question_ids=[q['question_id']for q in source['questions']],adjudication_profile=review_pipeline.PROFILE,
            report_sha256=stage['recollection_source']['report']['sha256']),
            dict(questions=source['questions'],source_method=source['method_version'],source_collection=source['collection']),
            source,{cid:case for cid,case in cases.items()if cid in ids}))
    if stage.get('parent_report'):
        source = stage['parent_report']
        evidence = settlement.load_parent(source['path'])
        if digest(Path(source['path']).read_bytes()) != source['sha256']:
            raise ValueError('clean previous accepted report changed')
        result.append(batch.parent_component(source['path'], evidence))
    child = checkpoint.completed_review(stage_root / 'review/package.json')
    native = load(stage_root / 'native/package.json')
    native_report = load(stage_root / 'native/run/report.json')
    if (child[1]['questions'] != native['questions'] or child[1]['source_package_sha256'] != digest(native)
            or child[1]['source_report_sha256'] != digest((stage_root / 'native/run/report.json').read_bytes())
            or child[1]['source_collection'] != native_report['collection']):
        raise ValueError('clean ordinary review source differs')
    result.append(child)
    return result


def _costs(components, native):
    current = report_costs(native, Path(native['configuration']['run_directory']))
    prior = components[0][2]['phase_costs'] if len(components) > 1 else {k: '0' for k in checkpoint.COST_KEYS}
    costs = {k: str(Decimal(prior[k]) + Decimal(current[k])) for k in checkpoint.COST_KEYS}
    costs['components'] = prior.get('components', []) + [dict(name='clean-new-' + str(len(native['questions'])), **current)]
    return costs


def _combine(components, stage, native):
    result = checkpoint.combine(components, stage, native, _costs(components, native))
    result['mode'] = STAGE_PROFILE
    result['active_review_profile'] = stage['accepted_profile']
    result['live_profiles'] = profile_authority.profiles(stage['accepted_profile'])
    if len(components) > 1:
        for key in ('publication_review', 'checker_acceptance'):
            if key in components[0][2]:
                result[key] = deepcopy(components[0][2][key])
    result['report_limits'][-1] = 'New clean collection and votes only. Stop at this declared checkpoint for external review; archived results are diagnostic.'
    if stage.get('recollection_source'):
        kept=components[0][2]['outcomes']
        result['recollection']=dict(source=deepcopy(stage['recollection_source']),
            replaced_configurations=_recollection_ids(stage['accepted_profile']),unchanged_outcomes=len(kept),
            unchanged_outcomes_sha256=digest(kept),new_search_cells=len(native['search_requests']),
            costs='Original source costs/holds plus this new native ledger, each once.')
        result['report_limits'][-1]='Only the declared providers were recollected later. Other accepted outcomes are retained byte-for-byte; shared scores and ranks are recomputed. Changes are not an isolated causal filter effect.'
    if native['configuration'].get('provider_date_profiles'):
        from .request_profiles import start_disclosures
        result['retrieval_date_controls']=start_disclosures(native)
        result['report_limits']+=result['retrieval_date_controls']['limitations']
    if native['configuration'].get('fixture_accounting'):
        result['report_limits'].append('Fixture-only conservative-rate simulation: amounts are synthetic holds, not service calls or invoices.')
    if stage.get('archive_comparison') == FIRST_RUN:
        result['archive_comparison'] = FIRST_RUN
        result['report_limits'].append('Archive comparison unavailable: first run without an archived benchmark.')
    return result


def _audit(native):
    directory = Path(native['configuration']['run_directory'])
    audit = inspect_ledger(directory)
    from .request_profiles import audit_dispatch
    dispatch_proof=audit_dispatch(native,directory)
    if dispatch_proof['verified_attempts']or dispatch_proof.get('uncertain_attempt_ids'):
        audit['search_dispatch']=dispatch_proof
    if audit['unclassified']:
        raise ValueError('clean native subscription completion is unclassified')
    # Repeated attempts are allowed, repeated successful dispatch is not.
    successes = {}
    def status(event):
        return event['task_id'], load(directory/event['result_path']).get('transport_success') is True
    finished = (e for e in records(directory/'attempts.jsonl') if e['event'] == 'finished')
    for task, success in parallel_map(status, finished):
        if success:
            successes[task] = successes.get(task, 0) + 1
    if any(n > 1 for n in successes.values()):
        raise ValueError('clean native task has duplicate successful completions')
    return audit


def _accepted_root(root):
    return root.parent / (root.name + '-accepted-review')


def _stage_evidence(root, p, stage, *, write=True):
    report_path = _accepted_root(root) / 'report/report.json'
    report, _, _ = settlement.load_parent(report_path)
    if stage.get('recollection_source'):
        source,_,_=_recollection(stage['recollection_source'],stage['accepted_profile'])
        changed=set(_recollection_ids(stage['accepted_profile']))
        retained=[o for o in report['outcomes']if o['configuration_id']not in changed]
        expected=[o for o in source['outcomes']if o['configuration_id']not in changed]
        if retained!=expected:raise ValueError('recollection changed an unchanged-provider outcome')
    prefix=load(p['completed_seed']['report']['path'])['questions']if p.get('completed_seed')else []
    if report['questions'] != prefix+[q for s in p['stages'][:stage['index']] for q in s['questions']]:
        raise ValueError('clean cumulative report differs from frozen ordered prefix')
    diagnostic = _comparison(p['archived_reference'], report, report_path)
    declared = p['stages'][stage['index'] - 1]['expected_differences']
    diagnostic['expected_differences'] = deepcopy(declared)
    path = root / 'comparison.json'
    if write:
        _save_same(path, diagnostic)
    elif not path.exists() or load(path) != diagnostic:
        raise ValueError('clean stage comparison does not replay')
    audit = load(root / 'native-completion-audit.json')
    return dict(profile='clean-stage-gate-evidence-v1', clean_package_sha256=digest(p), stage_index=stage['index'],
        report=_bound_file(report_path), comparison=_bound_file(path), audit=_bound_file(root / 'native-completion-audit.json'),
        ordinary_closure=_bound_file(root / 'review/run/closure.json'),
        accepted_closure=_bound_file(_accepted_root(root) / 'closure.json'),
        question_ids=[q['question_id'] for q in report['questions']],
        unclassified_native_completions=len(audit['unclassified']), undeclared_duplicate_tasks=0,
        costs=report['phase_costs'], accepted_profile_sha256=digest(p['accepted_profile']),
        expected_differences=deepcopy(declared))


def verify_stage_gates(path, package=None):
    """Authenticate existing gate artifacts read-only, including the last stage.

    A native/review stage without gate artifacts remains partial. Comparison may
    exist before the gate write after an interruption; it is still checked.
    """
    path = Path(path).resolve()
    p = verify(path)[0] if package is None else package
    if p.get('profile') == LEAN_PROFILE:
        checks = []
        latest = None
        for stage in p['stages']:
            root = path.parent/('stage-'+str(stage['index']))
            if not (root/'gate-evidence.json').exists(): continue
            gate = _check_lean_gate(path, p, stage['index'], check_files=False)
            review = load(root/'stage-review.json') if (root/'stage-review.json').exists() else None
            if review: _validate_review(review, gate)
            checks.append(dict(stage_index=stage['index'], verified=True, gate_present=True,
                review_decision=review['decision'] if review else None))
            latest = gate['evidence_closure']
        if latest: verify_manifest(_read_bound(latest)['files'])
        if (path.parent/'closure.json').exists() and (len(checks) != len(p['stages'])
                or any(r['review_decision'] != 'continue' for r in checks)):
            raise ValueError('lean completion requires all bound continuation verdicts')
        return checks
    checks = []
    continued = {}
    for stage in p['stages']:
        root = path.parent / ('stage-' + str(stage['index']))
        comparison_path = root / 'comparison.json'
        gate_path = root / 'gate-evidence.json'
        review_path = root / 'stage-review.json'
        if not any(file.exists() for file in (comparison_path, gate_path, review_path)):
            continue
        if review_path.exists() and not gate_path.exists():
            raise ValueError('clean stage review has no bound gate evidence')
        expected = _stage_evidence(root, p, stage, write=False)
        if gate_path.exists() and load(gate_path) != expected:
            raise ValueError('clean stage gate evidence does not replay')
        decision = None
        if review_path.exists():
            receipt = load(review_path)
            _validate_review(receipt, expected)
            decision = receipt['decision']
        continued[stage['index']] = dict(report=expected['report'], decision=decision,
            gate_present=gate_path.exists())
        checks.append(dict(stage_index=stage['index'], verified=True,
            comparison_verified=True, gate_present=gate_path.exists(), review_decision=decision))
    closure_path = path.parent / 'closure.json'
    if closure_path.exists():
        if any(stage['index'] not in continued or not continued[stage['index']]['gate_present']
                or continued[stage['index']]['decision'] != 'continue' for stage in p['stages']):
            raise ValueError('clean completion requires every bound checkpoint continuation verdict')
        expected_closure = dict(state='completed', clean_package_sha256=digest(p),
            report=continued[p['stages'][-1]['index']]['report'], questions=p['stages'][-1]['cumulative'])
        if load(closure_path) != expected_closure:
            raise ValueError('clean lifecycle closure differs from reviewed stages')
    return checks


def record_stage_review(path, receipt):
    """Record a supplied external decision; execution never creates this receipt."""
    path = Path(path).resolve()
    if load(path).get('profile') in {LEAN_PROFILE,RECOVERED_STAGE}:
        return _record_lean_review(path, receipt)
    p, _ = verify(path)
    i = receipt.get('stage_index')
    if type(i) is not int or not 1 <= i <= len(p['stages']):
        raise ValueError('clean stage review index differs')
    root = path.parent / ('stage-' + str(i))
    expected = _stage_evidence(root, p, p['stages'][i - 1])
    _validate_review(receipt, expected)
    _save_same(root / 'stage-review.json', receipt)
    return receipt


def _validate_review(receipt, evidence):
    if (receipt.get('decision') not in ('continue', 'stop') or receipt.get('evidence') != evidence
            or type(receipt.get('stage_index')) is not int
            or receipt.get('stage_index') != evidence.get('stage_index')
            or not receipt.get('reviewed_by') or not receipt.get('reviewed_at')
            or type(receipt.get('material_method_change')) is not bool
            or not isinstance(receipt.get('unexplained_material_differences'), list)
            or not isinstance(receipt.get('invariant_failures'), list)
            or not isinstance(receipt.get('inspection'), dict)
            or any(not receipt['inspection'].get(k) for k in ('model_inputs', 'date_source_and_quotes', 'changed_and_unchanged_cases', 'ranking_movements'))):
        raise ValueError('explicit clean checkpoint review and bound evidence required')
    if receipt['decision'] == 'continue' and (receipt['material_method_change']
            or receipt['unexplained_material_differences'] or receipt['invariant_failures']):
        raise ValueError('material method changes or unresolved checkpoint findings stop continuation')


def run(path, approval, *, env_file=None, fixture_call=None):
    path = Path(path).resolve()
    if load(path).get('profile') == RECOVERED_STAGE:
        return _run_recovered_stage(path,approval,fixture_call=fixture_call)
    if load(path).get('profile') == LEAN_PROFILE:
        return _run_lean(path, approval, env_file=env_file, fixture_call=fixture_call)
    p, template = verify(path)
    _approval(p, approval)
    if p['mode'] == 'live' and fixture_call is not None:
        raise ValueError('live clean run requires frozen transports')
    with Ledger(path.parent / 'coordinator', dict(package_sha256=digest(p), mode=p['mode'])).locked():
        _save_same(path.parent / 'approval.json', approval)
        seed=_seed(p.get('completed_seed'),p['archived_reference'],p['accepted_profile'])
        replacement=_recollection(p.get('recollection_source'),p['accepted_profile'])
        if seed and seed['review']['decision']!='continue':return dict(state='stopped_after_seed_review')
        parent = seed['reference']if seed else None
        for manifest in p['stages']:
            root = path.parent / ('stage-' + str(manifest['index']))
            carried = '0' if parent is None else settlement.load_parent(parent['path'])[0]['phase_costs']['conservative_total_usd']
            first_replacement=replacement is not None and manifest['index']==1
            if first_replacement:carried=replacement[0]['phase_costs']['conservative_total_usd']
            if Decimal(carried) >= Decimal(p['shared_ceiling_usd']):
                return dict(state='stopped_at_ceiling', stage_index=manifest['index'], carried_usd=carried)
            cfg, native = _native(template, manifest['questions'], root, p['shared_ceiling_usd'], carried, p['accepted_profile'], p['mode'],
                search_provider_ids=_recollection_ids(p['accepted_profile'])if first_replacement else p.get('search_provider_ids'))
            stage = dict(profile=STAGE_PROFILE, clean_manifest_path=str(path), clean_package_sha256=digest(p),
                index=manifest['index'], accepted_profile=p['accepted_profile'], parent_report=parent,
                carried_usd=carried, native_package_sha256=digest(native))
            if p.get('archive_comparison') == FIRST_RUN:
                stage['archive_comparison'] = FIRST_RUN
            if first_replacement:stage['recollection_source']=deepcopy(p['recollection_source'])
            _save_same(root / 'checkpoint.json', stage)
            _save_same(root / 'native/config.json', cfg)
            _save_same(root / 'native/package.json', native)
            native_receipt = dict(approval, package_sha256=digest(native), parent_clean_run_sha256=digest(p))
            _save_same(root / 'native/approval.json', native_receipt)
            native_path = root / 'native/run/report.json'
            result = load(native_path) if native_path.exists() else None
            if result is None or result['execution']['state'] != 'completed':
                result = run_native(native, approval=native_receipt, env_file=env_file)
            if result['package_sha256'] != digest(native):
                raise ValueError('clean native report identity differs')
            if result['execution']['state'] != 'completed':
                return dict(state=result['execution']['state'], stage='native', stage_index=stage['index'], execution=result['execution'], costs=report_costs(native, Path(cfg['run_directory'])), prior_committed_and_held_usd=carried)
            native_view=dict(result,questions=native['questions'])
            if first_replacement:
                selected=set(_recollection_ids(p['accepted_profile']))
                native_view['scores']=dict(result['scores'],
                    accounting={cid:row for cid,row in result['scores']['accounting'].items()if cid in selected},
                    question_rows=[r for r in result['scores']['question_rows']if r['configuration_id']in selected])
            comparison._index(native_view)
            if {c['configuration_id'] for c in result['collection']} != {c['configuration_id'] for c in native['search_requests']}:
                raise ValueError('clean native provider cells are missing')
            audit_path = root / 'native-completion-audit.json'
            audit = _audit(native)
            if audit_path.exists():
                old = load(audit_path)
                if {k: v for k, v in old.items() if k != 'generated_at'} != {k: v for k, v in audit.items() if k != 'generated_at'}:
                    raise ValueError('clean native audit changed')
            else:
                write_private(audit_path, audit)
            review_path = root / 'review/package.json'
            if not review_path.exists():
                child = review_pipeline.prepare_review(root / 'native/package.json', [q['question_id'] for q in manifest['questions']], root / 'review')
                if p['mode'] == 'fixture':
                    child['mode'] = 'fixture'
                    write_private(review_path, child, replace=True)
            child = load(review_path)
            with tempfile.TemporaryDirectory(prefix='openweb-clean-review-') as temp:
                expected = review_pipeline.prepare_review(root / 'native/package.json', [q['question_id'] for q in manifest['questions']], temp)
                expected.update(mode=p['mode'], run_directory=str(root / 'review/run'), created_at=child['created_at'])
                if child != expected or len(child['jobs']) > manifest['ordinary_review_task_bound']:
                    raise ValueError('clean ordinary review derivation differs')
            receipt = dict(approved=True, package_sha256=digest(child), parent_clean_run_sha256=digest(p), approved_by=approval['approved_by'], approved_at=approval['approved_at'])
            _save_same(root / 'review/approval.json', receipt)
            if not (root / 'closure.json').exists():
                reviewed = review_pipeline.run_review(review_path, approval=receipt, call=fixture_call)
                if reviewed['execution']['state'] != 'completed':
                    return dict(state=reviewed['execution']['state'], stage='review', stage_index=stage['index'], execution=reviewed['execution'])
                components = _components(root, stage)
                report = _combine(components, stage, native)
                # Also validates exact rectangular cells, ranks and score coverage.
                comparison._index(report)
                write_reports(root / 'report', report)
                write_private(root / 'closure.json', dict(checkpoint_sha256=digest(stage), report_sha256=digest((root / 'report/report.json').read_bytes()), execution='completed', costs=report['phase_costs'], native_report_sha256=digest(native_path.read_bytes())))
            else:
                load_completed(root / 'report/report.json')
            accepted_path = _accepted_root(root) / 'review-checkpoint.json'
            if not accepted_path.exists():
                accepted_review.prepare(root / 'report/report.json', p['accepted_profile']['package_path'], _accepted_root(root), allow_inference=True, maximum_tasks=manifest['accepted_review_task_bound'], mode=p['mode'])
            accepted = load(accepted_path)
            if (accepted['accepted_profile'] != p['accepted_profile'] or accepted['maximum_review_tasks'] != manifest['accepted_review_task_bound']
                    or accepted['parent_report_path'] != str(root / 'report/report.json') or accepted['mode'] != p['mode']):
                raise ValueError('clean accepted review derivation differs')
            receipt = dict(approved=True, package_sha256=digest(accepted), parent_clean_run_sha256=digest(p), approved_by=approval['approved_by'], approved_at=approval['approved_at'])
            final = accepted_review.run(accepted_path, receipt, call=fixture_call)
            execution = load(_accepted_root(root) / 'closure.json')['execution']
            if execution['state'] != 'completed':
                return dict(state=execution['state'], stage='accepted_review', stage_index=stage['index'], execution=execution)
            evidence = _stage_evidence(root, p, stage)
            _save_same(root / 'gate-evidence.json', evidence)
            _save_same(root / 'stage-review-template.json', dict(stage_index=stage['index'],
                decision='NOT_REVIEWED', evidence=evidence, reviewed_by='', reviewed_at='',
                material_method_change=None, unexplained_material_differences=[], invariant_failures=[],
                inspection={k:'' for k in ('model_inputs','date_source_and_quotes',
                    'changed_and_unchanged_cases','ranking_movements')}))
            review = root / 'stage-review.json'
            if not review.exists():
                return dict(state='awaiting_stage_review', stage_index=stage['index'], evidence=evidence, report=str(_accepted_root(root) / 'report/report.html'))
            decision = load(review)
            _validate_review(decision, evidence)
            if decision['decision'] == 'stop':
                return dict(state='stopped_after_review', stage_index=stage['index'], review=str(review))
            parent = evidence['report']
        closure = dict(state='completed', clean_package_sha256=digest(p), report=parent, questions=p['stages'][-1]['cumulative'])
        _save_same(path.parent / 'closure.json', closure)
        return closure


def load_completed(path, *, seen=()):
    """Authenticate and replay a new clean pooled parent for accepted review."""
    path = Path(path).resolve()
    if path in seen:
        raise ValueError('cyclic clean parent')
    root = path.parent.parent
    stage = load(root / 'checkpoint.json')
    p, template = verify(stage['clean_manifest_path'], historical=True)
    manifest = p['stages'][stage['index'] - 1]
    native = load(root / 'native/package.json')
    approval = load(Path(stage['clean_manifest_path']).parent / 'approval.json')
    _approval(p, approval)
    if (stage['profile'] != STAGE_PROFILE or stage['clean_package_sha256'] != digest(p)
            or root != Path(p['output_directory']) / ('stage-' + str(stage['index']))
            or stage['accepted_profile'] != p['accepted_profile'] or native['questions'] != manifest['questions']
            or stage['native_package_sha256'] != digest(native)):
        raise ValueError('clean completed stage identity differs')
    carried = '0'
    if stage['index'] == 1:
        seed=_seed(p.get('completed_seed'),p['archived_reference'],p['accepted_profile'])
        expected_parent=seed['reference']if seed else None
        if stage['parent_report']!=expected_parent or seed and seed['review']['decision']!='continue':
            raise ValueError('clean first stage differs from its declared repaired prefix')
        if seed:carried=seed['report']['phase_costs']['conservative_total_usd']
        replacement=_recollection(p.get('recollection_source'),p['accepted_profile'])
        if stage.get('recollection_source')!=p.get('recollection_source'):
            raise ValueError('completed stage recollection source differs')
        if replacement:carried=replacement[0]['phase_costs']['conservative_total_usd']
    else:
        expected_parent = Path(p['output_directory']) / ('stage-' + str(stage['index'] - 1) + '-accepted-review') / 'report/report.json'
        if stage['parent_report'] != _bound_file(expected_parent):
            raise ValueError('clean parent must be the preceding new stage')
        prior_report = settlement.load_parent(expected_parent, seen=(*seen, path))[0]
        prior_root = Path(p['output_directory']) / ('stage-' + str(stage['index'] - 1))
        prior_gate = load(prior_root / 'gate-evidence.json')
        prior_review = load(prior_root / 'stage-review.json')
        _validate_review(prior_review, prior_gate)
        if (prior_review['decision'] != 'continue' or prior_gate['clean_package_sha256'] != digest(p)
                or prior_gate['stage_index'] != stage['index'] - 1 or prior_gate['report'] != stage['parent_report']
                or prior_gate.get('expected_differences') != p['stages'][stage['index'] - 2]['expected_differences']
                or prior_gate['costs'] != prior_report['phase_costs']):
            raise ValueError('clean previous checkpoint has no bound continuation verdict')
        for name in ('report', 'comparison', 'audit', 'ordinary_closure', 'accepted_closure'):
            _verify_bound(prior_gate[name])
        carried = prior_report['phase_costs']['conservative_total_usd']
    _, expected = _native(template, manifest['questions'], root, p['shared_ceiling_usd'], carried, p['accepted_profile'], p['mode'],
        search_provider_ids=_recollection_ids(p['accepted_profile'])if stage.get('recollection_source')else p.get('search_provider_ids'))
    expected.update(code=native['code'], assets=native['assets'])
    if native != expected or stage['carried_usd'] != carried:
        raise ValueError('clean completed native allowance differs')
    audit = _audit(native)
    saved_audit = load(root / 'native-completion-audit.json')
    if {k: v for k, v in audit.items() if k != 'generated_at'} != {k: v for k, v in saved_audit.items() if k != 'generated_at'}:
        raise ValueError('clean completed audit differs')
    components = _components(root, stage)
    replay = _combine(components, stage, native)
    report = load(path)
    closure = load(root / 'closure.json')
    if (closure['checkpoint_sha256'] != digest(stage) or closure['execution'] != 'completed'
            or closure['report_sha256'] != digest(path.read_bytes()) or closure['costs'] != replay['phase_costs']
            or closure['native_report_sha256'] != digest((root / 'native/run/report.json').read_bytes())):
        raise ValueError('clean completed closure differs')
    for key in replay:
        if key != 'generated_at' and json.loads(encoded(replay[key])) != report.get(key):
            raise ValueError('clean completed report does not replay: ' + key)
    comparison._index(report)
    cases = {cid: case for _, _, _, own in components for cid, case in own.items()}
    if stage.get('recollection_source'):
        native=deepcopy(native)
        native['configuration'].pop('search_provider_ids',None)
    return report, cases, native
