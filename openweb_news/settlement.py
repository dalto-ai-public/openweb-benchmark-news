"""Versioned case-level publication review over an audited completed report."""
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from itertools import combinations
from pathlib import Path
import time

from . import publication
from .common import asset_bindings, code_bindings, digest, load, now, private_directory, records, write_private, file_digest, verify_manifest
from .checkpoint import completed_review, COST_KEYS
from .phase_review import completed_bundle
from .review_pipeline import review_execution
from .completion_audit import inspect_ledger
from .accounting import report_costs
from .reporting import write_reports
from .subscription_health import terminal_verified
from ._method import scoring


SNAPSHOT_PROFILE = 'verified-scoring-parent-v1'


class VerifiedCases(Mapping):
    """Scoring facts, with full evidence loaded only for publication review.

    This is a per-read view of a bound snapshot, not a global or mutable cache.
    Only read_snapshot constructs trusted instances in the execution path.
    """
    def __init__(self, facts, full_cases, authority):
        self.facts = facts
        self.full_cases = full_cases
        self.authority = authority

    def __iter__(self):
        return iter(self.facts)

    def __len__(self):
        return len(self.facts)

    def __getitem__(self, cid):
        if cid not in self.full_cases:
            return self.facts[cid]
        ref = self.full_cases[cid]
        path = Path(ref['path'])
        if path.is_symlink():
            raise ValueError('verified case must not be a symlink')
        raw = path.read_bytes()
        if digest(raw) != ref['sha256']:
            raise ValueError('verified publication case changed')
        import json
        return json.loads(raw)


def scoring_fact(case):
    """Only the prior-case fields consumed by the unchanged pooled scorer."""
    own = case['compiled']
    return dict(original=dict(configuration_id=case['original']['configuration_id']),
        compiled=dict(packet=dict(returned_url=own['packet']['returned_url']),
            input={k: deepcopy(own['input'][k]) for k in ('native_field_states', 'captured_at')}))


def snapshot_parent(report_path, cases, native, directory, *, case_refs, basis=()):
    """Seal facts after the existing full audit, without duplicating raw cases.

    The caller owns the one-time verification. The enclosing approved lifecycle
    and gate pin this snapshot; the snapshot itself never authorizes dispatch.
    """
    from .profile_authority import verify_case_method
    report_path = Path(report_path).resolve()
    report = load(report_path)
    authority = native['configuration']['live_profile_authority']
    if not isinstance(cases, VerifiedCases) or cases.authority != authority or not basis:
        raise ValueError('snapshot requires fully verified evidence and its bound basis')
    ids = {o['case_id'] for o in report['outcomes']}
    if set(cases) != ids or set(case_refs) != ids:
        raise ValueError('verified scoring facts require complete exact cases')
    full = {o['case_id'] for o in report['outcomes'] if o['grade'] is None}
    full.update(r['case_id'] for r in report.get('publication_review', {}).get('records', []))
    facts = {}
    for cid in sorted(ids):
        case = cases[cid]
        if not isinstance(cases, VerifiedCases):
            verify_case_method(authority, case['compiled'], case['provider'])
        facts[cid] = scoring_fact(case)
    selected_refs = {cid: case_refs[cid] for cid in sorted(full)}
    # Existing manifest references, never a newly blessed hash for a changed case.
    verify_manifest(list({r['path']: r for r in selected_refs.values()}.values()))
    snapshot = dict(profile=SNAPSHOT_PROFILE, code=code_bindings(), assets=asset_bindings(),
        report=dict(path=str(report_path), sha256=file_digest(report_path)),
        native=deepcopy(native), facts=facts, full_cases=selected_refs,
        case_refs=deepcopy(case_refs), basis=list(basis),
        outcomes_sha256=digest(report['outcomes']),
        costs=deepcopy(report['phase_costs']), question_ids=[q['question_id'] for q in report['questions']],
        scope='Verified scoring facts and publication evidence only; no execution approval.')
    path = Path(directory).resolve() / 'verified-parent.json'
    write_private(path, snapshot)
    return dict(path=str(path), sha256=file_digest(path))


def verify_snapshot(reference, report_path, *, authority=None):
    """Check sealed metadata/bytes without decoding the cumulative report."""
    from .profile_authority import read
    path = Path(reference['path'])
    if file_digest(path) != reference['sha256']:
        raise ValueError('verified parent snapshot changed')
    p = load(path)
    native = p['native']
    selected = read(native['configuration']['live_profile_authority']['package_path'],
        native['configuration']['reviewers'])
    if (p['profile'] != SNAPSHOT_PROFILE or p['code'] != code_bindings()
            or p['assets'] != asset_bindings() or selected != native['configuration']['live_profile_authority']
            or authority is not None and selected != authority
            or native['mode'] != selected['mode']
            or Path(p['report']['path']).resolve() != Path(report_path).resolve()):
        raise ValueError('verified parent code, method or report binding changed')
    verify_manifest(list({r['path']: r for r in [p['report'], *p['basis']]}.values()))
    return p, selected


def read_snapshot(reference, report_path, *, authority=None):
    """Authenticate sealed facts with byte hashes; never replay historical work."""
    p, selected = verify_snapshot(reference, report_path, authority=authority)
    native = p['native']
    report = load(report_path)
    ids = {o['case_id'] for o in report['outcomes']}
    if set(p['facts']) != ids or set(p['case_refs']) != ids:
        raise ValueError('verified parent scoring scope differs')
    if p['costs'] != report['phase_costs'] or p['question_ids'] != [q['question_id'] for q in report['questions']]:
        raise ValueError('verified parent scope/cost facts differ')
    return report, VerifiedCases(p['facts'], p['full_cases'], selected), native

PROFILE = 'publication-settlement-overlay-v1'
QUOTA_THIRDS = 'proven-unobtained-quota-thirds-v1'
TRIAL_PROFILES = dict(selection_profile=publication.SELECTION_PROFILE,
    checker_profile=publication.READING_PROFILE,date_profile='quoted-date-reading-v3',text_profile='unicode-spaces-v1')


