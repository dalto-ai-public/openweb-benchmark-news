

from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import re
from . import judgment_support as support
METHOD = 'news-returned-evidence-v2'
KINDS = {'first_publication', 'publication_unspecified', 'update', 'event', 'crawl', 'index', 'retrieval', 'capture', 'unclear'}

def as_of_meaning(question):
    req = question['contract']['as_of_requirement']
    explicit = question.get('as_of_interpretation')
    if explicit is not None:
        if req is None or any(explicit.get(k) != v for k, v in req.items()):
            raise ValueError('explicit as-of interpretation changed the frozen requirement')
        resolved = deepcopy(question)
        resolved.pop('as_of_interpretation', None)
        resolved['contract']['as_of_requirement'] = deepcopy(explicit)
        return as_of_meaning(resolved)
    if req is None:
        return None
    meaning = req.get('meaning')
    if meaning is None and question.get('provenance', {}).get('cutoff_policy_decision', {}).get('decision') == 'retain existing information-availability cutoff':
        return dict(meaning='historical_knowledge', boundary='inclusive', basis='recorded_owner_information_availability_decision', **deepcopy(req))
    if meaning not in {'historical_state', 'historical_knowledge', 'both'}:
        raise ValueError('as-of meaning must be resolved before judging')
    keys = ['target_time'] if meaning == 'historical_state' else ['cutoff'] if meaning == 'historical_knowledge' else ['target_time', 'cutoff']
    if any((not req.get(k) for k in [*keys, 'timezone', 'question_phrase'])) or (meaning != 'historical_state' and req.get('boundary') not in {'inclusive', 'exclusive'}):
        raise ValueError('as-of target/cutoff, timezone, phrase and cutoff boundary required')
    return deepcopy(req)

def required_conditions(question, as_of):
    rows = [r for r in support.required_conditions(question) if r['predicate_id'] != 'as_of_availability']
    for r in rows:
        r['scope'] = 'record' if r['predicate_id'] in {'source_rule', 'independence', 'language'} else 'candidate'
    for meaning, ident in [('historical_state', 'as_of_state'), ('historical_knowledge', 'as_of_knowledge')]:
        rows.append(dict(predicate_id=ident, domain='content', scope='candidate', required=as_of is not None and as_of['meaning'] in {meaning, 'both'}, contract_pointer='/contract/as_of_requirement'))
    return rows

def date_interval(value):
    """Possible UTC interval at supplied precision, never a claimed publication instant.

    Unknown timezone broadens bounds over UTC-12 through UTC+14. Relative prose,
    unlabeled numeric epochs and malformed data remain uninterpreted.
    """
    result = dict(precision='unresolved', timezone=None, timezone_state='absent', interval=None)
    if not isinstance(value, str) or not value.strip():
        return result
    s = value.strip()
    try:
        if re.fullmatch('\\d{4}(-\\d{2})?(-\\d{2})?', s):
            parts = [int(p) for p in s.split('-')]
            year, month, day = (parts + [1, 1])[:3] if len(parts) == 1 else (parts + [1])[:3]
            low = datetime(year, month, day)
            if len(parts) == 1:
                high = datetime(year + 1, 1, 1)
                precision = 'year'
            elif len(parts) == 2:
                high = datetime(year + (month == 12), month % 12 + 1, 1)
                precision = 'month'
            else:
                high = low + timedelta(days=1)
                precision = 'day'
        elif re.fullmatch('\\d{4}-\\d{2}-\\d{2}[T ]\\d{2}:\\d{2}(:\\d{2}(\\.\\d{1,6})?)?(Z|[+-]\\d{2}:?\\d{2})?', s):
            low = datetime.fromisoformat(s.replace('Z', '+00:00'))
            fraction = re.search('\\.(\\d+)', s)
            precision = 'fractional_second' if fraction else 'second' if re.search(':\\d{2}:\\d{2}', s) else 'minute'
            step = timedelta(microseconds=10 ** (6 - len(fraction[1]))) if fraction else timedelta(seconds=1) if precision == 'second' else timedelta(minutes=1)
            high = low + step
        else:
            low = parsedate_to_datetime(s)
            if not re.search('\\d{4}', s):
                return result
            precision = 'second'
            high = low + timedelta(seconds=1)
        result['precision'] = precision
        if low.tzinfo is None:
            low = (low - timedelta(hours=14)).replace(tzinfo=timezone.utc)
            high = (high + timedelta(hours=12)).replace(tzinfo=timezone.utc)
        else:
            token = re.search('(Z|[+-]\\d{2}:?\\d{2}|GMT|UTC)$', s)
            result.update(timezone=token[1] if token else str(low.tzinfo), timezone_state='supplied')
            low, high = (low.astimezone(timezone.utc), high.astimezone(timezone.utc))
        result['interval'] = dict(start_inclusive=low.isoformat(), end_exclusive=high.isoformat(), basis='supported_precision_and_supplied_offset_or_unknown_offset_range')
    except (ValueError, TypeError, OverflowError):
        pass
    return result

