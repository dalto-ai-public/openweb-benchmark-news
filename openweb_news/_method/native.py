
from __future__ import annotations
import hashlib
import json
import re
from urllib.parse import urlsplit
def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

def field_value(row: object, field: str) -> tuple[bool, object]:
    """Read a declared dotted path without conflating absent and explicit null."""
    value = row
    for part in field.split('.'):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and (int(part) < len(value)):
            value = value[int(part)]
        else:
            return (False, None)
    return (True, value)

def native_field(row: dict, field: str, path: str, shape: str) -> dict:
    present, value = field_value(row, field)
    if shape == 'object_text_array' and isinstance(value, list):
        # Native snippet objects remain intact beside their exact text projection.
        # Keep original indices, including malformed/missing entries; no joining,
        # fallback to document bodies, clipping or compaction of the array.
        items = [dict(index=i, **native_field(v, 'text', f'{path}.{field}[{i}]', 'string'))
                 if isinstance(v, dict) else dict(index=i, path=f'{path}.{field}[{i}]', state='wrong_type', value=v)
                 for i, v in enumerate(value)]
        return dict(path=f'{path}.{field}', state='empty' if not value else
                    'malformed_array' if any(item['state'] not in {'present', 'empty'} for item in items) else 'present',
                    native_value=value, value=[item.get('value') if isinstance(value[i], dict) else None for i, item in enumerate(items)], items=items)
    if not present:
        state = 'missing'
    elif value is None:
        state = 'null'
    elif shape == 'string':
        state = ('present' if value.strip() else 'empty') if isinstance(value, str) else 'wrong_type'
    else:
        state = ('present' if value else 'empty') if isinstance(value, list) else 'wrong_type'
        if isinstance(value, list) and any((not isinstance(v, str) for v in value)):
            state = 'malformed_array'
    result = dict(path=f'{path}.{field}', state=state)
    if present:
        result['value'] = value
    if shape == 'string_array' and isinstance(value, list):
        result['items'] = [dict(index=i, path=f'{path}.{field}[{i}]', value=v, state=('present' if v.strip() else 'empty') if isinstance(v, str) else 'wrong_type') for i, v in enumerate(value)]
    return result

def native_date_observations(c: dict, row: object, path: str) -> dict:
    """Keep exact date-named JSON fields, including nested context and bad types.

    No text parsing, timezone conversion or publication-eligibility decision.
    Unknown fields remain in raw/native rows; recognition is not semantic coverage.
    """
    observations = []
    provider = c.get('provider', '')

    def tokens(key):
        return set(re.findall('[a-z]+', re.sub('([a-z])([A-Z])', '\\1_\\2', key).lower()))

    def visit(value, parts, native_path):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = native_path + ('.' + key if re.fullmatch('[A-Za-z_][A-Za-z_0-9]*', key) else '[' + json.dumps(key, ensure_ascii=False) + ']')
                bits = tokens(key)
                relative = '.'.join((str(part) for part in [*parts, key]))
                known = NATIVE_DATE_RULES.get((provider, relative))
                recognized = bool(bits & {'date', 'dates', 'datetime', 'time', 'timestamp', 'timestamps', 'published', 'publication', 'modified', 'updated', 'created', 'fetched', 'crawled', 'indexed', 'seen', 'lastmod'}) or key in {'age', 'page_age', 'page_fetched'}
                if recognized or known:
                    if child is None:
                        state = 'null'
                    elif isinstance(child, str):
                        state = 'present' if child.strip() else 'empty'
                    elif type(child) in {int, float}:
                        state = 'numeric_uninterpreted'
                    else:
                        state = 'uninterpreted_json'
                    if known:
                        classification, source = known
                        basis = 'provider_contract' if classification in {'publication_or_update', 'relative_age', 'fetch_time'} else 'field_claim_or_unresolved_contract'
                    else:
                        source = None
                        basis = 'field_name_only'
                        classification = 'nested_date_unrelated_or_unknown' if parts else 'relative_age' if key == 'age' else 'update_claim' if bits & {'modified', 'updated'} else 'publication_claim_original_unresolved' if bits & {'published', 'publication'} else 'ambiguous_date'
                    observations.append(dict(path=child_path, relative_path=relative, field=key, context='nested_native_value' if parts else 'result_metadata', state=state, value=child, classification=classification, classification_basis=basis, contract_source=source, original_publication_established=False, eligibility='unreviewed'))
                visit(child, [*parts, key], child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, [*parts, index], f'{native_path}[{index}]')
    visit(row, [], path)
    return dict(version=1, observations=observations, coverage='Recognized native field names recursively; full original row and response retained. No date extraction from prose.', publication_eligibility='unreviewed')