def fixture_method(package):
    """A read-only fixture lineage marker; verify() authenticates its receipt."""
    from .profile_authority import METHOD_PROFILE
    from .review_pipeline import PROFILE as ORDINARY_REVIEW_PROFILE
    if package.get('profile') == PROFILE:
        authority=package.get('accepted_profile') or {}
    elif package.get('profile') == ORDINARY_REVIEW_PROFILE:
        authority=package.get('live_profile_authority') or {}
    else:
        return False
    return (package.get('mode') == 'fixture' and authority.get('profile') == METHOD_PROFILE
            and authority.get('mode') == 'fixture')


def _read_case(root, ref):
    p = Path(root) / ref['path']
    if p.is_symlink() or not p.resolve().is_relative_to(Path(root).resolve()) or digest(p.read_bytes()) != ref['sha256']:
        raise ValueError('source case binding differs')
    return load(p)


def load_parent(path, *, seen=()):
    """Historical audits never authorize dispatch under old code bindings."""
    path = Path(path).absolute()
    if path in seen:
        raise ValueError('cyclic settlement parent')
    report = load(path)
    root = path.parent.parent
    if report['mode']=='date-profile-report-v1':
        from .date_report import load_completed
        return load_completed(path,seen=seen)
    if (root/'review-checkpoint.json').exists():
        from .accepted_review import load_completed
        return load_completed(path,seen=seen)
    if report['mode']=='clean-stage-v1':
        from .clean_run import load_completed
        return load_completed(path,seen=seen)
    if report['mode']=='additive-batch-v1':
        from .batch import load_completed
        return load_completed(path,seen=seen)
    if report['mode']=='saved-native-checkpoint-recovery-v1':
        from .checkpoint_recovery import load_completed
        return load_completed(path,seen=seen)
    if report['mode'] == 'publication-checker-adoption-v1':
        from .adoption import load_completed
        return load_completed(path, seen=seen)
    if report['mode'] == PROFILE:
        package = load(root / 'package.json')
        if package['mode'] != 'live' and not fixture_method(package):
            raise ValueError('fixture review cannot be used as a live evidence parent')
        parent, cases, native = verify(package, root, historical=True, seen=(*seen, path))
        closure = load(root / 'run/closure.json')
        receipt = load(root / 'approval.json')
        if (report['package_sha256'] != digest(package) or closure['package_sha256'] != digest(package)
                or closure['report_sha256'] != digest(path.read_bytes()) or receipt.get('approved') is not True
                or receipt.get('package_sha256') != digest(package) or report['execution']['state'] != 'completed'):
            raise ValueError('completed settlement binding differs')
        if fixture_method(package):
            responses,ledger_sha=_fixture_responses(package)
            if (closure.get('fixture_ledger_sha256') != ledger_sha
                    or closure.get('fixture_response_hashes') != {task:digest(raw) for task,raw in responses.items()}):
                raise ValueError('settlement fixture evidence changed after closure')
        else:
            responses = _completed_responses(package)
        replay = accepted_overlay(parent, cases, native, package, responses)
        if (replay['outcomes'] != report['outcomes'] or replay.get('publication_review') != report.get('publication_review')
                or replay.get('quota_third_review') != report.get('quota_third_review')
                or replay.get('active_review_profile')!=report.get('active_review_profile')):
            raise ValueError('completed settlement replay differs')
        return report, cases, native
    if report['mode'] != 'additive-checkpoint-v1':
        raise ValueError('an audited combined checkpoint or settlement parent is required')
    checkpoint = load(root / 'checkpoint.json')
    closure = load(root / 'closure.json')
    approval = load(root / 'approval.json')
    native = load(root / 'native/package.json')
    if (report['execution']['state'] != 'completed' or report['package_sha256'] != digest(checkpoint)
            or closure['checkpoint_sha256'] != digest(checkpoint) or closure['execution'] != 'completed'
            or closure['report_sha256'] != digest(path.read_bytes()) or approval.get('decision') != 'approved'
            or approval.get('package_sha256') != digest(checkpoint)
            or digest(native) != checkpoint['native_package_sha256']):
        raise ValueError('completed checkpoint binding differs')
    cases, outcomes, questions, collection, costs = {}, {}, [], [], []
    original = load(checkpoint['source_package_path'])
    if digest(original) != checkpoint['source_package_sha256']:
        raise ValueError('original source package changed')
    costs.append(report_costs(original, Path(original['configuration']['run_directory'])))
    for expected in report['phase_components']:
        component = (completed_bundle(expected['bundle_path']) if 'bundle_path' in expected
                     else completed_review(expected['review_package_path']))
        binding, p, r, own = component
        if binding != expected or cases.keys() & own.keys():
            raise ValueError('completed component differs')
        cases.update(own)
        for o in r['outcomes']:
            outcomes[o['case_id']] = o
        questions += p['questions']
        collection += p['source_collection']
        if 'costs' in binding:
            costs.append(binding['costs'])
    costs.append(report_costs(native, Path(native['configuration']['run_directory'])))
    if (outcomes != {o['case_id']: o for o in report['outcomes']}
            or len(outcomes) != len(report['outcomes']) or questions != report['questions']
            or collection != report['collection']):
        raise ValueError('parent aggregate differs from audited components')
    for key in COST_KEYS:
        if sum((Decimal(c[key]) for c in costs), Decimal(0)) != Decimal(report['phase_costs'][key]):
            raise ValueError('parent accounting differs')
    if closure['costs'] != report['phase_costs']:
        raise ValueError('parent closure accounting differs')
    return report, cases, native


