
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from urllib.parse import urlencode
from . import native as native
SOURCES = {"keenable": ("https://docs.keenable.ai/api-reference/search", "")}
def _instant(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timestamps require an explicit timezone')
    return dt.astimezone(timezone.utc)

def build_request(config, question, *, date_profile=None):
    """Return non-secret method/endpoint/body and mapping states; never send it.

    ``question`` uses canonical provider_query, question_id, revision and
    contract.temporal_requirements. Geography stays in the common query/contract.
    Only basis=publication triggers publication filtering; event dates never do.
    """
    query = question['provider_query']
    if not isinstance(query, str) or not query.strip():
        raise ValueError('provider_query must be a nonempty string')
    temporal = question['contract']['temporal_requirements']
    publication = [r for r in temporal if r['basis'] == 'publication']
    if len(publication) > 1:
        raise ValueError('multiple publication windows require an explicit mapping')
    body = deepcopy(config['base_body'])
    for field, shape in config['query_fields'].items():
        if shape == 'google_url':
            body[field] = 'https://www.google.com/search?' + urlencode({'q': query, 'num': 10})
        elif shape == 'single_item_array':
            body[field] = [query]
        elif shape == 'string':
            body[field] = query
        else:
            raise ValueError('unsupported query field shape')
    state = 'no_publication_requirement'
    date_receipt = None
    dispatch_binding = None
    if date_profile is not None:
        from ..request_profiles import validate_start_profiles
        validate_start_profiles([config], {config['configuration_id']: date_profile})
    if 'date_request_profile' in config:
        from ..provider_dates import apply_profile, capability, map_control
        from ..common import digest
        apply_profile(config, config['date_request_profile'])
        date_receipt = dict(profile=config['date_request_profile'], state=state,
                            capability_sha256=digest(capability(config)),
                            sent_date_fields={}, eligibility_proof=False)
    if publication:
        start, end = (_instant(publication[0][key]) for key in ('start_inclusive', 'end_exclusive'))
        if start >= end:
            raise ValueError('publication window must have positive duration')
        mapping = config['date_mapping']
        state = 'unmapped_publication_requirement'
        if mapping == 'perplexity_publication':
            body['search_after_date_filter'] = (start.date() - timedelta(days=1)).strftime('%m/%d/%Y')
            body['search_before_date_filter'] = (end.date() + timedelta(days=int(end.time().isoformat() != '00:00:00'))).strftime('%m/%d/%Y')
        elif mapping == 'exa_publication':
            body['startPublishedDate'] = (start - timedelta(seconds=1)).isoformat()
            body['endPublishedDate'] = (end + timedelta(seconds=1)).isoformat()
        elif mapping == 'valyu_publication':
            body['start_date'] = start.date().isoformat()
            body['end_date'] = end.date().isoformat()
        elif mapping == 'keenable_publication':
            body['published_after'] = start.isoformat()
            body['published_before'] = end.isoformat()
        elif mapping == 'seltz_publication':
            body['from_date'] = start.isoformat()
            # Inclusive native upper bound conservatively includes the instant
            # at our exclusive boundary. Own-evidence eligibility still applies.
            body['to_date'] = end.isoformat()
        elif mapping != 'unmapped':
            raise ValueError('unsupported publication mapping')
        if mapping != 'unmapped':
            state = 'documented_publication_filter_conservative_time_bounds_not_eligibility_proof'
        if date_receipt is not None:
            additions, date_receipt = map_control(config, start, end, query)
            body.update(additions)
            state = date_receipt['state']
        if date_profile is not None:
            from ..request_profiles import map_start_control
            additions, date_receipt, dispatch_binding = map_start_control(config, start, end, date_profile)
            body.update(additions)
            state = date_receipt['state']
    result = dict(configuration_id=config['configuration_id'], question_id=question['question_id'], revision=question['revision'], method=config['method'], endpoint=config['endpoint'], body=body, query_sha256=hashlib.sha256(query.encode()).hexdigest(), date_mapping_state=state, location_mapping_state='no_subject_country_to_ranking_filter', live_execution_authorized=False)
    if date_receipt is not None:
        result['date_capability_receipt'] = date_receipt
    if dispatch_binding is not None:
        result['date_dispatch'] = dispatch_binding
    return result

def project_response(config, response, capture_time, *, http_status=200, complete=True, token_counter=None, tokenizer_id=None):
    """Project raw bytes via canonical native fields, adding capture/date context.

    Dict/list accepted only for synthetic replay; raw bytes required for authentic
    capture hashes. The caller retains full permitted raw/partial bytes privately.
    Native dates are observations, not verified original-publication predicates.
    """
    _instant(capture_time)
    if (token_counter is None) != (tokenizer_id is None):
        raise ValueError('token_counter and tokenizer_id must be supplied together')
    raw = response if isinstance(response, bytes) else json.dumps(response, ensure_ascii=False).encode()
    result = native.project_response(config, raw, http_status=http_status, complete=complete)
    if config['provider'] == 'Bright Data' and complete and 200 <= http_status < 300 and not raw.strip():
        # An empty HTTP body is not a parsed empty native result list. Keep the
        # original failure status and bytes, with a replayable diagnostic.
        result['failure_detail'] = 'empty_response_body'
    payload = None
    if complete and 200 <= http_status < 300:
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict) and (payload.get('success') is False or payload.get('error') not in (None, '', {}, [])):
            result.update(status='provider_error', positions=[])
        if config['provider'] == 'Seltz' and isinstance(payload, dict) and isinstance(payload.get('documents'), list):
            issues = [dict(rank=i + 1, reason='unexpected_full_document_content')
                      for i, row in enumerate(payload['documents']) if isinstance(row, dict) and row.get('content') not in (None, '')]
            if issues:
                result.update(status='native_product_mismatch', native_product_issues=issues)
    result['response_metadata'] = {key: dict(path=f'$.{key}', value=payload[key]) for key in ('query', 'metadata', 'searchParameters', 'searchInformation', 'mode', 'server_time', 'creditsUsed', 'total_deduction_dollars', 'usage', 'cost') if isinstance(payload, dict) and key in payload}
    result['capture_time'] = capture_time
    result['capture_time_role'] = 'collection_transport_time_not_publication_or_event'
    result['native_response_bytes_supplied'] = isinstance(response, bytes)
    result['mapping_version'] = 'news-provider-projection-v1'
    if config['provider'] == 'Keenable':
        for position in result['positions']:
            acquired = position.get('metadata', {}).get('acquired_at', {})
            if acquired.get('present'):
                value = acquired['value']
                position['native_dates']['observations'].append(dict(path=acquired['path'], relative_path='acquired_at', field='acquired_at', context='result_metadata', value=value, state='null' if value is None else 'present' if isinstance(value, str) else 'uninterpreted_json', classification='acquisition_or_index_time', classification_basis='provider_contract', contract_source=SOURCES['keenable'][0], original_publication_established=False, eligibility='unreviewed'))
    result['native_text_length_observations'] = [dict(rank=p['rank'], fields={name: len(field['value']) if isinstance(field.get('value'), str) else [len(x) if isinstance(x, str) else None for x in field.get('value', [])] if isinstance(field.get('value'), list) else None for name, field in {'title': p.get('title', {}), 'content': p.get('content', {}), **p.get('additional_content', {})}.items()}) for p in result['positions']]
    result['native_text_token_observations'] = dict(state='pending_selected_tokenizer' if token_counter is None else 'counted', tokenizer_id=tokenizer_id, positions=[])
    if token_counter is not None:
        for p in result['positions']:
            fields = {'title': p.get('title', {}), 'content': p.get('content', {}), **p.get('additional_content', {})}
            counts = {}
            for name, field in fields.items():
                value = field.get('value')
                counts[name] = token_counter(value) if isinstance(value, str) else [token_counter(v) if isinstance(v, str) else None for v in value] if isinstance(value, list) else None
            result['native_text_token_observations']['positions'].append(dict(rank=p['rank'], fields=counts))
    return result

def configuration_catalog(*, include_optional=False):
    from pathlib import Path
    rows = json.loads((Path(__file__).resolve().parents[1] / "assets/providers.json").read_text())
    return [row for row in rows if include_optional or not row["optional"]]
