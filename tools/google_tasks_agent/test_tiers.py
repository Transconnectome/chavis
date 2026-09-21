"""Offline tests for deadline parsing and urgency tiers. Synthetic titles only."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tiers  # noqa: E402


SEOUL = ZoneInfo('Asia/Seoul')
MONDAY = date(2026, 9, 21)


def task(key, title='확인할 작업', due='', updated='2026-09-21T01:00:00.000Z', list_title='업무'):
    return {'key': key, 'id': key, 'list_id': 'work', 'list_title': list_title, 'title': title,
            'due': due, 'updated': updated, 'fingerprint': 'f-' + key}


class TitleDeadlineTests(unittest.TestCase):
    def parsed(self, title, anchor=MONDAY):
        found = tiers.title_deadline(title, anchor)
        return found.isoformat() if found else None

    def test_until_marker_with_weekday_and_time(self):
        self.assertEqual(self.parsed('서면평가 요청 (9월 18일 오전 10시까지)'), '2026-09-18')
        self.assertEqual(self.parsed('9월 30일(수) 17:00까지 제출'), '2026-09-30')
        self.assertEqual(self.parsed('~ 9월 25일 (금) 까지 입력'), '2026-09-25')
        self.assertEqual(self.parsed('2026-09-25까지 회신'), '2026-09-25')
        self.assertEqual(self.parsed('2027.1.5.까지 제출'), '2027-01-05')

    def test_leading_tilde_is_a_deadline_but_a_range_end_is_not(self):
        self.assertEqual(self.parsed('(~10/2) 보고서'), '2026-10-02')
        self.assertIsNone(self.parsed('학회 9/15~9/21 참석'))
        self.assertIsNone(self.parsed('학회 9/15 ~ 9/21 참석'))
        self.assertEqual(self.parsed('접수 9/15~9/30까지'), '2026-09-30')

    def test_bare_event_date_is_not_a_deadline(self):
        self.assertIsNone(self.parsed('10월 27일(화) 웨비나 : 발표 초록 제출 요청'))
        self.assertIsNone(self.parsed('오후 3시까지 회신'))
        self.assertIsNone(self.parsed('날짜 없는 평범한 작업'))

    def test_numbers_that_only_look_like_dates_are_ignored(self):
        for title in ('880-1234까지 전화', 'v1.2까지 릴리스', '2/30까지 불가능한 날짜', '버전 1.5까지 반영',
                      '챕터 2-3까지 읽기', '~10.5% 인상안 검토', '9.25까지 (연도 없는 점 표기)'):
            self.assertIsNone(self.parsed(title), title)

    def test_padded_titles_are_collapsed_before_matching(self):
        self.assertEqual(self.parsed('보고서' + ' ' * 5000 + '9/25까지'), '2026-09-25')
        self.assertIsNone(self.parsed('1' + ' ' * 5000 + '2'))

    def test_earliest_of_several_deadlines_wins(self):
        self.assertEqual(self.parsed('1차 9/20까지, 최종 10/5까지'), '2026-09-20')

    def test_missing_year_is_inferred_from_the_anchor_across_new_year(self):
        self.assertEqual(self.parsed('1/15까지 연차보고', date(2026, 12, 20)), '2027-01-15')
        self.assertEqual(self.parsed('(~5/11(월)까지) 심의 결과', date(2026, 5, 4)), '2026-05-11')
        # A deadline a few days before the anchor is a missed deadline, not next year's.
        self.assertEqual(self.parsed('9월 18일까지', MONDAY), '2026-09-18')


class EffectiveDueTests(unittest.TestCase):
    def test_google_date_alone_and_title_alone(self):
        self.assertEqual(tiers.effective_due(task('a', due='2026-09-23'), MONDAY), ('2026-09-23', 'google'))
        self.assertEqual(tiers.effective_due(task('b', title='보고서 9/25까지'), MONDAY), ('2026-09-25', 'title'))
        self.assertEqual(tiers.effective_due(task('c'), MONDAY), ('', ''))

    def test_postponing_past_the_written_deadline_does_not_silence_it(self):
        moved = task('a', title='보고서 9/22까지', due='2026-09-30')
        self.assertEqual(tiers.effective_due(moved, MONDAY), ('2026-09-22', 'title'))
        planned_early = task('b', title='보고서 9/30까지', due='2026-09-22')
        self.assertEqual(tiers.effective_due(planned_early, MONDAY), ('2026-09-22', 'google'))

    def test_unreadable_updated_falls_back_to_today(self):
        self.assertEqual(tiers.effective_due(task('a', title='9/25까지', updated='garbage'), MONDAY),
                         ('2026-09-25', 'title'))


class TierTests(unittest.TestCase):
    def test_week_horizon_is_sunday_but_never_nearer_than_upcoming_days(self):
        self.assertEqual(tiers.week_end(MONDAY, 3), date(2026, 9, 27))
        friday = date(2026, 9, 25)
        self.assertEqual(tiers.week_end(friday, 3), date(2026, 9, 28))
        self.assertEqual(tiers.week_end(date(2026, 9, 27), 3), date(2026, 9, 30))

    def test_boundaries(self):
        horizon = tiers.week_end(MONDAY, 3)
        expect = {'': 'undated', '2026-09-06': 'stale', '2026-09-07': 'overdue', '2026-09-20': 'overdue',
                  '2026-09-21': 'today', '2026-09-22': 'week', '2026-09-27': 'week', '2026-09-28': 'later'}
        for due, tier in expect.items():
            self.assertEqual(tiers.tier_of(due, MONDAY, horizon, 14), tier, due)

    def test_classify_orders_recent_overdue_first_and_week_soonest_first(self):
        groups = tiers.classify([task('old', due='2026-09-10'), task('new', due='2026-09-19'),
                                 task('thu', due='2026-09-24'), task('tue', due='2026-09-22'),
                                 task('ancient', due='2026-01-19'), task('free')], MONDAY)
        self.assertEqual([t['key'] for t in groups['overdue']], ['new', 'old'])
        self.assertEqual([t['key'] for t in groups['week']], ['tue', 'thu'])
        self.assertEqual([t['key'] for t in groups['stale']], ['ancient'])
        self.assertEqual([t['key'] for t in tiers.critical(groups)], ['new', 'old'])
        self.assertEqual(groups['undated'][0]['eff_due'], '')

    def test_recently_added_undated_window(self):
        now = datetime(2026, 9, 21, 14, 0, tzinfo=SEOUL)
        groups = tiers.classify([task('fresh', updated='2026-09-21T04:34:33.543Z'),
                                 task('older', updated='2026-09-19T00:00:00.000Z'),
                                 task('gone', updated='2026-09-10T00:00:00.000Z'),
                                 task('broken', updated=''),
                                 task('dated', due='2026-09-22', updated='2026-09-21T04:00:00.000Z')], MONDAY)
        self.assertEqual([t['key'] for t in tiers.recently_added_undated(groups, now, 3)], ['fresh', 'older'])
        self.assertEqual(tiers.recently_added_undated(groups, now, 0), [])

    def test_day_label(self):
        self.assertEqual(tiers.day_label('2026-09-21', MONDAY), '9/21(월) · 오늘')
        self.assertEqual(tiers.day_label('2026-09-22', MONDAY), '9/22(화) · 내일')
        self.assertEqual(tiers.day_label('2026-09-25', MONDAY), '9/25(금) · D-4')
        self.assertEqual(tiers.day_label('2026-09-18', MONDAY), '9/18(금) · 3일 경과')


if __name__ == '__main__':
    unittest.main()
