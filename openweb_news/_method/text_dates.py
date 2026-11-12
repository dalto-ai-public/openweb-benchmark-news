

from datetime import datetime, timedelta, timezone
import re
from copy import deepcopy
REPLAY_VERSION = 'news-linked-text-dates-v3'
from . import evidence as evidence
MONTHS = {}
for number, names in enumerate(['january jan janvier enero janeiro gennaio januar', 'february feb février fevrier febrero fevereiro febbraio februar', 'march mar mars marzo março marco märz maerz', 'april apr avril abril aprile', 'may mai mayo maio maggio', 'june jun juin junio junho giugno juni', 'july jul juillet julio julho luglio juli', 'august aug août aout agosto', 'september sep sept septembre septiembre setembro settembre', 'october oct octobre octubre outubro ottobre oktober', 'november nov novembre noviembre novembro', 'december dec décembre decembre diciembre dezembro dicembre dezember'], 1):
    MONTHS.update({name: number for name in names.split()})
MONTH = '(?:' + '|'.join(sorted(MONTHS, key=len, reverse=True)) + ')'
YEAR = '(?:19|20|21)\\d{2}'
RELATIVE = 'today|yesterday|tomorrow|last\\s+(?:week|month|year)|(?:last|on)\\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
PATTERNS = [f'{MONTH}\\s*[–—-]\\s*{MONTH}(?:\\s+{YEAR})?', f'Q[1-4](?:\\s+{YEAR})?|{YEAR}\\s+Q[1-4]', f'\\d{{1,2}}\\s*/\\s*\\d{{1,2}}\\s*/\\s*{YEAR}', f'{YEAR}-\\d{{2}}-\\d{{2}}(?:[T ]\\d{{2}}:\\d{{2}}(?::\\d{{2}}(?:\\.\\d{{1,6}})?)?(?:Z|[+-]\\d{{2}}:?\\d{{2}})?)?', f'{MONTH}\\s+\\d{{1,2}}(?:st|nd|rd|th)?(?:,?\\s+{YEAR})?', f'\\d{{1,2}}(?:st|nd|rd|th)?\\s+(?:de\\s+)?{MONTH}(?:\\s+(?:de\\s+)?{YEAR})?', f'{MONTH}\\s+{YEAR}', f'{YEAR}-\\d{{2}}', YEAR, RELATIVE, MONTH]
SCAN = re.compile('(?<!\\w)(?:' + '|'.join(PATTERNS) + ')(?!\\w)', re.I)

def visible_text(text):
    """Only observed Markdown emphasis/inline links; preserve an original-index map."""
    hidden = set()
    for match in re.finditer('\\[([^\\[\\]\\n]+)\\]\\(', text):
        depth = 1
        end = match.end()
        while end < len(text) and depth:
            if text[end] == '(':
                depth += 1
            elif text[end] == ')':
                depth -= 1
            end += 1
        if not depth:
            hidden.add(match.start())
            hidden.update(range(match.end(1), end))
    for match in re.finditer('(?<![\\w*])\\*\\*(\\S(?:[^*\\n]*?\\S)?)\\*\\*(?![\\w*])', text):
        hidden.update(range(match.start(), match.start(1)))
        hidden.update(range(match.end(1), match.end()))
    positions = [i for i in range(len(text)) if i not in hidden]
    return (''.join((text[i] for i in positions)), positions)

def exact_span(text, quote, occurrence=0):
    """Resolve literal or lossless visible-text quotes, retaining original raw spans."""
    if not isinstance(quote, str) or not quote or type(occurrence) is not int or (occurrence < 0):
        raise ValueError('quote/occurrence required')

    def found(haystack):
        result = []
        start = 0
        while True:
            offset = haystack.find(quote, start)
            if offset < 0:
                return result
            result.append(offset)
            start = offset + 1
    matches = {(start, start + len(quote)): 'literal' for start in found(text)}
    visible, positions = visible_text(text)
    for start in found(visible):
        span = (positions[start], positions[start + len(quote) - 1] + 1)
        matches.setdefault(span, 'markdown_visible_to_raw_span')
    from ..representation import matches as mapped_matches
    for span, mode in mapped_matches(text, quote).items():
        matches.setdefault(span, mode)
    if not matches or (not occurrence and len(matches) != 1) or occurrence > len(matches):
        raise ValueError('quote absent or repeated occurrence unresolved')
    start, end = sorted(matches)[(occurrence or 1) - 1]
    mode = matches[start, end]
    return dict(quote=text[start:end], start=start, end=end, mode=mode, model_quote=quote)

