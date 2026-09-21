"""Offline tests for the capture CLI and the daemon's nudge path. No command or network is reached."""
from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent  # noqa: E402
import secretary  # noqa: E402


SEOUL = ZoneInfo('Asia/Seoul')
MONDAY = date(2026, 9, 21)


def at(value='2026-09-21 10:00'):
    return datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=SEOUL)


def task(key, title, due='', updated='2026-09-01T00:00:00.000Z'):
    return {'key': f'work/{key}', 'id': key, 'list_id': 'work', 'list_title': '업무', 'title': title,
            'due': due, 'updated': updated, 'fingerprint': 'f-' + key}


class Recorder:
    """Stands in for gog: serves lists and rows, records every write."""

    def __init__(self, rows=()):
        self.rows, self.writes = list(rows), []

    def __call__(self, cfg, args):
        if args[:3] == ['tasks', 'lists', 'list']:
            return {'tasklists': [{'id': 'work', 'title': '업무'}, {'id': 'star', 'title': 'High Priority'}]}
        if args[:2] == ['tasks', 'list']:
            return {'tasks': self.rows}
        self.writes.append(args)
        return {'id': 'new-1'}


class SecretaryCase(unittest.TestCase):
    def setUp(self):
        guard = patch.object(agent.subprocess, 'run', side_effect=AssertionError('external command forbidden in tests'))
        guard.start()
        self.addCleanup(guard.stop)
        directory = tempfile.TemporaryDirectory(prefix='secretary-test-')
        self.addCleanup(directory.cleanup)
        self.cfg = agent.DEFAULTS | {'account': 'reminder-test@example.com', 'state_dir': directory.name,
                                     'digest_times': ['08:30', '17:30'], 'nudge_times': ['13:30', '16:30']}


class ParseDayTests(unittest.TestCase):
    def test_words_offsets_weekdays_and_iso(self):
        expect = {'오늘': '2026-09-21', 'today': '2026-09-21', '내일': '2026-09-22', '모레': '2026-09-23',
                  '+3d': '2026-09-24', '금': '2026-09-25', 'fri': '2026-09-25', '금요일': '2026-09-25',
                  '월': '2026-09-21', 'sun': '2026-09-27', 'this-week': '2026-09-25', '이번주': '2026-09-25',
                  '2026-10-02': '2026-10-02'}
        for word, day in expect.items():
            self.assertEqual(secretary.parse_day(word, MONDAY).isoformat(), day, word)
        # Spoken on Friday it is today; over the weekend it is Sunday, never a day already gone.
        for today, day in (('2026-09-25', '2026-09-25'), ('2026-09-26', '2026-09-27'), ('2026-09-27', '2026-09-27')):
            self.assertEqual(secretary.parse_day('this-week', date.fromisoformat(today)).isoformat(), day)

    def test_policy_friday_is_always_at_least_two_days_away(self):
        expect = {'2026-09-21': '2026-09-25', '2026-09-23': '2026-09-25', '2026-09-24': '2026-10-02',
                  '2026-09-25': '2026-10-02', '2026-09-26': '2026-10-02', '2026-09-27': '2026-10-02'}
        for today, day in expect.items():
            self.assertEqual(secretary.policy_day('this-week', date.fromisoformat(today)).isoformat(), day, today)
        self.assertIsNone(secretary.policy_day('none', MONDAY))
        self.assertEqual(secretary.policy_day('tomorrow', MONDAY).isoformat(), '2026-09-22')

    def test_anything_else_is_rejected_instead_of_guessed(self):
        for word in ('next friday', '9/25', '2026-13-01', ''):
            with self.assertRaises(ValueError):
                secretary.parse_day(word, MONDAY)


