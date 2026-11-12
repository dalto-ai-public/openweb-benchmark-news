
from collections import Counter, defaultdict
from math import log2
from urllib.parse import urlsplit
from . import availability as availability_selection
from .evidence import as_of_meaning
from ..common import asset_path, load
_METHOD = load(asset_path('method.json'))
CUTOFFS = tuple(_METHOD['cutoffs'])
POOL_DEPTH = _METHOD['pool_depth']
ANSWERED = {'success', 'success_empty'}
METRICS = ('ndcg', 'answer', 'lead', 'useful', 'reports', 'events', 'facets')
GRADE_SLOT = {2: 'answer', 1: 'lead', 0: 'neither_or_disqualified'}
NATIVE_DATE_STATES = {'supported', 'absent', 'conflicting_or_unclear'}

def determined(value, **extra):
    return dict(status='determined', value=value, **extra)

def pending(reason, **extra):
    return dict(status='pending', value=None, reason=reason, **extra)

def not_applicable(reason):
    return dict(status='not_applicable', value=None, reason=reason)

def mean(values):
    values = list(values)
    return sum(values) / len(values)

def dcg(slots):
    """Doc 3 point 57: sum(gain_i / log2(i + 1)) over the original ranks; missing slots add zero."""
    return sum((s['gain'] / log2(s['rank'] + 1) for s in slots))

def url_identity(position, aliases):
    """Doc 3 point 56: exact returned URL unless a reviewed alias map joins it; no redirect or guess."""
    if position['url'] is None:
        return 'evidence:' + position['evidence_id']
    return aliases.get(position['url'], position['url'])

def ranked_slots(cell, labels, aliases):
    """Doc 3 points 20/56: keep original order; only the first occurrence of a URL can earn gain."""
    seen, slots = (set(), [])
    for p in cell['ranked'][:POOL_DEPTH]:
        ident = url_identity(p, aliases)
        slot = dict(rank=p['rank'], evidence_id=p['evidence_id'], url_id=ident)
        if ident in seen:
            slots.append(dict(slot, kind='duplicate', gain=0))
        else:
            seen.add(ident)
            grade = labels[p['evidence_id']]
            slots.append(dict(slot, kind='unresolved' if grade is None else 'graded', gain=grade))
    return slots

def shared_ideal(cells, labels, aliases):
    """Doc 3 points 56-58: best single judged variant per unique URL across the common pool.

    A URL is undetermined when an unresolved variant could still raise it to 2. IDCG@k is fixed
    when no URL is undetermined or at least k resolved grade-2 URLs exist."""
    known, unknown = (defaultdict(list), set())
    for cell in cells:
        for p in cell['ranked'][:POOL_DEPTH]:
            ident, grade = (url_identity(p, aliases), labels[p['evidence_id']])
            if grade is None:
                unknown.add(ident)
            else:
                known[ident].append(grade)
    resolved = {u: max(g) for u, g in known.items() if max(g) == 2 or u not in unknown}
    undetermined = sorted((set(known) | unknown) - set(resolved))
    gains = sorted(resolved.values(), reverse=True)
    idcg = {}
    for k in CUTOFFS:
        fixed = not undetermined or gains.count(2) >= k
        idcg[k] = dcg([dict(rank=i, gain=g) for i, g in enumerate(gains[:k], 1)]) if fixed else None
    return dict(url_count=len(set(known) | unknown), resolved_grade2_urls=gains.count(2), resolved_grade1_urls=gains.count(1), undetermined_urls=undetermined, idcg=idcg)

