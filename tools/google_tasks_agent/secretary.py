#!/usr/bin/env python3
"""Conversational front door for the Google Tasks reminder: capture, close, reschedule, brief.

The reminder daemon (agent.py) stays read-only. Every write to Google Tasks goes through
this file, is limited to add / complete / set-date / retitle, and never deletes anything.
Exit codes: 0 done, 1 error, 2 a decision is needed (duplicate, ambiguous, not found).
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

import agent
import brief
import scheduler
import sources
import tiers


SNAPSHOT_MAX_AGE = timedelta(minutes=20)
NO_DATE_WORDS = ('none', '없음', '미정', '날짜없음')
DAY_WORDS = {'today': 0, '오늘': 0, '금일': 0, 'tomorrow': 1, '내일': 1, '명일': 1, '모레': 2}
WEEKDAY_WORDS = {name: index for index, names in enumerate(
    (('mon', '월'), ('tue', '화'), ('wed', '수'), ('thu', '목'), ('fri', '금'), ('sat', '토'), ('sun', '일')))
    for name in names}

Decision = tiers.Decision


def normal(title):
    return re.sub(r'[\W_]+', '', (title or '').casefold())


def parse_day(value, today):
    """ISO date, a relative word, '+3d', a weekday (its next occurrence, today included), or 'this-week'."""
    word = value.strip().casefold()
    if word in DAY_WORDS:
        return today + timedelta(days=DAY_WORDS[word])
    if re.fullmatch(r'\+\d{1,3}d', word):
        return today + timedelta(days=int(word[1:-1]))
    if word in ('this-week', '이번주', '이번 주', '금주'):
        # Said out loud it means this Friday, or Sunday once the working week is over.
        return today + timedelta(days=(4 if today.weekday() <= 4 else 6) - today.weekday())
    key = word.removesuffix('요일')
    if key in WEEKDAY_WORDS:
        return today + timedelta(days=(WEEKDAY_WORDS[key] - today.weekday()) % 7)
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError('due_must_be_YYYY-MM-DD_or_today_tomorrow_+Nd_weekday_this-week') from None


def policy_day(policy, today):
    """Date for a capture that named none. 'this-week' is the nearest Friday at least two days away,
    so a Thursday-evening or weekend capture is not born due today."""
    if policy == 'none':
        return None
    if policy == 'this-week':
        return today + timedelta(days=(4 - today.weekday() - 2) % 7 + 2)
    return parse_day(policy, today)


def snapshot_tasks(cfg, now):
    """Open tasks from the daemon's last successful read, or None when it is missing or stale."""
    path = Path(cfg['state_dir']) / 'state.sqlite'
    if not path.exists():
        return None
    try:
        db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        try:
            row = db.execute("SELECT value FROM meta WHERE key='last_success'").fetchone()
            if not row or now - datetime.fromisoformat(json.loads(row[0])) > SNAPSHOT_MAX_AGE:
                return None
            return {key: json.loads(payload) for key, payload in db.execute('SELECT key, payload FROM tasks')}
        finally:
            db.close()
    except (sqlite3.Error, ValueError):
        return None


def open_tasks(cfg, now, live=False, fetch=None):
    tasks = None if live else snapshot_tasks(cfg, now)
    return tasks if tasks is not None else (fetch or agent.fetch_tasks)(cfg)[0]


def task_lists(cfg, read):
    data = read(cfg, ['tasks', 'lists', 'list', '--all', '--max', '1000'])
    lists = [row for row in data.get('tasklists') or [] if isinstance(row, dict) and row.get('id')]
    if not lists:
        raise agent.AgentError('google_no_tasklists')
    return lists


def pick_list(lists, wanted):
    if not wanted:
        return lists[0]
    matches = [row for row in lists if normal(row.get('title')) == normal(wanted) or row.get('id') == wanted]
    if len(matches) != 1:
        raise Decision({'status': 'list_not_found', 'wanted': wanted,
                        'available': [row.get('title', '') for row in lists]})
    return matches[0]


