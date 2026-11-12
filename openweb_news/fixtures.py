"""Explicit offline example backends. Never used by live execution."""
from copy import deepcopy
import json

from .common import asset_path, encoded, load, write_private


def config(directory):
    example = load(asset_path("fixtures/example.json"))
    return dict(questions=example["questions"], provider_ids=example["provider_ids"],
                run_directory=str(directory), coverage_target=0.9, tie_order=example["provider_ids"],
                reviewers={role: dict(backend=f"{role}-subscription", model="synthetic-model",
                                     reasoning_effort="high", timeout_seconds=600,
                                     **({"transport": "app-server-stdio-v1"} if role == "codex" else {})) for role in ("codex", "claude")},
                execution_policy=dict(spending_ceiling_usd="1", timeout_seconds=60,
                                      max_attempts_per_task=1,
                                      scheduling=dict(cache_mode="implicit-prefix-and-exact-tasks", accounts={
                                          "provider/OPENWEB_NEWS_BRAVE_API_KEY": dict(concurrency=2, minimum_start_interval_seconds=0),
                                          "provider/OPENWEB_NEWS_EXA_API_KEY": dict(concurrency=2, minimum_start_interval_seconds=0),
                                          "gemini-api/OPENWEB_NEWS_GEMINI_API_KEY": dict(concurrency=4, minimum_start_interval_seconds=0),
                                          "codex-subscription": dict(concurrency=4, minimum_start_interval_seconds=0),
                                          "claude-subscription": dict(concurrency=8, minimum_start_interval_seconds=0)}),
                                      retry_policy=dict(max_total=0, max_search=0, max_model=0, http_statuses=[], backoff_seconds=0),
                                      repair_branch=dict(max_repairs=1, mode="saved_response_redecode", additional_live_tasks=0)))


def search(request, provider, folder):
    example = load(asset_path("fixtures/example.json"))
    fields = provider["result_fields"]
    qid = request["question_id"]
    row = {fields["url"]: example["url"], fields["title"]: example["title"],
           fields["content"]: [dict(text=example["title"])] if fields["content_shape"] == "object_text_array" else
           [example["title"]] if fields["content_shape"] == "string_array" else example["title"]}
    if provider['configuration_id'] == 'valyu-search' and qid == 'question-0001':
        row['publication_date'] = '2026-10-12'
        row[fields['content']] += ' Published October 12, 2026.'
    rows = [row, deepcopy(row)] if qid in {"synthetic-answer", "question-0001"} else [row]
    if qid in {"synthetic-empty", "question-0002"}:
        rows = []
    payload = rows
    for part in reversed(fields["container"].split(".")):
        payload = {part: payload}
    failed = qid in {"synthetic-empty", "question-0002"} and provider["configuration_id"] == "exa-highlights"
    raw = encoded(dict(error="synthetic transport failure") if failed else payload)
    write_private(folder / "response.bin", raw)
    return dict(raw_text=raw.decode(), http_status=503 if failed else 200, complete=True,
                transport_success=not failed, captured_at="2026-10-28T00:00:00Z", elapsed_seconds=0,
                fixture=True)