def comparison_interval(observation, calendar_days=False):
    """Project a saved interval for comparison without changing returned dates."""
    interval = observation.get('interval')
    if not interval or not calendar_days:
        return interval
    low = datetime.fromisoformat(interval['start_inclusive'])
    high = datetime.fromisoformat(interval['end_exclusive'])
    if observation.get('timezone_state') == 'absent':
        low += timedelta(hours=14)
        high -= timedelta(hours=12)
    else:
        offset = observation.get('timezone') or 'UTC'
        match = re.fullmatch('([+-])(\\d{2}):?(\\d{2})', offset)
        if match:
            minutes = (int(match[2]) * 60 + int(match[3])) * (1 if match[1] == '+' else -1)
            zone = timezone(timedelta(minutes=minutes))
            low, high = (low.astimezone(zone), high.astimezone(zone))
    low = low.replace(tzinfo=timezone.utc)
    high = high.replace(tzinfo=timezone.utc)
    start = low.replace(hour=0, minute=0, second=0, microsecond=0)
    end = high.replace(hour=0, minute=0, second=0, microsecond=0)
    if high > end:
        end += timedelta(days=1)
    return dict(start_inclusive=start.isoformat(), end_exclusive=end.isoformat(), basis='owner_calendar_day_comparison_preserving_stated_period')

def calendar_window(requirement):
    start, end = (datetime.fromisoformat(requirement[k].replace('Z', '+00:00')) for k in ['start_inclusive', 'end_exclusive'])
    return start.time() == end.time() == datetime.min.time() and end - start >= timedelta(days=1)

def compare_interval(observation, requirement, calendar_days=False):
    if observation.get('date_kind') != 'first_publication' or not observation.get('interval'):
        return 'not_demonstrated'

    def instant(text):
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('comparison boundary requires an explicit offset')
        return dt
    start, end = (instant(requirement['start_inclusive']), instant(requirement['end_exclusive']))
    interval = comparison_interval(observation, calendar_days and calendar_window(requirement))
    low, high = (instant(interval['start_inclusive']), instant(interval['end_exclusive']))
    if low >= start and high <= end:
        return 'pass'
    if high <= start or low >= end:
        return 'fail'
    return 'unknown'

def publication_status(observations, requirement, calendar_days=False):
    if requirement is None:
        return 'not_required'
    statuses = [compare_interval(o, requirement, calendar_days) for o in observations if o['date_kind'] == 'first_publication' and o.get('interval')]
    if not statuses:
        return 'not_demonstrated'
    return statuses[0] if len(set(statuses)) == 1 else 'unknown'

def classify_date(observation, configuration, registry):
    rules = [r for r in registry['mappings'] if r['configuration_id'] == configuration['configuration_id'] and r['endpoint'] == configuration['endpoint'] and (r['configuration_sha256'] == configuration['mapping_configuration_sha256']) and (r['field_path'] == observation['relative_path'])]
    if len(rules) > 1:
        raise ValueError('duplicate date mapping')
    rule = rules[0] if rules else None
    return dict(field_path=observation['path'], relative_path=observation['relative_path'], raw_value=deepcopy(observation['value']), state=observation['state'], date_kind=rule['date_kind'] if rule else 'unclear', derivation=rule['derivation'] if rule else 'unspecified', mapping_id=rule['mapping_id'] if rule else None, mapping_version=registry['mapping_version'], interpretation_state='reviewed_mapping' if rule else 'unresolved_mapping', **date_interval(observation['value']))

def build_input(question, evidence, metadata, dates):
    if (evidence['question_id'], evidence['question_revision'], evidence['evidence_id']) != (question['question_id'], question['revision'], metadata['evidence_id']):
        raise ValueError('input occurrence/question join differs')
    as_of = as_of_meaning(question)
    observations = []
    for path, item in metadata['observed_metadata'].items():
        if item['present']:
            observations.append(dict(field='native:' + path, text=json.dumps(item['value'], ensure_ascii=False, sort_keys=True), role='eligibility_only'))
    for i, date in enumerate(dates):
        date['field'] = f'date:{i}'
        observations.append(dict(field=date['field'], text=json.dumps(date['raw_value'], ensure_ascii=False, sort_keys=True), role='eligibility_only'))
    packet = dict(schema_version=2, method_revision=METHOD, case_id=evidence['evidence_id'], question={k: deepcopy(question[k]) for k in ('question_id', 'revision', 'provider_query', 'geography', 'contract')}, as_of_interpretation=as_of, required_conditions=required_conditions(question, as_of), allowed_text=deepcopy(evidence['allowed_text']), returned_url=metadata['observed_url']['value'] if metadata['observed_url']['state'] == 'present' else None, native_metadata=observations, date_observations=dates, native_field_states=deepcopy(evidence['native_field_states']), native_record_state=evidence['native_record_state'])
    errors = validate_input(packet)
    if errors:
        raise ValueError('; '.join(errors))
    return packet