def describe(task, today):
    due, source = tiers.effective_due(task, today)
    horizon = tiers.week_end(today)
    return {'key': task['key'], 'title': task['title'], 'list': task.get('list_title', ''),
            'due': due, 'due_source': source, 'due_label': tiers.day_label(due, today) if due else '',
            'tier': tiers.tier_of(due, today, horizon)}


def reminder_plan(cfg, tier, now):
    """Human-readable promise of when this task will be raised again."""
    clock = now.strftime('%H:%M')
    digests = [t for t in sorted(cfg['digest_times']) if t > clock]
    nudges = [t for t in sorted(cfg['nudge_times']) if t > clock]
    if tier in ('overdue', 'today'):
        slots = sorted([f'{t} 마감 점검' for t in nudges] + [f'{t} 브리핑' for t in digests])
        return slots or ['내일 첫 브리핑부터 기한 경과로 표시']
    if tier == 'week':
        return [f'{t} 브리핑의 "이번 주" 구간' for t in digests[:1]] or ['내일 브리핑의 "이번 주" 구간']
    if tier == 'undated':
        return [f'날짜 미정: 등록 후 {cfg["new_undated_days"]}일 동안만 브리핑에 표시되고 이후 백로그로 내려갑니다']
    return ['기한이 이번 주 범위에 들어오면 브리핑에 올라옵니다']


def find(tasks, query):
    wanted = normal(query)
    if query in tasks:
        return [tasks[query]]
    return [task for task in tasks.values() if wanted and wanted in normal(task['title'])]


def one(tasks, query, today):
    matches = find(tasks, query)
    if len(matches) == 1:
        return matches[0]
    exact = [task for task in matches if normal(task['title']) == normal(query)]
    if len(exact) == 1:
        return exact[0]
    raise Decision({'status': 'not_found' if not matches else 'ambiguous', 'query': query,
                    'candidates': [describe(task, today) for task in matches[:10]]})


def add(cfg, now, title, due='', list_name='', notes='', allow_duplicate=False, allow_past=False,
        dry_run=False, read=agent.gog_json, snapshot=None):
    today = now.date()
    title = ' '.join(title.split())
    if not title or len(title) > 1024:
        raise ValueError('title_must_be_1_to_1024_characters')
    # A task captured without a date sinks into the undated backlog and is never raised again,
    # so the configured policy gives it one. Saying 'none' keeps it undated on purpose.
    policy = cfg.get('capture_default_due', 'none')
    if due.strip().casefold() in NO_DATE_WORDS:
        day, defaulted = None, False
    elif due:
        day, defaulted = parse_day(due, today), False
    else:
        day = policy_day(policy, today)
        defaulted = day is not None
    if day and day < today and not allow_past:
        raise ValueError('due_is_in_the_past_use_--allow-past')
    target = pick_list(task_lists(cfg, read), list_name or cfg.get('capture_list', ''))
    if not allow_duplicate:
        # The snapshot may lag by minutes, so the target list is re-read before writing.
        # Other lists are checked against the snapshot, which is good enough to catch an old twin.
        rows = read(cfg, ['tasks', 'list', target['id'], '--all', '--max', '100']).get('tasks') or []
        twins = [{'key': f'{target["id"]}/{row["id"]}', 'title': row.get('title', ''),
                  'list_title': target.get('title', ''), 'due': (row.get('due') or '')[:10],
                  'updated': row.get('updated', '')}
                 for row in rows if isinstance(row, dict) and row.get('id') and row.get('status') == 'needsAction']
        twins += [task for task in (snapshot or {}).values() if task.get('list_id') != target['id']]
        for existing in twins:
            if normal(existing['title']) == normal(title):
                raise Decision({'status': 'duplicate', 'existing': describe(existing, today),
                                'hint': '같은 제목의 미완료 작업이 있습니다. 날짜만 바꾸려면 due, 그래도 새로 만들려면 --allow-duplicate.'})
    command = ['tasks', 'add', target['id'], '--title', title]
    if day:
        command += ['--due', day.isoformat()]
    if notes:
        command += ['--notes', notes]
    if dry_run:
        command.append('--dry-run')
    created = read(cfg, command)
    created = created.get('task', created)
    stored = (created.get('due') or '')[:10]
    status = 'dry_run' if dry_run else 'created'
    if day and stored and stored != day.isoformat():
        status = 'created_but_due_differs'
    tier = tiers.tier_of(day.isoformat() if day else '', today, tiers.week_end(today, cfg['upcoming_days']), cfg['stale_days'])
    return {'status': status, 'id': created.get('id', ''), 'title': title,
            'list': target.get('title', ''), 'due': day.isoformat() if day else '', 'stored_due': stored,
            'due_label': tiers.day_label(day.isoformat(), today) if day else '', 'due_defaulted': defaulted,
            'tier': tier, 'reminders': reminder_plan(cfg, tier, now)}