def identity(compiled, reviewers, *, decoder_code=None, authority=None):
    packet = publication.packet_for(compiled)
    if authority is not None:
        binding=dict(packet=packet,profile=authority['profile_bindings'],
            native_proof_profile='verified-normal-or-recovered-terminal-v1')
        if compiled.get('judge_input_profile'):
            binding['private_source_packet_sha256']=compiled['packet_sha256']
        return digest(binding)
    decoder_files = ('publication.py', 'judging.py', 'representation.py', '_method/evidence.py',
                     '_method/two_step.py', '_method/text_dates.py', '_method/judgment_support.py', '_method/relative_dates.py')
    code = code_bindings() if decoder_code is None else decoder_code
    return digest(dict(packet=packet, reviewers=reviewers, profile=PROFILE,
                       decoder_bindings={name: code[name] for name in decoder_files},
                       native_proof_profile='verified-normal-or-recovered-terminal-v1'))


def selected(parent, cases, reviewers, *, decoder_code=None, authority=None,trial_profiles=None):
    from .profile_authority import profiles, POLICY_SELECTION, verify_case_method
    if isinstance(cases, VerifiedCases):
        if cases.authority != authority:
            raise ValueError('verified scoring facts belong to another method')
    else:
        for case in cases.values():
            verify_case_method(authority,case['compiled'],case['provider'])
    live=profiles(authority)if authority is not None else {}
    terminal = {r['terminal_key'] for r in parent.get('publication_review', {}).get('records', []) if r['terminal']}
    if (authority is not None and (live or parent.get('checker_acceptance',{}).get('approval_sha256')==authority['approval_sha256'])):
        active=parent.get('active_review_profile',{});parent_profiles=active.get('profile_bindings',active)
        for row in parent.get('publication_review',{}).get('records',[]):
            if not row['terminal']or row.get('checker_profile')!=authority['profile_bindings']['checker_profile']:
                continue
            if live and any(row.get(k,parent_profiles.get(k))!=live[k]for k in ('date_profile','text_profile')):continue
            compiled=cases[row['case_id']]['compiled'];packet=publication.packet_for(compiled)
            provenance=row.get('provenance',{})
            if (set(provenance)=={'codex','claude'} and all(v.get('native_completion_verified')is True
                    and v.get('input_sha256')==packet['input_sha256']for v in provenance.values())):
                terminal.add(identity(compiled,reviewers,authority=authority))
    result = []
    trial_terminal=set()
    if trial_profiles is not None:
        if trial_profiles!=TRIAL_PROFILES:raise ValueError('unsupported experimental review profiles')
        for row in parent.get('publication_review',{}).get('records',[]):
            cid=row['case_id'];proof=row.get('provenance',{})
            if (row.get('terminal')and cid in cases and set(proof)=={'codex','claude'}
                and all(v.get('native_completion_verified')is True and v.get('input_sha256')==publication.packet_for(cases[cid]['compiled'])['input_sha256']for v in proof.values())):
                trial_terminal.add(cid)
    for o in parent['outcomes']:
        if o['grade'] is not None:
            continue
        c = cases[o['case_id']]
        if o['case_id']in trial_terminal:continue
        selection=(trial_profiles or live).get('selection_profile')
        if selection==POLICY_SELECTION:selection=None
        route = publication.eligibility(c['compiled'], o['positions'], c['provider'],
            **(dict(selection_profile=selection)if selection is not None else {}))
        if route:
            key = identity(c['compiled'], reviewers, decoder_code=decoder_code,authority=authority)
            if trial_profiles:key=digest(dict(base=key,experimental_review_profiles=trial_profiles))
            if key not in terminal:
                result.append(dict(case_id=o['case_id'], terminal_key=key, route=route))
    return result


def primary_category(outcome):
    from .review import numeric
    n=sum(numeric(outcome['positions'].get(r,{}))is not None for r in ('pro','codex'))
    return ('no_primary_numeric','one_primary_numeric','conflicting_primaries')[n]


def representative_roster(roster,parent):
    """One case per eligible provider, covering available primary-vote strata."""
    outcomes={o['case_id']:o for o in parent['outcomes']};chosen={}
    ordered=sorted(roster,key=lambda r:digest(r['case_id']))
    categories={primary_category(outcomes[r['case_id']])for r in ordered}
    for category in sorted(categories,key=lambda c:(sum(primary_category(outcomes[r['case_id']])==c for r in ordered),c)):
        options=[r for r in ordered if primary_category(outcomes[r['case_id']])==category
                 and outcomes[r['case_id']]['configuration_id']not in chosen]
        if options:
            row=options[0];chosen[outcomes[row['case_id']]['configuration_id']]=row
    for row in ordered:chosen.setdefault(outcomes[row['case_id']]['configuration_id'],row)
    result=[r for r in roster if r['case_id']in {v['case_id']for v in chosen.values()}]
    if {primary_category(outcomes[r['case_id']])for r in result}!=categories:raise ValueError('one-per-provider sample cannot cover review strata')
    return result


def jobs_for(roster, cases, reviewers, *, authority=None):
    from .profile_authority import verify_case_method
    if isinstance(cases, VerifiedCases):
        if cases.authority != authority:
            raise ValueError('verified scoring facts belong to another method')
    else:
        for case in cases.values():
            verify_case_method(authority,case['compiled'],case['provider'])
    jobs = []
    for row in roster:
        quota = row.get('route') == QUOTA_THIRDS
        from .judging import blind_packet
        packet = (blind_packet(cases[row['case_id']]['compiled']) if quota
                  else publication.packet_for(cases[row['case_id']]['compiled']))
        for role in (('claude',) if quota else ('codex', 'claude')):
            stage = 'third' if quota else 'publication'
            jobs.append(dict(task_id=stage + '-' + role + '-' + row['case_id'], stage=stage,
                kind='model', role=role, case_id=row['case_id'], backend=reviewers[role]['backend'],
                backend_config=deepcopy(reviewers[role]), blind_packet=packet,
                reservation_usd='0', reservation_basis='configured subscription; quota and time separate'))
    return jobs