class AddTests(SecretaryCase):
    def test_creates_with_date_notes_and_promises_the_coming_reminders(self):
        gog = Recorder()
        result = secretary.add(self.cfg, at(), '  토론   평가 완료  ', due='오늘', notes='채점표', read=gog)
        self.assertEqual(gog.writes, [['tasks', 'add', 'work', '--title', '토론 평가 완료', '--due', '2026-09-21', '--notes', '채점표']])
        self.assertEqual((result['status'], result['tier'], result['list']), ('created', 'today', '업무'))
        self.assertEqual(result['reminders'], ['13:30 마감 점검', '16:30 마감 점검', '17:30 브리핑'])

    def test_capture_without_a_date_gets_the_policy_date_and_says_so(self):
        gog = Recorder()
        result = secretary.add(self.cfg | {'capture_default_due': 'this-week'}, at(), '말로만 등록한 일', read=gog)
        self.assertEqual((result['due'], result['due_defaulted'], result['tier']), ('2026-09-25', True, 'week'))
        self.assertEqual(gog.writes[0][-2:], ['--due', '2026-09-25'])
        stated = secretary.add(self.cfg, at(), '날짜를 말한 일', due='내일', read=Recorder())
        self.assertFalse(stated['due_defaulted'])

    def test_undated_capture_is_the_default_and_none_overrides_a_policy(self):
        for cfg, due in ((self.cfg | {'capture_default_due': 'this-week'}, '없음'), (self.cfg, '')):
            gog = Recorder()
            result = secretary.add(cfg, at(), '언젠가 할 일', due=due, read=gog)
            self.assertEqual((result['due'], result['due_defaulted'], result['tier']), ('', False, 'undated'))
            self.assertNotIn('--due', gog.writes[0])
            self.assertIn('날짜 미정', result['reminders'][0])

    def test_a_date_google_stored_differently_is_reported_not_hidden(self):
        class Drifting(Recorder):
            def __call__(self, cfg, args):
                result = super().__call__(cfg, args)
                return {'id': 'new-1', 'due': '2026-09-24T00:00:00.000Z'} if args[1] == 'add' else result
        result = secretary.add(self.cfg, at(), '날짜가 어긋난 일', due='2026-09-25', read=Drifting())
        self.assertEqual((result['status'], result['stored_due']), ('created_but_due_differs', '2026-09-24'))

    def test_open_task_with_the_same_title_blocks_the_write(self):
        gog = Recorder([{'id': 'x', 'title': '토론 평가  완료!', 'status': 'needsAction'}])
        with self.assertRaises(secretary.Decision) as raised:
            secretary.add(self.cfg, at(), '토론 평가 완료', read=gog)
        self.assertEqual(raised.exception.payload['status'], 'duplicate')
        self.assertEqual(raised.exception.payload['existing']['key'], 'work/x')
        self.assertEqual(gog.writes, [])
        secretary.add(self.cfg, at(), '토론 평가 완료', allow_duplicate=True, read=gog)
        self.assertEqual(len(gog.writes), 1)

    def test_twin_in_another_list_is_caught_from_the_snapshot(self):
        elsewhere = {'star/z': task('z', '토론 평가 완료') | {'key': 'star/z', 'list_id': 'star', 'list_title': 'High Priority'}}
        gog = Recorder()
        with self.assertRaises(secretary.Decision) as raised:
            secretary.add(self.cfg, at(), '토론 평가 완료', read=gog, snapshot=elsewhere)
        self.assertEqual(raised.exception.payload['existing']['list'], 'High Priority')
        self.assertEqual(gog.writes, [])

    def test_completed_twin_does_not_block_and_named_list_is_honoured(self):
        gog = Recorder([{'id': 'x', 'title': '토론 평가 완료', 'status': 'completed'}])
        result = secretary.add(self.cfg, at(), '토론 평가 완료', list_name='high priority', read=gog)
        self.assertEqual(result['list'], 'High Priority')
        self.assertEqual(gog.writes[0][:3], ['tasks', 'add', 'star'])
        with self.assertRaises(secretary.Decision) as raised:
            secretary.add(self.cfg, at(), '다른 일', list_name='없는 목록', read=gog)
        self.assertEqual(raised.exception.payload['status'], 'list_not_found')

    def test_past_date_empty_title_and_dry_run(self):
        with self.assertRaisesRegex(ValueError, 'past'):
            secretary.add(self.cfg, at(), '지난 일', due='2026-09-18', read=Recorder())
        with self.assertRaisesRegex(ValueError, 'title'):
            secretary.add(self.cfg, at(), '   ', read=Recorder())
        gog = Recorder()
        result = secretary.add(self.cfg, at(), '연습', due='fri', dry_run=True, read=gog)
        self.assertEqual((result['status'], gog.writes[0][-1]), ('dry_run', '--dry-run'))


