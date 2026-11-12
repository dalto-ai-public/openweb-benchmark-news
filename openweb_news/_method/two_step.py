

from copy import deepcopy
from datetime import datetime, timedelta
import json
from . import compact as compact
from . import evidence as evidence
from . import text_dates as dates

def date_predicate(spec):
    ident = spec['predicate_id']
    return spec['required'] and (ident.startswith('publication:') or (ident.startswith('event:') and ident.endswith(':window')) or ident == 'as_of_knowledge')

def input_for(packet, calendar_days=False):
    result = compact.compact_input(packet)
    result['text_dates'] = dates.mentions(packet)
    result['date_observations'] = deepcopy(packet['date_observations'])
    result['native_metadata'] = deepcopy(packet['native_metadata'])
    result['date_checks'] = [dict(predicate_id=s['predicate_id'], scope=s['scope']) for s in packet['required_conditions'] if date_predicate(s)]
    result['semantic_checks'] = [dict(predicate_id=s['predicate_id'], scope=s['scope']) for s in packet['required_conditions'] if s['required'] and (not date_predicate(s))]
    result['native_date_ids'] = [dict(date_id=f'N{i + 1}', **deepcopy(row)) for i, row in enumerate(packet['date_observations'])]
    if calendar_days:
        result['date_comparison_policy'] = 'news-calendar-dates-v1'
        for row in result['text_dates'] + result['native_date_ids']:
            row['calendar_comparison_interval'] = evidence.comparison_interval(row, True)
    return result

def prompt_for(packet):
    required = {r['predicate_id'] for r in packet['required_conditions'] if r['required']}
    parts = [(compact.ASSETS / 'prompt-v3.md').read_text().strip(), 'Return two structured parts in this one response. dates: for every candidate and required date check, link only dates whose meaning belongs to that candidate and requested condition. Select supplied D/N IDs or add an exact own-text date quote the helper missed. Set link_status established only with contextual evidence; otherwise not_demonstrated or genuinely grade-changing unknown. Code computes interval membership, not you. Semantics: record-scoped conditions once, candidate-scoped conditions per candidate, facts/lead clues and requested facets. Include required checks only. Code fills unrequested conditions, copies record conditions, derives missing predicates and computes the final grade; your overall_grade is retained as a cross-check. Every candidate, including a lead, needs a nonempty evidence-grounded claim. Quotes must be literal, contiguous permitted text. Repeated quotes need an explicit occurrence number (1=first); use0only for a unique quote. Native timestamps never invent content facts. A date at a title/snippet start is a positional hint, not an automatic role.']
    parts.append('For source_rule/independence, URL publisher identity must be paired with own title/snippet evidence about this item. A domain alone cannot prove item type. Evidence of an ordinary editorial report can be its own title/snippet; do not invent missing item evidence.')
    if 'independence' in required:
        parts.append((compact.ASSETS / 'independence-v1.md').read_text().strip())
    if any((s.startswith('publication:') for s in required)):
        parts.append('Publication checks require original/first-publication meaning. An update, generic item date, unclear publication role, crawl or capture time cannot establish first publication. Native roles are fixed by the reviewed mapping: do not upgrade them. Text first-publication roles require explicit own-text context establishing that meaning.')
    if any((s.startswith('event:') and s.endswith(':window') for s in required)):
        parts.append('Event dates must date the requested event/stage, not a future effective date or another event. A title date may anchor a current event reported by this item, including a dated daily bulletin reporting it has approved a decision; explicit different event dates take precedence. Native publication timestamps alone do not date events. Relative dates remain unresolved without an explicit own-evidence anchor.')
    if any((r.get('stage') == 'first_publicly_disclosed' for r in packet['question']['contract']['temporal_requirements'])):
        parts.append("First publicly disclosed requires evidence of the first public disclosure of this particular incident. Disclosed/revealed alone does not prove firstness; a dated report about disclosures does not automatically date their first disclosure. Separate incidents when their stage/date evidence differs; a combined report cannot transfer one incident's first-disclosure evidence to another. Preserve a specific lead when firstness or timing is missing.")
    if 'as_of_knowledge' in required:
        parts.append('Historical knowledge needs evidence that this particular fact was publicly available at the linked date. A generic article timestamp/current snippet alone is insufficient. Use fact_availability only with exact own-text context linking this fact to its public announcement/report; code compares it with the unchanged cutoff.')
    if 'as_of_state' in required:
        parts.append('Historical state is a semantic check: determine what applied at the supplied target; earlier events alone do not establish continued validity. Later retrospective own text may establish the state.')
    return '\n\n'.join(parts) + '\n'