def quota_rejected_without_inference(raw, events):
    """A rejected account request is not an answered-but-invalid third vote."""
    final = [e for e in events if e.get('type') == 'result']
    if len(final) != 1 or not events or events[-1] != final[0]:
        return False
    end = final[0]
    usage = raw.get('usage')
    if (raw.get('transport_success') is not False or raw.get('exit_code') != 1
            or raw.get('dispatch_state') != 'completed' or raw.get('verdict') is not None
            or raw.get('served_model') != '<synthetic>' or raw.get('cli_cost_usd_estimate') != 0
            or end.get('is_error') is not True or end.get('api_error_status') != 429
            or end.get('structured_output') is not None or end.get('modelUsage') != {}
            or end.get('total_cost_usd') != 0 or not isinstance(usage, dict)
            or usage != end.get('usage') or usage.get('iterations') != []
            or any(type(usage.get(k)) is not int or usage[k] != 0 for k in
                ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))):
        return False
    def zero_numbers(value):
        if isinstance(value, dict): return all(zero_numbers(v) for v in value.values())
        if isinstance(value, list): return not value
        return value == 0 if isinstance(value, (int, float)) else True
    rates = [e.get('rate_limit_info', {}) for e in events if e.get('type') == 'rate_limit_event']
    return (zero_numbers(usage) and bool(rates) and all(r.get('status') == 'rejected' for r in rates)
            and all(e.get('message', {}).get('model') == '<synthetic>'
                    for e in events if e.get('type') == 'assistant'))


def selected_quota_thirds(parent, cases, native, *, exclusions=None):
    """Select missing first thirds from closed evidence, never by provider or grade preference."""
    from .clean_run import _read_bound
    from .judging import blind_packet
    from .review import needs_third
    source = native.get('quota_repair_source')
    if not source:
        raise ValueError('quota repair requires a gate-bound evidence source')
    files = {r['path']: r['sha256'] for r in _read_bound(source['closure'])['files']}
    run = Path(native['configuration']['run_directory']).resolve()
    roster = []
    exclusions = [] if exclusions is None else exclusions
    for old in parent['outcomes']:
        if old['grade'] is not None or old['positions'].get('claude', {}).get('state') != 'unjudged':
            continue
        cid = old['case_id']; case = cases[cid]
        if not needs_third(old['positions']['pro'], old['positions']['codex']):
            exclusions.append(dict(case_id=cid, reason='existing_table_does_not_require_third'))
            continue
        provenance = case.get('provenance', {}).get('claude', {})
        aid = provenance.get('attempt_id', '')
        if not isinstance(aid, str) or len(aid) != 64 or any(c not in '0123456789abcdef' for c in aid):
            raise ValueError('unjudged quota candidate has no valid source attempt binding')
        folder = run/'attempts'/aid
        paths = [folder/name for name in ('request.json', 'result.json', 'stdout.jsonl')]
        if not all(str(p) in files for p in paths):
            raise ValueError('unjudged quota candidate lacks closed source evidence')
        refs = [dict(path=str(p), sha256=files[str(p)]) for p in paths]
        verify_manifest(refs)
        request, raw = load(paths[0]), load(paths[1])
        if (refs[0]['sha256'] != provenance.get('request_sha256')
                or refs[1]['sha256'] != provenance.get('result_sha256')
                or request.get('role') != 'claude'
                or request.get('backend_config') != native['configuration']['reviewers']['claude']
                or request.get('blind_packet') != blind_packet(case['compiled'])):
            raise ValueError('quota rejection differs from the original blind request')
        if quota_rejected_without_inference(raw, list(records(paths[2]))):
            roster.append(dict(case_id=cid, route=QUOTA_THIRDS, source_attempt=aid, rejection=refs))
        else:
            exclusions.append(dict(case_id=cid, reason='not_proven_no_inference_quota_rejection'))
    return roster