def question_metrics(slots, k, idcg, findings, facets):
    """Doc 3 points 53, 57-58 and 61-63 for one answered cell at cutoff k."""
    first = [s for s in slots[:k] if s['kind'] != 'duplicate']
    open_ranks = [s['rank'] for s in first if s['gain'] is None]
    if idcg is None:
        ndcg = pending('shared ideal undetermined')
    elif idcg == 0:
        ndcg = not_applicable('zero shared ideal: no positive pooled grade')
    elif open_ranks:
        ndcg = pending('own unresolved result within k', ranks=open_ranks)
    else:
        ndcg = determined(dcg(slots[:k]) / idcg)

    def rate(match):
        count = sum((s['gain'] is not None and match(s['gain']) for s in first))
        if open_ranks:
            return pending('own unresolved result within k', ranks=open_ranks, known_count=count)
        return determined(count / k, count=count)

    def supported(field):
        if open_ranks:
            return pending('own unresolved result within k', ranks=open_ranks)
        answers = [s['evidence_id'] for s in first if s['gain'] == 2]
        if answers and findings is None:
            return pending('no reviewed identity input')
        missing = [e for e in answers if not findings.get(e)]
        if missing:
            return pending('grade-2 result without reviewed finding', evidence_ids=missing)
        ids, open_ids = (set(), [])
        for e in answers:
            for row in findings[e]:
                value = row.get(field)
                if value is None:
                    open_ids.append(e)
                else:
                    ids.update(value if isinstance(value, list) else [value])
        if open_ids:
            return pending('unreviewed ' + field, evidence_ids=sorted(set(open_ids)))
        return ids

    def count(field):
        ids = supported(field)
        return ids if isinstance(ids, dict) else determined(len(ids))
    if not facets:
        facet = not_applicable('question requests no facets')
    else:
        ids = supported('facet_ids')
        facet = ids if isinstance(ids, dict) else determined(len(ids & facets) / len(facets), supported=sorted(ids & facets))
    return dict(ndcg=ndcg, answer=rate(lambda g: g == 2), lead=rate(lambda g: g == 1), useful=rate(lambda g: g > 0), reports=count('report_id'), events=count('event_id'), facets=facet)

def failure_metrics(idcg, facets):
    """Doc 3 points 69-72: a failed or uncollected call delivers zero; unknown ideals stay pending."""
    ndcg = pending('shared ideal undetermined') if idcg is None else not_applicable('zero shared ideal: no positive pooled grade') if idcg == 0 else determined(0.0)
    zero = determined(0)
    return dict(ndcg=ndcg, answer=zero, lead=zero, useful=zero, reports=zero, events=zero, facets=determined(0.0) if facets else not_applicable('question requests no facets'))

def availability(config_ids, questions, statuses, target, tie_order):
    """Doc 3 points 65-67: availability alone selects strategy A or B before quality is read."""
    if sorted(tie_order) != sorted(config_ids):
        raise ValueError('tie order must contain each configuration once')
    qids = [q['question_id'] for q in questions]
    answered = {c: {q for q in qids if statuses.get((c, q)) in ANSWERED} for c in config_ids}
    comparison, removed, common, initial_common = availability_selection.select(config_ids, qids, answered, target, tie_order)
    strategy = 'A' if len(initial_common) / len(qids) >= target else 'B'
    families = defaultdict(set)
    for q in questions:
        families[q['family_id']].add(q['question_id'])
    return dict(coverage_target=target, unit='released questions in scope', strategy=strategy, target_met=len(common) / len(qids) >= target, all_configuration_common_question_count=len(initial_common), comparison_configurations=comparison, removed_configurations=removed, common_question_ids=sorted(common), common_question_count=len(common), released_question_count=len(qids), complete_common_family_count=sum((m <= common for m in families.values())), released_family_count=len(families), answered_question_ids={c: sorted(answered[c]) for c in config_ids}, table={c: {q: statuses.get((c, q), 'not_collected') for q in qids} for c in config_ids})