def schema_for(packet, geography_text=None):

    def obj(props):
        return dict(type='object', properties=props, required=list(props), additionalProperties=False)

    def arr(item):
        return dict(type='array', items=item)

    def string(enum=None):
        return dict(type='string', **{'enum': enum} if enum else {})
    fields = [r['field'] for r in packet['allowed_text'] + packet['native_metadata']] + ['url']
    quote = obj(dict(field=string(fields), quote=string(), occurrence=dict(type='integer', minimum=0)))

    def condition_array(scope):
        ids = [s['predicate_id'] for s in packet['required_conditions'] if s['required'] and s['scope'] == scope and (not date_predicate(s))]
        condition_quote = quote
        if scope == 'candidate' and geography_text:
            condition_quote = obj(dict(field=string(fields + list(geography_text)), quote=string(), occurrence=dict(type='integer', minimum=0)))
        row = obj(dict(predicate_id=string(ids), status=string(['pass', 'fail', 'not_demonstrated', 'unknown']), reason=string(), support=arr(condition_quote)))
        return arr(row) if ids else dict(type='array', items=row, maxItems=0)
    facets = [r['id'] for r in packet['question']['contract'].get('requested_facets', [])]
    facet_schema = arr(string(facets)) if facets else dict(type='array', items=string(), maxItems=0)
    candidate = obj(dict(candidate_id=string(), claim=string(), answer_supported=dict(type='boolean'), clear_lead=dict(type='boolean'), support=arr(quote), conditions=condition_array('candidate'), supported_facet_ids=facet_schema))
    link = obj(dict(candidate_id=string(), predicate_id=string([s['predicate_id'] for s in packet['required_conditions'] if date_predicate(s)]), link_status=string(['established', 'not_demonstrated', 'unknown']), role=string(['event', 'first_publication', 'fact_availability', 'update', 'publication_unspecified', 'item_date', 'other', 'unclear']), date_ids=arr(string()), anchor_date_id=string(), additional_dates=arr(quote), context_support=arr(quote), reason=string()))
    return obj(dict(schema_version=dict(type='integer', enum=[1]), case_id=string([packet['case_id']]), status=string(['judged', 'unjudged', 'refusal', 'error']), overall_grade=dict(anyOf=[dict(type='integer', enum=[0, 1, 2]), dict(type='null')]), reason=string(), dates=arr(link) if any((date_predicate(s) for s in packet['required_conditions'])) else dict(type='array', items=link, maxItems=0), record_conditions=condition_array('record'), candidates=arr(candidate)))

def resolve_quotes(packet, quotes, audit=None, extra_text=None):
    texts = {r['field']: r['text'] for r in packet['allowed_text'] + packet['native_metadata']}
    texts['url'] = packet['returned_url'] or ''
    if extra_text:
        texts.update(extra_text)
    resolved = []
    if not isinstance(quotes, list):
        raise ValueError('support must be a list')
    for row in quotes:
        if set(row) != {'field', 'quote', 'occurrence'} or type(row['occurrence']) is not int or row['occurrence'] < 0:
            raise ValueError('support fields/occurrence differ')
        text = texts.get(row['field'])
        quote = row['quote']
        if not isinstance(text, str) or not isinstance(quote, str) or (not quote):
            raise ValueError('quote field/text required')
        span = dates.exact_span(text, quote, row['occurrence'])
        if span['mode'] != 'literal' and row['field'] not in {r['field'] for r in packet['allowed_text']}:
            raise ValueError('metadata/URL cannot use Markdown reconciliation')
        if audit is not None and span['mode'] != 'literal':
            audit.append(dict(field=row['field'], **span))
        resolved.append(dict(field=row['field'], **{k: span[k] for k in ['quote', 'start', 'end']}))
    return resolved

