"""Deadline parsing and urgency tiering for open Google Tasks. Pure functions, no IO."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import re


# Order is display priority. 'stale' and 'undated' never trigger a nudge.
TIERS = ('overdue', 'today', 'week', 'later', 'stale', 'undated')
WEEKDAYS = '월화수목금토일'


class Decision(Exception):
    """Not a failure: the caller has to choose. Carries a JSON-ready payload."""

    def __init__(self, payload):
        super().__init__(payload.get('status', 'decision'))
        self.payload = payload

# Without a year only "9/25" and "9월 25일" count: "1.5" is a version and "2-3" a range of chapters.
_DATE = (r'(?<![\dA-Za-z./-])(?:(?P<y>20\d{2})\s*(?:[./-]|년)\s*(?P<m>\d{1,2})\s*(?:[./-]|월)'
         r'|(?P<m2>\d{1,2})\s*(?:/|월))\s*(?P<d>\d{1,2})(?![\d%])\s*일?\.?')
_WEEKDAY = r'(?:\s*\(\s*[월화수목금토일]\s*(?:요일)?\s*\))?'
_TIME = (r'(?:\s*(?:오전|오후|낮|밤|정오|자정)?\s*'
         r'(?:\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?|\d{1,2}:\d{2})?)?')
# "9월 18일 오전 10시까지", "5/11(월)까지": the date must be closed by 까지.
_UNTIL = re.compile(_DATE + _WEEKDAY + _TIME + r'\s*까지')
# "(~5/11(월))": a leading tilde marks a deadline unless it closes a range like 9/15 ~ 9/21.
# `re` has no variable-width lookbehind, so the range opener is captured and rejected later.
_TILDE = re.compile(r'(?P<pre>[\d)일]\s*)?[~∼〜～]\s*' + _DATE)


def _resolve(match, anchor):
    """Turn a regex match into a date, inferring a missing year from the anchor day."""
    try:
        month, day = int(match['m'] or match['m2']), int(match['d'])
        if match['y']:
            return date(int(match['y']), month, day)
        candidate = date(anchor.year, month, day)
        # A title written in December about "1/15까지" means next January.
        if candidate < anchor - timedelta(days=60):
            candidate = date(anchor.year + 1, month, day)
        return candidate
    except ValueError:
        return None


def title_deadline(title, anchor):
    """Earliest explicitly marked deadline in a title, or None.

    Only "…까지" and a leading "~" mark a deadline. A bare date such as
    "10월 27일(화) 웨비나" is an event day, not a deadline, and is ignored.
    """
    found = []
    # Collapsed and bounded first: the optional-whitespace groups backtrack badly on padded input.
    text = ' '.join((title or '').split())[:400]
    for pattern in (_UNTIL, _TILDE):
        for match in pattern.finditer(text):
            if match.groupdict().get('pre'):
                continue
            resolved = _resolve(match, anchor)
            if resolved:
                found.append(resolved)
    return min(found) if found else None


def _anchor(task, today):
    try:
        return datetime.fromisoformat(task.get('updated', '').replace('Z', '+00:00')).date()
    except ValueError:
        return today


def effective_due(task, today):
    """(iso_date, source). The earlier of the Google date and the title deadline wins.

    Taking the minimum is deliberate: dragging a task past the deadline written in its
    own title must not silence the reminder.
    """
    google = task.get('due') or ''
    parsed = title_deadline(task.get('title', ''), _anchor(task, today))
    written = parsed.isoformat() if parsed else ''
    if google and written:
        return (google, 'google') if google <= written else (written, 'title')
    if google:
        return google, 'google'
    return (written, 'title') if written else ('', '')


def week_end(today, upcoming_days=3):
    """Sunday of this week, but never closer than the upcoming window (Friday sees Monday)."""
    sunday = today + timedelta(days=6 - today.weekday())
    return max(sunday, today + timedelta(days=upcoming_days))


def tier_of(due, today, horizon, stale_days=14):
    if not due:
        return 'undated'
    day = date.fromisoformat(due)
    if day < today - timedelta(days=stale_days):
        return 'stale'
    if day < today:
        return 'overdue'
    if day == today:
        return 'today'
    return 'week' if day <= horizon else 'later'


def classify(tasks, today, upcoming_days=3, stale_days=14):
    """Group task dicts by tier. Each returned task gains 'eff_due' and 'due_source'."""
    horizon = week_end(today, upcoming_days)
    groups = {name: [] for name in TIERS}
    for task in tasks:
        due, source = effective_due(task, today)
        groups[tier_of(due, today, horizon, stale_days)].append(task | {'eff_due': due, 'due_source': source})
    groups['overdue'].sort(key=lambda t: (t['eff_due'], t['key']), reverse=True)
    for name in ('week', 'later'):
        groups[name].sort(key=lambda t: (t['eff_due'], t['key']))
    groups['stale'].sort(key=lambda t: (t['eff_due'], t['key']), reverse=True)
    for name in ('today', 'undated'):
        groups[name].sort(key=lambda t: t['key'])
    return groups


def critical(groups):
    """Tasks that must be closed before the end of today."""
    return groups['overdue'] + groups['today']


def recently_added_undated(groups, now, days=3):
    """Undated tasks touched within the window: captured by voice or chat and still lacking a date."""
    fresh = []
    for task in groups['undated']:
        try:
            touched = datetime.fromisoformat(task.get('updated', '').replace('Z', '+00:00'))
        except ValueError:
            continue
        if timedelta(0) <= now - touched <= timedelta(days=days):
            fresh.append((touched, task))
    return [task for _, task in sorted(fresh, key=lambda pair: pair[0], reverse=True)]


def day_label(iso, today):
    """'9/23(수)' plus how far it is from today."""
    day = date.fromisoformat(iso)
    delta = (day - today).days
    if delta == 0:
        distance = '오늘'
    elif delta == 1:
        distance = '내일'
    elif delta > 1:
        distance = f'D-{delta}'
    else:
        distance = f'{-delta}일 경과'
    return f'{day.month}/{day.day}({WEEKDAYS[day.weekday()]}) · {distance}'
