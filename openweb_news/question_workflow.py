"""Thin simple-question entry point over the existing bank and clean-run owners."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from . import batch, clean_run, preparation, profile_authority, question_bank, review_pipeline, review_capacity
from ._method import question_records
from .common import (asset_bindings, asset_path, code_bindings, digest, encoded, load,
                     now, records, verify_manifest, write_private)
from .ledger import Ledger, UncertainAttempt
from .schema import validate

PROFILE = 'simple-question-to-report-v2'
HISTORY = {'reuse_snapshot', 'fresh_judging', 'prior_work', 'interrupted_run_recovery',
           'benchmark_continuation_reuse', 'subscription_health_transition',
           'scale_expansion', 'reuse_scope', 'cache_expiry_recovery'}


def _save(path, value):
    path = Path(path)
    raw = value if isinstance(value, bytes) else encoded(value)
    if path.is_symlink() or path.exists() and path.read_bytes() != raw:
        raise ValueError('workflow derived bytes changed')
    if not path.exists():
        write_private(path, raw)


def _inputs(raw):
    rows = [dict(question_id=f'question-{i:04d}', original_need=line)
            for i, line in enumerate((s for s in raw.decode('utf-8').splitlines() if s.strip()), 1)]
    if not rows:
        raise ValueError('at least one simple question required')
    return rows


def prepare(questions_path, config, directory, *, mode, origin, stage_counts=None, context=''):
    """Freeze the inputs and exact approved derivation before any agent call."""
    root = Path(directory).expanduser().absolute()
    if root.exists() or root.is_symlink():
        raise ValueError('workflow output must be new; existing reports are preserved')
    if mode not in {'fixture', 'live'} or origin not in question_records.ORIGINS:
        raise ValueError('explicit workflow mode and question origin required')
    raw = Path(questions_path).read_bytes()
    inputs = _inputs(raw)
    counts = list(stage_counts) if stage_counts is not None else [len(inputs)]
    if (not counts or any(type(n) is not int or n <= 0 for n in counts)
            or sum(counts) != len(inputs)):
        raise ValueError('stage counts must cover every simple question exactly once')
    cfg = deepcopy(config)
    if HISTORY.intersection(cfg) or cfg.get('execution_stage') not in (None, 'benchmark'):
        raise ValueError('simple-question workflow is a new benchmark, without imported history')
    cfg.pop('questions', None)
    cfg.pop('questions_path', None)
    capacity = cfg.pop('review_capacity', None)
    review_capacity.validate(capacity, cfg['reviewers'], mode)
    cfg['run_directory'] = str(root / 'unused-native-template')
    clean_run._review_capability(dict(configuration=cfg))
    authority = profile_authority.read(cfg['method_acceptance_package'], cfg['reviewers'])
    if (authority.get('profile') != profile_authority.METHOD_PROFILE or authority.get('mode') != mode
            or authority['method_options'] != profile_authority.method_options(cfg)
            or authority['profile_bindings']['provider_ids'] != cfg['provider_ids']):
        raise ValueError('workflow requires exact independent method acceptance in its mode')
    ceiling = Decimal(str(cfg['execution_policy']['spending_ceiling_usd']))
    if not ceiling.is_finite() or ceiling <= 0:
        raise ValueError('positive finite spending ceiling required')
    if mode == 'live' and 'fixture_accounting' in cfg:
        raise ValueError('fixture accounting cannot authorize live workflow')
    p = dict(profile=PROFILE, mode=mode, created_at=now(), output_directory=str(root),
        code=code_bindings(), assets=asset_bindings(), source_questions=raw.decode('utf-8'),
        source_sha256=digest(raw), inputs=inputs, origin=origin, context=context,
        configuration=cfg, accepted_method=authority, stage_counts=counts, review_capacity=capacity,
        shared_ceiling_usd=str(ceiling),
        derivation='One Codex contract author and one independent Claude admission; existing bank freeze and clean stages; capacity-checked complete checkpoint parts under the explicit call bound, with verified unchanged-case reuse. Only complete continuing independent coverage may derive a continuing gate. No input omissions, truncation, sampling, historical imports or runtime/method changes.',
        maximum_additional_subscription_tasks=2 + len(counts) * capacity['max_checkpoint_calls'],
        subscription_accounting='Author/admission/checkpoint attempts retain native usage in agents/ ledgers; subscription quota/time is separate from the shared native API ceiling. Existing clean stages own native/review accounting.',
        approval_scope='Exact inputs/context, source-bound authoring/admission contracts, settings/method, ordered stages, derived clean package, subscription task bound, shared ceiling and private destinations. Only admitted unchanged membership may dispatch; any unresolved admission or negative checkpoint stops.')
    write_private(root / 'workflow.json', p)
    write_private(root / 'approval-template.json', dict(decision='NOT_APPROVED', package_sha256=digest(p),
        approved_by='', approved_at='', accept_derived_execution=False))
    write_private(root / 'APPROVAL-REVIEW.md', (
        '# Simple questions to report\n\n' + p['approval_scope'] + '\n\n' + p['derivation'] +
        f"\n\nMode: {mode}. Questions: {len(inputs)}. Stages: {counts}. "
        f"Shared API ceiling: USD {ceiling}. Additional subscription task bound: {p['maximum_additional_subscription_tasks']}. "
        f"Checkpoint calls per stage: at most {capacity['max_checkpoint_calls']}.\n\n" +
        review_capacity.rules()['planning_rule'] + '\n\n' +
        p['subscription_accounting'] + '\n\nNo calls or execution approval follow from preparation. '
        'The owner approves the exact derivation and spending before model-authored '
        'contracts exist, rather than approving specific final contracts. '
        'The owner must explicitly accept this exact derived-execution scope. '
        'Actual checkpoint part counts are known only after collection/judging; '
        'an insufficient bound stops after that spend, before checkpoint review sends. '
        'INSTALL reviewer-capacity documentation gives a measured fixture example, not a live estimate. '
        'Fixture outputs are synthetic software evidence, not benchmark results or live verification.\n').encode())
    return p


def verify(path):
    path = Path(path).absolute()
    p = load(path)
    if (p.get('profile') != PROFILE or p['output_directory'] != str(path.parent)
            or p.get('mode') not in {'live', 'fixture'} or p.get('origin') not in question_records.ORIGINS
            or p['code'] != code_bindings() or p['assets'] != asset_bindings()
            or p['inputs'] != _inputs(p['source_questions'].encode('utf-8'))
            or p['source_sha256'] != digest(p['source_questions'].encode('utf-8'))
            or p['accepted_method'] != profile_authority.read(
                p['configuration']['method_acceptance_package'], p['configuration']['reviewers'])):
        raise ValueError('workflow source, code, assets or method authority changed')
    authority, cfg = p['accepted_method'], p['configuration']
    review_capacity.validate(p.get('review_capacity'), cfg['reviewers'], p['mode'])
    if (authority.get('mode') != p['mode'] or authority.get('profile') != profile_authority.METHOD_PROFILE
            or authority['method_options'] != profile_authority.method_options(cfg)
            or authority['profile_bindings']['provider_ids'] != cfg['provider_ids']
            or not p['stage_counts'] or any(type(n) is not int or n <= 0 for n in p['stage_counts'])
            or sum(p['stage_counts']) != len(p['inputs'])
            or p['maximum_additional_subscription_tasks'] != 2 + len(p['stage_counts']) * p['review_capacity']['max_checkpoint_calls']
            or Decimal(p['shared_ceiling_usd']) != Decimal(str(cfg['execution_policy']['spending_ceiling_usd']))):
        raise ValueError('workflow mode, method choices or scope differ')
    return p


def _packet(kind, data):
    contract = load(asset_path('question-workflow-v1.json'))
    prompts = dict(author='question-authoring-v1.md', admission='question-admission-v1.md',
                   checkpoint=review_capacity.rules()['part_prompt'])
    return dict(profile=PROFILE, input=data, schema=contract[kind],
        prompt=asset_path(prompts[kind]).read_text() + '\n\n' +
               asset_path('questions-and-references.md').read_text())


class Blocked(Exception):
    pass


def _agent(p, root, task, role, packet, fixture_call, *, require_retained=False):
    """Reuse isolated native dispatch, audited completion and durable no-resend."""
    reviewers = p['configuration']['reviewers']
    directory = root / 'agents' / task
    capacity = review_capacity.measure(packet, p['review_capacity']['reviewers'][role])
    _save(directory / 'capacity.json', capacity)
    if not capacity['fits']:
        _save(directory / 'blocked-packet.json', packet)
        raise Blocked(f'{task}: conservative reviewer capacity budget exceeded; no review sent')
    if require_retained and (not (directory / 'attempts.jsonl').exists() or not any(
            e['event'] == 'finished' and e['task_id'] == task for e in records(directory / 'attempts.jsonl'))):
        raise ValueError('recorded automated review has no completed agent attempt evidence')
    job = dict(task_id=task, role=role, stage=task, backend_config=reviewers[role], blind_packet=packet,
               capacity_sha256=digest(capacity))
    ep = dict(mode=p['mode'], reviewers=reviewers, jobs=[job], run_directory=str(directory),
        scheduling={role+'-subscription':p['configuration']['execution_policy']['scheduling']['accounts'][role+'-subscription']})
    with review_pipeline.review_execution(ep, call=fixture_call) as (responses, _, _, _, _):
        response = responses[task]
    if (not response.get('transport_success') or response.get('native_completion_verified') is not True
            or response.get('fatal_configuration') or validate(response.get('verdict'), packet['schema'])):
        raise Blocked(f'{task}: invalid or incomplete agent response; retained for inspection')
    event = next(e for e in reversed(list(records(directory / 'attempts.jsonl')))
                 if e['event'] == 'finished' and e['task_id'] == task)
    return response['verdict'], dict(task=task, role=role, reviewed_at=event['recorded_at'],
        request_sha256=event['request_sha256'], result_path=str(directory / event['result_path']),
        result_sha256=event['result_sha256'])


def _drafts(p, answer):
    originals = {r['question_id']: r['original_need'] for r in p['inputs']}
    rows = deepcopy(answer['questions'])
    ids = [q['question_id'] for q in rows] + [q['question_id'] for q in answer['unresolved']]
    if len(ids) != len(set(ids)) or set(ids) != set(originals):
        raise Blocked('author changed or omitted question membership')
    if answer['unresolved']:
        raise Blocked('question intent unresolved; inspect the retained author dispositions')
    for q in rows:
        if q['original_need'] != originals[q['question_id']] or q['revision'] != 1:
            raise Blocked('author changed original wording or initial revision')
        q.update(input_surface='agent_query', origin=p['origin'],
            provenance=dict(source_id=p['source_sha256'], candidate_id=q['question_id'],
                original_text=q['original_need'], original_sha256=digest(q['original_need'].encode()),
                source_record_id=q['question_id'], adaptation='Packaged contract author; independent admission required.'),
            review=dict(state='draft', reviewer='', reviewed_at=None,
                query_contract_consistency='pending', issues=[]))
        if q.get('source_pair_id') is None:
            q.pop('source_pair_id', None)
        # Omit inapplicable nullable as-of keys, preserving their declared meanings.
        if q['contract']['as_of_requirement'] is not None:
            q['contract']['as_of_requirement'] = {k:v for k,v in q['contract']['as_of_requirement'].items() if v is not None}
    rows.sort(key=lambda q: list(originals).index(q['question_id']))
    errors = question_records.validate_questions(rows)
    try:
        preparation.validate_questions(rows)
        for q in rows:
            question_bank._validate_contract(q)
    except (KeyError, TypeError, ValueError) as error:
        errors.append(str(error))
    if errors:
        raise Blocked('invalid authored contracts: ' + '; '.join(errors))
    return rows


def _admit(rows, verdict, proof):
    decisions = verdict['decisions']
    ids = [r['question_id'] for r in decisions]
    if len(ids) != len(set(ids)) or set(ids) != {q['question_id'] for q in rows}:
        raise Blocked('independent admission membership differs')
    if any(d['decision'] != 'admit' or not d['reason'].strip() or not d['valid_answer_description'].strip()
           for d in decisions):
        raise Blocked('independent semantic admission did not pass every question')
    by_id = {d['question_id']: d for d in decisions}
    admitted = deepcopy(rows)
    for q in admitted:
        d = by_id[q['question_id']]
        q['review'] = dict(state='reviewed', reviewer='claude-subscription', reviewed_at=proof['reviewed_at'],
            query_contract_consistency='pass', issues=[], admission_reason=d['reason'],
            valid_answer_description=d['valid_answer_description'], caveats=d['caveats'],
            admission_proof=proof)
    errors = question_records.validate_questions(admitted)
    question_records.validate_pairs(admitted, errors, complete=True)
    if errors:
        raise Blocked('admitted bank fails source record checks: ' + '; '.join(errors))
    return admitted


def _share_documents(value, documents):
    """Factor repeated prompt/schema values without dropping any source bytes."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key in {'prompt', 'schema'}:
                ident = digest(child)
                documents[ident] = child
                result[key] = dict(shared_document_sha256=ident)
            else:
                result[key] = _share_documents(child, documents)
        return result
    if isinstance(value, list):
        return [_share_documents(child, documents) for child in value]
    return value


