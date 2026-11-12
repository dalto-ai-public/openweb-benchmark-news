"""Pure checkpoint diagnostics; no report mutation, evidence loading or execution.

Inputs are detailed reports with full questions, collection, outcomes and
scores.question_rows (not compact BUNDLE-REPORT summaries). Callers verify source
receipts and the frozen-bank prefix. This helper verifies exact question bytes
and a complete rectangular subset of the archived cells, independent of list order.
"""
from collections import Counter, defaultdict
import json
import math

from ._method.scoring import ANSWERED, CUTOFFS, POOL_DEPTH, family_mean


def _name(value):
    if not isinstance(value, str) or not value:
        raise ValueError('comparison identifiers/statuses must be nonempty strings')
    return value


def _index(report):
    if not isinstance(report, dict) or not all(isinstance(report.get(k), list)
            for k in ('questions', 'collection', 'outcomes')):
        raise ValueError('comparison requires detailed questions, collection and outcomes')
    questions = {}
    for question in report['questions']:
        if not isinstance(question, dict) or 'revision' not in question or not isinstance(question.get('contract'), dict):
            raise ValueError('comparison requires complete question objects')
        qid = _name(question.get('question_id'))
        _name(question.get('family_id'))
        _name(question.get('need_id'))
        if qid in questions:
            raise ValueError('duplicate comparison question')
        questions[qid] = json.dumps(question, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if not questions:
        raise ValueError('comparison requires nonempty question scope')
    cells = {}
    for cell in report['collection']:
        key = _name(cell.get('configuration_id')), _name(cell.get('question_id'))
        if key in cells or key[1] not in questions:
            raise ValueError('duplicate or foreign comparison cell')
        _name(cell.get('status'))
        cells[key] = cell
    configs = {cid for cid, _ in cells}
    if not configs or set(cells) != {(cid, qid) for cid in configs for qid in questions}:
        raise ValueError('comparison cell coverage is incomplete')
    accounting = (report.get('scores') or {}).get('accounting')
    if accounting is not None and set(accounting) != configs:
        raise ValueError('comparison configuration roster differs from score accounting')
    outcomes, case_ids = {}, set()
    for outcome in report['outcomes']:
        cell = _name(outcome.get('configuration_id')), _name(outcome.get('question_id'))
        rank, grade = outcome.get('rank'), outcome.get('grade')
        case_id = _name(outcome.get('case_id'))
        key = (*cell, rank)
        if (cell not in cells or type(rank) is not int or not 1 <= rank <= POOL_DEPTH
                or key in outcomes or case_id in case_ids or 'grade' not in outcome
                or grade is not None and (type(grade) is not int or grade not in (0, 1, 2))):
            raise ValueError('duplicate, foreign or invalid comparison outcome')
        outcomes[key] = outcome
        case_ids.add(case_id)
    by_cell = defaultdict(list)
    for (cid, qid, rank), outcome in sorted(outcomes.items()):
        by_cell[cid, qid].append(outcome)
    for key, cell in cells.items():
        own = by_cell[key]
        if [o['rank'] for o in own] != list(range(1, len(own) + 1)):
            raise ValueError('comparison outcome ranks have gaps')
        if (cell['status'] == 'success' and not own) or (cell['status'] != 'success' and own):
            raise ValueError('comparison outcomes disagree with collection status')
    rows = (report.get('scores') or {}).get('question_rows')
    if not isinstance(rows, list):
        raise ValueError('comparison requires score question rows to verify returned-outcome coverage')
    scores = {}
    for row in rows:
        key = row.get('configuration_id'), row.get('question_id'), row.get('k')
        if (key[:2] not in cells or type(key[2]) is not int or key[2] not in CUTOFFS or key in scores
                or row.get('call_status') != cells[key[:2]]['status']):
            raise ValueError('duplicate, foreign or invalid comparison score row')
        if row['call_status'] in ANSWERED:
            expected = [o['case_id'] for o in by_cell[key[:2]][:key[2]]]
            if row.get('evidence_ids') != expected:
                raise ValueError('comparison score row differs from returned outcomes')
        scores[key] = row
    if set(scores) != {(*cell, k) for cell in cells for k in CUTOFFS}:
        raise ValueError('comparison score row coverage is incomplete')
    return questions, cells, outcomes, scores, configs


def _stats(cells, outcomes, cid):
    statuses = Counter(cell['status'] for key, cell in cells.items() if key[0] == cid)
    own = [o for key, o in outcomes.items() if key[0] == cid]
    unknown = sum(o['grade'] is None for o in own)
    known = [o['grade'] for o in own if o['grade'] is not None]
    scheduled = sum(statuses.values())
    answered = sum(statuses[s] for s in ANSWERED)
    return dict(grade_counts={label: sum(_grade(o['grade']) == label for o in own) for label in ('0','1','2','unknown')},
        mean_known_grade=sum(known)/len(known) if known else None,
        cells=scheduled, statuses=dict(sorted(statuses.items())),
        answered_cells=answered, empty_cells=statuses['success_empty'],
        failure_or_uncollected_statuses={s: n for s, n in sorted(statuses.items()) if s not in ANSWERED},
        coverage=answered / scheduled, returned_outcomes=len(own),
        known_outcomes=len(own) - unknown, unknown_outcomes=unknown,
        unknown_returned_share=unknown / len(own) if own else None,
        unknown_scheduled_slot_share=unknown / (POOL_DEPTH * scheduled))


def _grade_summary(outcomes):
    known = [o['grade'] for o in outcomes if o['grade'] is not None]
    return dict(mean_known_grade=sum(known)/len(known) if known else None,
                known=len(known), unknown=len(outcomes)-len(known), returned=len(outcomes))


def _grade(value):
    return 'unknown' if value is None else str(value)


def _metric(row, name):
    metrics = row.get('metrics') if row['call_status'] in ANSWERED else row.get('delivered_metrics')
    metric = (metrics or {}).get(name) or {}
    status = metric.get('status', 'pending')
    if status not in {'determined', 'pending', 'not_applicable'}:
        raise ValueError('invalid comparison metric status')
    if status != 'determined':
        return dict(status=status, value=None)
    value = metric.get('value')
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('comparison determined score must be finite numeric')
    return dict(status='determined', value=value)


def _ranks(values):
    # Competition ranks: exact ties share 1, 1, 3. Unknowns have no rank.
    known = sorted((v for v in values.values() if v is not None), reverse=True)
    return {cid: known.index(value) + 1 if value is not None else None for cid, value in values.items()}


def _rankings(questions, configs, scope, old_rows, new_rows, *, same_pool):
    roles = [(epoch, cid) for epoch in ('archived', 'current') for cid in configs]
    result = {}
    for k in CUTOFFS:
        for name in ('answer', 'useful', 'ndcg'):
            metric = f'{name}@{k}'
            if name == 'ndcg' and not same_pool:
                result[metric] = dict(status='unavailable', reason='provider pools differ; shared ideals cannot be reconstructed from reports')
                continue
            values = {(q['question_id'], (epoch, cid)): _metric(rows[cid, q['question_id'], k], name)
                      for epoch, rows in (('archived', old_rows), ('current', new_rows))
                      for q in questions if q['question_id'] in scope for cid in configs}
            # One call includes both epochs, so every provider/epoch uses the
            # identical determined families. No separate denominator selection.
            summary = family_mean(questions, roles, scope, values)
            means = {epoch: {cid: summary['per_configuration'][epoch, cid]['resolved_family_mean']
                             for cid in configs} for epoch in ('archived', 'current')}
            ranks = {epoch: _ranks(own) for epoch, own in means.items()}
            complete = summary['resolved_family_count'] == summary['released_family_count']
            table = []
            for cid in configs:
                before, after = means['archived'][cid], means['current'][cid]
                old_rank, new_rank = ranks['archived'][cid], ranks['current'][cid]
                table.append(dict(configuration_id=cid, archived_score=before, current_score=after,
                    score_delta=after - before if before is not None and after is not None else None,
                    archived_rank=old_rank, current_rank=new_rank,
                    rank_delta=new_rank - old_rank if old_rank is not None and new_rank is not None else None,
                    archived_tie_size=sum(v == before for v in means['archived'].values()) if before is not None else None,
                    current_tie_size=sum(v == after for v in means['current'].values()) if after is not None else None,
                    whole_scope_archived_rank=old_rank if complete else None,
                    whole_scope_current_rank=new_rank if complete else None))
            result[metric] = dict(status='determined' if complete else 'pending',
                rank_basis='same determined families across both epochs and all selected providers; delivery metric: failed or uncollected calls count as zero delivery, not provider-quality evidence',
                included_family_ids=sorted(summary['family_values']),
                excluded_family_ids=sorted(summary['excluded_families']),
                included_questions=summary['resolved_question_count'], released_questions=len(questions),
                included_families=summary['resolved_family_count'], released_families=summary['released_family_count'],
                table=table)
    return result


def _rank_review(rankings):
    """Descriptive review flags, never grade tolerances or automatic gates."""
    rows=[]
    for view,metrics in rankings.items():
        for name,metric in metrics.items():
            families=metric.get('included_families',0)
            skipped=families<3
            rows.append(dict(view=view,metric=name,included_families=families,
                skipped=skipped,reason='fewer than three common families'if skipped else None,
                movements=[row for row in metric.get('table',[])
                    if row['rank_delta']is not None and abs(row['rank_delta'])>=3]if not skipped else []))
    return dict(minimum_common_families=3,minimum_absolute_rank_move=3,descriptive_only=True,
                rows=rows,limit='Review prompts only; partial ranks and ties remain as reported. No correctness or continuation decision follows automatically.')


def _provider_review(archived,current,pairs):
    def context(report):
        active=report.get('active_review_profile')or{}
        binding=active.get('profile_bindings',active)
        return binding,active.get('method_options',binding.get('method_options',{}))
    old,old_options=context(archived);new,new_options=context(current)
    rows=[]
    for cid,prior in sorted(pairs.items()):
        before=old.get('provider_configurations',{}).get(prior)
        after=new.get('provider_configurations',{}).get(cid)
        reasons=[]
        if cid!=prior:reasons.append('configuration_replacement')
        if before is not None and after is not None and before!=after:reasons.append('configuration_hash_changed')
        profiles={name:new_options.get(name,{}).get(cid)for name in ('provider_request_profiles','provider_projection_profiles')}
        if cid in new_options.get('provider_date_profiles',{}):
            profiles['provider_date_profiles']=new_options['provider_date_profiles'][cid]
        for name,value in profiles.items():
            if value is not None:reasons.append('selected_'+name)
        fields=(new.get('date_trust',{}).get('selections',{}).get(cid)or{}).get('eligible_fields',[])
        if fields:reasons.append('selected_native_publication_fields')
        rows.append(dict(configuration_id=cid,archived_configuration_id=prior,
            archived_configuration_sha256=before,current_configuration_sha256=after,
            configuration_comparison='changed'if before is not None and after is not None and before!=after
                else 'unchanged'if before is not None and after is not None else 'unavailable',
            review_reasons=reasons,selected_profiles=profiles,selected_native_publication_fields=fields))
    return dict(rows=rows,
        changed_providers=[r['configuration_id']for r in rows if r['configuration_comparison']=='changed'or r['configuration_id']!=r['archived_configuration_id']],
        selected_choice_review_providers=[r['configuration_id']for r in rows if r['review_reasons']],
        unavailable_configuration_comparisons=[r['configuration_id']for r in rows if r['configuration_comparison']=='unavailable'],
        source='Report-bound method configuration hashes, replacements, selected request/projection profiles and date-trust admissions.',
        limit='A selected choice warrants inspection but is not proof it differs from the archive. Missing archived metadata stays unavailable, never assumed unchanged.')


def configuration_pairs(archived_ids, current_ids, replacements=None):
    """Explicit reviewed product changes, diagnostic only; never vote reuse."""
    replacements = {} if replacements is None else replacements
    if not isinstance(replacements, dict) or any(
            pair != ('tavily-general', 'tavily-news') for pair in replacements.items()):
        raise ValueError('unsupported diagnostic configuration pairing')
    if not set(replacements) <= set(current_ids):
        raise ValueError('diagnostic pairing names an unselected current configuration')
    pairs = {cid: replacements.get(cid, cid) for cid in current_ids}
    if len(set(pairs.values())) != len(pairs) or not set(pairs.values()) <= set(archived_ids):
        raise ValueError('diagnostic pairing is not an injective archived subset')
    return pairs


def compare_reports(archived, current, *, replacements=None):
    """Compare provider rankings only on exact overlapping questions/cells.

    Reuse method family weighting with the same determined families across both
    epochs/providers. Scores need no equivalent evidence. Rank-coordinate grade
    transitions are secondary observations, never same-item equivalence claims.
    """
    aq, ac, ao, ar, ap = _index(archived)
    cq, cc, co, cr, cp = _index(current)
    pairs = configuration_pairs(ap, cp, replacements)
    overlap=set(cq)&set(aq)
    if not overlap or any(aq[q] != cq[q] for q in overlap):
        raise ValueError('comparison overlapping questions/configurations differ from archived scope')
    current_only=sorted(set(cq)-set(aq));current_total=len(cq)
    cq={q:v for q,v in cq.items() if q in overlap}
    cc={key:v for key,v in cc.items() if key[1] in overlap}
    co={key:v for key,v in co.items() if key[1] in overlap}
    cr={key:v for key,v in cr.items() if key[1] in overlap}
    # Rekey only local diagnostic indices. Original reports, case IDs, scores,
    # evidence and configuration names remain immutable and are never reused.
    old_to_new = {old: new for new, old in pairs.items()}
    same_pool = ap == cp
    ac = {(old_to_new[cid], qid): row for (cid, qid), row in ac.items() if cid in old_to_new}
    ao = {(old_to_new[cid], qid, rank): row for (cid, qid, rank), row in ao.items() if cid in old_to_new}
    ar = {(old_to_new[cid], qid, k): row for (cid, qid, k), row in ar.items() if cid in old_to_new}
    ac = {key: ac[key] for key in cc}
    ao = {key: value for key, value in ao.items() if key[:2] in cc}
    providers = {}
    for cid in sorted(cp):
        old = {key: o for key, o in ao.items() if key[0] == cid}
        new = {key: o for key, o in co.items() if key[0] == cid}
        positions = old.keys() & new.keys()
        transitions = Counter(_grade(old[key]['grade']) + '->' + _grade(new[key]['grade']) for key in positions)
        providers[cid] = dict(archived=_stats(ac, ao, cid), current=_stats(cc, co, cid),
            matched_rank_positions=len(positions), new_rank_positions=len(new.keys() - old.keys()),
            removed_rank_positions=len(old.keys() - new.keys()),
            same_cell_rank_grade_transitions=dict(sorted(transitions.items())),
            question_grade_changes=[dict(question_id=qid,
                archived=_grade_summary([o for key,o in old.items() if key[1]==qid]),
                current=_grade_summary([o for key,o in new.items() if key[1]==qid]))
                for qid in sorted(cq)])
        if pairs[cid] != cid:
            providers[cid].update(archived_configuration_id=pairs[cid], current_configuration_id=cid)
    questions = current['questions']
    configs = sorted(cp)
    matched = {qid for qid in cq if all(ac[cid, qid]['status'] in ANSWERED
               and cc[cid, qid]['status'] in ANSWERED for cid in cp)}
    rankings=dict(delivered_utility=_rankings(questions,configs,set(cq),ar,cr,same_pool=same_pool),
        matched_answered=_rankings(questions,configs,matched,ar,cr,same_pool=same_pool))
    return dict(profile='archived-checkpoint-comparison-v1', diagnostic_only=True,
        **({'configuration_replacements': dict(replacements)} if replacements else {}),
        scope=dict(question_ids=sorted(cq), configuration_ids=configs, cells=len(cc),
                   current_total_questions=current_total,current_only_question_ids=current_only,
                   archived_total_questions=len(aq), archived_total_cells=len(archived['collection']),
                   matched_answered_question_ids=sorted(matched)),
        rankings=rankings,rank_review=_rank_review(rankings),
        provider_review=_provider_review(archived,current,pairs),
        providers=providers,
        limits=['Current-only questions have no archived counterpart and are not compared; comparisons use only the overlap and complete families.',
                'Source authenticity and frozen-bank prefix require caller verification; full question objects match exactly.',
                *(['Explicit configuration replacements compare different products of the same provider; score movement is diagnostic, not a controlled estimate of the mode effect.'] if replacements else []),
                'Rankings use existing wording/need/family weighting on the same determined families in both epochs and all selected providers.',
                'Partial ranks describe included families only; whole-scope ranks remain unavailable when any family is excluded. Exact ties share competition ranks.',
                'Rank delta is current minus archived; positive means a lower rank. No dramatic-change threshold or correctness inference is applied.',
                'Delivered utility includes failed calls as defined in report score rows; matched-answered excludes questions unanswered by any selected provider in either epoch.',
                'Same-cell/rank grade transitions can involve different items. No URL, packet hash or projection equivalence is required or inferred.',
                'Coverage counts success and success_empty; non-answered statuses include failures, holds and uncollected work.',
                'Known-grade means exclude unknowns and can change with coverage; per-question means are descriptive, not accuracy tests or automatic degradation gates.',
                'Later captures, changed methods and model judgments do not establish causal improvement or human accuracy.'])
