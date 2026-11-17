"""Keep original provider close prices for the unsold-equivalent shares of T cycles."""
from datetime import time
from pathlib import Path
import re

from action_center import CN, positive
from quote_health import parse_provider_time
from strategy_runtime import read, write


def _path(output, day):
    return Path(output) / 't_close_marks' / (day + '.json')


def _mark(quote, day, now):
    stamp = parse_provider_time(quote.get('source_timestamp'))
    price = positive(quote.get('price'))
    if stamp is None or stamp > now or price is None:
        return None
    local = stamp.astimezone(CN)
    if local.date().isoformat() != day or local.time() < time(15):
        return None
    return {'price': price, 'source_timestamp': local.isoformat(), 'source': quote.get('source')}


def load_marks(output, day, now, display_snapshot=None):
    """Never substitute a different day's mark or infer a timestamp from a candle."""
    archived = read(_path(output, day))
    candidates = archived if isinstance(archived, dict) else {}
    marks = {code: mark for code, quote in candidates.items() if isinstance(quote, dict)
             and (mark := _mark(quote, day, now))}
    snapshot = display_snapshot or {}
    if snapshot.get('historical') is True:
        for quote in snapshot.get('quotes', []):
            code = str(quote.get('code', ''))
            marked = _mark({**quote, 'source': snapshot.get('source')}, day, now)
            if marked and code not in marks:
                marks[code] = marked
    return marks


def quote_targets(book, day):
    """Only request a T code with actual shares sold and still unmatched."""
    targets = set()
    for row in book.get('cycles', []):
        code, kind = row.get('code'), row.get('kind')
        remaining = row.get('unbought_quantity')
        if (row.get('date') == day and isinstance(code, str) and re.fullmatch(r'\d{6}', code)
                and kind in ('stock', 'etf') and type(remaining) is int and remaining > 0):
            targets.add((kind, code))
    return targets


def capture_marks(output, day, now, targets, feed, source):
    """Persist available close quotes independently of full portfolio validity."""
    original = load_marks(output, day, now)
    marks = dict(original)
    for kind, code in sorted(targets):
        raw = feed.get((kind, code))
        if not raw or len(raw) < 4:
            continue
        mark = _mark({'price': raw[0], 'source_timestamp': raw[3], 'source': source}, day, now)
        current = marks.get(code)
        if mark and (not current or parse_provider_time(mark['source_timestamp'])
                     >= parse_provider_time(current['source_timestamp'])):
            marks[code] = mark
    if marks != original:
        write(_path(output, day), marks)