def model(role, compiled, folder):
    packet = compiled["packet"]
    qid = packet["question"]["question_id"]
    if qid in {"synthetic-primary-failure", "question-0003"} and role == "pro":
        raw = b"synthetic invalid primary response"
        write_private(folder / "response.bin", raw)
        return dict(transport_success=True, raw_text=raw.decode(), fixture=True)
    grade = {"pro": 0, "codex": 2, "claude": 1}[role] if qid in {"synthetic-disagreement", "question-0004"} else 1 if qid in {"synthetic-primary-failure", "question-0003"} else 2
    quote = dict(field=packet["allowed_text"][0]["field"], quote=packet["allowed_text"][0]["text"], occurrence=0)
    records, conditions = [], []
    for spec in packet["required_conditions"]:
        if not spec["required"]:
            continue
        if qid.startswith('question-'):
            from ._method.two_step import date_predicate
            if date_predicate(spec):
                continue
        target = records if spec["scope"] == "record" else conditions
        target.append(dict(predicate_id=spec["predicate_id"], status="pass",
                           reason="Synthetic own editorial report supports this condition.", support=[deepcopy(quote)]))
    if grade == 1:
        conditions[0].update(status="not_demonstrated", support=[], reason="Specific lead with missing answer proof.")
    elif grade == 0:
        conditions[0].update(status="fail", reason="Synthetic own evidence establishes a disqualifier.")
    verdict = dict(schema_version=1, case_id=packet["case_id"], status="judged", overall_grade=grade,
                   reason="Synthetic software fixture position.", dates=[], record_conditions=records,
                   candidates=[dict(candidate_id="candidate-1", claim="The fictional observatory opened.",
                                    answer_supported=grade == 2, clear_lead=grade > 0, support=[quote],
                                    conditions=conditions, supported_facet_ids=[])])
    # The existing publication-gap fixture, exposed through ordinary configured
    # transport selection so an installed simple-input proof can exercise both
    # nonempty review stages without patching pipeline decisions.
    native = next((r['date_id'] for r in compiled['input']['native_date_ids']
                   if r['relative_path'] == 'publication_date'), None)
    if qid == 'question-0001' and native:
        for spec in packet['required_conditions']:
            if spec['predicate_id'].startswith('publication:'):
                verdict['dates'].append(dict(candidate_id='candidate-1', predicate_id=spec['predicate_id'],
                    link_status='not_demonstrated', role='first_publication', date_ids=[native], anchor_date_id='',
                    additional_dates=[], context_support=[], reason='Synthetic native date needs occurrence review.'))
    if compiled.get("model_projection"):
        # This is an offline model stub: emit the actual model-view identifiers.
        # The real decoder must still restore and validate the retained response.
        projection = compiled["model_projection"]
        verdict["case_id"] = projection["packet"]["input"]["case_id"]
        aliases = {field: alias for alias, field in projection["binding"]["field_aliases"].items()
                   if alias not in projection["binding"].get("source_leaf_spans", {})}
        def references(value):
            if isinstance(value, dict):
                if "field" in value and "quote" in value:
                    value["field"] = aliases[value["field"]]
                for child in value.values():
                    references(child)
            elif isinstance(value, list):
                for child in value:
                    references(child)
        references(verdict)
    raw = encoded(verdict)
    write_private(folder / "fixture-verdict.json", raw)
    if role == "pro":
        from .judging import METHOD
        response = dict(modelVersion=METHOD["primary"]["model"], candidates=[dict(finishReason="STOP",
            content=dict(parts=[dict(text=raw.decode())]))], usageMetadata=dict(promptTokenCount=0, candidatesTokenCount=0))
        return dict(transport_success=True, raw_text=encoded(response).decode(), http_status=200, complete=True, fixture=True)
    return dict(transport_success=True, verdict=verdict, served_model="synthetic-model", fixture=True,
                prior_verdict_exposure=False)


def starter(directory, *, simple=False):
    """Export editable constructed inputs. Never write owner/reviewer approvals."""
    from pathlib import Path
    from .blind_input import LIVE_PROFILE
    from .request_profiles import MIXED_START_PROFILE
    from .recovery import sibling_rule
    from .accounting import FIXTURE_PROFILE
    root = Path(directory).expanduser().resolve()
    if root.exists():
        raise ValueError('starter output must be new; preserve existing inputs')
    cfg = config(root/'unused')
    questions = cfg.pop('questions')
    for q in questions:
        q['family_id'] = 'synthetic-observatory-family'
        q['need_id'] = 'synthetic-observatory-need'
        q['provenance'] = dict(kind='constructed-software-fixture', semantic_admission='not_verified', rights='synthetic')
        q['date_anchor'] = '2026-10-28T00:00:00Z'
    questions[0]['contract']['temporal_requirements'] = [dict(id='window', basis='publication',
        start_inclusive='2026-10-01T00:00:00Z', end_exclusive='2026-10-28T00:00:00Z')]
    cfg.update(questions_path=str(root/'questions.jsonl'), provider_ids=['brave-web','exa-highlights'],
        fixture_accounting=FIXTURE_PROFILE,
        tie_order=['brave-web','exa-highlights'], judge_input_profile=LIVE_PROFILE,
        provider_date_profiles={'brave-web': MIXED_START_PROFILE},
        date_trust_package=str(root/'trust/package.json'),
        method_acceptance_package=str(root/'method/package.json'), run_directory=str(root/'native-run'))
    cfg['execution_policy']['spending_ceiling_usd'] = '100'
    scheduling = cfg['execution_policy']['scheduling']
    scheduling['recovery'] = {k: sibling_rule('provider' if k.startswith('provider/') else 'judge', v['concurrency'])
        for k,v in scheduling['accounts'].items()}
    if simple:
        cfg.pop('questions_path')
        from .review_capacity import rules as capacity_rules
        cfg['review_capacity'] = dict(profile=capacity_rules()['profile'], kind='fixture', max_checkpoint_calls=64,
            reviewers={role:dict(model=reviewer['model'], context_window_tokens=500000,
                max_output_tokens=4096, scaffolding_reserve_tokens=8192,
                source='Synthetic capacity declaration for offline transport tests; not a real model/account capability.')
                for role, reviewer in cfg['reviewers'].items()})
        questions[0]['provider_query'] += ' Published October 1–27, 2026.'
        write_private(root/'simple-questions.txt', ('\n'.join(q['provider_query'] for q in questions)+'\n').encode())
    else:
        write_private(root/'questions.jsonl', b''.join(encoded(q) for q in questions))
    write_private(root/'config.json', cfg)
    write_private(root/'source-notice.txt', b'Constructed synthetic software fixtures only; no real news or sourced agent needs. Date diagnostics unavailable before collection. No semantic admission, account access, method/execution approval or public license follows.\n')
    write_private(root/'fixture-review-response.json', dict(fixture=True, native_completion_verified=True,
        transport_success=True, complete=True, verdict={}, notice='Synthetic deliberately invalid review output; remains unresolved, never a passing review.'))
    return dict(output=str(root), questions=len(questions), mode='fixture-inputs-only', approved=False, calls=0)


