

from calendar import monthrange
from datetime import datetime, timedelta, timezone
import re
from . import two_step as two
VERSION = 'news-relative-native-dates-v1'
UNCERTAINTY = 'Provider rounding and reference time are unverified; this is not an exact date or a verified interval.'
INSTRUCTIONS = 'native_date_estimates are approximate calendar context for the referenced native N IDs, computed assuming their relative labels refer to response collection time. Original values and reviewed meanings remain in native_metadata/native_date_ids. These estimates are not quotable returned evidence, verified intervals, publication/event roles or historical availability proof. Do not promote unclear dates or establish pass/fail timing from an estimate alone. Missing/unsupported anchors remain unresolved. Relative wording inside title/snippet still needs its own explicit returned-evidence anchor, not collection time.'
WORDS = dict(a=1, an=1, one=1, two=2, three=3, four=4, five=5, six=6, seven=7, eight=8, nine=9, ten=10, eleven=11, twelve=12)
AGO = re.compile('(?:about\\s+)?(\\d+|' + '|'.join(WORDS) + ')\\s+(seconds?|minutes?|hours?|days?|weeks?|months?|years?)\\s+ago', re.I)

def estimate(value, captured_at):
    """Return a labeled point estimate; None means no relative native value found."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    match = AGO.fullmatch(text)
    if not match and text not in {'today', 'yesterday', 'just now'}:
        if not re.search('\\b(ago|last|today|yesterday|tomorrow|now)\\b', text):
            return None
        return dict(state='unresolved', reason='unsupported_relative_expression')
    unresolved = dict(state='unresolved', reason='missing_or_invalid_collection_anchor')
    try:
        anchor = datetime.fromisoformat(captured_at.replace('Z', '+00:00'))
        if anchor.tzinfo is None:
            return unresolved
        anchor = anchor.astimezone(timezone.utc)
        if match:
            number = int(match[1]) if match[1].isdigit() else WORDS[match[1]]
            unit = match[2].rstrip('s')
        else:
            number, unit = (1, 'day') if text == 'yesterday' else (0, 'day')
        if unit in {'month', 'year'}:
            month_index = anchor.year * 12 + anchor.month - 1 - number * (12 if unit == 'year' else 1)
            year, month = divmod(month_index, 12)
            month += 1
            point = anchor.replace(year=year, month=month, day=min(anchor.day, monthrange(year, month)[1]))
        else:
            point = anchor - timedelta(**{unit + 's': number})
    except (ValueError, TypeError, AttributeError, OverflowError):
        return unresolved
    key = 'approximate_datetime_utc' if unit in {'second', 'minute', 'hour'} else 'approximate_date_utc'
    value = point.replace(microsecond=0).isoformat() if key == 'approximate_datetime_utc' else point.date().isoformat()
    return dict(state='estimated', **{key: value}, basis='estimated_from_collection_time', uncertainty=UNCERTAINTY)

def input_for(packet, captured_at, calendar_days=False):
    """Extend the existing projection only; raw packet/date intervals remain unchanged."""
    result = two.input_for(packet, calendar_days=calendar_days)
    result['input_revision'] = VERSION
    result['native_date_estimates'] = []
    for index, row in enumerate(packet['date_observations']):
        derived = estimate(row.get('raw_value'), captured_at)
        if derived is not None:
            result['native_date_estimates'].append(dict(date_id=f'N{index + 1}', **derived))
    return result

def prompt_for(packet):
    return two.prompt_for(packet) + '\n' + INSTRUCTIONS + '\n'
