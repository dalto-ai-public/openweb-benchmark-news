"""Opt-in, mapped inline presentation repair; raw evidence is never changed."""
from contextlib import contextmanager
from contextvars import ContextVar
import re

PROFILE = "markdown-escapes-v1"
SPACE_PROFILE = "unicode-spaces-v1"
PROFILES = ("raw-v1", PROFILE, SPACE_PROFILE)
# Unicode space separators only: no line breaks, tabs, zero-width characters,
# punctuation or compatibility normalization. One input character stays one.
_SPACES = frozenset('\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000')
_profile = ContextVar("news_text_representation", default="raw-v1")
_TOKEN = re.compile(r"\\[!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~]")


@contextmanager
def selected(profile):
    if profile not in PROFILES:
        raise ValueError("unknown text representation profile")
    token = _profile.set(profile)
    try:
        yield
    finally:
        _profile.reset(token)


def mapped(text):
    """Return visible characters with exact raw start/end indices.

    Only Markdown punctuation escapes are supported. No HTML, whitespace folding
    or fuzzy wording. Existing literal/bold/link matching has priority.
    """
    chars, spans, cursor = [], [], 0
    for token in _TOKEN.finditer(text):
        for i in range(cursor, token.start()):
            chars.append(text[i]); spans.append((i, i + 1))
        raw = token.group()
        replacement = raw[1:]
        for char in replacement:
            chars.append(char); spans.append((token.start(), token.end()))
        cursor = token.end()
    for i in range(cursor, len(text)):
        chars.append(text[i]); spans.append((i, i + 1))
    return "".join(chars), spans


def _find(visible, indices, quote, mode, check):
    found, start = {}, 0
    while True:
        offset = visible.find(quote, start)
        if offset < 0:
            break
        low, high = indices[offset][0], indices[offset + len(quote) - 1][1]
        # Never accept a partial expansion of one raw entity.
        if check(low, high):
            found[low, high] = mode
        start = offset + 1
    return found


def space_equivalent(text):
    return ''.join(' ' if char in _SPACES else char for char in text)


def matches(text, quote):
    profile = _profile.get()
    if profile == 'raw-v1':
        return {}
    visible, indices = mapped(text)
    escaped = _find(visible, indices, quote, PROFILE + '_to_raw_span',
                    lambda low, high: mapped(text[low:high])[0] == quote)
    if profile == PROFILE:
        return escaped
    from ._method.text_dates import visible_text
    plain, positions = visible_text(text)
    # Extend failed matching only; preserve every historical successful span and
    # occurrence choice, including a literal match elsewhere in the same text.
    if quote in text or quote in plain or escaped:
        return escaped
    query = space_equivalent(quote)
    views = [(visible, indices), (plain, [(i, i + 1) for i in positions])]
    # Compose the existing emphasis/link and escape mappings, keeping raw spans.
    combined, mapping = mapped(plain)
    views.append((combined, [(positions[low], positions[high - 1] + 1) for low, high in mapping]))
    result = {}
    for view, offsets in views:
        normalized = space_equivalent(view)
        result.update(_find(normalized, offsets, query, SPACE_PROFILE + '_to_raw_span',
                            lambda low, high: (space_equivalent(text[low:high]) != text[low:high]
                                              or query != quote)))
    return result
