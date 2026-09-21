"""Calendar, mail and credential-age reads through gog.

Everything here is fail-soft: a missing scope, a timeout or a malformed response
yields None so that the Tasks reminder itself is never blocked by a side source.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import re

import tiers


MAIL_QUERY = 'is:unread is:important newer_than:3d'
DEADLINE_WORDS = re.compile(r'마감|기한|까지|제출|회신|deadline|due\b|submission|reminder', re.IGNORECASE)


def _moment(value, zone):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(zone)


def normalize_event(raw, zone):
    """One calendar row in local time, or None for rows that must not be shown."""
    if not isinstance(raw, dict) or raw.get('status') == 'cancelled':
        return None
    for person in raw.get('attendees') or []:
        if isinstance(person, dict) and person.get('self') and person.get('responseStatus') == 'declined':
            return None
    start, end = raw.get('start') or {}, raw.get('end') or {}
    try:
        if start.get('dateTime'):
            begin = _moment(start['dateTime'], zone)
            finish = _moment(end['dateTime'], zone) if end.get('dateTime') else begin
            all_day, first, last = False, begin.date(), finish.date()
        else:
            first = date.fromisoformat(start['date'])
            # Google's all-day end date is exclusive.
            last = date.fromisoformat(end['date']) - timedelta(days=1) if end.get('date') else first
            all_day, begin, finish = True, None, None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    organizer = raw.get('organizer') or {}
    return {'title': ' '.join(str(raw.get('summary') or '(제목 없음)').split()), 'all_day': all_day,
            'start': begin, 'end': finish, 'first_day': first, 'last_day': max(first, last),
            'calendar': '' if organizer.get('self') else str(organizer.get('displayName') or '')}


def fetch_events(cfg, now, read, days=7):
    """Upcoming events sorted by start, or None when the calendar cannot be read."""
    try:
        data = read(cfg, ['calendar', 'events', '--days', str(days), '--all', '--max', '250'])
        rows = data.get('events')
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            return None
    except Exception:
        return None
    hidden = [name.casefold() for name in cfg.get('calendar_exclude', []) if name]
    events, seen = [], set()
    for raw in rows:
        event = normalize_event(raw, now.tzinfo)
        if not event or any(name in event['calendar'].casefold() for name in hidden):
            continue
        # --all repeats an event once per calendar that carries it.
        identity = (raw.get('iCalUID') or raw.get('id') or event['title'], event['start'] or event['first_day'])
        if identity not in seen:
            seen.add(identity)
            events.append(event)
    events.sort(key=lambda e: (e['first_day'], not e['all_day'], e['start'] or now, e['title']))
    return events


def events_on(events, day, now=None):
    """Events touching a day. Multi-day all-day rows show only on their first and last day.

    With `now`, timed events that already ended are dropped.
    """
    chosen = []
    for event in events or []:
        if event['all_day']:
            if day in (event['first_day'], event['last_day']):
                chosen.append(event)
        elif event['first_day'] <= day <= event['last_day']:
            if now is None or event['end'] > now:
                chosen.append(event)
    return chosen


def create_calendar_event(cfg, title, start_dt, end_dt, calendar_id='primary', description='', dry_run=False, read=None):
    """Create an event on Google Calendar using gog."""
    from agent import gog_json
    read_fn = read or gog_json
    command = [
        'calendar', 'create', calendar_id,
        '--summary', title,
        '--from', start_dt.isoformat(),
        '--to', end_dt.isoformat(),
    ]
    if description:
        command += ['--description', description]
    if dry_run:
        command.append('--dry-run')
    res = read_fn(cfg, command)
    if dry_run:
        return {'status': 'dry_run', 'title': title, 'start': start_dt.isoformat(), 'end': end_dt.isoformat(),
                'calendar': calendar_id}
    event = res.get('event', res)
    return {
        'status': 'created',
        'id': event.get('id', ''),
        'htmlLink': event.get('htmlLink', ''),
        'summary': event.get('summary', title),
        'start': start_dt.isoformat(),
        'end': end_dt.isoformat(),
        'calendar': calendar_id,
    }


def delete_calendar_event(cfg, event_id, calendar_id='primary', read=None):
    """Delete an event on Google Calendar using gog."""
    from agent import gog_json
    read_fn = read or gog_json
    command = ['calendar', 'delete', calendar_id, event_id, '--force']
    return read_fn(cfg, command)


def fetch_mail(cfg, now, read):
    """Unread important threads with a deadline hint, or None when mail cannot be read."""
    try:
        data = read(cfg, ['gmail', 'search', cfg.get('mail_query') or MAIL_QUERY, '--max', '15'])
        rows = data.get('threads')
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            return None
    except Exception:
        return None
    threads = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        subject = ' '.join(str(raw.get('subject') or '(제목 없음)').split())
        deadline = tiers.title_deadline(subject, now.date())
        sender = re.sub(r'\s*<[^>]*>', '', str(raw.get('from') or '')).strip().strip('"')
        threads.append({'subject': subject, 'from': sender,
                        'deadline': deadline.isoformat() if deadline else '',
                        'flagged': bool(deadline or DEADLINE_WORDS.search(subject))})
    return {'threads': threads, 'more': bool(data.get('nextPageToken'))}


def fetch_credential_age(cfg, now, read):
    """When the stored Google credential was issued, or None if unknown."""
    try:
        data = read(cfg, ['auth', 'list'])
        for row in data.get('accounts') or []:
            if isinstance(row, dict) and str(row.get('email', '')).casefold() == cfg['account'].casefold():
                created = row.get('created_at') or row.get('createdAt') or row.get('created')
                return _moment(created, now.tzinfo) if created else None
    except Exception:
        return None
    return None


def gather(cfg, now, read, skip=False):
    """Side context for one brief. Disabled, skipped or failing sources come back as None."""
    if skip:
        return {'events': None, 'mail': None, 'issued': None}
    return {
        'events': fetch_events(cfg, now, read) if cfg.get('calendar') else None,
        'mail': fetch_mail(cfg, now, read) if cfg.get('mail') else None,
        'issued': fetch_credential_age(cfg, now, read) if cfg.get('oauth_ttl_days') else None,
    }