def date_condition(packet, spec, link, audit=None, calendar_days=False, text_dates=None, refinements=None, replay_fn=None, reviewed_publication_fields=(), date_reader=None, native_publication_fields=None):
    """Default source behavior; scoped reviewed publication may also prove cutoff availability.

    The optional fields are trusted validator output, never an unvalidated model
    assertion. Native observations are copied and never relabelled. Event timing
    never receives the publication-policy exception.
    """
    reader = date_reader or dates
    expected = 'first_publication' if spec['predicate_id'].startswith('publication:') else 'fact_availability' if spec['predicate_id'] == 'as_of_knowledge' else 'event'
    support = resolve_quotes(packet, link['context_support'], audit)
    own_context = deepcopy(support)
    text_fields = {r['field'] for r in packet['allowed_text']}
    if any((q['field'] not in text_fields for q in support)):
        raise ValueError('date context must be own title/snippet')
    state = link['link_status']
    reason = link['reason']
    flags = []
    if state not in {'established', 'unknown', 'not_demonstrated'} or not isinstance(reason, str) or (not reason.strip()):
        raise ValueError('date link status/reason required')
    mentions = dates.mentions(packet) if text_dates is None else (replay_fn or dates.replay_mentions)(packet, text_dates)
    table = {r['date_id']: r for r in mentions}
    table.update({f'N{i + 1}': r for i, r in enumerate(packet['date_observations'])})
    rows = []
    for ident in link['date_ids']:
        if ident not in table:
            raise ValueError('unknown date ID')
        row = deepcopy(table[ident])
        row['_native'] = ident.startswith('N')
        rows.append(row)
    for quote in resolve_quotes(packet, link['additional_dates'], audit):
        if quote['field'] not in text_fields:
            raise ValueError('additional date must be own text')
        rows.append(dict(reader.quoted_date(packet, quote), _native=False))
    anchor = link['anchor_date_id']
    anchor_row = None
    if anchor:
        if anchor not in table or not anchor.startswith('D'):
            raise ValueError('anchor must be an own-text date ID')
        anchor_row = table[anchor]
        for row in rows:
            offset = {'today': 0, 'yesterday': -1, 'tomorrow': 1}.get(row.get('quote', '').lower())
            if row.get('type') == 'relative' and offset is not None and anchor_row.get('interval'):
                row['interval'] = {key: (datetime.fromisoformat(anchor_row['interval'][key]) + timedelta(days=offset)).isoformat() for key in ['start_inclusive', 'end_exclusive']}
                row['interval']['basis'] = 'explicit_own_text_anchor_preserving_precision'
                if calendar_days:
                    for key in ['precision', 'timezone', 'timezone_state']:
                        row[key] = anchor_row[key]
                support.append({key: anchor_row[key] for key in ['field', 'quote', 'start', 'end']})
    if state != 'established':
        return (dict(predicate_id=spec['predicate_id'], status=state, reason=reason, support=[]), flags)
    if link['role'] != expected:
        return (dict(predicate_id=spec['predicate_id'], status='not_demonstrated', reason=reason + ' Date role does not establish the requested timing.', support=[]), ['unsupported_date_role'])
    if native_publication_fields is not None and expected in {'first_publication','fact_availability'}:
        permitted=[]
        for row in rows:
            if row['_native'] and row.get('field') not in native_publication_fields:
                flags.append('native_publication_field_not_admitted')
            else:
                permitted.append(row)
        rows=permitted
    rows, notes = reader.linked_rows(rows, anchor_row)
    if refinements is not None:
        refinements.extend((dict(candidate_id=link['candidate_id'], predicate_id=spec['predicate_id'], **note) for note in notes))
    if anchor_row and any((note['kind'] == 'linked_year_composition' for note in notes)):
        support.append({k: anchor_row[k] for k in ('field', 'quote', 'start', 'end')})
    for row in rows:
        row['_reviewed_or_first_publication'] = row.get('field') in reviewed_publication_fields or row.get('date_kind') == 'first_publication'
    statuses = []
    date_support = []
    req = packet['question']['contract']['temporal_requirements']
    for row in rows:
        if row['_native']:
            reviewed = row.get('field') in reviewed_publication_fields
            publication = expected == 'first_publication' and (row.get('date_kind') == 'first_publication' or reviewed)
            availability = expected == 'fact_availability' and reviewed
            if not (publication or availability):
                flags.append('native_role_cannot_establish_requested_timing')
                continue
            native = next((r for r in packet['native_metadata'] if r['field'] == row['field']), None)
            if not native:
                raise ValueError('native date field missing')
            quote = dict(field=native['field'], quote=native['text'])
        else:
            if not support:
                raise ValueError('established text date needs semantic context')
            quote = {k: row[k] for k in ['field', 'quote', 'start', 'end']}
        if spec['predicate_id'] == 'as_of_knowledge':
            asof = packet['as_of_interpretation']
            cutoff = datetime.fromisoformat(asof['cutoff'].replace('Z', '+00:00'))
            if asof['boundary'] == 'inclusive':
                cutoff += timedelta(microseconds=1)
            interval = evidence.comparison_interval(row, calendar_days and asof.get('precision') == 'day')
            if interval:
                low = datetime.fromisoformat(interval['start_inclusive'])
                high = datetime.fromisoformat(interval['end_exclusive'])
                status = 'pass' if high <= cutoff else 'fail' if low >= cutoff else 'unknown'
            else:
                status = 'not_demonstrated'
            if date_reader:
                status = date_reader.cutoff(row, cutoff, calendar_days and asof.get('precision') == 'day')
        else:
            requirement = next((r for r in req if spec['predicate_id'] in {f"publication:{r['id']}", f"event:{r['id']}:window"}))
            item_natives=packet['date_observations']
            if native_publication_fields is not None and expected in {'first_publication','fact_availability'}:
                item_natives=[r for r in item_natives if r.get('field') in native_publication_fields]
            status = (date_reader.compare(row, requirement, calendar_days, key=(link['candidate_id'], spec['predicate_id']), rows=rows, expected=expected, item_natives=item_natives, context_support=own_context) if date_reader else evidence.compare_interval(dict(row, date_kind='first_publication'), requirement, calendar_days))
        if not row.get('_redundant'):
            statuses.append(status)
        date_support.append(quote)
    supported = [s for s in statuses if s != 'not_demonstrated']
    status = 'not_demonstrated' if not supported else supported[0] if len(set(supported)) == 1 else 'unknown'
    if not rows:
        flags.append('established_date_link_without_date')
    return (dict(predicate_id=spec['predicate_id'], status=status, reason=reason, support=support + date_support if status in {'pass', 'fail'} else []), flags)

