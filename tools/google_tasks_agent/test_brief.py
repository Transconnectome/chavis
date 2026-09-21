"""Offline tests for side sources and the integrated brief. Synthetic data only."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brief  # noqa: E402
import sources  # noqa: E402


SEOUL = ZoneInfo('Asia/Seoul')
CFG = {'account': 'reminder-test@example.com', 'upcoming_days': 3, 'stale_days': 14, 'new_undated_days': 3,
       'calendar': True, 'mail': True, 'calendar_exclude': [], 'oauth_ttl_days': 7}


def at(value='2026-09-21 13:50'):
    return datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=SEOUL)


def task(key, title=None, due='', updated='2026-09-01T00:00:00.000Z', list_title='업무'):
    return {'key': key, 'id': key, 'list_id': 'work', 'list_title': list_title, 'title': title or f'작업 {key}',
            'due': due, 'updated': updated, 'fingerprint': 'f-' + key}


def timed(title, start, end, **extra):
    return {'summary': title, 'status': 'confirmed', 'start': {'dateTime': start}, 'end': {'dateTime': end}, **extra}


def all_day(title, first, end_exclusive, **extra):
    return {'summary': title, 'status': 'confirmed', 'start': {'date': first}, 'end': {'date': end_exclusive}, **extra}


def reader(events=None, threads=None, accounts=None, fail=()):
    def read(cfg, args):
        if args[0] in fail:
            raise RuntimeError('source down')
        if args[0] == 'calendar':
            return {'events': events or []}
        if args[0] == 'gmail':
            return {'threads': threads or []}
        return {'accounts': accounts or []}
    return read


class SourceTests(unittest.TestCase):
    def test_all_day_end_is_exclusive_and_other_zones_become_local(self):
        event = sources.normalize_event(all_day('공모 접수', '2026-09-15', '2026-09-22'), SEOUL)
        self.assertEqual((event['first_day'], event['last_day']), (date(2026, 9, 15), date(2026, 9, 21)))
        remote = sources.normalize_event(timed('해외 회의', '2026-09-21T09:00:00-04:00', '2026-09-21T10:00:00-04:00'), SEOUL)
        self.assertEqual(remote['start'], at('2026-09-21 22:00'))

    def test_cancelled_declined_and_malformed_rows_are_dropped(self):
        self.assertIsNone(sources.normalize_event({'summary': 'x', 'status': 'cancelled'}, SEOUL))
        declined = timed('불참', '2026-09-21T10:00:00+09:00', '2026-09-21T11:00:00+09:00',
                         attendees=[{'self': True, 'responseStatus': 'declined'}])
        self.assertIsNone(sources.normalize_event(declined, SEOUL))
        self.assertIsNone(sources.normalize_event({'summary': 'no start'}, SEOUL))
        self.assertIsNone(sources.normalize_event('not a dict', SEOUL))

    def test_events_are_sorted_filtered_and_multi_day_rows_show_on_edges_only(self):
        read = reader(events=[
            timed('오후 회의', '2026-09-21T16:00:00+09:00', '2026-09-21T17:00:00+09:00'),
            timed('끝난 회의', '2026-09-21T09:00:00+09:00', '2026-09-21T10:00:00+09:00'),
            all_day('긴 공모전', '2026-09-15', '2026-09-26', organizer={'displayName': '구독 달력'}),
            all_day('오늘 마감 공모', '2026-09-15', '2026-09-22'),
            timed('숨길 일정', '2026-09-21T15:00:00+09:00', '2026-09-21T15:30:00+09:00', organizer={'displayName': 'Noise Cal'}),
        ])
        events = sources.fetch_events(CFG | {'calendar_exclude': ['noise']}, at(), read)
        today = [e['title'] for e in sources.events_on(events, date(2026, 9, 21), at())]
        self.assertEqual(today, ['오늘 마감 공모', '오후 회의'])
        self.assertEqual([e['title'] for e in sources.events_on(events, date(2026, 9, 25))], ['긴 공모전'])

    def test_every_source_is_fail_soft(self):
        context = sources.gather(CFG, at(), reader(fail=('calendar', 'gmail', 'auth')))
        self.assertEqual(context, {'events': None, 'mail': None, 'issued': None})
        self.assertIsNone(sources.fetch_events(CFG, at(), lambda cfg, args: {'events': 'broken'}))
        off = sources.gather(CFG | {'calendar': False, 'mail': False, 'oauth_ttl_days': 0}, at(),
                             lambda cfg, args: self.fail('disabled source was read'))
        self.assertEqual(off, {'events': None, 'mail': None, 'issued': None})

    def test_mail_deadline_hints_and_credential_age(self):
        read = reader(threads=[{'subject': '참석 인원 조사 (~9/20)', 'from': '"담당자" <a@example.com>'},
                               {'subject': '논문 초안 공유', 'from': 'b@example.com'},
                               {'subject': 'Submission deadline reminder', 'from': 'c@example.com'}],
                      accounts=[{'email': 'Reminder-Test@example.com', 'created': '2026-09-21T02:11:33Z'}])
        mail = sources.fetch_mail(CFG, at(), read)
        self.assertEqual([(m['flagged'], m['deadline']) for m in mail['threads']],
                         [(True, '2026-09-20'), (False, ''), (True, '')])
        self.assertEqual(mail['threads'][0]['from'], '담당자')
        self.assertEqual(sources.fetch_credential_age(CFG, at(), read), at('2026-09-21 11:11').replace(second=33))
        self.assertIsNone(sources.fetch_credential_age(CFG, at(), reader(accounts=[{'email': 'other@example.com'}])))


class BriefTests(unittest.TestCase):
    def tasks(self, *rows):
        return {row['key']: row for row in rows}

    def events(self, *rows):
        return sources.fetch_events(CFG, at(), reader(events=list(rows)))

    def test_sections_follow_urgency_and_members_equal_printed_tasks(self):
        tasks = self.tasks(task('late', '지난 평가', due='2026-09-18'), task('now', '오늘 회신', due='2026-09-21'),
                           task('wed', '초록 제출', due='2026-09-23'), task('titled', '보고서 9/25까지'),
                           task('ancient', '오래된 기록', due='2026-01-19'),
                           task('fresh', '방금 말로 등록한 일', updated='2026-09-21T04:34:33.543Z'),
                           task('star', '중요 과제', list_title='High Priority'), task('far', '먼 일', due='2026-12-01'))
        text, members = brief.render(tasks, CFG, at(), events=self.events(), mail=None, issued=None)
        order = [text.index(label) for label in ('🔴 기한 경과', '🟠 오늘 일과 종료 전', '🟡 이번 주 안에', '🆕 최근 등록', '⭐ High Priority')]
        self.assertEqual(order, sorted(order))
        self.assertIn('보고서 9/25까지 (9/25(금) · D-4 · 제목의 기한)', text)
        self.assertNotIn('오래된 기록', text)
        self.assertNotIn('먼 일', text)
        self.assertIn('오래된 경과 1', text)
        self.assertEqual(set(members), {'late', 'now', 'wed', 'titled', 'fresh', 'star'})
        self.assertTrue(all(tasks[key]['title'] in text for key in members))
        self.assertTrue(text.endswith(brief.LINK))

    def test_right_now_prefers_running_then_imminent_meeting_then_most_urgent_task(self):
        tasks = self.tasks(task('late', '지난 평가', due='2026-09-18'), task('now', '오늘 회신', due='2026-09-21'))
        meeting = timed('면담', '2026-09-21T14:00:00+09:00', '2026-09-21T15:00:00+09:00')
        line = lambda moment: brief.render(tasks, CFG, moment, events=self.events(meeting))[0].split('\n')[1]
        self.assertEqual(line(at('2026-09-21 14:10')), '▶ 지금: 진행 중 · 14:00–15:00 면담')
        self.assertEqual(line(at('2026-09-21 13:50')), '▶ 지금: 10분 뒤 시작 · 14:00–15:00 면담')
        self.assertEqual(line(at('2026-09-21 11:30')), '▶ 지금: 지난 평가 — 다음 일정 14:00까지 2시간 30분')
        self.assertEqual(line(at('2026-09-21 15:30')), '▶ 지금: 지난 평가')
        calm = brief.render(self.tasks(task('wed', '초록 제출', due='2026-09-23')), CFG, at(), events=[])[0]
        self.assertIn('오늘 마감 없음 · 이번 주 먼저 볼 일 — 초록 제출', calm)

    def test_unreadable_calendar_is_said_aloud_and_disabled_calendar_is_silent(self):
        tasks = self.tasks(task('now', due='2026-09-21'))
        self.assertIn('캘린더를 읽지 못했습니다', brief.render(tasks, CFG, at(), events=None)[0])
        self.assertNotIn('캘린더', brief.render(tasks, CFG | {'calendar': False}, at(), events=None)[0])
        self.assertIn('🗓 오늘 남은 일정: 없음', brief.render(tasks, CFG, at(), events=[])[0])

    def test_tomorrow_block_only_in_the_evening_and_mail_block_lists_flagged_threads(self):
        tomorrow = timed('랩미팅', '2026-09-22T09:00:00+09:00', '2026-09-22T10:00:00+09:00')
        mail = {'threads': [{'subject': '조사 (~9/20)', 'from': '담당자', 'deadline': '2026-09-20', 'flagged': True},
                            {'subject': '안부', 'from': '동료', 'deadline': '', 'flagged': False}], 'more': True}
        morning = brief.render({}, CFG, at('2026-09-21 08:30'), events=self.events(tomorrow), mail=mail)[0]
        evening = brief.render({}, CFG, at('2026-09-21 17:30'), events=self.events(tomorrow), mail=mail)[0]
        self.assertNotIn('내일 일정', morning)
        self.assertIn('🗓 내일 일정\n• 09:00–10:00 랩미팅', evening)
        self.assertIn('📧 중요·미확인 메일 2+건 · 마감 언급 1건\n• 조사 (~9/20) (9/20(일) · 1일 경과) — 담당자', evening)
        self.assertNotIn('안부', evening)

    def test_credential_warning_window(self):
        issued = at('2026-09-21 11:11')
        self.assertEqual(brief.credential_warning(issued, CFG, at('2026-09-26 11:00')), '')
        self.assertIn('만료 예상: 09/28 11:11 (약 24시간 후)', brief.credential_warning(issued, CFG, at('2026-09-27 11:00')))
        self.assertIn('지났습니다', brief.credential_warning(issued, CFG, at('2026-09-28 12:00')))
        self.assertEqual(brief.credential_warning(issued, CFG | {'oauth_ttl_days': 0}, at('2026-09-28 12:00')), '')
        self.assertEqual(brief.credential_warning(None, CFG, at()), '')

    def test_utf16_limit_holds_with_astral_titles_and_every_block_full(self):
        rows = [task(f'{name}-{i}', '🧑🏽‍🔬' * 100, due=due, updated=updated, list_title=list_title)
                for name, due, updated, list_title in (
                    ('late', '2026-09-18', '2026-09-01T00:00:00.000Z', '업무'), ('today', '2026-09-21', '2026-09-01T00:00:00.000Z', '업무'),
                    ('week', '2026-09-24', '2026-09-01T00:00:00.000Z', '업무'), ('fresh', '', '2026-09-21T01:00:00.000Z', '업무'),
                    ('star', '', '2026-09-01T00:00:00.000Z', 'High Priority'), ('rest', '', '2026-09-01T00:00:00.000Z', '업무'))
                for i in range(9)]
        tasks = self.tasks(*rows)
        events = self.events(*(timed('🧑🏽‍🔬' * 100, f'2026-09-21T{hour}:00:00+09:00', f'2026-09-21T{hour}:30:00+09:00',
                                     organizer={'displayName': '🧑🏽‍🔬' * 50}) for hour in range(14, 23)),
                             *(timed('🧑🏽‍🔬' * 100, f'2026-09-22T{hour:02}:00:00+09:00', f'2026-09-22T{hour:02}:30:00+09:00') for hour in range(8, 14)))
        mail = {'threads': [{'subject': '🧑🏽‍🔬' * 100, 'from': '🧑🏽‍🔬' * 40, 'deadline': '2026-09-22', 'flagged': True}] * 5, 'more': True}
        text, members = brief.render(tasks, CFG, at('2026-09-21 17:30'), events=events, mail=mail, issued=at('2026-09-15 11:11'))
        self.assertLessEqual(brief.units(text), brief.LIMIT)
        self.assertNotIn('�', text)
        self.assertTrue(text.endswith(brief.LINK))
        self.assertIn('🔴 기한 경과', text)
        self.assertIn('🟠 오늘 일과 종료 전', text)
        self.assertTrue(members)
        self.assertTrue(all(key.split('-')[0] in ('late', 'today', 'week', 'fresh', 'star', 'rest') for key in members))

    def test_overflow_drops_optional_blocks_before_touching_urgent_ones(self):
        tasks = self.tasks(*(task(f'today-{i}', 'ㄱ' * 84, due='2026-09-21') for i in range(6)),
                           *(task(f'rest-{i}', '선택 구간') for i in range(5)))
        _, blocks = brief.build(tasks, CFG | {'calendar': False}, at())
        full = brief.units(brief.fit(['머리'], blocks, ['꼬리'])[0])
        self.addCleanup(setattr, brief, 'LIMIT', brief.LIMIT)
        brief.LIMIT = full - 1
        text, kept = brief.fit(['머리'], blocks, ['꼬리'])
        self.assertNotIn('선택 구간', text)
        self.assertEqual(set(brief.members_of(kept)), {f'today-{i}' for i in range(6)})
        # With no optional block left, urgent rows are trimmed last and the cut is announced.
        brief.LIMIT = brief.units(text) - 1
        shorter, kept = brief.fit(['머리'], kept, ['꼬리'])
        self.assertLessEqual(brief.units(shorter), brief.LIMIT)
        self.assertIn('(일부 생략)', shorter)
        self.assertEqual(len(brief.members_of(kept)), 5)

    def test_nudge_is_silent_without_critical_work_and_ignores_stale_items(self):
        quiet = self.tasks(task('ancient', due='2026-01-19'), task('wed', due='2026-09-23'), task('free'))
        self.assertEqual(brief.nudge(quiet, CFG, at()), ('', {}))
        busy = self.tasks(task('late', '지난 평가', due='2026-09-18'), task('now', '오늘 회신', due='2026-09-21'),
                          task('ancient', '오래된 기록', due='2026-01-19'))
        text, members = brief.nudge(busy, CFG, at('2026-09-21 16:30'), events=[])
        self.assertIn('끝내야 할 일 2건', text)
        self.assertNotIn('오래된 기록', text)
        self.assertEqual(set(members), {'late', 'now'})
        self.assertLessEqual(brief.units(text), brief.LIMIT)

    def test_nudge_caps_the_list_and_reports_only_printed_members(self):
        tasks = self.tasks(*(task(f'now-{i}', f'오늘 일 {i:02}', due='2026-09-21') for i in range(9)))
        text, members = brief.nudge(tasks, CFG, at(), events=None)
        self.assertEqual(len(members), 6)
        self.assertIn('외 3개', text)
        self.assertEqual({key for key in tasks if tasks[key]['title'] in text}, set(members))


if __name__ == '__main__':
    unittest.main()