def complete(cfg, now, query, dry_run=False, read=agent.gog_json, tasks=None):
    # Writes pick their target from a live read: a snapshot that misses a just-added task
    # can turn an ambiguous query into a confident match on the wrong one.
    task = one(tasks if tasks is not None else open_tasks(cfg, now, live=True), query, now.date())
    read(cfg, ['tasks', 'done', task['list_id'], task['id']] + (['--dry-run'] if dry_run else []))
    return {'status': 'dry_run' if dry_run else 'completed', **describe(task, now.date())}


def reschedule(cfg, now, query, due, allow_past=False, dry_run=False, read=agent.gog_json, tasks=None):
    today = now.date()
    day = parse_day(due, today)
    if day < today and not allow_past:
        raise ValueError('due_is_in_the_past_use_--allow-past')
    task = one(tasks if tasks is not None else open_tasks(cfg, now, live=True), query, today)
    read(cfg, ['tasks', 'update', task['list_id'], task['id'], '--due', day.isoformat()]
         + (['--dry-run'] if dry_run else []))
    moved = describe(task | {'due': day.isoformat()}, today)
    result = {'status': 'dry_run' if dry_run else 'rescheduled', **moved,
              'reminders': reminder_plan(cfg, moved['tier'], now)}
    if moved['due_source'] == 'title':
        # The earlier of the two dates rules, so the date written in the title still decides the tier.
        result['title_deadline_earlier'] = moved['due']
        result['hint'] = '제목에 적힌 기한이 새 날짜보다 이릅니다. 제목의 기한을 retitle로 고쳐야 브리핑에서 내려갑니다.'
    return result


def retitle(cfg, now, query, title, dry_run=False, read=agent.gog_json, tasks=None):
    title = ' '.join(title.split())
    if not title or len(title) > 1024:
        raise ValueError('title_must_be_1_to_1024_characters')
    task = one(tasks if tasks is not None else open_tasks(cfg, now, live=True), query, now.date())
    read(cfg, ['tasks', 'update', task['list_id'], task['id'], '--title', title]
         + (['--dry-run'] if dry_run else []))
    return {'status': 'dry_run' if dry_run else 'retitled', 'previous_title': task['title'],
            **describe(task | {'title': title}, now.date())}