def _checkpoint_packet(clean_path, result):
    """Supply real case bytes, not only summaries or hash-only review claims."""
    p, template = clean_run.verify(clean_path)
    clean_run.verify_stage_gates(clean_path, p)
    evidence = result['evidence']
    report = clean_run._read_bound(evidence['report'])
    roots = [clean_path.parent / f'stage-{s["index"]}' for s in p['stages'][:result['stage_index']]]
    refs = clean_run._case_refs([root / 'review/package.json' for root in roots], report['outcomes'])
    verify_manifest(list(refs.values()))
    # Requests are captured before credentials are resolved by the transport.
    requests = []
    for root in roots:
        ledger = root / 'native/run/attempts.jsonl'
        for event in records(ledger):
            if event['event'] == 'started':
                request = load(ledger.parent / 'attempts' / event['attempt_id'] / 'request.json')
                if digest(request) != event['request_sha256']:
                    raise ValueError('checkpoint native request changed')
                if request['kind'] == 'search':
                    requests.append(request)
    review_attempts = []
    for root in roots:
        for ledger in (root / 'review/run/attempts.jsonl',
                       clean_run._accepted_root(root) / 'publication/run/attempts.jsonl'):
            if not ledger.exists():
                continue
            events = list(records(ledger))
            finishes = {e['attempt_id']:e for e in events if e['event'] == 'finished'}
            for event in events:
                if event['event'] != 'started':
                    continue
                request = load(ledger.parent / 'attempts' / event['attempt_id'] / 'request.json')
                finished = finishes.get(event['attempt_id'])
                if finished is None or digest(request) != event['request_sha256']:
                    raise ValueError('checkpoint review attempt is incomplete or changed')
                result_path = ledger.parent / finished['result_path']
                if digest(result_path.read_bytes()) != finished['result_sha256']:
                    raise ValueError('checkpoint review response changed')
                review_attempts.append(dict(request=request, response=load(result_path)))
    documents = {}
    cases = _share_documents([load(refs[cid]['path']) for cid in sorted(refs)], documents)
    review_attempts = _share_documents(review_attempts, documents)
    return _packet('checkpoint', dict(evidence=evidence, report=report,
        comparison=clean_run._read_bound(evidence['comparison']),
        native_completion_audit=clean_run._read_bound(evidence['audit']),
        cases=cases, review_attempts=review_attempts, shared_documents=documents, collection_requests=requests,
        method=p['accepted_profile'], declared_stages=[{k:s[k] for k in
            ('index', 'count', 'cumulative', 'questions', 'expected_differences')} for s in p['stages']],
        earlier_reviews=[dict(stage_index=load(root / 'stage-review.json')['stage_index'],
            decision=load(root / 'stage-review.json')['decision'],
            review=clean_run._bound_file(root / 'stage-review.json'),
            report=load(root / 'stage-review.json')['evidence']['report']) for root in roots[:-1]],
        archive_comparison=p.get('archive_comparison'), required_inspection=list(
            load(asset_path('question-workflow-v1.json'))['checkpoint']['properties']['inspection']['required'])))


