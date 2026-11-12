"""Opt-in reading of complete quoted dates; frozen scanner and roles stay unchanged."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

from ._method import evidence, text_dates as legacy

PROFILE = 'quoted-date-reading-v1'
SAME_DAY_PROFILE = 'quoted-date-reading-v2'
LATEST_PROFILE = 'quoted-date-reading-v3'
NATIVE_LITERAL_PROFILE = 'quoted-date-reading-v4'
COMPARISON_PROFILES = (SAME_DAY_PROFILE, LATEST_PROFILE, NATIVE_LITERAL_PROFILE)
PROFILES = (PROFILE, *COMPARISON_PROFILES)
M = legacy.MONTH
Y = legacy.YEAR
# Match complete dates only. In particular, no implicit year or locale.
DATE = re.compile(rf"(?:(?<!\d)(?:{Y}[-/]\d{{1,2}}[-/]\d{{1,2}}|\d{{1,2}}\s*[-/.]\s*\d{{1,2}}\s*[-/.]\s*{Y}|\d{{1,2}}\.?[\s-]+{M}\.?[\s-]+{Y})|(?<![^\W\d_]){M}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\s*,?\s*{Y})(?!\d{{2}})", re.I)
CLOCK = re.compile(r'^\s*(?:[,|/]\s*|at\s*|T)?(\d{1,2}):(\d{2})(?::(\d{2})(\.\d{1,6})?)?\s*(am|pm)?(?:\s*(Z|[+-]\d{2}:?\d{2}|UTC|GMT|CET/CEST|CEST|CET|EDT|EST|PDT|PST|ET|PT))?(?!\w)', re.I)
OFFSETS = {'Z': (0,), 'UTC': (0,), 'GMT': (0,), 'CET': (60,), 'CEST': (120,), 'CET/CEST': (60,120), 'EDT': (-240,), 'EST': (-300,), 'PDT': (-420,), 'PST': (-480,), 'ET': (-300,-240), 'PT': (-480,-420)}


def _days(token):
    s = re.sub(r'(?<=\w)\.(?=\s)', '', token.strip())
    s = re.sub(r'\s*,\s*', ', ', s)
    numeric = re.fullmatch(r'(\d{1,4})\s*([-/.])\s*(\d{1,2})\s*\2\s*(\d{1,4})', s)
    if numeric:
        a, b, c = int(numeric[1]), int(numeric[3]), int(numeric[4])
        if len(numeric[1]) == 4:
            dates = [(a,b,c)]; order = 'year_first'
        elif len(numeric[4]) == 4:
            dates = [(c,b,a)] if a > 12 or a == b else [(c,a,b)] if b > 12 else [(c,a,b),(c,b,a)]
            order = 'day_first' if a > 12 else 'month_first' if b > 12 else 'ambiguous'
        else:
            return [], None
        result=[]
        for year, month, day in dates:
            try:
                normalized=datetime(year,month,day).date().isoformat()
                if normalized not in result:result.append(normalized)
            except ValueError:return [], None
        return result, order
    s = re.sub(r'(?<=\d)[.-](?=[A-Za-z])|(?<=[A-Za-z])[.-](?=\d)', ' ', s)
    parsed=legacy.parse(s)
    if parsed.get('interval') and parsed.get('precision')=='day':
        # Month-name forms are normalized by the existing reader.
        normalized=parsed.get('normalized')
        if normalized and re.fullmatch(r'\d{4}-\d{2}-\d{2}', normalized):return [normalized], None
    return [], None


def _reading(day, tail):
    clock=CLOCK.match(tail)
    if not clock:
        if re.search(r'^\s*(?:[,|/]\s*|at\s*|T)?\d{1,2}(?:[.:]\d{2}|\s*(?:am|pm)\b)',tail,re.I):raise ValueError('unsupported clock syntax')
        return dict(type='absolute', normalized=day, **evidence.date_interval(day))
    remainder=tail[clock.end():]
    if remainder.startswith(':') or re.match(r'^\.\d',remainder):
        raise ValueError('incomplete or malformed clock continuation')
    if re.match(r'^\s*(?:[+-]\d|[ap]\.\s*m\.|/[A-Za-z]{2,5})',remainder,re.I):
        raise ValueError('unsupported clock or timezone suffix')
    hour,minute,second=int(clock[1]),int(clock[2]),int(clock[3]or 0)
    if clock[5]:
        if not 1<=hour<=12:raise ValueError('invalid twelve-hour clock')
        hour=hour%12+(12 if clock[5].lower()=='pm' else 0)
    fraction=clock[4]or ''
    stamp=f'{day}T{hour:02}:{minute:02}:{second:02}{fraction}' if clock[3] else f'{day}T{hour:02}:{minute:02}'
    zone=(clock[6]or '').upper()
    if zone and zone not in OFFSETS:
        sign=1 if zone[0]=='+'else -1
        if int(zone[-2:])>=60:raise ValueError('invalid offset minutes')
        offset=sign*(int(zone[1:3])*60+int(zone[-2:]))
        if abs(offset)>14*60:raise ValueError('invalid offset')
        offsets=(offset,)
    else:offsets=OFFSETS.get(zone)
    if offsets:
        readings=[]
        for offset in offsets:
            token=f'{"+"if offset>=0 else "-"}{abs(offset)//60:02}:{abs(offset)%60:02}'
            readings.append(evidence.date_interval(stamp+token))
        if any(not r.get('interval')for r in readings):raise ValueError('invalid clock')
        result=deepcopy(readings[0]);result['timezone']=zone
        if len(readings)>1:
            result['timezone_state']='ambiguous'
            result['interval']['start_inclusive']=min(r['interval']['start_inclusive']for r in readings)
            result['interval']['end_exclusive']=max(r['interval']['end_exclusive']for r in readings)
    else:result=evidence.date_interval(stamp)
    if not result.get('interval'):raise ValueError('invalid clock')
    return dict(type='absolute',normalized=stamp,**result)


def parse(text):
    """Keep every resolved legacy reading; otherwise require one distinct full date."""
    old=legacy.parse(text)
    if old.get('interval') or old.get('type')=='relative':return old
    visible,_=legacy.visible_text(text)
    # Markdown escaping changes display only, never the source quote or its span.
    visible=re.sub(r'\\([./-])',r'\1',visible)
    matches=list(DATE.finditer(visible))
    if not matches:return old
    readings=[];orders=[]
    try:
        for match in matches:
            days,order=_days(match[0])
            if not days:return old
            current=[dict(_reading(day,visible[match.end():]),reading_profile=PROFILE)for day in days]
            signature={(r['normalized'],r.get('timezone'),str(r.get('interval')))for r in current}
            if readings and signature!={(r['normalized'],r.get('timezone'),str(r.get('interval')))for r in readings}:return old
            readings=current;orders.append(order)
    except (ValueError,OverflowError):return old
    if len(readings)==1:result=readings[0]
    else:result=dict(legacy.unresolved(),type='ambiguous_numeric',alternatives=readings)
    return dict(result,reading_profile=PROFILE,quoted_context=visible,numeric_order=orders[0]if len(set(orders))==1 else None)


def native_reading_rows(rows, admitted_fields, *, profile=None):
    """Private checker view only; never replace retained evidence or model inputs.

    The v4 fallback reads a complete, unambiguous date-only literal from an
    explicitly admitted publication field. Existing interval readings and all
    native roles, raw values and mapping identities are preserved.
    """
    result = deepcopy(rows)
    if profile != NATIVE_LITERAL_PROFILE or admitted_fields is None:
        return result
    for row in result:
        raw = row.get('raw_value')
        if (row.get('field') not in admitted_fields or row.get('interval')
                or row.get('date_kind') not in {'first_publication', 'publication_unspecified'}
                or not isinstance(raw, str) or not DATE.fullmatch(raw.strip())):
            continue
        reading = parse(raw.strip())
        if reading.get('interval') and reading.get('precision') == 'day' and not reading.get('alternatives'):
            for key in ('interval', 'precision', 'timezone', 'timezone_state'):
                row[key] = deepcopy(reading.get(key))
    return result


def native_reading_packet(packet, admitted_fields, *, profile=None):
    result = deepcopy(packet)
    result['date_observations'] = native_reading_rows(
        packet['date_observations'], admitted_fields, profile=profile)
    return result


class Reader:
    """Per-decode context; no global monkeypatches or changed model inputs."""
    def __init__(self, enabled=True, captured_at=None, *, profile=LATEST_PROFILE):
        if profile not in PROFILES:raise ValueError('unknown date-reading profile')
        self.profile=profile
        self.enabled=enabled;self.captured_at=captured_at;self.comparisons={};self.readings=[]

    def replay(self, packet, supplied):
        from .judging import replay_mentions
        rows=replay_mentions(packet,supplied)
        if self.enabled:
            for row in rows:
                candidate=parse(row['display_text'])
                if candidate!=legacy.parse(row['display_text']):self.readings.append(dict(field=row['field'],quote=row['quote'],date_id=row['date_id'],reading=deepcopy(candidate)))
                row.update(candidate)
        return rows

    def quoted_date(self, packet, quote):
        row=legacy.quoted_date(packet,quote)
        if self.enabled:
            visible,_=legacy.visible_text(row['quote']);candidate=parse(visible)
            if candidate!=legacy.parse(visible):self.readings.append(dict(field=row['field'],quote=row['quote'],date_id=None,reading=deepcopy(candidate)))
            row.update(candidate)
        return row

    def linked_rows(self, rows, anchor=None):
        return legacy.linked_rows(rows,anchor,parser=parse if self.enabled else None)

    @staticmethod
    def calendar(row, calendar_days):
        return calendar_days and not (row.get('reading_profile')==PROFILE and row.get('precision')not in {'day','month','year'})

    def cutoff(self, row, cutoff, calendar_days):
        states=[]
        for choice in row.get('alternatives')or [row]:
            interval=evidence.comparison_interval(choice,self.calendar(choice,calendar_days))
            if not interval:states.append('not_demonstrated');continue
            low,high=(datetime.fromisoformat(interval[k])for k in ('start_inclusive','end_exclusive'))
            states.append('pass'if high<=cutoff else 'fail'if low>=cutoff else 'unknown')
        return states[0]if len(set(states))==1 else 'unknown'

    def compare(self, row, requirement, calendar_days, *, key, rows, expected, item_natives=(), context_support=()):
        def status(r):
            # Newly read clock precision is retained even at a day window boundary.
            calendar=self.calendar(r,calendar_days)
            return evidence.compare_interval(dict(r,date_kind='first_publication'),requirement,calendar)
        alternatives=row.get('alternatives')
        hints=[];witness_refs=[]
        if not alternatives:result=status(row)
        else:
            states=[status(a)for a in alternatives]
            if len(set(states))==1:result=states[0];hints=['same_window_result']
            else:
                selections=[]
                if expected=='first_publication' and self.captured_at:
                    try:
                        capture=datetime.fromisoformat(self.captured_at.replace('Z','+00:00'))
                        if capture.tzinfo is not None:
                            allowed={i for i,a in enumerate(alternatives)if datetime.fromisoformat(a['interval']['start_inclusive'])<=capture+timedelta(days=1)}
                            if len(allowed)<len(alternatives):selections.append(allowed);hints.append('not_after_capture')
                    except (ValueError,TypeError):pass
                for other in rows:
                    interval=other.get('interval')
                    if not interval or other is row:continue
                    # Only established same-link witnesses; a page-wide format hint
                    # cannot establish the order of a different candidate or source.
                    if other.get('_native'):
                        if expected!='first_publication' or not other.get('_reviewed_or_first_publication'):continue
                        low,high=(datetime.fromisoformat(interval[k])for k in ('start_inclusive','end_exclusive'))
                        if high-low>timedelta(days=3):continue
                        chosen={i for i,a in enumerate(alternatives)if low<datetime.fromisoformat(a['interval']['end_exclusive']) and datetime.fromisoformat(a['interval']['start_inclusive'])<high}
                        if len(chosen)==1:selections.append(chosen);hints.append('same_link_native_match')
                    elif other.get('field')==row.get('field') and other.get('numeric_order')in {'day_first','month_first'}:
                        target=other['numeric_order'];chosen={0 if target=='month_first'else 1}
                        selections.append(chosen);hints.append('same_link_numeric_order')
                # A witness disambiguates the printed day, not the date's role.
                # Same-link text is candidate-bound. Item native dates are only
                # order hints for an already-established publication text link.
                if self.profile in COMPARISON_PROFILES:
                    witnesses=[(o,'same_link_day_match')for o in rows if o is not row and not o.get('_native')]
                    if self.profile in {LATEST_PROFILE, NATIVE_LITERAL_PROFILE}:
                        for span in context_support:
                            visible,_=legacy.visible_text(span['quote']);reading=parse(visible)
                            witnesses.append((dict(reading,field=span['field'],quote=span['quote'],start=span['start'],end=span['end']),'same_link_context_day_match'))
                    if expected=='first_publication':
                        witnesses += [(o,'same_item_native_day_match')for o in item_natives
                                      if o.get('date_kind')in {'first_publication','publication_unspecified','unclear'}]
                        witnesses += [(o,'same_link_day_match')for o in rows if o is not row and o.get('_native')
                                      and o.get('date_kind')in {'first_publication','publication_unspecified','unclear'}]
                    def days(interval):
                        low=datetime.fromisoformat(interval['start_inclusive'])
                        high=datetime.fromisoformat(interval['end_exclusive'])-timedelta(microseconds=1)
                        return {(low+timedelta(days=i)).date()for i in range((high.date()-low.date()).days+1)}
                    witness_refs=[]
                    for witness,hint in witnesses:
                        interval=witness.get('interval')
                        if not interval or witness.get('alternatives')or witness.get('precision')not in {'day','hour','minute','second','fractional_second'}:continue
                        low,high=(datetime.fromisoformat(interval[k])for k in ('start_inclusive','end_exclusive'))
                        if high-low>timedelta(days=3):continue
                        chosen={i for i,a in enumerate(alternatives)if days(a['interval']) & days(interval)}
                        if len(chosen)==1:
                            selections.append(chosen);hints.append(hint)
                            witness_refs.append(dict(hint=hint,field=witness.get('field'),quote=witness.get('quote'),date_kind=witness.get('date_kind'),interval=deepcopy(interval),selected_alternative=next(iter(chosen))))
                remaining=set.intersection(*selections)if selections else set(range(len(alternatives)))
                result=states[next(iter(remaining))]if len(remaining)==1 else 'unknown'
        self.comparisons.setdefault(key,[]).append(dict(status=result,precision=row.get('precision'),native=bool(row.get('_native')),date_id=row.get('date_id'),field=row.get('field'),quote=row.get('quote'),alternatives=[status(a)for a in alternatives]if alternatives else [],hints=hints,**(dict(witnesses=witness_refs)if self.profile in COMPARISON_PROFILES else {}),redundant=bool(row.get('_redundant'))))
        return result