class CloseAndMoveTests(SecretaryCase):
    def setUp(self):
        super().setUp()
        self.tasks = {t['key']: t for t in (task('a', '토론 평가 완료'), task('b', '토론 준비'), task('c', '초록 제출', due='2026-09-23'))}

    def test_unique_match_is_completed_by_list_and_task_id(self):
        gog = Recorder()
        result = secretary.complete(self.cfg, at(), '초록', read=gog, tasks=self.tasks)
        self.assertEqual(gog.writes, [['tasks', 'done', 'work', 'c']])
        self.assertEqual(result['status'], 'completed')

    def test_ambiguous_and_missing_queries_never_write(self):
        gog = Recorder()
        for query, status in (('토론', 'ambiguous'), ('없는 일', 'not_found')):
            with self.assertRaises(secretary.Decision) as raised:
                secretary.complete(self.cfg, at(), query, read=gog, tasks=self.tasks)
            self.assertEqual(raised.exception.payload['status'], status)
        self.assertEqual(len(raised.exception.payload['candidates']), 0)
        self.assertEqual(gog.writes, [])

    def test_exact_title_or_key_breaks_a_tie(self):
        tasks = self.tasks | {'work/d': task('d', '토론')}
        gog = Recorder()
        secretary.complete(self.cfg, at(), '토론', read=gog, tasks=tasks)
        secretary.complete(self.cfg, at(), 'work/b', read=gog, tasks=tasks)
        self.assertEqual([w[-1] for w in gog.writes], ['d', 'b'])

    def test_writes_choose_their_target_from_a_live_read_not_the_snapshot(self):
        db = agent.connect(self.cfg['state_dir'])
        agent.snapshot(db, {'work/old': task('old', '예전 초록 제출 준비')}, 1, at('2026-09-21 09:58'))
        db.close()
        live = {'work/old': task('old', '예전 초록 제출 준비'), 'work/new': task('new', '학회 초록 제출')}
        with patch.object(secretary.agent, 'fetch_tasks', return_value=(live, 1)) as fetch:
            for action in (lambda: secretary.complete(self.cfg, at(), '초록 제출', read=Recorder()),
                           lambda: secretary.reschedule(self.cfg, at(), '초록 제출', '내일', read=Recorder()),
                           lambda: secretary.retitle(self.cfg, at(), '초록 제출', '새 제목', read=Recorder())):
                with self.assertRaises(secretary.Decision) as raised:
                    action()
                self.assertEqual(raised.exception.payload['status'], 'ambiguous')
        self.assertEqual(fetch.call_count, 3)

    def test_postponing_past_a_deadline_written_in_the_title_is_flagged_and_retitle_fixes_it(self):
        tasks = {'work/t': task('t', '서면평가 (9월 18일 오전 10시까지)', due='2026-09-18')}
        gog = Recorder()
        moved = secretary.reschedule(self.cfg, at(), '서면평가', '2026-09-25', read=gog, tasks=tasks)
        self.assertEqual((moved['tier'], moved['title_deadline_earlier']), ('overdue', '2026-09-18'))
        fixed = secretary.retitle(self.cfg, at(), '서면평가', '서면평가 (9월 25일까지)', read=gog,
                                  tasks={'work/t': tasks['work/t'] | {'due': '2026-09-25'}})
        self.assertEqual((fixed['status'], fixed['tier'], fixed['previous_title']),
                         ('retitled', 'week', '서면평가 (9월 18일 오전 10시까지)'))
        self.assertEqual(gog.writes[-1], ['tasks', 'update', 'work', 't', '--title', '서면평가 (9월 25일까지)'])

    def test_reschedule_sets_the_date_and_reports_the_new_tier(self):
        gog = Recorder()
        result = secretary.reschedule(self.cfg, at(), '토론 평가', '내일', read=gog, tasks=self.tasks)
        self.assertEqual(gog.writes, [['tasks', 'update', 'work', 'a', '--due', '2026-09-22']])
        self.assertEqual((result['due'], result['tier']), ('2026-09-22', 'week'))
        with self.assertRaisesRegex(ValueError, 'past'):
            secretary.reschedule(self.cfg, at(), '토론 평가', '2026-09-01', read=gog, tasks=self.tasks)