def show(cfg, now, scope='full', live=False, read=agent.gog_json):
    tasks = open_tasks(cfg, now, live)
    context = sources.gather(cfg, now, read)
    if scope == 'nudge':
        return brief.nudge(tasks, cfg, now, events=context['events'], issued=context['issued'])[0] \
            or '오늘 일과 종료 전에 끝내야 할 열린 일이 없습니다.'
    text = brief.render(tasks, cfg, now, **context)[0]
    return text.split('\n\n')[0] if scope == 'now' else text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', type=Path, default=agent.DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest='command', required=True)
    adding = commands.add_parser('add', help='register a task; refuses an exact duplicate')
    adding.add_argument('--title', required=True)
    adding.add_argument('--due', default='', help='YYYY-MM-DD, today, tomorrow, +3d, fri, this-week, none; '
                        'omitted = capture_default_due from the config')
    adding.add_argument('--list', default='', dest='list_name')
    adding.add_argument('--notes', default='')
    adding.add_argument('--allow-duplicate', action='store_true')
    closing = commands.add_parser('done', help='complete the single task matching QUERY')
    closing.add_argument('query')
    moving = commands.add_parser('due', help='set the date of the single task matching QUERY')
    moving.add_argument('query')
    moving.add_argument('day')
    naming = commands.add_parser('retitle', help='replace the title of the single task matching QUERY')
    naming.add_argument('query')
    naming.add_argument('title')
    for sub in (adding, moving):
        sub.add_argument('--allow-past', action='store_true')
    for sub in (adding, closing, moving, naming):
        sub.add_argument('--dry-run', action='store_true')
    finding = commands.add_parser('find', help='open tasks whose title contains QUERY')
    finding.add_argument('query')
    briefing = commands.add_parser('brief', help='print the integrated brief without sending it')
    briefing.add_argument('--scope', choices=['full', 'now', 'nudge'], default='full')
    briefing.add_argument('--live', action='store_true', help='re-read Google Tasks instead of the snapshot')
    scheduling = commands.add_parser('schedule', help='schedule a task with a dedicated calendar slot')
    scheduling.add_argument('--title', required=True)
    scheduling.add_argument('--when', required=True, help='e.g. "화요일 오후", "내일 14:00", "수요일 (1시간)"')
    scheduling.add_argument('--duration', default='2h', help='duration (e.g. 1h, 2h, 30m, 90m, default: 2h)')
    scheduling.add_argument('--calendar', default='primary', help='Google Calendar name or ID')
    scheduling.add_argument('--list', default='', dest='list_name', help='Google Tasks list')
    scheduling.add_argument('--notes', default='', help='additional notes')
    scheduling.add_argument('--allow-duplicate', action='store_true')
    scheduling.add_argument('--dry-run', action='store_true')
    commands.add_parser('lists', help='task list names')
    args = parser.parse_args(argv)
    try:
        cfg = agent.config(args.config)
        now = datetime.now(ZoneInfo(cfg['timezone']))
        if args.command == 'brief':
            print(show(cfg, now, args.scope, args.live))
            return 0
        if args.command == 'add':
            result = add(cfg, now, args.title, args.due, args.list_name, args.notes,
                         args.allow_duplicate, args.allow_past, args.dry_run, snapshot=snapshot_tasks(cfg, now))
        elif args.command == 'schedule':
            duration_mins = scheduler.parse_duration_minutes(args.duration, default=120)
            result = scheduler.schedule(
                cfg, now,
                title=args.title,
                when=args.when,
                duration_minutes=duration_mins,
                calendar_id=args.calendar,
                list_name=args.list_name,
                notes=args.notes,
                allow_duplicate=args.allow_duplicate,
                dry_run=args.dry_run
            )
        elif args.command == 'done':
            result = complete(cfg, now, args.query, args.dry_run)
        elif args.command == 'due':
            result = reschedule(cfg, now, args.query, args.day, args.allow_past, args.dry_run)
        elif args.command == 'retitle':
            result = retitle(cfg, now, args.query, args.title, args.dry_run)
        elif args.command == 'find':
            matches = find(open_tasks(cfg, now), args.query)
            result = {'status': 'ok', 'count': len(matches),
                      'tasks': [describe(task, now.date()) for task in matches[:20]]}
        else:
            result = {'status': 'ok', 'lists': [row.get('title', '') for row in task_lists(cfg, agent.gog_json)]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result['status'] == 'created_but_due_differs' else 0
    except Decision as choice:
        print(json.dumps(choice.payload, ensure_ascii=False, indent=2))
        return 2
    except (agent.AgentError, ValueError, OSError, sqlite3.Error) as exc:
        code = str(exc) if isinstance(exc, (agent.AgentError, ValueError)) else type(exc).__name__
        print(json.dumps({'status': 'error', 'error': code}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