def validate_input(packet):
    keys = {'schema_version', 'method_revision', 'case_id', 'question', 'as_of_interpretation', 'required_conditions', 'allowed_text', 'returned_url', 'native_metadata', 'date_observations', 'native_field_states', 'native_record_state'}
    if not isinstance(packet, dict) or set(packet) != keys or packet.get('schema_version') != 2 or (packet.get('method_revision') != METHOD):
        return ['v2 input fields/version differ']
    errors = []
    q = deepcopy(packet['question'])
    q['contract']['as_of_requirement'] = packet['as_of_interpretation']
    original_as_of = packet['question']['contract']['as_of_requirement']
    resolved_as_of = packet['as_of_interpretation']
    if (original_as_of is None) != (resolved_as_of is None) or (original_as_of is not None and any((resolved_as_of.get(k) != v for k, v in original_as_of.items()))):
        errors.append('as-of interpretation changed the frozen requirement')
    try:
        as_of = as_of_meaning(q)
        if packet['required_conditions'] != required_conditions(packet['question'], as_of):
            errors.append('conditions differ from contract')
    except (KeyError, ValueError):
        errors.append('unresolved as-of contract')
    texts = packet['allowed_text']
    if not isinstance(texts, list) or any((set(r) != {'field', 'text'} or not isinstance(r['text'], str) or (not re.fullmatch('title|snippet(\\[\\d+\\])?|additional_snippet_\\d+(\\[\\d+\\])?', r['field'])) for r in texts)):
        errors.append('only mapped title/snippet text is allowed')
    fields = [r['field'] for r in texts + packet['native_metadata']] + ['url']
    if len(fields) != len(set(fields)):
        errors.append('duplicate input field')
    if packet['returned_url'] is not None and (not isinstance(packet['returned_url'], str)):
        errors.append('returned URL must be text or null')
    for date in packet['date_observations']:
        if date.get('date_kind') not in KINDS or date.get('derivation') not in {'provider_reported', 'documented_estimated', 'unspecified'} or (not date.get('mapping_version')):
            errors.append('invalid native date interpretation')
    return errors

def blank_judgment(packet):
    return dict(schema_version=2, case_id=packet['case_id'], status=None, grade=None, evidence_class=None, reason='', candidates=[])

def candidate_grade(candidate, required):
    statuses = [r['status'] for r in candidate['conditions'] if required[r['predicate_id']]['required']]
    if 'fail' in statuses:
        return 0
    if 'unknown' in statuses:
        return None
    if candidate['answer_supported'] and all((s == 'pass' for s in statuses)):
        return 2
    return 1 if candidate['clear_lead'] else 0