def project_response(c: dict, raw: bytes, http_status: int=200, complete: bool=True) -> dict:
    """Project allowed native fields without changing rank or substituting page bodies.

    Raw bytes must be kept by the future collector; their hash binds every projection.
    Successful parsing is not an answer, eligibility judgment or historical availability proof.
    """
    result = dict(configuration_id=c['configuration_id'], raw_sha256=sha256(raw), http_status=http_status, complete=complete, status=None, positions=[])
    if not complete:
        result['status'] = 'incomplete_transport'
        return result
    if not 200 <= http_status < 300:
        result['status'] = {401: 'authentication_error', 403: 'access_denied', 402: 'payment_or_quota_error', 429: 'rate_limit_or_quota_error'}.get(http_status, 'http_error')
        return result
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        result['status'] = 'invalid_json'
        return result
    fields = c['result_fields']
    version = fields.get('native_dates_version')
    if version is not None and (type(version) is not int or version != 1):
        raise ValueError('unsupported native dates projection version')
    key = fields['container']
    decoded_path = fields.get('json_decode_path')
    source_prefix = '$'
    if decoded_path:
        present_status, embedded_status = field_value(payload, fields['envelope_http_status_path'])
        if not present_status or type(embedded_status) is not int or (not 100 <= embedded_status <= 599):
            result['status'] = 'malformed_envelope_http_status'
            return result
        result['envelope_http_status'] = embedded_status
        if not 200 <= embedded_status < 300:
            result['status'] = {401: 'authentication_error', 403: 'access_denied', 402: 'payment_or_quota_error', 429: 'rate_limit_or_quota_error'}.get(embedded_status, 'http_error')
            return result
        present, embedded = field_value(payload, decoded_path)
        if not present or not isinstance(embedded, str):
            result['status'] = 'missing_json_body' if not present else 'non_string_json_body'
            return result
        try:
            payload = json.loads(embedded)
        except ValueError:
            result['status'] = 'invalid_json_body'
            return result
        source_prefix = f'json_decode($.{decoded_path})'
    if not isinstance(payload, dict):
        result['status'] = 'malformed_envelope'
        return result
    present, rows = field_value(payload, key)
    if not present:
        result['status'] = 'missing_results'
        return result
    if not isinstance(rows, list):
        result['status'] = 'null_results' if rows is None else 'non_list_results'
        return result
    for i, row in enumerate(rows):
        position = dict(rank=i + 1, path=f'{source_prefix}.{key}[{i}]', in_original_top_ten=i < 10)
        if not isinstance(row, dict):
            position.update(status='non_object_result', value=row)
        else:
            path = position['path']
            url = native_field(row, fields['url'], path, 'string')
            if url['state'] == 'present':
                try:
                    parsed = urlsplit(url['value'])
                    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
                        url['state'] = 'invalid_url'
                except ValueError:
                    url['state'] = 'invalid_url'
            position.update(status='native_result', url=url, title=native_field(row, fields['title'], path, 'string'), content=native_field(row, fields['content'], path, fields['content_shape']), metadata={f: dict(path=f'{path}.{f}', present=field_value(row, f)[0], **{'value': field_value(row, f)[1]} if field_value(row, f)[0] else {}) for f in fields['metadata']}, additional_content={f: native_field(row, f, path, shape) for f, shape in fields.get('additional_content', {}).items()})
            if version == 1:
                position['native_dates'] = native_date_observations(c, row, path)
        result['positions'].append(position)
    result['status'] = 'success_empty' if not rows else 'success'
    return result

NATIVE_DATE_RULES = {('Tavily', 'published_date'): ('publication_or_update', 'https://docs.tavily.com/documentation/api-reference/endpoint/search'), ('Brave', 'page_age'): ('publication_or_update', 'https://api-dashboard.search.brave.com/api-reference/news/news_search/get'), ('Brave', 'age'): ('relative_age', 'https://api-dashboard.search.brave.com/api-reference/news/news_search/get'), ('Brave', 'page_fetched'): ('fetch_time', 'https://api-dashboard.search.brave.com/api-reference/news/news_search/get'), ('Perplexity', 'last_updated'): ('update_claim', 'https://docs.perplexity.ai/api-reference/search-post'), ('Perplexity', 'date'): ('ambiguous_date', 'https://docs.perplexity.ai/api-reference/search-post'), ('Parallel', 'publish_date'): ('publication_claim_original_unresolved', 'https://docs.parallel.ai/api-reference/search/search'), ('Exa', 'publishedDate'): ('publication_claim_original_unresolved', 'https://exa.ai/docs/reference/search'), ('Searlo', 'date'): ('ambiguous_date', 'https://searlo.tech/api-reference'), ('Firecrawl', 'date'): ('ambiguous_date', 'https://docs.firecrawl.dev/api-reference/endpoint/search')}