def unresolved(kind='absolute'):
    return dict(type=kind, precision='unresolved', timezone=None, timezone_state='absent', interval=None, normalized=None)

def bounds(low, high, precision):
    return dict(type='period', precision=precision, timezone=None, timezone_state='absent', interval=dict(start_inclusive=(low - timedelta(hours=14)).replace(tzinfo=timezone.utc).isoformat(), end_exclusive=(high + timedelta(hours=12)).replace(tzinfo=timezone.utc).isoformat(), basis='stated_precision_unknown_offset_range'), normalized=None)

def parse(text):
    """Parse supported formats only. Unknown/year-less/relative mentions stay saved."""
    value = unresolved()
    s = text.strip()
    s = re.sub('</?strong>', '', s, flags=re.I)
    s = re.sub(f'({MONTH})\\.(?=\\s|$)', '\\1', s, flags=re.I)
    year_first = re.fullmatch(f'({YEAR})-({MONTH})-(\\d{{1,2}})', s, re.I)
    if year_first:
        s = f'{year_first[2]} {year_first[3]}, {year_first[1]}'
    if re.fullmatch(RELATIVE, s, re.I):
        return unresolved('relative')
    direct = evidence.date_interval(s)
    if direct['interval']:
        return dict(type='absolute', normalized=s, **direct)
    try:
        quarter = re.fullmatch(f'(?:Q([1-4])\\s+({YEAR})|({YEAR})\\s+Q([1-4]))', s, re.I)
        if quarter:
            q, year = (int(quarter[1]), int(quarter[2])) if quarter[1] else (int(quarter[4]), int(quarter[3]))
            month = (q - 1) * 3 + 1
            return bounds(datetime(year, month, 1), datetime(year + (month == 10), 1 if month == 10 else month + 3, 1), 'quarter')
        period = re.fullmatch(f'({MONTH})\\s*[–—-]\\s*({MONTH})\\s+({YEAR})', s, re.I)
        if period:
            a, b, year = (MONTHS[period[1].lower()], MONTHS[period[2].lower()], int(period[3]))
            if a > b:
                return unresolved('period')
            return bounds(datetime(year, a, 1), datetime(year + (b == 12), 1 if b == 12 else b + 1, 1), 'month_range')
        numeric = re.fullmatch(f'(\\d{{1,2}})\\s*([/.])\\s*(\\d{{1,2}})\\s*\\2\\s*({YEAR})', s)
        if numeric:
            a, b, year = (int(numeric[1]), int(numeric[3]), int(numeric[4]))
            if a <= 12 and b <= 12 and (a != b):
                return value
            day, month = (a, b) if a > 12 or a == b else (b, a)
            normalized = f'{year:04}-{month:02}-{day:02}'
        else:
            month_first = re.fullmatch(f'({MONTH})\\s+(\\d{{1,2}})(?:st|nd|rd|th)?(?:,?\\s+({YEAR}))?', s, re.I)
            day_first = re.fullmatch(f'(\\d{{1,2}})(?:st|nd|rd|th)?\\s+(?:de\\s+)?({MONTH})(?:\\s+(?:de\\s+)?({YEAR}))?', s, re.I)
            month_year = re.fullmatch(f'({MONTH})\\s+({YEAR})', s, re.I)
            if month_first or day_first:
                match = month_first or day_first
                if not match[3]:
                    return value
                month = MONTHS[match[1 if month_first else 2].lower()]
                day = int(match[2 if month_first else 1])
                year = int(match[3])
                normalized = f'{year:04}-{month:02}-{day:02}'
            elif month_year:
                normalized = f'{int(month_year[2]):04}-{MONTHS[month_year[1].lower()]:02}'
            else:
                return value
        parsed = evidence.date_interval(normalized)
        return dict(type='absolute', normalized=normalized, **parsed)
    except (ValueError, KeyError, OverflowError):
        return value