def prepare_settlement(parent_report, directory, *, build_identity=None, accepted_profile=None, source_evidence=None,experimental=False,maximum_tasks=None,mode=None,verified_parent=None,quota_thirds=False):
    if experimental and maximum_tasks is None:raise ValueError('experimental review requires an explicit task maximum')
    parent_report = Path(parent_report).resolve()
    root = Path(directory).resolve()
    if (root == parent_report.parent.parent or root.is_relative_to(parent_report.parent.parent)
            or parent_report.is_relative_to(root)):
        raise ValueError('settlement must be outside its source run')
    parent, cases, native = load_parent(parent_report)if source_evidence is None else source_evidence
    cfg = native['configuration']
    reviewers = cfg['reviewers']
    if (reviewers['codex']['backend'] != 'codex-subscription'
            or reviewers['codex'].get('transport') != 'app-server-stdio-v1'
            or reviewers['claude']['backend'] != 'claude-subscription'):
        raise ValueError('both isolated subscription reviewers are required')
    authority=None
    if accepted_profile is not None:
        from .profile_authority import read
        authority=read(accepted_profile,reviewers)
    from .profile_authority import METHOD_PROFILE, verify_native
    source_authority=cfg.get('live_profile_authority')
    if (cfg.get('judge_input_profile') or (source_authority or {}).get('profile') == METHOD_PROFILE) and authority != source_authority:
        raise ValueError('settlement source method authority differs')
    if (authority or {}).get('profile') == METHOD_PROFILE:
        mode=native['mode'] if mode is None else mode
        if cfg.get('live_profile_authority') != authority or native['mode'] != mode:
            raise ValueError('settlement source method authority or mode differs')
        verify_native(cfg,mode=mode)
    elif mode is None:
        mode='live'
    if mode not in {'live','fixture'}:
        raise ValueError('settlement mode differs')
    trial_profiles=TRIAL_PROFILES if experimental else None
    if experimental and accepted_profile is not None:raise ValueError('experimental review cannot claim accepted authority')
    if experimental and any(parent.get('active_review_profile',{}).get(k)!=TRIAL_PROFILES[k]for k in ('checker_profile','date_profile','text_profile')):
        raise ValueError('experimental review requires the adopted parent profiles')
    if quota_thirds and (experimental or verified_parent is None or authority is None):
        raise ValueError('quota repair requires an accepted verified parent and no experimental route')
    exclusions = []
    roster = (selected_quota_thirds(parent, cases, native, exclusions=exclusions) if quota_thirds
              else selected(parent, cases, reviewers,authority=authority,trial_profiles=trial_profiles))
    if quota_thirds and not roster:
        raise ValueError('quota repair requires a nonempty proven missing-vote roster')
    if experimental:roster=representative_roster(roster,parent)
    task_count = len(roster) if quota_thirds else 2*len(roster)
    if maximum_tasks is not None and (type(maximum_tasks)is not int or maximum_tasks<0 or task_count>maximum_tasks):raise ValueError('review tasks exceed declared maximum')
    root = private_directory(root)
    refs = []
    for row in roster:
        path = 'inputs/' + row['case_id'] + '.json'
        write_private(root / path, cases[row['case_id']])
        refs.append(dict(case_id=row['case_id'], path=path, sha256=digest((root / path).read_bytes())))
    package = dict(profile=PROFILE, created_at=now(), mode=mode, code=code_bindings(), assets=asset_bindings(),
        build_identity=build_identity, parent_report_path=str(parent_report),
        parent_report_sha256=digest(parent_report.read_bytes()), run_directory=str(root / 'run'),
        reviewers=deepcopy(reviewers), roster=roster, cases=refs, jobs=jobs_for(roster, cases, reviewers,authority=authority),
        scheduling={r + '-subscription': deepcopy(cfg['execution_policy']['scheduling']['accounts'][r + '-subscription'])
                    for r in ('codex', 'claude')},
        recovery={r + '-subscription': deepcopy(cfg['execution_policy']['scheduling']['recovery'][r + '-subscription'])
                  for r in ('codex', 'claude')}, automatic_recovery=deepcopy(cfg.get('automatic_recovery')),
        max_attempts_per_task=cfg['execution_policy']['max_attempts_per_task'],
        retry_policy=deepcopy(cfg['execution_policy']['retry_policy']),
        incremental_api_calls=0, maximum_new_subscription_tasks=task_count,
        parent_conservative_usd=parent['phase_costs']['conservative_total_usd'],
        scope='Saved current unknown publication cases only; no searches, Pro calls, known-grade rejudging or new questions.')
    if verified_parent is not None:
        # This reference is pinned by the child package/approval, not discovered
        # from an arbitrary neighboring file or a process-global cache.
        read_snapshot(verified_parent, parent_report, authority=authority)
        package['verified_parent'] = deepcopy(verified_parent)
    if quota_thirds:
        package.update(quota_thirds=QUOTA_THIRDS, quota_exclusions=exclusions,
            scope='Only proven no-inference quota-rejected thirds still unknown in the accepted parent; no searches, Pro calls or known-grade rejudging.')
    if authority is not None:
        package.update(accepted_profile=authority,checker_profile=authority['profile_bindings']['checker_profile'])
        from .profile_authority import profiles
        live=profiles(authority)
        if live:package['live_profiles']=live
    if experimental:
        outcomes={o['case_id']:o for o in parent['outcomes']}
        package.update(experimental_review_profile=deepcopy(TRIAL_PROFILES),sample_rule='one-per-provider-primary-strata-v1',
            sample_categories=dict(Counter(primary_category(outcomes[r['case_id']])for r in roster)),
            declared_maximum_tasks=maximum_tasks,
            scope='Approved small representative broader-review test over saved unknown results only. No searches, Pro, new questions or automatic acceptance for future batches.')
    write_private(root / 'package.json', package)
    return package