def decode(packet, value, geography_text=None, calendar_days=False, text_dates=None, replay_fn=None, reviewed_publications=None, date_reader=None, native_publication_fields=None):
    """Strict adapter; semantic labels are never inferred from dates or grades."""
    if date_reader is not None:
        from ..date_reading import native_reading_packet
        packet = native_reading_packet(packet, native_publication_fields, profile=date_reader.profile)
    try:
        keys = {'schema_version', 'case_id', 'status', 'overall_grade', 'reason', 'dates', 'record_conditions', 'candidates'}
        if not isinstance(value, dict) or set(value) != keys or value['schema_version'] != 1 or (value['case_id'] != packet['case_id']):
            raise ValueError('two-step identity/shape differs')
        if value['overall_grade'] is not None and (type(value['overall_grade']) is not int or value['overall_grade'] not in {0, 1, 2}):
            raise ValueError('overall grade differs')
        specs = {r['predicate_id']: r for r in packet['required_conditions']}
        candidates = []
        flags = []
        used_dates = set()
        audit = []
        refinements = []

        def condition(row):
            if set(row) != {'predicate_id', 'status', 'reason', 'support'}:
                raise ValueError('condition fields differ')
            return dict(row, support=resolve_quotes(packet, row['support'], audit, extra_text=geography_text if row['predicate_id'] == 'geography' else None))
        records = [condition(r) for r in value['record_conditions']]
        expected_record = {s['predicate_id'] for s in specs.values() if s['required'] and s['scope'] == 'record' and (not date_predicate(s))}
        if len(records) != len(expected_record) or {r['predicate_id'] for r in records} != expected_record:
            raise ValueError('record conditions differ')
        for source in value['candidates']:
            if set(source) != {'candidate_id', 'claim', 'answer_supported', 'clear_lead', 'support', 'conditions', 'supported_facet_ids'}:
                raise ValueError('candidate fields differ')
            candidate = deepcopy(source)
            candidate['support'] = resolve_quotes(packet, candidate['support'], audit)
            own = [condition(r) for r in source['conditions']]
            expected_own = {s['predicate_id'] for s in specs.values() if s['required'] and s['scope'] == 'candidate' and (not date_predicate(s))}
            if len(own) != len(expected_own) or {r['predicate_id'] for r in own} != expected_own:
                raise ValueError('candidate conditions differ')
            rows = {r['predicate_id']: r for r in own + records}
            for spec in specs.values():
                ident = spec['predicate_id']
                if not spec['required']:
                    rows[ident] = dict(predicate_id=ident, status='not_required', reason='The question does not request this condition.', support=[])
                elif date_predicate(spec):
                    matches = [(i, r) for i, r in enumerate(value['dates']) if r['candidate_id'] == candidate['candidate_id'] and r['predicate_id'] == ident]
                    if len(matches) != 1:
                        raise ValueError('each candidate/date condition needs one link')
                    index, link = matches[0]
                    if set(link) != {'candidate_id', 'predicate_id', 'link_status', 'role', 'date_ids', 'anchor_date_id', 'additional_dates', 'context_support', 'reason'}:
                        raise ValueError('date link fields differ')
                    rows[ident], notes = date_condition(packet, spec, link, audit, calendar_days, text_dates, refinements, replay_fn,
                        reviewed_publication_fields=(reviewed_publications or {}).get((candidate['candidate_id'], ident), ()), date_reader=date_reader,
                        native_publication_fields=native_publication_fields)
                    flags.extend(notes)
                    used_dates.add(index)
            candidate['conditions'] = [deepcopy(rows[s]) for s in specs]
            candidate['missing_predicates'] = [r['predicate_id'] for r in candidate['conditions'] if r['status'] == 'not_demonstrated']
            candidates.append(candidate)
        if len(used_dates) != len(value['dates']):
            raise ValueError('unused/extra date link')
        grades = [evidence.candidate_grade(c, specs) for c in candidates]
        grade = 2 if 2 in grades else None if None in grades else max(grades, default=0)
        if value['status'] != 'judged':
            grade = None
        verdict = dict(schema_version=2, case_id=packet['case_id'], status=value['status'], grade=grade, evidence_class={0: 'neither', 1: 'lead', 2: 'answer', None: 'unknown'}[grade], reason=value['reason'], candidates=candidates)
        errors, normalized = evidence.validate_judgment(packet, verdict, geography_text=geography_text, calendar_days=calendar_days,
            reviewed_publications=reviewed_publications or (), native_publication_fields=native_publication_fields)
        return dict(state='valid' if not errors and value['status'] == 'judged' else 'unjudged', errors=errors, judgment=normalized, judge_overall_grade=value['overall_grade'], computed_grade=grade, grade_disagreement=value['overall_grade'] != grade, date_flags=flags, quote_reconciliations=audit, date_replay_version=dates.REPLAY_VERSION, date_refinements=refinements)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, StopIteration) as error:
        return dict(state='unjudged', errors=[str(error)], judgment=None)