def mentions(packet):
    rows = []
    for field in packet['allowed_text']:
        visible, positions = visible_text(field['text'])
        for match in SCAN.finditer(visible):
            start, end = (positions[match.start()], positions[match.end() - 1] + 1)
            rows.append(dict(date_id=f'D{len(rows) + 1}', field=field['field'], quote=field['text'][start:end], display_text=match[0], start=start, end=end, position='title' if field['field'] == 'title' else 'snippet_start' if not visible[:match.start()].strip() else 'snippet_body', **parse(match[0])))
    return rows

def quoted_date(packet, support):
    """Fallback for scanner misses, resolved through the unchanged exact-quote owner."""
    errors = []
    texts = {r['field']: r['text'] for r in packet['allowed_text']}
    resolved = evidence.support._resolve_support([support], texts, 'additional_date', errors)
    if errors:
        raise ValueError('; '.join(errors))
    visible, _ = visible_text(resolved[0]['quote'])
    return dict(resolved[0], **parse(visible))

def compare(row, requirement):
    return evidence.compare_interval(dict(date_kind='first_publication', interval=row.get('interval')), requirement)

def replay_mentions(packet, supplied):
    """Reparse saved wire mentions without renumbering or scanning new D IDs."""
    texts = {r['field']: r['text'] for r in packet['allowed_text']}
    rows = []
    for index, source in enumerate(supplied, 1):
        row = deepcopy(source)
        text = texts.get(row['field'])
        if row['date_id'] != f'D{index}' or not isinstance(text, str) or type(row['start']) is not int or (type(row['end']) is not int) or (not 0 <= row['start'] < row['end'] <= len(text)) or (text[row['start']:row['end']] != row['quote']):
            raise ValueError('saved text date identity/span differs')
        visible, _ = visible_text(row['quote'])
        if visible != row['display_text']:
            raise ValueError('saved text date display differs')
        row.update(parse(visible))
        rows.append(row)
    return rows

def linked_rows(rows, anchor=None, parser=None):
    """Compose only the judge's established own-text date links; no date roles."""
    result = deepcopy(rows)
    years = [r for r in result if not r['_native'] and re.fullmatch(YEAR, r.get('normalized') or '')]
    if anchor and anchor.get('normalized'):
        years.append(anchor)
    year_values = {r['normalized'][:4] for r in years}
    notes = []
    if len(year_values) == 1:
        year = next(iter(year_values))
        for row in result:
            if row['_native'] or row.get('interval') or row.get('type') != 'absolute':
                continue
            visible, _ = visible_text(row['quote'])
            parsed = (parser or parse)(f'{visible} {year}')
            if not parsed.get('interval'):
                continue
            if anchor and (not any((re.fullmatch(YEAR, r.get('normalized') or '') for r in years if r is not anchor))) and parsed['normalized'] and (len(anchor['normalized']) >= 7) and (parsed['normalized'][5:7] != anchor['normalized'][5:7]):
                continue
            row.update(parsed)
            notes.append(dict(kind='linked_year_composition', date_id=row.get('date_id'), field=row['field'], quote=row['quote'], normalized=row['normalized'], year_sources=[{k: r[k] for k in ('date_id', 'field', 'quote') if k in r} for r in years]))
    for index, row in enumerate(result):
        interval = row.get('interval')
        if row['_native'] or not interval or row.get('precision') not in {'year', 'month', 'quarter', 'month_range'}:
            continue
        low, high = (datetime.fromisoformat(interval[k]) for k in ('start_inclusive', 'end_exclusive'))
        narrower = [other for j, other in enumerate(result) if j != index and (not other['_native']) and other.get('interval') and (low <= datetime.fromisoformat(other['interval']['start_inclusive'])) and (datetime.fromisoformat(other['interval']['end_exclusive']) <= high) and (datetime.fromisoformat(other['interval']['start_inclusive']) > low or datetime.fromisoformat(other['interval']['end_exclusive']) < high)]
        if narrower:
            row['_redundant'] = True
            notes.append(dict(kind='compatible_broad_date_refined', date_id=row.get('date_id'), field=row['field'], quote=row['quote'], refined_by=[{k: r[k] for k in ('date_id', 'field', 'quote') if k in r} for r in narrower]))
    return (result, notes)