def verify(package, root, *, historical=False, seen=()):
    assets = asset_bindings()
    if (package['profile'] != PROFILE or (not historical and package['code'] != code_bindings())
            or not all(assets.get(k) == v for k, v in package['assets'].items())
            or (not historical and package['assets'] != assets) or package['incremental_api_calls'] != 0):
        raise ValueError('settlement code/assets/dispatch scope changed')
    path = Path(package['parent_report_path'])
    if digest(path.read_bytes()) != package['parent_report_sha256']:
        raise ValueError('settlement parent changed')
    parent, cases, native = (read_snapshot(package['verified_parent'], path,
        authority=package.get('accepted_profile')) if package.get('verified_parent')
        else load_parent(path, seen=seen))
    if Path(package['run_directory']).resolve() != (Path(root) / 'run').resolve():
        raise ValueError('settlement output destination changed')
    cfg = native['configuration']
    policy = cfg['execution_policy']
    expected = dict(automatic_recovery=cfg.get('automatic_recovery'), max_attempts_per_task=policy['max_attempts_per_task'],
        retry_policy=policy['retry_policy'], scheduling={r+'-subscription':policy['scheduling']['accounts'][r+'-subscription'] for r in ('codex','claude')},
        recovery={r+'-subscription':policy['scheduling']['recovery'][r+'-subscription'] for r in ('codex','claude')},
        parent_conservative_usd=parent['phase_costs']['conservative_total_usd'])
    if any(package.get(k) != v for k,v in expected.items()):
        raise ValueError('settlement execution policy or carried cost changed')
    if package['reviewers'] != native['configuration']['reviewers']:
        raise ValueError('settlement reviewer identity changed')
    authority=package.get('accepted_profile')
    from .profile_authority import METHOD_PROFILE
    source_authority=cfg.get('live_profile_authority')
    if (cfg.get('judge_input_profile') or (source_authority or {}).get('profile') == METHOD_PROFILE) and authority != source_authority:
        raise ValueError('settlement source method authority differs')
    trial=package.get('experimental_review_profile')
    if trial is not None and (trial!=TRIAL_PROFILES or authority is not None or package.get('sample_rule')!='one-per-provider-primary-strata-v1'):
        raise ValueError('experimental review binding differs')
    if authority is not None:
        from .profile_authority import read,profiles,METHOD_PROFILE,verify_native
        if (read(authority['package_path'],package['reviewers'])!=authority
                or package.get('checker_profile')!=authority['profile_bindings']['checker_profile']):
            raise ValueError('settlement accepted profile changed')
        if package.get('live_profiles',{})!=profiles(authority):raise ValueError('settlement live profiles changed')
        if authority.get('profile') == METHOD_PROFILE:
            if cfg.get('live_profile_authority') != authority or native['mode'] != package['mode']:
                raise ValueError('settlement source method authority or mode differs')
            verify_native(cfg,mode=package['mode'])
    elif 'checker_profile'in package:
        raise ValueError('checker profile lacks accepted authority')
    quota = package.get('quota_thirds')
    if quota is not None and (quota != QUOTA_THIRDS or trial is not None or not package.get('verified_parent') or authority is None):
        raise ValueError('quota repair route differs')
    exclusions = []
    roster = (selected_quota_thirds(parent, cases, native, exclusions=exclusions) if quota else selected(parent, cases, package['reviewers'], decoder_code=package['code'] if historical else None,authority=authority,trial_profiles=trial))
    if quota and (not roster or package.get('quota_exclusions') != exclusions):
        raise ValueError('quota repair missing-vote or exclusion accounting differs')
    if trial is not None:
        roster=representative_roster(roster,parent);outcomes={o['case_id']:o for o in parent['outcomes']}
        if (package['sample_categories']!=dict(Counter(primary_category(outcomes[r['case_id']])for r in roster))
            or package['declared_maximum_tasks']is not None and len(package['jobs'])>package['declared_maximum_tasks']):raise ValueError('experimental review sample changed')
    if roster != package['roster'] or jobs_for(roster, cases, package['reviewers'],authority=authority) != package['jobs']:
        raise ValueError('settlement eligibility or blind jobs changed')
    if package['maximum_new_subscription_tasks'] != len(package['jobs']):
        raise ValueError('settlement task bound differs')
    if len(package['cases']) != len(roster) or {r['case_id'] for r in package['cases']} != {r['case_id'] for r in roster}:
        raise ValueError('settlement input roster differs')
    for ref in package['cases']:
        if _read_case(root, ref) != cases[ref['case_id']]:
            raise ValueError('settlement input differs from parent')
    return parent, cases, native


def _completed_responses(package):
    directory = Path(package['run_directory'])
    audit = inspect_ledger(directory, allow_empty=not package['jobs'])
    closure = load(directory / 'closure.json')
    if (closure.get('native_ledger_sha256') != audit['source_ledger_sha256']
            or closure.get('native_audit_records_sha256') != digest(audit['records'])):
        raise ValueError('settlement native evidence changed after closure')
    jobs = {j['task_id']: j for j in package['jobs']}
    if audit['unclassified'] or {r['task_id'] for r in audit['records']} != jobs.keys():
        raise ValueError('settlement completions lack native audit')
    latest = {r['task_id']: r for r in audit['records']}
    ends = {e['task_id']: e for e in records(directory / 'attempts.jsonl') if e['event'] == 'finished'} if jobs else {}
    responses = {}
    for task, row in latest.items():
        if row['classification'] not in {'normal', 'recovered','excluded_intervention'} or row['request_sha256'] != digest(jobs[task]):
            raise ValueError('settlement terminal proof differs')
        result_path = directory / ends[task]['result_path']
        response = load(result_path)
        from .subscription_health import completion_view
        response=completion_view(jobs[task],response,result_path.parent)
        if not response.get('native_intervention_excluded')and not terminal_verified(jobs[task], response, result_path.parent):
            raise ValueError('settlement native completion proof binding changed')
        responses[task] = response
    return responses


def _fixture_responses(package):
    """Read fixture completions with exact package/request/result ledger bindings.

    These records are synthetic execution proof, never native completion proof.
    Callers first authenticate the fixture method through verify().
    """
    if not fixture_method(package):
        raise ValueError('fixture lineage requires exact fixture method authority')
    directory=Path(package['run_directory']).resolve()
    ledger=directory/'attempts.jsonl'
    if ledger.is_symlink() or (directory/'binding.json').is_symlink() or load(directory/'binding.json') != dict(package_sha256=digest(package),mode='fixture'):
        raise ValueError('settlement fixture ledger binding differs')
    before=digest(ledger.read_bytes() if ledger.exists() else b'')
    events=list(records(ledger)) if ledger.exists() else []
    jobs={job['task_id']:job for job in package['jobs']}
    starts={row['attempt_id']:row for row in events if row['event']=='started'}
    finished={row['attempt_id']:row for row in events if row['event']=='finished'}
    latest={row['task_id']:row for row in events if row['event']=='finished'}
    if starts.keys()!=finished.keys() or latest.keys()!=jobs.keys():
        raise ValueError('settlement fixture completion scope differs')
    responses={}
    for task,event in latest.items():
        start=starts[event['attempt_id']]
        folder=directory/'attempts'/event['attempt_id']
        result=directory/event['result_path']
        if (result.is_symlink() or (folder/'request.json').is_symlink()
                or not folder.resolve().is_relative_to(directory/'attempts')
                or result.resolve() != folder.resolve()/'result.json'
                or start['task_id']!=task or start['request_sha256']!=digest(jobs[task])
                or event['request_sha256']!=digest(jobs[task]) or load(folder/'request.json')!=jobs[task]
                or digest(result.read_bytes())!=event['result_sha256']):
            raise ValueError('settlement fixture request/result differs')
        raw=load(result)
        if (raw.get('transport_success') is not True or raw.get('native_completion_verified') is not True
                or raw.get('complete') is False or raw.get('dispatch_state') in {'not_sent','uncertain'}):
            raise ValueError('settlement fixture task incomplete')
        responses[task]=raw
    if before!=digest(ledger.read_bytes() if ledger.exists() else b''):
        raise ValueError('settlement fixture ledger changed during replay')
    return responses,before


