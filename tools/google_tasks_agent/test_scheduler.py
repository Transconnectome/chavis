"""Offline tests for the natural language scheduler and calendar dual-registration.

Validates:
1. Relative date and time parsing ("화요일 오후", "내일 14:00", "수요일 저녁 (1시간)", "다음주 금요일").
2. Slot finding algorithm against conflicting and open calendar schedules.
3. Dual-registration transaction: Tasks + Calendar creation and linking.
4. Pre-flight duplicate check prevention.
5. Dry-run safety.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent  # noqa: E402
import scheduler  # noqa: E402
import secretary  # noqa: E402


SEOUL = ZoneInfo('Asia/Seoul')
MONDAY = date(2026, 9, 21)


def at(value='2026-09-21 10:00'):
    return datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=SEOUL)


def make_event(title, start_str, end_str, cal='primary'):
    start_dt = at(start_str)
    end_dt = at(end_str)
    return {
        'title': title,
        'all_day': False,
        'start': start_dt,
        'end': end_dt,
        'first_day': start_dt.date(),
        'last_day': end_dt.date(),
        'calendar': cal
    }


class MockGogRecorder:
    def __init__(self, tasklists=None, tasks=None, events=None):
        self.tasklists = tasklists or [{'id': 'work', 'title': '업무'}]
        self.tasks = tasks or []
        self.events = events or []
        self.calls = []

    def __call__(self, cfg, args):
        self.calls.append(args)
        if args[:3] == ['tasks', 'lists', 'list']:
            return {'tasklists': self.tasklists}
        if args[:2] == ['tasks', 'list']:
            return {'tasks': self.tasks}
        if args[:2] == ['calendar', 'events']:
            return {'events': self.events}
        if args[:2] == ['calendar', 'create']:
            if '--dry-run' in args:
                return {'dry_run': True, 'op': 'calendar.create'}
            return {'event': {'id': 'cal-evt-999', 'htmlLink': 'https://calendar.google.com/event?id=cal-evt-999'}}
        if args[:2] == ['tasks', 'add']:
            if '--dry-run' in args:
                return {'task': {'id': 'dry-task-1', 'due': '2026-09-22'}}
            return {'task': {'id': 'task-new-888', 'due': '2026-09-22'}}
        return {'id': 'dummy-id'}


class SchedulerParsingTests(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(scheduler.parse_duration_minutes('2시간'), 120)
        self.assertEqual(scheduler.parse_duration_minutes('1시간 30분'), 90)
        self.assertEqual(scheduler.parse_duration_minutes('45분'), 45)
        self.assertEqual(scheduler.parse_duration_minutes('1.5h'), 90)
        self.assertEqual(scheduler.parse_duration_minutes('30m'), 30)
        self.assertEqual(scheduler.parse_duration_minutes(''), 120)

    def test_parse_korean_expressions(self):
        # Anchor is Monday 2026-09-21
        # "화요일 오후" -> Tuesday 2026-09-22, period afternoon, default duration 120
        d, period, exact, dur = scheduler.parse_schedule_expression('화요일 오후', MONDAY)
        self.assertEqual(d, date(2026, 9, 22))
        self.assertEqual(period, 'afternoon')
        self.assertIsNone(exact)
        self.assertEqual(dur, 120)

        # "내일 오전" -> Tuesday 2026-09-22, period morning
        d, period, exact, dur = scheduler.parse_schedule_expression('내일 오전', MONDAY)
        self.assertEqual(d, date(2026, 9, 22))
        self.assertEqual(period, 'morning')

        # "수요일 14:00" -> Wednesday 2026-09-23, exact 14:00
        d, period, exact, dur = scheduler.parse_schedule_expression('수요일 14:00', MONDAY)
        self.assertEqual(d, date(2026, 9, 23))
        self.assertEqual(exact, time(14, 0))

        # "목요일 오후 3시 (1시간)" -> Thursday 2026-09-24, exact 15:00, duration 60
        d, period, exact, dur = scheduler.parse_schedule_expression('목요일 오후 3시 (1시간)', MONDAY)
        self.assertEqual(d, date(2026, 9, 24))
        self.assertEqual(exact, time(15, 0))
        self.assertEqual(dur, 60)

        # "다음주 화요일" -> Tuesday next week = 2026-09-29
        d, period, exact, dur = scheduler.parse_schedule_expression('다음주 화요일 오후', MONDAY)
        self.assertEqual(d, date(2026, 9, 29))
        self.assertEqual(period, 'afternoon')


class SlotFinderTests(unittest.TestCase):
    def test_preferred_afternoon_slot_free(self):
        # Existing event at 12:00-14:00 (InnoEdu)
        events = [make_event('이노에듀', '2026-09-22 12:00', '2026-09-22 14:00')]
        start_dt, end_dt, conflicts = scheduler.find_free_slot(
            events, date(2026, 9, 22), period='afternoon', duration_minutes=120, tz=SEOUL
        )
        self.assertEqual(conflicts, [])
        self.assertEqual(start_dt, at('2026-09-22 14:00'))
        self.assertEqual(end_dt, at('2026-09-22 16:00'))

    def test_preferred_slot_blocked_shifts_to_later(self):
        # Events at 14:00-15:30
        events = [make_event('미팅 A', '2026-09-22 14:00', '2026-09-22 15:30')]
        start_dt, end_dt, conflicts = scheduler.find_free_slot(
            events, date(2026, 9, 22), period='afternoon', duration_minutes=120, tz=SEOUL
        )
        self.assertEqual(conflicts, [])
        # 14:00-16:00 is blocked, next available 2h block within 13:00-18:00 is 15:30-17:30
        self.assertEqual(start_dt, at('2026-09-22 15:30'))
        self.assertEqual(end_dt, at('2026-09-22 17:30'))

    def test_exact_time_conflict_detected(self):
        events = [make_event('팀 회의', '2026-09-22 14:00', '2026-09-22 15:00')]
        start_dt, end_dt, conflicts = scheduler.find_free_slot(
            events, date(2026, 9, 22), exact_time=time(14, 30), duration_minutes=60, tz=SEOUL
        )
        self.assertIsNone(start_dt)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['title'], '팀 회의')


class ScheduleExecutionTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(agent.subprocess, 'run', side_effect=AssertionError('external command forbidden in tests'))
        guard.start()
        self.addCleanup(guard.stop)
        self.temp_dir = tempfile.TemporaryDirectory(prefix='scheduler-test-')
        self.addCleanup(self.temp_dir.cleanup)
        self.cfg = agent.DEFAULTS | {
            'account': 'test@example.com',
            'state_dir': self.temp_dir.name,
            'capture_list': '업무'
        }

    def test_schedule_success_dual_write(self):
        mock_read = MockGogRecorder(
            events=[make_event('이노에듀 회의', '2026-09-22 12:00', '2026-09-22 14:00')]
        )
        now = at('2026-09-21 11:00')
        result = scheduler.schedule(
            self.cfg, now,
            title='원고 보고 고치기',
            when='화요일 오후',
            duration_minutes=120,
            read=mock_read
        )
        self.assertEqual(result['status'], 'scheduled')
        self.assertEqual(result['slot']['day'], '2026-09-22')
        self.assertEqual(result['slot']['start'], at('2026-09-22 14:00').isoformat())
        self.assertEqual(result['slot']['end'], at('2026-09-22 16:00').isoformat())
        self.assertEqual(result['calendar']['id'], 'cal-evt-999')
        # Check that both calendar create and tasks add were called
        cal_calls = [c for c in mock_read.calls if c[:2] == ['calendar', 'create']]
        task_calls = [c for c in mock_read.calls if c[:2] == ['tasks', 'add']]
        self.assertEqual(len(cal_calls), 1)
        self.assertEqual(len(task_calls), 1)
        # Check task notes reference calendar
        self.assertIn('캘린더 일정', task_calls[0][task_calls[0].index('--notes') + 1])

    def test_schedule_dry_run_no_writes(self):
        mock_read = MockGogRecorder()
        now = at('2026-09-21 11:00')
        result = scheduler.schedule(
            self.cfg, now,
            title='원고 보고 고치기',
            when='내일 14:00',
            duration_minutes=60,
            dry_run=True,
            read=mock_read
        )
        self.assertEqual(result['status'], 'dry_run')
        cal_calls = [c for c in mock_read.calls if c[:2] == ['calendar', 'create']]
        task_calls = [c for c in mock_read.calls if c[:2] == ['tasks', 'add']]
        self.assertIn('--dry-run', cal_calls[0])
        self.assertIn('--dry-run', task_calls[0])

    def test_schedule_duplicate_preflight_blocks_calendar_write(self):
        existing_task = {
            'id': 'twin-1', 'title': '원고 보고 고치기', 'status': 'needsAction',
            'due': '2026-09-22', 'updated': '2026-09-21T00:00:00.000Z'
        }
        mock_read = MockGogRecorder(tasks=[existing_task])
        now = at('2026-09-21 11:00')
        with self.assertRaises(secretary.Decision) as ctx:
            scheduler.schedule(
                self.cfg, now,
                title='원고 보고 고치기',
                when='화요일 오후',
                read=mock_read
            )
        self.assertEqual(ctx.exception.payload['status'], 'duplicate')
        # Ensure NO calendar create call was made
        cal_calls = [c for c in mock_read.calls if c[:2] == ['calendar', 'create']]
        self.assertEqual(len(cal_calls), 0)


if __name__ == '__main__':
    unittest.main()