def family_mean(questions, configs, scope, values):
    """Doc 3 point 70: average wordings within a need, needs within a family, then families equally.

    A family enters only when every declared member is in scope and determined for every compared
    configuration; the same families are used for every configuration."""
    families = defaultdict(lambda: defaultdict(list))
    for q in questions:
        families[q['family_id']][q['need_id']].append(q['question_id'])
    entered, excluded = ({}, {})
    for family, needs in sorted(families.items()):
        members = sorted((q for qs in needs.values() for q in qs))
        outside = [q for q in members if q not in scope]
        unresolved = [dict(question_id=q, configuration_id=c, status=values[q, c]['status'], reason=values[q, c].get('reason')) for q in members if q in scope for c in configs if values[q, c]['status'] != 'determined']
        if outside or unresolved:
            excluded[family] = dict(outside_scope=outside, unresolved=unresolved)
            continue
        entered[family] = {c: mean((mean((values[q, c]['value'] for q in qs)) for qs in needs.values())) for c in configs}
    resolved_questions = sum((len(qs) for f in entered for qs in families[f].values()))
    complete = len(entered) == len(families)
    per_config = {}
    for c in configs:
        value = mean((v[c] for v in entered.values())) if entered else None
        per_config[c] = dict(resolved_family_mean=value, whole_scope=determined(value) if complete else pending('families excluded or pending'))
    return dict(label=f'comparison on resolved families: {len(entered)}/{len(families)} families, {resolved_questions}/{len(questions)} questions', resolved_family_count=len(entered), released_family_count=len(families), resolved_question_count=resolved_questions, released_question_count=len(questions), per_configuration=per_config, family_values=entered, excluded_families=excluded)

def paired(questions, a, b, answered, values):
    """Doc 3 points 68/71: a named pair on shared answered questions; exact wins/ties/losses."""
    shared = set(answered[a]) & set(answered[b])
    result = family_mean(questions, [a, b], shared, values)
    diffs = [v[a] - v[b] for v in result['family_values'].values()]
    return dict(configurations=[a, b], label=result['label'], wins=sum((d > 0 for d in diffs)), ties=sum((d == 0 for d in diffs)), losses=sum((d < 0 for d in diffs)), mean_paired_difference=mean(diffs) if diffs else None, family_differences={f: v[a] - v[b] for f, v in result['family_values'].items()}, excluded_families=result['excluded_families'])

def source_rule_contrast(questions, config_ids, values):
    """Doc 3 point 71: with-minus-without independence on matched source-rule pairs, per configuration."""
    pairs = defaultdict(dict)
    for q in questions:
        if q.get('source_pair_id'):
            pairs[q['source_pair_id']][bool(q['contract']['source_rule']['independence_required'])] = q['question_id']
    out = {}
    for pair, members in sorted(pairs.items()):
        if set(members) != {True, False}:
            out[pair] = not_applicable('pair lacks one with- and one without-independence member in scope')
            continue
        rows = {}
        for (name, k), table in sorted(values.items()):
            for c in config_ids:
                left, right = (table.get((members[True], c)), table.get((members[False], c)))
                if left is None or right is None:
                    rows.setdefault(c, {})[f'{name}@{k}'] = not_applicable('call not answered for both members')
                elif left['status'] == right['status'] == 'determined':
                    rows.setdefault(c, {})[f'{name}@{k}'] = determined(left['value'] - right['value'])
                elif 'not_applicable' in {left['status'], right['status']}:
                    rows.setdefault(c, {})[f'{name}@{k}'] = not_applicable('member metric not applicable')
                else:
                    rows.setdefault(c, {})[f'{name}@{k}'] = pending('member metric not determined', statuses=[left['status'], right['status']])
        out[pair] = dict(with_independence=members[True], without_independence=members[False], per_configuration=rows)
    return out

def alias_candidates(cells, question_ids):
    """Doc 3 point 56: record possible unconfirmed aliases; they stay separate identities."""
    groups = defaultdict(set)
    for (c, q), cell in cells.items():
        if q in question_ids:
            for p in cell['ranked'][:POOL_DEPTH]:
                if p['url']:
                    parts = urlsplit(p['url'])
                    key = parts.netloc.lower().removeprefix('www.') + parts.path.rstrip('/') + ('?' + parts.query if parts.query else '')
                    groups[q, key].add(p['url'])
    return [dict(question_id=q, urls=sorted(urls)) for (q, _), urls in sorted(groups.items()) if len(urls) > 1]

def native_date_required(question):
    """Reuse the canonical as-of interpretation; event/state-only questions need no native date."""
    contract = question['contract']
    publication = any((r['basis'] == 'publication' for r in contract.get('temporal_requirements', [])))
    as_of = as_of_meaning(question) if contract.get('as_of_requirement') is not None else None
    return publication or (as_of is not None and as_of['meaning'] in {'historical_knowledge', 'both'})

