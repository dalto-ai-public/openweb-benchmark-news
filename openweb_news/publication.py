"""Source-bound whole-result publication review, separate from original votes."""
from copy import deepcopy

from .common import asset_path, digest, load
from . import adjudication, judging
from .schema import validate
from ._method import text_dates, two_step

PROFILE = 'whole-result-publication-review-v1'
CHECKER_PROFILE = 'scoped-native-publication-role-v2'
READING_PROFILE = 'scoped-publication-witness-v3'
SELECTION_PROFILE = 'invalid-own-source-review-v1'
NATIVE_POLICY = 'publication-estimate-policy-v2.json'
TEXT_POLICY = 'publication-context-policy-v1.json'
CHECKS = ('item_applicability', 'date_meaning', 'own_conflicts', 'credibility_and_cutoff')


def eligibility(compiled, positions, provider, *, selection_profile=None):
    judging.verify_compiled(compiled)
    if compiled != judging.compile_packet(compiled['packet'], compiled['input']['captured_at'], config=provider):
        raise ValueError('publication compiler/mapping differs')
    if selection_profile not in {None, SELECTION_PROFILE}:raise ValueError('unknown review selection profile')
    if selection_profile == SELECTION_PROFILE:
        from .review import classify, numeric
        from collections import Counter
        own={r:positions.get(r,{})for r in ('pro','codex')}
        if 'claude'in positions:own['claude']=positions['claude']
        counts=Counter(numeric(p)for p in own.values()if numeric(p)is not None)
        if any(n>=2 for n in counts.values()):return None
        invalid=sorted(r for r,p in own.items()if classify(p)in {'invalid','unjudged'})
        if invalid:return dict(roles=[],pattern='invalid-or-unjudged',invalid_roles=invalid,source_only=True)
    app = compiled['input'].get('publication_estimate_applicability')
    if not app or app.get('policy') != NATIVE_POLICY:
        return None
    roles = []
    for role, pos in positions.items():
        if pos.get('state') != 'valid' or pos.get('proposal_grade') == pos.get('structural_base_grade'):
            continue
        gaps = [r for c in (pos.get('judgment') or {}).get('candidates', [])
                for r in c['conditions'] if r['status'] not in {'pass', 'not_required'}]
        if gaps and all(r['predicate_id'].startswith('publication:') for r in gaps):
            roles.append(role)
    if not roles:
        return None
    return dict(roles=sorted(roles), pattern='both-primary' if {'pro', 'codex'} <= set(roles)
                else 'mixed' if len(roles) > 1 else 'single',
                invalid_roles=sorted(r for r, p in positions.items() if p.get('state') == 'invalid'))


def packet_for(compiled, *, _source=False):
    """Date choices depend only on source mappings, never on earlier proposals."""
    data = deepcopy(compiled['input'])
    app = data['publication_estimate_applicability']
    if app:app.pop('configuration_sha256', None)
    native = data['native_date_ids']
    ids = [r['date_id'] for r in native if r['relative_path'] in judging.applicability_fields(app)]
    data['eligible_native_date_ids'] = ids
    data['native_mapping_proof'] = (adjudication.mapping_proof(compiled, ids) if ids else
        dict(field_path=app.get('field_path') if app else None, observations=[], portable_authority=app['source_bindings'] if app else [],
             limit='No eligible native observation; own text and every other rule remain available.'))
    data['existing_policies'] = load(asset_path('policies.json'))['policies']
    if not compiled.get('judge_input_profile'):
        adjudication.reject_exposure(data)
    obj = lambda props: dict(type='object', properties=props, required=list(props), additionalProperties=False)
    string = lambda values=None: dict(type='string', **({'enum': values} if values else {}))
    array = lambda item: dict(type='array', items=item)
    fields = [r['field'] for r in compiled['packet']['allowed_text']]
    quote = obj(dict(field=string(fields), quote=string(), occurrence=dict(type='integer', minimum=1)))
    specs = compiled['packet']['required_conditions']
    check = obj(dict(check=string(list(CHECKS)), status=string(['pass', 'fail', 'unknown']), reason=string(),
                     support=dict(type='array', items=quote)))
    application = obj(dict(candidate_ids=array(string()), predicate_id=string([r['predicate_id'] for r in specs
                           if r['predicate_id'].startswith('publication:') or r['predicate_id'] == 'as_of_knowledge']),
                           policy=string([NATIVE_POLICY, TEXT_POLICY]), status=string(['pass', 'fail']), checks=array(check)))
    schema = deepcopy(compiled['schema'])
    extra = dict(contrary_reading=string(), complete_occurrence_reviewed=dict(type='boolean'),
                 policy_applications=array(application))
    schema['properties'].update(extra)
    schema['required'] += list(extra)
    prompt = asset_path('publication-review-v1.md').read_text()
    packet = dict(profile=PROFILE, input=data, schema=schema, prompt=prompt,
                packet_sha256=compiled['packet_sha256'], input_sha256=digest(data),
                schema_sha256=digest(schema), prompt_sha256=digest(prompt.encode()))
    if _source:
        return packet
    result = judging.model_packet(compiled, packet)
    adjudication.reject_exposure(result['input'])
    return result


