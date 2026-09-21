#!/usr/bin/env python3
"""Read Google Tasks continuously and send bounded personal Telegram reminders."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from zoneinfo import ZoneInfo


DEFAULT_CONFIG = Path.home() / '.config/google-tasks-agent/config.json'
DEFAULTS = {
    'account': '',  # Set only in the private local config.
    'gog_path': str(Path.home() / 'bin/gog'),
    'openclaw_path': str(Path.home() / '.npm-global/bin/openclaw'),
    'route_path': str(Path.home() / '.local/state/codex-coach/telegram-route.json'),
    'state_dir': str(Path.home() / '.local/state/google-tasks-agent'),
    'timezone': 'Asia/Seoul', 'digest_times': ['08:30', '17:30'],
    'quiet_start': '22:00', 'quiet_end': '08:00', 'upcoming_days': 3,
    # Secretary extensions, all opt-in from the private config. `brief` swaps the digest body for the
    # tiered brief and is the first rollback lever; side sources need calendar and mail scopes that a
    # Tasks-only installation does not have.
    'brief': False, 'nudge_times': [], 'nudge_window_minutes': 20, 'nudge_overdue_days': 3,
    'stale_days': 14, 'new_undated_days': 3,
    'calendar': False, 'mail': False, 'calendar_exclude': [], 'oauth_ttl_days': 0,
    # Capture: list title for new tasks (empty = the account's first list) and the date given to a
    # task captured without one: 'none', 'today', 'tomorrow' or 'this-week' (Friday).
    'capture_list': '', 'capture_default_due': 'this-week',
}
# Seconds of a tick that may pass before side sources are skipped. systemd kills the unit at 240s and
# the Tasks scan plus one Telegram send can already take 235s, so side reads only run on a fast tick.
SIDE_BUDGET = 120


class AgentError(Exception):
    """Only fixed, non-sensitive error codes go into logs."""


def config(path=DEFAULT_CONFIG):
    result = DEFAULTS | (json.loads(Path(path).read_text()) if Path(path).exists() else {})
    ZoneInfo(result['timezone'])
    for value in [*result['digest_times'], *result['nudge_times'], result['quiet_start'], result['quiet_end']]:
        datetime.strptime(value, '%H:%M')
        if len(value) != 5:
            raise ValueError('time_requires_HH_MM')
    if not result['account'] or not 0 <= result['upcoming_days'] <= 30:
        raise ValueError('invalid_configuration')
    if not (1 <= result['nudge_window_minutes'] <= 120 and 1 <= result['stale_days'] <= 365
            and 0 <= result['nudge_overdue_days'] <= result['stale_days']
            and 0 <= result['new_undated_days'] <= 30 and 0 <= result['oauth_ttl_days'] <= 365
            and result['capture_default_due'] in ('none', 'today', 'tomorrow', 'this-week')):
        raise ValueError('invalid_configuration')
    return result


def stamp(now):
    return now.isoformat(timespec='seconds')


def gog_json(cfg, args, timeout=40):
    try:
        p = subprocess.run([cfg['gog_path'], '--account', cfg['account'], '--json',
                            '--no-input', *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AgentError('google_timeout') from None
    except OSError:
        raise AgentError('google_command_unavailable') from None
    if p.returncode:
        raise AgentError('google_read_failed')
    try:
        result = json.loads(p.stdout)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except ValueError:
        raise AgentError('google_invalid_response') from None


def fetch_tasks(cfg, read=gog_json):
    """Do not commit partial results; gog --all handles both pagination levels."""
    started = time.monotonic()
    data = read(cfg, ['tasks', 'lists', 'list', '--all', '--max', '1000'])
    if 'tasklists' not in data:
        raise AgentError('google_invalid_tasklists_response')
    lists = data.get('tasklists')
    if lists is None:
        lists = []
    if not isinstance(lists, list) or data.get('nextPageToken'):
        raise AgentError('google_incomplete_tasklists')
    tasks = {}
    for tasklist in lists:
        if time.monotonic() - started > 150:
            raise AgentError('google_scan_timeout')
        if not isinstance(tasklist, dict) or not tasklist.get('id'):
            raise AgentError('google_invalid_tasklist')
        data = read(cfg, ['tasks', 'list', tasklist['id'], '--all', '--max', '100', '--show-assigned'])
        if 'tasks' not in data:
            raise AgentError('google_invalid_tasks_response')
        rows = data['tasks']
        if rows is None:
            rows = []
        if not isinstance(rows, list) or data.get('nextPageToken'):
            raise AgentError('google_incomplete_tasks')
        for row in rows:
            if not isinstance(row, dict) or not row.get('id') or row.get('status') not in ('needsAction', 'completed'):
                raise AgentError('google_invalid_task')
            if row['status'] != 'needsAction' or row.get('deleted') or row.get('hidden'):
                continue
            due = row.get('due', '')[:10]
            if due:
                try:
                    date.fromisoformat(due)
                except ValueError:
                    raise AgentError('google_invalid_due') from None
            key = tasklist['id'] + '/' + row['id']
            # Notes and full source metadata are unnecessary for reminding.
            task = {'key': key, 'id': row['id'], 'list_id': tasklist['id'],
                    'list_title': tasklist.get('title', ''), 'title': row.get('title') or '(제목 없음)',
                    'due': due, 'updated': row.get('updated', '')}
            task['fingerprint'] = hashlib.sha256(json.dumps(
                [task['title'], task['due'], task['list_title']], ensure_ascii=False).encode()).hexdigest()
            tasks[key] = task
    return tasks, len(lists)


def connect(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    db = sqlite3.connect(state / 'state.sqlite', timeout=10)
    db.row_factory = sqlite3.Row
    os.chmod(state / 'state.sqlite', 0o600)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS changes (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries (
            key TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
            created TEXT NOT NULL, updated TEXT NOT NULL, receipt TEXT, error TEXT);
    ''')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(deliveries)')}
    if 'members' not in columns:
        db.execute("ALTER TABLE deliveries ADD COLUMN members TEXT NOT NULL DEFAULT '{}'")
        db.commit()
    return db


@contextmanager
def locked(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'run.lock').open('a') as lock:
        os.chmod(state / 'run.lock', 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgentError('already_running') from None
        yield


def get_meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put_meta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, json.dumps(value)))


def snapshot(db, tasks, list_count, now):
    previous = {r['key']: json.loads(r['payload']) for r in db.execute('SELECT * FROM tasks')}
    initialized = get_meta(db, 'last_success') is not None
    today = now.date().isoformat()
    with db:
        for key, task in tasks.items():
            if initialized and task['due'] and task['due'] <= today and (
                key not in previous or previous[key]['fingerprint'] != task['fingerprint']
            ):
                db.execute('INSERT OR REPLACE INTO changes VALUES (?, ?)', (key, task['fingerprint']))
        # A queued change must never outlive completion, deletion, or postponement.
        for row in db.execute('SELECT * FROM changes').fetchall():
            current = tasks.get(row['key'])
            if not current or not current['due'] or current['due'] > today:
                db.execute('DELETE FROM changes WHERE key=?', (row['key'],))
        db.execute('DELETE FROM tasks')
        db.executemany('INSERT INTO tasks VALUES (?, ?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in tasks.items()])
        put_meta(db, 'last_success', stamp(now))
        put_meta(db, 'list_count', list_count)
        put_meta(db, 'consecutive_failures', 0)
        put_meta(db, 'last_error', None)
        # Keep a year of compact receipts; task snapshots are always current only.
        db.execute('DELETE FROM deliveries WHERE created < ?', (stamp(now - timedelta(days=366)),))


def is_quiet(cfg, now):
    current = now.strftime('%H:%M')
    start, end = cfg['quiet_start'], cfg['quiet_end']
    return start <= current < end if start < end else current >= start or current < end


def clean_title(value, limit=110):
    text = ' '.join(value.split())
    return text.encode('utf-16-le')[:limit * 2].decode('utf-16-le', errors='ignore')


def selection(tasks, cfg, now):
    rows = list(tasks.values())
    today = now.date().isoformat()
    soon = (now.date() + timedelta(days=cfg['upcoming_days'])).isoformat()
    overdue = sorted((t for t in rows if t['due'] and t['due'] < today), key=lambda t: (t['due'], t['key']), reverse=True)
    due_today = sorted((t for t in rows if t['due'] == today), key=lambda t: t['key'])
    upcoming = sorted((t for t in rows if today < t['due'] <= soon), key=lambda t: (t['due'], t['key']))
    undated = sorted((t for t in rows if not t['due']), key=lambda t: t['key'])
    priority = [t for t in undated if t['list_title'].casefold() == 'high priority']
    others = [t for t in undated if t not in priority]
    cycle = now.date().toordinal() * 2 + (now.hour >= 12)

    def rotate(group, cap, fixed=0):
        if len(group) <= cap:
            return group
        rest = group[fixed:]
        offset = (cycle * (cap - fixed)) % len(rest)
        return group[:fixed] + (rest[offset:] + rest[:offset])[:cap-fixed]

    groups = [
        ('오늘 예정', due_today, rotate(due_today, 5)),
        ('가까운 예정일', upcoming, rotate(upcoming, 4, 1)),
        ('예정일 경과 (일부 순환 표시)', overdue, rotate(overdue, 4, 2)),
        ('High Priority · 날짜 없음', priority, rotate(priority, 3)),
        ('날짜 없는 작업 돌아보기', others, rotate(others, 3)),
    ]
    counts = {'all': len(rows), 'overdue': len(overdue), 'today': len(due_today),
              'upcoming': len(upcoming), 'undated': len(undated)}
    return groups, counts


def displayed_members(tasks, cfg, now):
    groups, _ = selection(tasks, cfg, now)
    return {t['key']: t['fingerprint'] for _, _, shown in groups for t in shown}


def summary(tasks, cfg, now, heading='할 일 리마인드'):
    groups, counts = selection(tasks, cfg, now)
    lines = [f'📋 {heading} · {now:%m/%d %H:%M}',
             f'미완료 {counts["all"]} · 예정일 경과 {counts["overdue"]} · 오늘 {counts["today"]} · {cfg["upcoming_days"]}일 이내 {counts["upcoming"]} · 날짜 없음 {counts["undated"]}']
    for label, group, shown in groups:
        if shown:
            lines += ['', label]
            lines += [f'• {clean_title(t["title"])}' + (f' ({t["due"]})' if t['due'] else '') for t in shown]
            if len(group) > len(shown):
                lines.append(f'  외 {len(group)-len(shown)}개')
    if not counts['all']:
        lines += ['', '현재 미완료 작업이 없습니다.']
    lines += ['', 'Google Tasks에서 완료·날짜를 바꾸면 다음 확인에 반영됩니다.', 'https://tasks.google.com/']
    return '\n'.join(lines)


def compose(tasks, cfg, now, kind='digest', read=None, spare=SIDE_BUDGET):
    """(text, members, error): the integrated brief, or the legacy summary when it cannot be built.

    A broken or missing secretary module must never stop the reminder, so every failure
    degrades to the legacy digest and is surfaced through `status` instead of raising.
    `spare` is what is left of SIDE_BUDGET; without it the brief is built from tasks alone.
    """
    if not cfg['brief'] and kind != 'nudge':
        return summary(tasks, cfg, now), displayed_members(tasks, cfg, now), None
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import brief
        import sources
        context = sources.gather(cfg, now, read or functools.partial(gog_json, timeout=15), skip=spare < 45)
        if kind == 'nudge':
            return (*brief.nudge(tasks, cfg, now, events=context['events'], issued=context['issued']), None)
        return (*brief.render(tasks, cfg, now, **context), None)
    except Exception as exc:
        error = type(exc).__name__
        if kind == 'nudge':
            return '', {}, error
        body = summary(tasks, cfg, now) + f'\n(통합 브리핑을 만들지 못해 기본 요약으로 대체했습니다: {error})'
        return body, displayed_members(tasks, cfg, now), error


def open_slot(times, now, window_minutes):
    """Latest slot that opened within the window. A nudge missed by downtime is dropped, not replayed."""
    current = now.hour * 60 + now.minute
    for value in sorted(times, reverse=True):
        start = int(value[:2]) * 60 + int(value[3:])
        if start <= current < start + window_minutes:
            return value
    return None


def sent_recently(db, kinds, now, minutes):
    floor = stamp(now - timedelta(minutes=minutes))
    marks = ','.join('?' * len(kinds))
    return db.execute(f"SELECT 1 FROM deliveries WHERE kind IN ({marks}) AND status != 'failed' AND created >= ?",
                      (*kinds, floor)).fetchone() is not None


def consume_changes(db, members):
    db.executemany('DELETE FROM changes WHERE key=? AND fingerprint=?', list(members.items()))


def send_telegram(cfg, message):
    try:
        route = json.loads(Path(cfg['route_path']).expanduser().read_text())
    except (OSError, ValueError):
        raise AgentError('telegram_route_unavailable') from None
    target = str(route.get('target', ''))
    if route.get('channel') != 'telegram' or not target.isdigit():
        raise AgentError('telegram_private_route_required')
    command = [cfg['openclaw_path'], 'message', 'send', '--channel', 'telegram',
               '--target', target, '--message', message, '--json']
    if route.get('account'):
        command += ['--account', route['account']]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    except OSError:
        raise AgentError('telegram_command_unavailable') from None
    except subprocess.TimeoutExpired:
        raise AgentError('telegram_unknown_timeout') from None
    if result.returncode:
        raise AgentError('telegram_unknown_result')
    try:
        receipt = json.loads(result.stdout[result.stdout.index('{'):])
        payload = receipt.get('payload', receipt)
        message_id = payload.get('messageId') or payload.get('result', {}).get('message_id')
        if not message_id:
            raise ValueError()
        return {'message_id': str(message_id)}
    except (ValueError, TypeError, AttributeError):
        raise AgentError('telegram_unknown_receipt') from None


def deliver(db, cfg, key, kind, message, now, sender=send_telegram, members=None):
    members = members or {}
    # Commit intent before IO. A crash/timeout is not permission to send twice.
    with db:
        existing = db.execute('SELECT * FROM deliveries WHERE key=?', (key,)).fetchone()
        if existing and (existing['status'] != 'failed' or
                         now - datetime.fromisoformat(existing['updated']) < timedelta(minutes=15)):
            return existing['status']
        db.execute('INSERT OR REPLACE INTO deliveries (key, kind, status, created, updated, receipt, error, members) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)',
                   (key, kind, 'sending', stamp(now), stamp(now), json.dumps(members)))
    try:
        receipt = sender(cfg, message)
        if not isinstance(receipt, dict) or not receipt.get('message_id'):
            raise AgentError('telegram_unknown_receipt')
    except Exception as exc:
        code = str(exc) if isinstance(exc, AgentError) else 'telegram_unknown_exception'
        status = 'failed' if code in ('telegram_route_unavailable', 'telegram_private_route_required',
                                     'telegram_command_unavailable') else 'unknown'
        with db:
            db.execute('UPDATE deliveries SET status=?, error=?, updated=? WHERE key=?',
                       (status, code, stamp(now), key))
            if status == 'unknown':
                consume_changes(db, members)
        return status
    with db:
        db.execute('UPDATE deliveries SET status=?, receipt=?, updated=? WHERE key=?',
                   ('sent', json.dumps(receipt), stamp(now), key))
        consume_changes(db, members)
    return 'sent'


def tick(db, cfg, now, fetch=fetch_tasks, sender=send_telegram, force=False, read=None):
    began = time.monotonic()
    with db:
        put_meta(db, 'last_attempt', stamp(now))
        # These records can only be remnants of a terminated prior run (outer flock).
        interrupted = db.execute("SELECT members FROM deliveries WHERE status='sending'").fetchall()
        for row in interrupted:
            consume_changes(db, json.loads(row['members']))
        db.execute("UPDATE deliveries SET status='unknown', error='interrupted_send' WHERE status='sending'")
    try:
        tasks, count = fetch(cfg)
    except Exception as exc:
        code = str(exc) if isinstance(exc, AgentError) else 'google_unexpected_error'
        with db:
            failures = get_meta(db, 'consecutive_failures', 0) + 1
            put_meta(db, 'consecutive_failures', failures)
            put_meta(db, 'last_error', code)
        if failures >= 3 and not is_quiet(cfg, now):
            deliver(db, cfg, f'health:{now.date()}', 'health',
                    '⚠️ Google Tasks를 연속 3회 이상 읽지 못했습니다. 리마인드 정보 갱신이 멈췄습니다.\n'
                    f'마지막 성공: {get_meta(db, "last_success", "없음")}\n'
                    '서버에서 google_tasks_agent/agent.py status로 확인할 수 있습니다.', now, sender)
        return {'status': 'source_failed', 'error': code, 'consecutive_failures': failures}
    snapshot(db, tasks, count, now)
    if is_quiet(cfg, now) and not force:
        return {'status': 'quiet_hours', 'tasks': len(tasks)}
    day = now.date().isoformat()
    if force:
        key, kind = f'manual:{day}', 'manual'
    else:
        eligible = [t for t in cfg['digest_times'] if t <= now.strftime('%H:%M')]
        # Only the latest slot catches up after downtime; never burst both digests.
        slot = max(eligible) if eligible else None
        key, kind = (f'digest:{day}:{slot}', 'digest') if slot else ('', '')
    outcomes = {}
    if key:
        previous = db.execute('SELECT status FROM deliveries WHERE key=?', (key,)).fetchone()
        if not previous or previous['status'] == 'failed':
            body, members, error = compose(tasks, cfg, now, read=read, spare=SIDE_BUDGET - (time.monotonic() - began))
            with db:
                put_meta(db, 'brief_error', error)
            outcomes[kind] = deliver(db, cfg, key, kind, body, now, sender, members)
            return {'status': 'ok', 'tasks': len(tasks), 'delivery': outcomes}
    slot = None if force else open_slot(cfg['nudge_times'], now, cfg['nudge_window_minutes'])
    if slot and not sent_recently(db, ('digest', 'manual'), now, 45):
        nudge_key = f'nudge:{day}:{slot}'
        previous = db.execute('SELECT status FROM deliveries WHERE key=?', (nudge_key,)).fetchone()
        if not previous or previous['status'] == 'failed':
            body, members, error = compose(tasks, cfg, now, 'nudge', read=read,
                                           spare=SIDE_BUDGET - (time.monotonic() - began))
            if error:
                with db:
                    put_meta(db, 'brief_error', error)
            if body:
                outcomes['nudge'] = deliver(db, cfg, nudge_key, 'nudge', body, now, sender, members)
                return {'status': 'ok', 'tasks': len(tasks), 'delivery': outcomes}
    pending = [tasks[row['key']] for row in db.execute('SELECT key FROM changes') if row['key'] in tasks]
    if pending:
        # One attempt per hour, including ambiguous delivery. Fresh snapshot cancels completed tasks.
        hourkey = f'changes:{day}:{now.hour:02d}'
        attempted = db.execute('SELECT status FROM deliveries WHERE key=?', (hourkey,)).fetchone()
        if attempted and attempted['status'] != 'failed':
            # New changes after an earlier send remain queued for the next hour.
            return {'status': 'ok', 'tasks': len(tasks), 'delivery': outcomes, 'queued_changes': len(pending)}
        selected = {t['key']: t for t in pending}
        body = summary(selected, cfg, now, '오늘·이전 예정일 작업 추가·변경')
        result = deliver(db, cfg, hourkey, 'changes', body, now, sender, displayed_members(selected, cfg, now))
        outcomes['changes'] = result
    return {'status': 'ok', 'tasks': len(tasks), 'delivery': outcomes}


def status(db, now):
    last = get_meta(db, 'last_success')
    age = (now - datetime.fromisoformat(last)).total_seconds() if last else None
    recent = [dict(row) for row in db.execute('SELECT * FROM deliveries ORDER BY updated DESC LIMIT 8')]
    for row in recent:
        row['receipt'] = json.loads(row['receipt']) if row['receipt'] else None
        row.pop('members', None)
    source_status = 'healthy' if age is not None and 0 <= age < 1200 and not get_meta(db, 'last_error') else 'stale'
    delivery_status = ('healthy' if recent[0]['status'] == 'sent' else 'degraded') if recent else 'not_sent_yet'
    overall = 'stale' if source_status != 'healthy' else ('healthy' if delivery_status == 'healthy' else 'degraded')
    return {'status': overall, 'source_status': source_status, 'delivery_status': delivery_status,
            'last_success': last, 'last_attempt': get_meta(db, 'last_attempt'),
            'snapshot_age_seconds': age, 'list_count': get_meta(db, 'list_count'),
            'incomplete_tasks': db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],
            'consecutive_failures': get_meta(db, 'consecutive_failures', 0),
            'last_error': get_meta(db, 'last_error'), 'brief_error': get_meta(db, 'brief_error'),
            'deliveries': recent}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['tick', 'preview', 'status', 'send-now'])
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        cfg = config(args.config)
        now = datetime.now(ZoneInfo(cfg['timezone']))
        if args.command == 'preview':
            tasks, _ = fetch_tasks(cfg)
            print(compose(tasks, cfg, now)[0])
            return 0
        with locked(cfg['state_dir']):
            db = connect(cfg['state_dir'])
            try:
                result = status(db, now) if args.command == 'status' else tick(
                    db, cfg, now, force=args.command == 'send-now')
            finally:
                db.close()
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result['status'] in ('source_failed', 'stale', 'degraded') or any(
            state != 'sent' for state in result.get('delivery', {}).values()) else 0
    except (AgentError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc) if isinstance(exc, AgentError) else type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