def native_date_accounting(questions, config_ids, cells, ledger):
    """Count the bound own-occurrence support ledger, independently of gains or padding.

    A supported native date is not proof of answer/event/cutoff eligibility. Estimation and
    context limits remain in each source row's details. Failed judge outputs still have native
    retrieval evidence; failed/empty retrieval calls have no invented absent-date positions.
    """
    applicability = {q['question_id']: bool(native_date_required(q)) for q in questions}
    expected = {p['evidence_id']: q for (c, q), cell in cells.items() if q in applicability and applicability[q] for p in cell['ranked']}
    dates = {}
    if ledger is not None:
        for row in ledger:
            eid = row['evidence_id']
            if eid in dates or expected.get(eid) != row['question_id'] or row.get('required') is not True or (row.get('state') not in NATIVE_DATE_STATES) or ('details' not in row):
                raise ValueError('native-date ledger join, applicability or state differs: ' + eid)
            dates[eid] = row
        if set(dates) != set(expected):
            raise ValueError('native-date ledger must cover every applicable returned occurrence')
    counts = {}
    for c in config_ids:
        calls = Counter()
        for q, required in applicability.items():
            if required:
                cell = cells.get((c, q))
                calls['not_collected' if cell is None else 'answered' if cell['status'] in ANSWERED else 'failed'] += 1
                if cell is not None and cell['status'] == 'success_empty':
                    calls['answered_empty'] += 1
        counts[c] = {}
        for k in CUTOFFS:
            own = [p for (cid, q), cell in cells.items() if cid == c and applicability.get(q) for p in cell['ranked'] if p['rank'] <= k]
            states = Counter((dates[p['evidence_id']]['state'] for p in own)) if ledger is not None else None
            if states is not None and sum(states.values()) != len(own):
                raise ValueError('native-date accounting does not reconcile')
            counts[c][k] = dict(applicable_question_count=sum(applicability.values()), applicable_call_count=sum(applicability.values()), call_states={name: calls[name] for name in ['answered', 'failed', 'not_collected']}, answered_empty_call_count=calls['answered_empty'], returned_occurrence_denominator=len(own), state_counts={name: states[name] for name in sorted(NATIVE_DATE_STATES)} if states is not None else None)
    return dict(status='determined' if ledger is not None else 'not_supplied', question_applicability=applicability, scheduled_question_count=len(questions), applicable_question_count=sum(applicability.values()), applicable_call_denominator=sum(applicability.values()) * len(config_ids), applicable_returned_occurrence_count=len(expected), per_configuration=counts, meaning='Native-date support only; no automatic eligibility, answer or historical availability proof.')