def workflow(job, folder):
    """Synthetic agent transport only; never selected by a live workflow.

    The toy author supports the exported fictional editorial requests. This is
    schema/control-flow evidence, not a language model or semantic accuracy test.
    """
    from .common import digest
    task = job['stage']
    data = job['blind_packet']['input']
    if task == 'author':
        questions, unresolved = [], []
        for item in data['inputs']:
            ident, text = item['question_id'], item['original_need']
            if any(word in text.lower() for word in ('latest', 'last week', 'ambiguous')):
                unresolved.append(dict(question_id=ident, reason='Synthetic unresolved-intent control.'))
                continue
            period = [dict(id='window', basis='publication', start_inclusive='2026-10-01T00:00:00Z',
                end_exclusive='2026-10-28T00:00:00Z', timezone='UTC', precision='day',
                question_phrase='Published October 1–27, 2026')] if 'Published October 1–27, 2026' in text else []
            questions.append(dict(question_id=ident, revision=1, need_id=ident, family_id=ident, source_pair_id=None,
                original_need=text, provider_query=text, primary_subject='fictional observatory', secondary_tags=[],
                geography=dict(applies_to='no_geographic_restriction', regions=['global']),
                contract=dict(query_language='English',
                    source_rule=dict(independence_required=False, qualifying_material='editorial reporting'),
                    temporal_requirements=period, as_of_requirement=None, allowed_snippet_languages=['*'],
                    background_context_allowed=False, background_context_definition=None,
                    qualifying_predicates=[text], requested_facets=[], requested_report_target=None)))
        verdict = dict(questions=questions, unresolved=unresolved)
    elif task == 'admission':
        verdict = dict(decisions=[dict(question_id=q['question_id'], decision='admit',
            reason='Synthetic semantic reviewer stub; no actual model review.',
            valid_answer_description='Own returned editorial evidence of the requested fictional opening.',
            caveats=['Software fixture, not verified semantic admission for real questions.'])
            for q in data['candidate_bank']])
    elif task.startswith('checkpoint-'):
        verdict = dict(decision='continue', material_method_change=False,
            unexplained_material_differences=[], invariant_failures=[],
            inspection={k:'Synthetic checkpoint reviewer stub inspected the supplied '+k+'.'
                        for k in data['required_inspection']}, reviewed_unit_ids=data['unit_ids'])
    else:
        # Ordinary/fixed review still receives a retained invalid position, so
        # unresolved cases remain unknown rather than fabricating a grade.
        verdict = {}
    raw = dict(fixture=True, transport_success=True, complete=True,
        native_completion_verified=True, verdict=verdict,
        fixture_packet_sha256=digest(job['blind_packet']),
        notice='Synthetic transport stub only; no independent live model verdict or factual accuracy claim.')
    write_private(folder / 'fixture-response.json', raw)
    return raw