def overlay(parent, cases, native, package, responses, *, checker_profile=publication.PROFILE,date_profile=None,text_profile=None):
    report = deepcopy(parent)
    records_out = deepcopy(parent.get('publication_review', {}).get('records', []))
    by_id = {o['case_id']: o for o in report['outcomes']}
    for row in package['roster']:
        cid = row['case_id']
        old = by_id[cid]
        if old['grade'] is not None:
            raise ValueError('overlay cannot replace a known parent grade')
        own = {r: responses['publication-' + r + '-' + cid] for r in ('codex', 'claude')}
        packet = publication.packet_for(cases[cid]['compiled'])
        result = publication.corroborate(cases[cid]['compiled'], packet, own, checker_profile=checker_profile,
            **(dict(date_profile=date_profile,text_profile=text_profile)if date_profile is not None or text_profile is not None else {}))
        provenance = {r: dict(prior_role_involvement=r in old['positions'],
            input_sha256=packet['input_sha256'], request_sha256=digest(next(j for j in package['jobs']
                if j['case_id'] == cid and j['role'] == r)), response_sha256=digest(own[r]),
            native_completion_verified=own[r].get('native_completion_verified') is True,
            prior_verdicts_in_input=False, prior_labels_in_input=False) for r in own}
        record = dict(case_id=cid, configuration_id=old['configuration_id'], terminal_key=row['terminal_key'],
            route=row['route'], accepted=result['accepted'], grade=result['grade'], terminal=result['terminal'],
            audits=result['audits'], provenance=provenance,
            reasons={r: (own[r]['verdict'].get('reason', 'No valid review disposition')
                         if isinstance(own[r].get('verdict'), dict) else 'No valid review disposition') for r in own})
        if checker_profile != publication.PROFILE:
            record['checker_profile'] = checker_profile
        if date_profile is not None:record.update(date_profile=date_profile,text_profile=text_profile)
        records_out.append(record)
        if result['accepted']:
            roles = []
            for role, audit in result['audits'].items():
                name = 'publication-review-' + role
                roles.append(name)
                old['positions'][name] = dict(state='valid', grade=result['grade'], judgment=audit['judgment'],
                    policy_conformance='supported_scoped_policy', review_provenance=provenance[role])
                old['states'][name] = 'conforming_numeric'
            old.update(grade=result['grade'], status='determined', selected_roles=roles,
                       grade_producer='attributable_publication_review', original_grade=None,
                       reason='Two validated whole-result reviews under the existing publication policies')
    report.update(mode=PROFILE, package_sha256=digest(package), generated_at=now())
    report['source_parent_report_limits'] = deepcopy(parent.get('report_limits', []))
    report['report_limits'] = [line for line in parent.get('report_limits', [])
                              if line != 'The original20 are retained without repeated calls. The checkpoint stops at30.']
    report['report_limits'].append('Original searches and ordinary judgments are preserved. Only selected parent-unknown results receive the declared publication reviews; no new questions are collected.')
    report['publication_review'] = dict(profile=publication.PROFILE, parent_report_sha256=package['parent_report_sha256'],
        records=records_out, new_search_calls=0, new_pro_calls=0, incremental_api_usd='0',
        selected_cases=len(package['roster']), subscription_tasks=len(package['jobs']))
    report['review_pipeline']['records'] += [r for r in records_out[len(parent.get('publication_review', {}).get('records', [])):]]
    report['review_pipeline']['final_unknowns'] = sum(o['grade'] is None for o in report['outcomes'])
    report['outcome_counts'] = dict(Counter('unknown' if o['grade'] is None else str(o['grade']) for o in report['outcomes']))
    _rescore(report, cases, native)
    return report


def accepted_overlay(parent,cases,native,package,responses):
    if package.get('quota_thirds'):
        return quota_third_overlay(parent, cases, native, package, responses)
    trial=package.get('experimental_review_profile')
    live=package.get('live_profiles')
    effective=trial or live
    report=overlay(parent,cases,native,package,responses,
        checker_profile=effective['checker_profile']if effective else package.get('checker_profile',publication.PROFILE),
        **({k:effective[k]for k in ('date_profile','text_profile')}if effective else {}))
    if trial:
        report['publication_review']['profile']=trial['checker_profile']
        report['experimental_review']=dict(profiles=trial,sample_rule=package['sample_rule'],
            sample_categories=package['sample_categories'],future_batch_authority=False)
        report['report_limits'].append('This report includes a representative broader-review pilot, one unknown case per eligible provider. It is not a recovery forecast or future-batch profile acceptance.')
    if package.get('accepted_profile'):
        report['publication_review']['profile']=package['checker_profile']
        report['active_review_profile']=package['accepted_profile']
    return report