def run(path, approval, *, env_file=None, fixture_call=None):
    path = Path(path).absolute()
    p = verify(path)
    if (approval.get('decision') != 'approved' or approval.get('package_sha256') != digest(p)
            or approval.get('accept_derived_execution') is not True
            or not approval.get('approved_by') or not approval.get('approved_at')):
        raise ValueError('exact workflow approval accepting its derived-execution scope required')
    if p['mode'] == 'live' and fixture_call is not None:
        raise ValueError('live workflow cannot inject fixture responses')
    if p['mode'] == 'fixture' and fixture_call is None:
        from .fixtures import workflow as fixture_call
    root = path.parent
    with Ledger(root / 'coordinator', dict(package_sha256=digest(p))).locked():
        _save(root / 'approval.json', approval)
        _save(root / 'source-questions.txt', p['source_questions'].encode('utf-8'))
        try:
            return _run(p, root, approval, env_file, fixture_call)
        except (Blocked, UncertainAttempt) as error:
            result = dict(state='blocked', reason=str(error), mode=p['mode'],
                package_sha256=digest(p), evidence=str(root / 'agents'))
            write_private(root / 'status.json', result, replace=True)
            return result


def _checkpoint_review(p, root, clean_path, result, fixture_call):
    index = result['stage_index']
    directory = root / 'agents' / f'checkpoint-{index}'
    source = _checkpoint_packet(clean_path, result)['input']
    _save(directory / 'source.json', source)
    prior, authority, previous = None, None, None
    if index > 1:
        prior_path = clean_path.parent / f'stage-{index-1}' / 'stage-review.json'
        prior = load(prior_path)
        clean_run._validate_review(prior, load(prior_path.parent / 'gate-evidence.json'))
        if prior['decision'] != 'continue' or not prior['coverage']['complete']:
            raise ValueError('unchanged-case reuse requires a complete continuing prior checkpoint')
        authority = dict(review=clean_run._bound_file(prior_path), plan=prior['agent_proof']['plan'],
                         source=clean_run._bound_file(root / 'agents' / f'checkpoint-{index-1}' / 'source.json'))
        old_plan = clean_run._read_bound(authority['plan'])
        previous = dict(case_fingerprints=old_plan['case_fingerprints'], authority=authority,
                        source=clean_run._read_bound(authority['source']))
    context = dict(evidence=result['evidence'], complete_source_sha256=digest(source),
        current_overview=_overview(source['report']),
        prior_overview=_overview(clean_run._read_bound(prior['evidence']['report'])) if prior else None,
        prior_continuation=authority, method=source['method'],
        archive_comparison=source.get('archive_comparison'),
        coverage_rule=review_capacity.rules()['coverage_rule'])
    def packet_for(data):
        packet = _packet('checkpoint', data)
        field = review_capacity.rules()['coverage_field']
        packet['schema'] = deepcopy(packet['schema'])
        packet['schema']['properties'][field] = dict(type='array', items=dict(type='string'))
        packet['schema']['required'].append(field)
        return packet
    try:
        plan = review_capacity.plan(source, context, p['review_capacity']['reviewers']['claude'],
            p['review_capacity']['max_checkpoint_calls'], packet_for, previous=previous)
    except review_capacity.CapacityError as error:
        _save(directory / 'capacity-block.json', dict(reason=str(error), source_sha256=digest(source)))
        raise Blocked(str(error)) from error
    _save(directory / 'plan.json', plan)
    recorded = (clean_path.parent / f'stage-{index}' / 'stage-review.json').exists()
    responses, proofs = [], []
    for i, part in enumerate(plan['parts'], 1):
        task = f'checkpoint-{index}-part-{i:04d}'
        response, proof = _agent(p, root, task, 'claude', part['packet'], fixture_call,
                                 require_retained=recorded)
        try:
            review_capacity.validate_part(response, part['packet'])
            clean_run._validate_review(dict(response, stage_index=index, evidence=result['evidence'],
                reviewed_by='claude-subscription', reviewed_at=proof['reviewed_at']), result['evidence'])
        except (ValueError, review_capacity.CapacityError) as error:
            raise Blocked('checkpoint part has invalid verdict or incomplete evidence coverage') from error
        responses.append(response)
        proofs.append(proof)
        if response['decision'] == 'stop':
            break
    response = review_capacity.combine(plan, responses)
    return dict(response, stage_index=index, evidence=result['evidence'],
        reviewed_by='claude-subscription', reviewed_at=proofs[-1]['reviewed_at'],
        agent_proof=dict(plan=clean_run._bound_file(directory / 'plan.json'), parts=proofs),
        coverage=dict(complete=len(responses) == len(plan['parts']),
            units=len(plan['units']), reused_cases=len(plan['reused']),
            planned_parts=len(plan['parts']), reviewed_parts=len(responses)))