class SnapshotTests(SecretaryCase):
    def test_fresh_snapshot_is_used_and_a_stale_or_missing_one_triggers_a_live_read(self):
        live = Mock(return_value=({'live/1': task('1', '실시간')}, 1))
        self.assertEqual(list(secretary.open_tasks(self.cfg, at(), fetch=live)), ['live/1'])
        db = agent.connect(self.cfg['state_dir'])
        agent.snapshot(db, {'work/a': task('a', '저장본')}, 1, at('2026-09-21 09:58'))
        db.close()
        live.reset_mock()
        self.assertEqual(list(secretary.open_tasks(self.cfg, at(), fetch=live)), ['work/a'])
        live.assert_not_called()
        self.assertEqual(list(secretary.open_tasks(self.cfg, at('2026-09-21 10:30'), fetch=live)), ['live/1'])
        self.assertEqual(list(secretary.open_tasks(self.cfg, at(), live=True, fetch=live)), ['live/1'])


class NudgeTickTests(SecretaryCase):
    def setUp(self):
        super().setUp()
        self.cfg['digest_times'] = []
        self.db = agent.connect(self.cfg['state_dir'])
        self.addCleanup(self.db.close)
        self.sender = Mock(return_value={'message_id': 'test-only-1'})
        self.urgent = {'work/a': task('a', '오늘 회신', due='2026-09-21')}

    def tick(self, tasks, moment, **kwargs):
        return agent.tick(self.db, self.cfg, at(moment), fetch=lambda cfg: (tasks, 1), sender=self.sender,
                          read=lambda cfg, args: self.fail('side sources are disabled by default'), **kwargs)

    def test_one_nudge_per_slot_and_only_inside_its_window(self):
        self.tick(self.urgent, '2026-09-21 13:25')
        self.assertEqual(self.sender.call_count, 0)
        result = self.tick(self.urgent, '2026-09-21 13:30')
        self.assertEqual(result['delivery'], {'nudge': 'sent'})
        self.assertIn('오늘 회신', self.sender.call_args.args[1])
        self.tick(self.urgent, '2026-09-21 13:35')
        self.assertEqual(self.sender.call_count, 1)
        self.tick(self.urgent, '2026-09-21 16:30')
        self.assertEqual(self.sender.call_count, 2)

    def test_slot_missed_by_downtime_is_not_replayed(self):
        self.tick(self.urgent, '2026-09-21 13:55')
        self.assertEqual(self.sender.call_count, 0)

    def test_nothing_critical_means_silence_and_no_delivery_record(self):
        calm = {'work/b': task('b', '수요일 일', due='2026-09-23'), 'work/c': task('c', '아주 오래된 일', due='2026-01-19')}
        result = self.tick(calm, '2026-09-21 13:30')
        self.assertEqual((self.sender.call_count, result['delivery']), (0, {}))
        self.assertIsNone(self.db.execute("SELECT 1 FROM deliveries WHERE kind='nudge'").fetchone())
        self.assertEqual(agent.status(self.db, at('2026-09-21 13:31'))['delivery_status'], 'not_sent_yet')

    def test_quiet_hours_and_a_digest_in_the_last_45_minutes_suppress_the_nudge(self):
        self.cfg['nudge_times'] = ['07:30', '13:30']
        self.assertEqual(self.tick(self.urgent, '2026-09-21 07:30')['status'], 'quiet_hours')
        self.cfg['digest_times'] = ['13:00']
        self.tick(self.urgent, '2026-09-21 13:00')
        self.tick(self.urgent, '2026-09-21 13:30')
        self.assertEqual([row['kind'] for row in self.db.execute('SELECT kind FROM deliveries')], ['digest'])

    def test_nudge_consumes_the_change_it_displays(self):
        self.tick({}, '2026-09-21 13:00')
        self.tick(self.urgent, '2026-09-21 13:30')
        self.assertEqual(self.sender.call_count, 1)
        self.assertIsNone(self.db.execute('SELECT 1 FROM changes').fetchone())
        self.tick(self.urgent, '2026-09-21 14:00')
        self.assertEqual(self.sender.call_count, 1)

    def test_digest_is_the_integrated_brief_and_a_broken_brief_degrades_to_the_legacy_summary(self):
        self.cfg['digest_times'] = ['08:30']
        self.tick(self.urgent, '2026-09-20 08:30')
        self.assertIn('할 일 리마인드', self.sender.call_args.args[1])  # opt-in: the default stays legacy
        self.cfg['brief'] = True
        self.tick(self.urgent, '2026-09-21 08:30')
        self.assertIn('🟠 오늘 일과 종료 전 반드시', self.sender.call_args.args[1])
        self.assertIsNone(agent.status(self.db, at('2026-09-21 08:31'))['brief_error'])
        import brief
        with patch.object(brief, 'render', side_effect=RuntimeError('boom')):
            self.tick(self.urgent, '2026-09-22 08:30')
        fallback = self.sender.call_args.args[1]
        self.assertIn('할 일 리마인드', fallback)
        self.assertIn('기본 요약으로 대체했습니다: RuntimeError', fallback)
        self.assertEqual(agent.status(self.db, at('2026-09-22 08:31'))['brief_error'], 'RuntimeError')

    def test_side_sources_are_skipped_once_the_tick_has_used_its_budget(self):
        cfg = self.cfg | {'brief': True, 'calendar': True, 'mail': True, 'oauth_ttl_days': 7}
        reads = []
        reader = lambda c, args: reads.append(args[0]) or {}
        text, members, error = agent.compose(self.urgent, cfg, at('2026-09-21 08:30'), read=reader, spare=44)
        self.assertEqual((reads, error, set(members)), ([], None, {'work/a'}))
        self.assertIn('캘린더를 읽지 못했습니다', text)
        agent.compose(self.urgent, cfg, at('2026-09-21 08:30'), read=reader, spare=45)
        self.assertEqual(reads, ['calendar', 'gmail', 'auth'])

    def test_nudge_survives_a_broken_brief_module_with_googles_own_dates(self):
        import brief
        with patch.object(brief, 'nudge', side_effect=RuntimeError('boom')):
            result = self.tick(self.urgent, '2026-09-21 13:30')
        self.assertEqual(result['delivery'], {'nudge': 'sent'})
        self.assertIn('오늘 마감 점검', self.sender.call_args.args[1])
        self.assertIn('오늘 회신', self.sender.call_args.args[1])
        self.assertEqual(agent.status(self.db, at('2026-09-21 13:31'))['brief_error'], 'RuntimeError')

    def test_configuration_rejects_wrong_types_before_they_can_crash_a_tick(self):
        path = Path(self.cfg['state_dir']) / 'config.json'
        for bad in ({'brief': 'false'}, {'nudge_times': None}, {'oauth_ttl_days': '7'}, {'calendar': 1},
                    {'nudge_times': [1330]}, {'digest_times': '08:30'}, {'upcoming_days': True}):
            path.write_text(json.dumps({'account': 'reminder-test@example.com'} | bad))
            with self.assertRaisesRegex(ValueError, 'invalid_configuration'):
                agent.config(path)
        path.write_text('["not", "an", "object"]')
        with self.assertRaisesRegex(ValueError, 'invalid_configuration'):
            agent.config(path)

    def test_configuration_rejects_malformed_nudge_settings(self):
        path = Path(self.cfg['state_dir']) / 'config.json'
        for bad in ({'nudge_times': ['1330']}, {'nudge_window_minutes': 0}, {'stale_days': 0}):
            path.write_text(json.dumps({'account': 'reminder-test@example.com'} | bad))
            with self.assertRaises(ValueError):
                agent.config(path)


if __name__ == '__main__':
    unittest.main()