def _span(quote, texts):
    span = text_dates.exact_span(texts[quote['field']], quote['quote'], quote['occurrence'])
    result=dict(field=quote['field'], **{k: span[k] for k in ('quote', 'start', 'end')})
    from .representation import SPACE_PROFILE
    if span['mode']==SPACE_PROFILE+'_to_raw_span':result.update(mode=span['mode'],model_quote=span['model_quote'])
    return result


def validate_review(value, compiled, packet, *, checker_profile=PROFILE, date_profile=None):
    """Source decoder plus a typed, evidence-bound reviewed-policy capability.

    Ordinary native decoding never receives this capability. Observations and
    original results stay unchanged; defaults preserve the source base rules.
    """
    if checker_profile not in {PROFILE, CHECKER_PROFILE, READING_PROFILE}:
        raise ValueError('unknown publication checker profile')
    try:
        judging.verify_compiled(compiled)
        if packet != packet_for(compiled):
            raise ValueError('review packet differs')
        raw_value = deepcopy(value)
        value = judging.restore_model_verdict(value, compiled, packet_for(compiled, _source=True))
        errors = validate(value, packet_for(compiled, _source=True)['schema'])
        if errors:
            return dict(valid=False, errors=errors)
        if not value['complete_occurrence_reviewed'] or not value['reason'].strip() or not value['contrary_reading'].strip():
            raise ValueError('complete review and substantive reasoning required')
        from .date_reading import native_reading_packet, native_reading_rows
        admitted = judging.native_publication_fields(compiled)
        source = native_reading_packet(compiled['packet'], admitted, profile=date_profile)
        texts = {r['field']: r['text'] for r in source['allowed_text']}
        specs = {r['predicate_id']: r for r in source['required_conditions']}
        ids = [c['candidate_id'] for c in value['candidates']]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate review candidate')
        links = {}
        for link in value['dates']:
            key = link['candidate_id'], link['predicate_id']
            if key in links or key[0] not in ids or key[1] not in specs or not two_step.date_predicate(specs[key[1]]):
                raise ValueError('duplicate or unused date link')
            links[key] = link
        expected_links = {(cid, ident) for cid in ids for ident, spec in specs.items() if two_step.date_predicate(spec)}
        if set(links) != expected_links:
            raise ValueError('every required candidate/date predicate needs one source link')
        native = {r['date_id']: r for r in native_reading_rows(
            compiled['input']['native_date_ids'], admitted, profile=date_profile)}
        applications, approved_fields, proofs = {}, {}, []
        for app in value['policy_applications']:
            if (not app['candidate_ids'] or len(set(app['candidate_ids'])) != len(app['candidate_ids'])
                    or not set(app['candidate_ids']) <= set(ids)):
                raise ValueError('policy candidate scope differs')
            if sorted(c['check'] for c in app['checks']) != sorted(CHECKS):
                raise ValueError('all scoped policy checks required exactly once')
            support = []
            for check in app['checks']:
                if not check['reason'].strip() or check['status'] != 'pass':
                    raise ValueError('every applicability/credibility check must pass')
                spans = [_span(q, texts) for q in check['support']]
                if check['check'] in {'item_applicability', 'date_meaning'} and not spans:
                    raise ValueError('policy requires affirmative own item/date-context support')
                support.extend(spans)
            observations = []
            for cid in app['candidate_ids']:
                key = cid, app['predicate_id']
                if key in applications or key not in links:
                    raise ValueError('duplicate or unsupported policy scope')
                link = links[key]
                if app['policy'] == NATIVE_POLICY:
                    if not compiled['input'].get('publication_estimate_applicability'):
                        raise ValueError('native policy needs an eligible native mapping')
                    if not link['date_ids'] or len(set(link['date_ids'])) != len(link['date_ids']):
                        raise ValueError('native publication needs distinct eligible observations')
                    rows = [native[ident] for ident in link['date_ids']]
                    fields = judging.applicability_fields(compiled['input']['publication_estimate_applicability'])
                    if any(r['relative_path'] not in fields or not r.get('interval') for r in rows):
                        raise ValueError('ineligible or missing native publication field')
                    approved_fields[key] = {r['field'] for r in rows}
                    observations += deepcopy(rows)
                    if app['predicate_id'] == 'as_of_knowledge' and app['status'] != 'pass':
                        raise ValueError('publication alone does not prove facts unavailable earlier')
                elif (not app['predicate_id'].startswith('publication:')
                        or any(i.startswith('N') for i in link['date_ids'])
                        or not (link['date_ids'] or link['additional_dates'])):
                    raise ValueError('publication context requires own linked text dates')
                applications[key] = app
            proofs.append(dict(application=deepcopy(app), observations=observations, support=support,
                               policy_assets_sha256=digest(load(asset_path('policies.json')))))
        for key, fields in approved_fields.items():
            if key[1] == 'as_of_knowledge':
                witnesses = [f for (cid, pred), f in approved_fields.items() if cid == key[0] and pred.startswith('publication:')]
                if not any(fields <= witness for witness in witnesses):
                    raise ValueError('historical availability requires the same reviewed publication witness')
        ordinary = {k: deepcopy(v) for k, v in value.items()
                    if k not in {'contrary_reading', 'complete_occurrence_reviewed', 'policy_applications'}}
        role_bridges = []
        if checker_profile in {CHECKER_PROFILE, READING_PROFILE}:
            for link in ordinary['dates']:
                key = link['candidate_id'], link['predicate_id']
                app = applications.get(key)
                if (link['role'] == 'publication_unspecified' and link['link_status'] == 'established'
                        and key[1].startswith('publication:') and app and app['policy'] == NATIVE_POLICY
                        and key in approved_fields
                        and all(native[i]['date_kind'] == link['role'] for i in link['date_ids'])):
                    role_bridges.append(dict(candidate_id=key[0], predicate_id=key[1],
                        date_ids=deepcopy(link['date_ids']), original_role=link['role'],
                        effective_role='first_publication', policy=NATIVE_POLICY))
                    # Derived decoder view only; the original assertion and native
                    # observations remain available in the immutable response.
                    link['role'] = 'first_publication'
        reader = None
        if date_profile is not None or checker_profile == READING_PROFILE:
            from .date_reading import Reader, PROFILE as DATE_PROFILE, PROFILES as DATE_PROFILES, COMPARISON_PROFILES as COMPARISON_DATE_PROFILES
            if date_profile not in {None, *DATE_PROFILES}:raise ValueError('unknown date-reading profile')
            reader=Reader(enabled=date_profile is not None,captured_at=compiled['input'].get('captured_at'),profile=date_profile or DATE_PROFILE)
        decoded = two_step.decode(source, ordinary, calendar_days=True,
            text_dates=compiled['input']['text_dates'], replay_fn=reader.replay if reader else judging.replay_mentions,
            reviewed_publications=approved_fields, date_reader=reader,
            native_publication_fields=judging.native_publication_fields(compiled))
        fills=[]
        if checker_profile == READING_PROFILE:
            from .publication_reading import fill
            decoded,fills=fill(decoded,value,source,applications,proofs,reader.comparisons,approved_fields,
                               native_publication_fields=judging.native_publication_fields(compiled))
        if decoded['state'] != 'valid' or decoded['computed_grade'] != value['overall_grade']:
            raise ValueError('review disagrees with source date/grade validation')
        judgment = decoded['judgment']
        conditions = {(c['candidate_id'], r['predicate_id']): r
                      for c in judgment['candidates'] for r in c['conditions']}
        for key, r in conditions.items():
            if key[1].startswith('publication:') and r['status'] in {'pass', 'fail'} and key not in applications:
                raise ValueError('publication pass/fail lacks scoped policy proof')
        for key, app in applications.items():
            if conditions[key]['status'] != app['status']:
                raise ValueError('scoped policy disagrees with source linked-date comparison')
        base = two_step.decode(source, ordinary, calendar_days=True,
            text_dates=compiled['input']['text_dates'], replay_fn=judging.replay_mentions,
            native_publication_fields=judging.native_publication_fields(compiled))
        result = dict(valid=True, errors=[], grade=decoded['computed_grade'], judgment=judgment,
            source_structural_base_grade=base.get('computed_grade'), policy_applications=proofs,
            date_reconciliations=decoded['quote_reconciliations'], date_refinements=decoded['date_refinements'],
            limit='Own-span and structural checks do not prove semantic completeness or accuracy.')
        if compiled.get('judge_input_profile'):
            result.update(raw_verdict=raw_value, decoder_verdict=deepcopy(value),
                          judge_input_profile=compiled['judge_input_profile'], model_input_sha256=packet['input_sha256'])
        if checker_profile in {CHECKER_PROFILE, READING_PROFILE}:
            result.update(checker_profile=checker_profile, role_bridges=role_bridges)
        if date_profile is not None:
            result.update(date_reading_profile=date_profile,date_readings=reader.readings)
            if date_profile in COMPARISON_DATE_PROFILES:
                result['date_comparisons']=[dict(candidate_id=cid,predicate_id=predicate,rows=rows)for (cid,predicate),rows in reader.comparisons.items()]
        if checker_profile == READING_PROFILE:
            result.update(reviewed_date_fills=fills,date_reading_profile=date_profile)
        return result
    except (ValueError, KeyError, StopIteration, TypeError, IndexError, OverflowError, AttributeError):
        return dict(valid=False, errors=['review scope, evidence, date or policy proof is invalid'])


def corroborate(compiled, packet, responses, *, checker_profile=PROFILE, date_profile=None, text_profile=None):
    audits = {r: validate_review(v.get('verdict'), compiled, packet, checker_profile=checker_profile, date_profile=date_profile)
              for r, v in responses.items()}
    if text_profile is not None:
        from .representation import selected
        with selected(text_profile):
            for role, raw in responses.items():
                if not audits[role]['valid']:
                    audits[role] = validate_review(raw.get('verdict'), compiled, packet,
                        checker_profile=checker_profile, date_profile=date_profile)
    valid = (set(responses) == {'codex', 'claude'} and all(a['valid'] for a in audits.values())
             and all(v.get('native_completion_verified') is True and v.get('transport_success') is True for v in responses.values()))
    agrees = valid and audits['codex']['grade'] == audits['claude']['grade']
    return dict(accepted=bool(agrees and audits['codex']['grade'] is not None),
                grade=audits['codex']['grade'] if agrees else None, audits=audits,
                judgment=audits['codex']['judgment'] if agrees else None,
                terminal=all(v.get('native_completion_verified') is True and v.get('transport_success') is True
                             for v in responses.values()) and set(responses) == {'codex', 'claude'})