def quota_third_overlay(parent, cases, native, package, responses):
    from .runner import decode_native
    from .review import settle
    report = deepcopy(parent)
    by_id = {o['case_id']: o for o in report['outcomes']}
    completed = []
    for job in package['jobs']:
        cid = job['case_id']; old = by_id[cid]; raw = responses[job['task_id']]
        if old['grade'] is not None:
            raise ValueError('quota repair cannot change a determined grade')
        if not (raw.get('native_completion_verified') or raw.get('native_intervention_excluded')):
            continue
        # The ordinary decoder retains native validation and the frozen reader.
        folder = None
        ledger = Path(package['run_directory'])/'attempts.jsonl'
        if ledger.exists():
            event = next(e for e in reversed(list(records(ledger))) if e['event'] == 'finished' and e['task_id'] == job['task_id'])
            folder = Path(package['run_directory'])/Path(event['result_path']).parent
        third = decode_native('claude', raw, cases[cid]['compiled'], package.get('live_profiles'), request=job, folder=folder)
        third.update(independent=True, prior_verdict_exposure=False)
        old.update(settle(old['positions']['pro'], old['positions']['codex'], third))
        from .reporting import compact_outcome
        compact_outcome(old)
        completed.append(dict(case_id=cid, request_sha256=digest(job), response_sha256=digest(raw)))
    report.update(mode=PROFILE, package_sha256=digest(package), generated_at=now(), active_review_profile=package['accepted_profile'])
    report['quota_third_review'] = dict(profile=QUOTA_THIRDS, parent_report_sha256=package['parent_report_sha256'],
        selected_cases=len(package['roster']), records=completed, new_search_calls=0, new_pro_calls=0, incremental_api_usd='0')
    report.setdefault('report_limits', []).append('Previously missing Claude votes are obtained after proven account-quota rejections. Original attempts are preserved; known grades are unchanged.')
    report['review_pipeline']['final_unknowns'] = sum(o['grade'] is None for o in report['outcomes'])
    report['outcome_counts'] = dict(Counter('unknown' if o['grade'] is None else str(o['grade']) for o in report['outcomes']))
    _rescore(report, cases, native)
    return report


def _rescore(report, cases, native):
    outcomes = report['outcomes']
    cells = {}
    for row in report['collection']:
        key = row['configuration_id'], row['question_id']
        cells[key] = dict(status=row['status'], ranked=[dict(rank=o['rank'], evidence_id=o['case_id'],
            url=cases[o['case_id']]['compiled']['packet']['returned_url']) for o in sorted(outcomes, key=lambda o: o['rank'])
            if (o['configuration_id'], o['question_id']) == key])
    labels = {o['case_id']: o['grade'] for o in outcomes}
    reasons = {o['case_id']: o['reason'] for o in outcomes}
    findings = {}
    for o in outcomes:
        pos = o['positions'].get(o['selected_roles'][0], {}) if o['selected_roles'] else {}
        j = pos.get('conformance_judgment') or pos.get('judgment') or {}
        findings[o['case_id']] = [dict(report_id=None, event_id=None, facet_ids=c['supported_facet_ids'])
            for c in j.get('candidates', []) if c['answer_supported']] if o['grade'] == 2 else []
    cfg = native['configuration']
    configs = [p['configuration_id'] for p in native['providers']]
    scores = scoring.score(report['questions'], configs, cells, labels, reasons, cfg['coverage_target'], cfg['tie_order'], findings=findings)
    pairs = []
    for k in (3, 5, 10):
        for metric in ('answer', 'useful'):
            values = {(r['question_id'], r['configuration_id']): r['metrics'][metric]
                      for r in scores['question_rows'] if r['k'] == k and r['metrics'] is not None}
            for a, b in combinations(configs, 2):
                pairs.append(dict(metric=f'{metric}@{k}', **scoring.paired(report['questions'], a, b,
                    scores['availability']['answered_question_ids'], values)))
    report.update(scores=scores, resolved_pairwise=pairs)


def run_settlement(path, approval, *, call=None):
    path = Path(path)
    package = load(path)
    from .resilience import enabled
    from .codex_runtime import RuntimeStopped
    ledger_path = Path(package['run_directory']) / 'attempts.jsonl'
    def count():
        return sum(e['event'] == 'started' for e in records(ledger_path)) if ledger_path.exists() else 0
    while True:
        before = count()
        try:
            result = _run(path, package, approval, call=call)
        except RuntimeStopped:
            if not enabled(package) or count() <= before:
                raise
            continue
        if (not enabled(package) or result['execution']['state'] == 'completed'
                or result['execution']['permanent_stop_roles'] or count() <= before):
            return result


def _run(path, package, approval, *, call=None):
    parent, cases, native = verify(package, path.parent)
    live = package['mode'] == 'live'
    if live and (approval.get('approved') is not True or approval.get('package_sha256') != digest(package)):
        raise ValueError('exact settlement approval required')
    if live and call is not None:
        raise ValueError('live settlement cannot replace its frozen transport')
    with review_execution(package, call=call) as (responses, stopped, pending, started, directory):
        report = accepted_overlay(parent,cases,native,package,responses)
        missing = [j['task_id'] for j in package['jobs'] if not responses[j['task_id']].get('native_completion_verified')
                   and not responses[j['task_id']].get('native_intervention_excluded')]
        report['execution'] = dict(state='completed_with_holds' if missing else 'completed', held_tasks=missing,
            permanent_stop_roles=sorted(stopped), reused_tasks=len(package['jobs']) - len(pending),
            elapsed_seconds=time.monotonic() - started)
        audit = None
        fixture_evidence={}
        if live and not missing:
            audit = inspect_ledger(directory, allow_empty=not package['jobs'])
            if audit['unclassified']:
                raise ValueError('settlement has unclassified native completions')
            write_private(directory / 'completion-audit.json', audit, replace=True)
        elif fixture_method(package) and not missing:
            retained,ledger_sha=_fixture_responses(package)
            fixture_evidence=dict(fixture_ledger_sha256=ledger_sha,
                fixture_response_hashes={task:digest(raw) for task,raw in retained.items()})
        write_reports(directory, report)
        write_private(directory / 'closure.json', dict(package_sha256=digest(package),
            parent_report_sha256=package['parent_report_sha256'], report_sha256=digest((directory / 'report.json').read_bytes()),
            execution=report['execution'], selected_cases=len(package['roster']), new_search_calls=0, new_pro_calls=0,
            incremental_api_usd='0', original_known_outcomes_unchanged=True,
            native_ledger_sha256=audit['source_ledger_sha256'] if audit else None,
            native_audit_records_sha256=digest(audit['records']) if audit else None,**fixture_evidence), replace=True)
        return report