def validate_judgment(packet, value, geography_text=None, calendar_days=False, reviewed_publications=(), native_publication_fields=None):
    errors = validate_input(packet)
    if errors:
        return (errors, None)
    keys = {'schema_version', 'case_id', 'status', 'grade', 'evidence_class', 'reason', 'candidates'}
    if not isinstance(value, dict) or set(value) != keys:
        return (['judgment fields differ from v2 schema'], None)
    result = deepcopy(value)
    if type(value['schema_version']) is not int or value['schema_version'] != 2 or value['case_id'] != packet['case_id']:
        errors.append('judgment identity/version differs')
    if not isinstance(value['status'], str) or value['status'] not in support.OUTCOMES or (not support._text(value['reason'])):
        errors.append('judgment status/reason required')
    grade = value['grade']
    if grade is not None and (type(grade) is not int or grade not in {0, 1, 2}):
        errors.append('grade must be 0/1/2/null')
        grade = None
    if value['evidence_class'] != {0: 'neither', 1: 'lead', 2: 'answer', None: 'unknown'}.get(grade):
        errors.append('grade/class differ')
    specs = {r['predicate_id']: r for r in packet['required_conditions']}
    answer_text = {r['field']: r['text'] for r in packet['allowed_text']}
    native_text = {r['field']: r['text'] for r in packet['native_metadata']}
    all_text = dict(answer_text, **native_text)
    if packet['returned_url']:
        all_text['url'] = packet['returned_url']
    candidates = result['candidates']
    if not isinstance(candidates, list):
        return (errors + ['candidates must be a list'], None)
    grades, record_conditions, ids = ([], {}, set())
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {'candidate_id', 'claim', 'answer_supported', 'clear_lead', 'support', 'conditions', 'missing_predicates', 'supported_facet_ids'}:
            errors.append('candidate fields differ')
            continue
        ident = candidate['candidate_id']
        if not support._text(ident) or not support._text(candidate['claim']):
            errors.append('candidate requires unique identity/claim')
            continue
        if ident in ids:
            errors.append('candidate requires unique identity/claim')
        ids.add(ident)
        if type(candidate['answer_supported']) is not bool or type(candidate['clear_lead']) is not bool:
            errors.append('candidate support flags must be boolean')
        support_text = answer_text if candidate['answer_supported'] else {k: v for k, v in all_text.items() if k not in native_text}
        candidate['support'] = support._resolve_support(candidate['support'], support_text, 'candidate.support', errors)
        if (candidate['answer_supported'] or candidate['clear_lead']) and (not candidate['support']):
            errors.append('answer/lead requires exact permitted support')
        rows = candidate['conditions']
        if not isinstance(rows, list) or any((not isinstance(r, dict) for r in rows)) or sorted((str(r.get('predicate_id')) for r in rows)) != sorted(specs):
            errors.append('candidate requires every predicate exactly once')
            continue
        for row in rows:
            spec = specs[row['predicate_id']]
            status = row.get('status')
            if set(row) != {'predicate_id', 'status', 'reason', 'support'} or not isinstance(status, str) or status not in support.STATUSES or (not support._text(row.get('reason'))):
                errors.append('condition fields/status/reason differ')
                continue
            if (status == 'not_required') == spec['required']:
                errors.append('condition applicability differs')
            texts = all_text if (spec['domain'] == 'metadata' or (row['predicate_id'] == 'as_of_knowledge'
                and (candidate['candidate_id'], row['predicate_id']) in reviewed_publications)) else answer_text
            if row['predicate_id'] == 'geography' and geography_text:
                texts = dict(texts, **geography_text)
            source_condition = row['predicate_id'] in {'source_rule', 'independence'}
            if not source_condition:
                texts = {k: v for k, v in texts.items() if k != 'url'}
            row['support'] = support._resolve_support(row['support'], texts, 'condition.support', errors)
            if (native_publication_fields is not None and status in {'pass','fail'}
                    and (row['predicate_id'].startswith('publication:') or row['predicate_id']=='as_of_knowledge')
                    and any(s['field'] in native_text and s['field'] not in native_publication_fields for s in row['support'])):
                errors.append('native publication field not admitted for publication/availability proof')
            if status in {'pass', 'fail'} and (not row['support']):
                errors.append('condition pass/fail requires own permitted evidence')
            if source_condition and status in {'pass', 'fail'} and row['support'] and all((s['field'] == 'url' for s in row['support'])):
                errors.append('URL publisher identity requires own item evidence for source verdict')
            if spec['scope'] == 'record':
                shared = dict(status=status, support=row['support'])
                if row['predicate_id'] in record_conditions and record_conditions[row['predicate_id']] != shared:
                    errors.append('whole-record conditions must agree across candidates')
                record_conditions[row['predicate_id']] = shared
            if row['predicate_id'].startswith('publication:') and status in {'pass', 'fail'} and row['support'] and all((s['field'] in native_text for s in row['support'])):
                req = next((r for r in packet['question']['contract']['temporal_requirements'] if r['id'] == row['predicate_id'].split(':', 1)[1]))
                fields = {s['field'] for s in row['support']}
                selected = [d for d in packet['date_observations'] if d['field'] in fields
                            and (native_publication_fields is None or d['field'] in native_publication_fields)]
                if (publication_status(selected, req, calendar_days) != status
                        and (candidate['candidate_id'], row['predicate_id']) not in reviewed_publications):
                    errors.append('native date meaning/interval does not establish publication verdict')
        missing = [r['predicate_id'] for r in rows if r['status'] == 'not_demonstrated']
        if candidate['missing_predicates'] != missing:
            errors.append('missing_predicates differ from condition gaps')
        facets = {f['id'] for f in packet['question']['contract']['requested_facets']}
        if not isinstance(candidate['supported_facet_ids'], list) or any((f not in facets for f in candidate['supported_facet_ids'])):
            errors.append('candidate facet IDs differ from contract')
        if all((isinstance(r.get('status'), str) for r in rows)):
            grades.append(candidate_grade(candidate, specs))
    expected = 2 if 2 in grades else None if None in grades else max(grades, default=0)
    if value['status'] != 'judged':
        if grade is not None or candidates:
            errors.append('unfinished/refused/error judgment needs null and no judged candidates')
    elif grade != expected:
        errors.append('result grade differs from candidate failure/unknown/maximum rule')
    if grade is not None and (packet['native_record_state'] != 'native_result' or any((s in {'wrong_type', 'malformed_array'} for s in packet['native_field_states'].values()))):
        errors.append('malformed native text needs capture review before numeric judgment')
    return (errors, None if errors else result)