def score(questions, config_ids, cells, labels, reasons, target, tie_order, findings=None, aliases=None, pairs=(), native_dates=None):
    """Score in-scope questions. labels maps every scheduled occurrence to 0/1/2 or None (unresolved)."""
    aliases = aliases or {}
    questions = sorted(questions, key=lambda q: q['question_id'])
    qids = [q['question_id'] for q in questions]
    cells = {key: cell for key, cell in cells.items() if key[1] in qids}
    avail = availability(config_ids, questions, {key: cell['status'] for key, cell in cells.items()}, target, tie_order)
    slots = {key: ranked_slots(cell, labels, aliases) for key, cell in cells.items()}
    ideals = {q: shared_ideal([cells[c, q] for c in config_ids if (c, q) in cells], labels, aliases) for q in qids}
    facets = {q['question_id']: {f['id'] for f in q['contract'].get('requested_facets') or []} for q in questions}
    rows, values, delivered = ([], defaultdict(dict), defaultdict(dict))
    for q in qids:
        for c in config_ids:
            cell = cells.get((c, q))
            status = cell['status'] if cell else 'not_collected'
            for k in CUTOFFS:
                if status in ANSWERED:
                    m = question_metrics(slots[c, q], k, ideals[q]['idcg'][k], findings, facets[q])
                    top = slots[c, q][:k]
                    rows.append(dict(question_id=q, configuration_id=c, k=k, call_status=status, gains=[s['gain'] for s in top], slot_kinds=[s['kind'] for s in top] + ['empty_or_short_list'] * (k - len(top)), evidence_ids=[s['evidence_id'] for s in top], shared_idcg=ideals[q]['idcg'][k], metrics=m, dcg=None if any((s['gain'] is None for s in top)) else dcg(top)))
                    for name in METRICS:
                        values[name, k][q, c] = m[name]
                else:
                    m = failure_metrics(ideals[q]['idcg'][k], facets[q])
                    rows.append(dict(question_id=q, configuration_id=c, k=k, call_status=status, metrics=None, delivered_metrics=m))
                for name in METRICS:
                    delivered[name, k][q, c] = m[name]
    accounting = {}
    for c in config_ids:
        accounting[c] = {}
        for k in CUTOFFS:
            n, why, failed = (Counter(), Counter(), Counter())
            for q in qids:
                cell = cells.get((c, q))
                if cell is None:
                    n['not_collected'] += k
                elif cell['status'] not in ANSWERED:
                    n['failed_call'] += k
                    failed[cell['status']] += 1
                else:
                    top = slots[c, q][:k]
                    n['empty_or_short_list'] += k - len(top)
                    for s in top:
                        if s['kind'] == 'duplicate':
                            n['duplicate'] += 1
                        elif s['gain'] is None:
                            n['unresolved'] += 1
                            why[reasons[s['evidence_id']]] += 1
                        else:
                            n[GRADE_SLOT[s['gain']]] += 1
            total = len(qids) * k
            if sum(n.values()) != total:
                raise ValueError('slot accounting does not reconcile')
            accounting[c][k] = dict(slots=total, counts=dict(sorted(n.items())), unresolved_reasons=dict(sorted(why.items())), failed_call_statuses=dict(sorted(failed.items())), observed_answer_rate=n['answer'] / total, observed_lead_rate=n['lead'] / total, unresolved_share=n['unresolved'] / total, failed_share=n['failed_call'] / total, not_collected_share=n['not_collected'] / total, rates_are_final=n['unresolved'] == 0)
    key = lambda name, k: f'{name}@{k}'
    common = set(avail['common_question_ids'])
    aggregates = dict(conditional_quality=dict(configurations=avail['comparison_configurations'], question_ids=sorted(common), metrics={key(n, k): family_mean(questions, avail['comparison_configurations'], common, values[n, k]) for n in METRICS for k in CUTOFFS}), delivered_utility=dict(configurations=list(config_ids), metrics={key(n, k): family_mean(questions, config_ids, set(qids), delivered[n, k]) for n in METRICS for k in CUTOFFS}), partial_coverage={c: dict(label='partial coverage: own answered questions', question_ids=avail['answered_question_ids'][c], metrics={key(n, k): family_mean(questions, [c], set(avail['answered_question_ids'][c]), values[n, k]) for n in METRICS for k in CUTOFFS}) for c in avail['removed_configurations']}, source_rule_contrast=source_rule_contrast(questions, config_ids, values), named_pairs=[dict(metric=key(n, 5), **paired(questions, a, b, avail['answered_question_ids'], values[n, 5])) for a, b in pairs for n in ('ndcg', 'events')])
    dependencies = []
    for (c, q), cell in sorted(cells.items(), key=lambda item: (item[0][1], item[0][0])):
        firsts = {s['evidence_id'] for s in slots[c, q] if s['kind'] != 'duplicate'}
        for p in cell['ranked'][:POOL_DEPTH]:
            if labels[p['evidence_id']] is None:
                ident = url_identity(p, aliases)
                dependencies.append(dict(question_id=q, configuration_id=c, evidence_id=p['evidence_id'], rank=p['rank'], reason=reasons[p['evidence_id']], url_id=ident, own_numerator_cutoffs=[k for k in CUTOFFS if p['rank'] <= k and p['evidence_id'] in firsts], shared_ideal_cutoffs=[k for k in CUTOFFS if ident in ideals[q]['undetermined_urls'] and ideals[q]['idcg'][k] is None]))
    return dict(availability=avail, accounting=accounting, native_date_accounting=native_date_accounting(questions, config_ids, cells, native_dates), ideals=ideals, question_rows=rows, aggregates=aggregates, dependencies=dependencies, alias_candidates=alias_candidates(cells, set(qids)))