def _overview(report):
    """Existing score means/counts for cross-part context; full details stay in units."""
    panels = {}
    for name, panel in report['scores']['aggregates'].items():
        if isinstance(panel, dict) and 'metrics' in panel:
            panels[name] = {metric:{k:v for k,v in row.items() if k not in {'family_values', 'excluded_families'}}
                            for metric, row in panel['metrics'].items()}
    return dict(question_count=len(report['questions']), outcome_counts=report['outcome_counts'],
        aggregates=panels, phase_costs=report['phase_costs'], report_limits=report.get('report_limits', []))


def _run(p, root, approval, env_file, fixture_call):
    original = dict(inputs=p['inputs'], origin=p['origin'], context=p['context'])
    answer, author_proof = _agent(p, root, 'author', 'codex', _packet('author', original), fixture_call)
    rows = _drafts(p, answer)
    _save(root / 'draft-questions.jsonl', b''.join(encoded(q) for q in rows))
    verdict, admission_proof = _agent(p, root, 'admission', 'claude',
        _packet('admission', dict(original, candidate_bank=rows)), fixture_call)
    admitted = _admit(rows, verdict, admission_proof)
    source = root / 'reviewed-questions.jsonl'
    _save(source, b''.join(encoded(q) for q in admitted))
    frozen = question_bank.freeze(source, root / 'bank', p['stage_counts'], resume=True)
    _save(root / 'admission.json', dict(author=author_proof, admission=admission_proof,
        bank_sha256=digest(source.read_bytes()), selection_sha256=frozen['selection_sha256']))
    cfg = deepcopy(p['configuration'])
    cfg['questions'] = admitted
    template_path = root / 'template.json'
    _save(template_path, preparation.prepare(cfg, mode=p['mode']))
    clean = clean_run.prepare(template_path, root / 'bank/selection.json', root / 'clean',
        ceiling=p['shared_ceiling_usd'], accepted_profile=p['accepted_method']['package_path'],
        stage_counts=p['stage_counts'], mode=p['mode'], created_at=p['created_at'])
    clean_path = root / 'clean/clean-run.json'
    # This derives only the exact branch explicitly covered by the parent receipt.
    receipt = dict(approval, package_sha256=digest(clean), parent_workflow_sha256=digest(p),
                   admission_sha256=digest(load(root / 'admission.json')))
    _save(root / 'clean/derived-approval.json', receipt)
    # Authenticate retained verdicts against their original audited agent attempts
    # before clean_run can use them to advance or return an existing closure.
    for stage in clean['stages']:
        stage_root = clean_path.parent / f'stage-{stage["index"]}'
        if (stage_root / 'stage-review.json').exists():
            old_result = dict(stage_index=stage['index'], evidence=load(stage_root / 'gate-evidence.json'))
            review = _checkpoint_review(p, root, clean_path, old_result, fixture_call)
            _save(stage_root / 'stage-review.json', review)
    while True:
        result = clean_run.run(clean_path, receipt, env_file=env_file, fixture_call=fixture_call)
        if result['state'] != 'awaiting_stage_review':
            break
        review = _checkpoint_review(p, root, clean_path, result, fixture_call)
        clean_run.record_stage_review(clean_path, review)
    if result['state'] == 'completed':
        clean_run.verify_stage_gates(clean_path)
        result = dict(result, report_html=str(Path(result['report']['path']).with_suffix('.html')),
            mode=p['mode'], workflow_package_sha256=digest(p),
            question_admission=str(root / 'admission.json'),
            automation='Packaged authoring, independent admission and checkpoint verdicts; no manual routine handoffs.',
            proof_limits='Fixture mode proves software wiring, not semantic accuracy or live access.' if p['mode'] == 'fixture' else 'Model review is not a factual accuracy guarantee.')
        _save(root / 'closure.json', result)
    write_private(root / 'status.json', result, replace=True)
    return result
