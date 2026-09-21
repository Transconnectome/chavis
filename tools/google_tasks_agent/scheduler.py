"""Natural language scheduling, calendar slot discovery, and dual Tasks+Calendar registration.

Bridges the Google Tasks reminder with Google Calendar: captures intentions like
"화요일 오후에 할까?", checks calendar availability, allocates an open slot, and registers
both the Google Calendar event and the Google Tasks action item with cross-referencing metadata.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import agent
import secretary
import sources
import tiers


WEEKDAY_WORDS = {
    '월': 0, '월요일': 0, 'mon': 0, 'monday': 0,
    '화': 1, '화요일': 1, 'tue': 1, 'tuesday': 1,
    '수': 2, '수요일': 2, 'wed': 2, 'wednesday': 2,
    '목': 3, '목요일': 3, 'thu': 3, 'thursday': 3,
    '금': 4, '금요일': 4, 'fri': 4, 'friday': 4,
    '토': 5, '토요일': 5, 'sat': 5, 'saturday': 5,
    '일': 6, '일요일': 6, 'sun': 6, 'sunday': 6,
}

REL_DAY_WORDS = {
    '오늘': 0, '금일': 0, 'today': 0,
    '내일': 1, '명일': 1, 'tomorrow': 1,
    '모레': 2,
    '글피': 3,
}

PERIOD_WINDOWS = {
    'morning': (time(9, 0), time(12, 0), time(10, 0)),
    'afternoon': (time(13, 0), time(18, 0), time(14, 0)),
    'evening': (time(18, 0), time(22, 0), time(19, 0)),
    'night': (time(20, 0), time(23, 0), time(20, 0)),
}

PERIOD_KEYWORDS = {
    '오전': 'morning', '아침': 'morning', 'morning': 'morning',
    '오후': 'afternoon', '낮': 'afternoon', 'afternoon': 'afternoon',
    '저녁': 'evening', 'evening': 'evening',
    '밤': 'night', 'night': 'night',
}


def parse_duration_minutes(text: str, default: int = 120) -> int:
    """Extract duration in minutes from strings like '2시간', '1시간 30분', '90분', '2h', '1.5h'."""
    if not text:
        return default
    text = text.strip().casefold()
    m_float_h = re.search(r'(\d+\.\d+)\s*(?:시간|h|hours?)', text)
    if m_float_h:
        return int(float(m_float_h.group(1)) * 60)
    m_hour_min = re.search(r'(\d+)\s*(?:시간|h)\s*(\d+)?\s*(?:분|m)?', text)
    if m_hour_min:
        hours = int(m_hour_min.group(1))
        minutes = int(m_hour_min.group(2) or 0)
        return hours * 60 + minutes
    m_min = re.search(r'(\d+)\s*(?:분|m|min)', text)
    if m_min:
        return int(m_min.group(1))
    try:
        return int(text)
    except ValueError:
        return default


def parse_schedule_expression(
    phrase: str,
    today: date,
    default_duration: int = 120
) -> Tuple[date, Optional[str], Optional[time], int]:
    """Parse relative expressions like '화요일 오후', '내일 14:00', '다음 주 수요일 오전 (1시간)'.

    Returns (target_date, period_name, exact_time, duration_minutes).
    """
    phrase = ' '.join((phrase or '').split())
    if not phrase:
        raise ValueError('when_expression_cannot_be_empty')

    # 1. Duration extraction
    duration = parse_duration_minutes(phrase, default_duration)

    # 2. Check next-week flag
    is_next_week = bool(re.search(r'(?:다음\s*주|차주|next\s*week)', phrase, re.IGNORECASE))

    # 3. Time of day / Exact time extraction
    exact_time: Optional[time] = None
    period: Optional[str] = None

    # Exact time pattern: "14:00", "오후 2시", "오전 10시 30분", "15시"
    m_time = re.search(r'(?:(?P<ampm>오전|오후)\s*)?(?P<h>\d{1,2})\s*(?:시(?:\s*(?P<m>\d{1,2})\s*분)?|:(?P<m2>\d{2}))', phrase)
    if m_time:
        raw_h = int(m_time.group('h'))
        raw_m = int(m_time.group('m') or m_time.group('m2') or 0)
        ampm = m_time.group('ampm')
        if ampm == '오후' and raw_h < 12:
            raw_h += 12
        elif ampm == '오전' and raw_h == 12:
            raw_h = 0
        if 0 <= raw_h <= 23 and 0 <= raw_m <= 59:
            exact_time = time(raw_h, raw_m)
            period = 'exact'

    if not exact_time:
        for kw, p_name in PERIOD_KEYWORDS.items():
            if kw in phrase:
                period = p_name
                break

    # 4. Date extraction
    target_date: Optional[date] = None

    # Check relative day words (오늘, 내일, 모레)
    for kw, offset in REL_DAY_WORDS.items():
        if kw in phrase:
            target_date = today + timedelta(days=offset)
            break

    # Check explicit ISO date
    if not target_date:
        m_iso = re.search(r'\b(20\d{2}-\d{1,2}-\d{1,2})\b', phrase)
        if m_iso:
            try:
                target_date = date.fromisoformat(m_iso.group(1))
            except ValueError:
                pass

    # Check M/D or M월 D일
    if not target_date:
        m_md = re.search(r'(?:(?P<y>20\d{2})\s*년\s*)?(?P<m>\d{1,2})\s*(?:/|월)\s*(?P<d>\d{1,2})\s*일?', phrase)
        if m_md:
            y = int(m_md.group('y') or today.year)
            m = int(m_md.group('m'))
            d = int(m_md.group('d'))
            try:
                target_date = date(y, m, d)
            except ValueError:
                pass

    # Check weekday words
    if not target_date:
        for w_word, w_idx in WEEKDAY_WORDS.items():
            if re.search(rf'(?<![가-힣]){w_word}(?![가-힣])', phrase) or (w_word in phrase and '요일' in phrase):
                diff = (w_idx - today.weekday()) % 7
                if diff == 0 and not any(w in phrase for w in ('오늘', '금일', 'today')):
                    diff = 7
                if is_next_week:
                    diff += 7
                target_date = today + timedelta(days=diff)
                break

    # Default to today if nothing matched
    if not target_date:
        try:
            target_date = secretary.parse_day(phrase.split()[0], today)
        except Exception:
            raise ValueError(f'could_not_parse_date_from_{phrase}')

    if not exact_time and not period:
        period = 'afternoon'

    return target_date, period, exact_time, duration


def find_free_slot(
    events: List[Dict[str, Any]],
    target_date: date,
    period: str = 'afternoon',
    duration_minutes: int = 120,
    exact_time: Optional[time] = None,
    tz: Optional[ZoneInfo] = None
) -> Tuple[Optional[datetime], Optional[datetime], List[Dict[str, Any]]]:
    """Find a non-overlapping window of `duration_minutes` on `target_date`.

    Returns (start_datetime, end_datetime, conflicting_events).
    """
    tz = tz or ZoneInfo('Asia/Seoul')
    day_events = sources.events_on(events, target_date)
    timed_events = [e for e in day_events if not e.get('all_day') and e.get('start') and e.get('end')]

    duration = timedelta(minutes=duration_minutes)

    # Case A: Exact time requested
    if exact_time is not None:
        slot_start = datetime.combine(target_date, exact_time, tzinfo=tz)
        slot_end = slot_start + duration
        conflicts = [
            e for e in timed_events
            if not (e['end'] <= slot_start or e['start'] >= slot_end)
        ]
        if conflicts:
            return None, None, conflicts
        return slot_start, slot_end, []

    # Case B: Period requested (morning, afternoon, evening, night)
    win_start_time, win_end_time, pref_start_time = PERIOD_WINDOWS.get(
        period, PERIOD_WINDOWS['afternoon']
    )
    win_start = datetime.combine(target_date, win_start_time, tzinfo=tz)
    win_end = datetime.combine(target_date, win_end_time, tzinfo=tz)

    # 1. Try preferred start time first (e.g. 14:00 for afternoon)
    cand_start = datetime.combine(target_date, pref_start_time, tzinfo=tz)
    cand_end = cand_start + duration
    if cand_end <= win_end:
        conflicts = [e for e in timed_events if not (e['end'] <= cand_start or e['start'] >= cand_end)]
        if not conflicts:
            return cand_start, cand_end, []

    # 2. Preferred time is blocked; search candidates including event end points and 30m grid
    grid_points = []
    curr = win_start
    while curr + duration <= win_end:
        grid_points.append(curr)
        curr += timedelta(minutes=30)

    event_ends = [
        e['end'] for e in timed_events
        if win_start <= e['end'] and e['end'] + duration <= win_end
    ]

    candidates = sorted(set([cand_start] + event_ends + grid_points))
    all_conflicts = []
    for start_c in candidates:
        if start_c < win_start or start_c + duration > win_end:
            continue
        end_c = start_c + duration
        conflicts = [e for e in timed_events if not (e['end'] <= start_c or e['start'] >= end_c)]
        if not conflicts:
            return start_c, end_c, []
        all_conflicts.extend(conflicts)

    # 3. No non-overlapping slot found within the window
    seen, unique_conflicts = set(), []
    for c in all_conflicts:
        cid = c.get('title', '') + str(c.get('start'))
        if cid not in seen:
            seen.add(cid)
            unique_conflicts.append(c)

    return None, None, unique_conflicts


def schedule(
    cfg: Dict[str, Any],
    now: datetime,
    title: str,
    when: str,
    duration_minutes: int = 120,
    calendar_id: str = 'primary',
    list_name: str = '',
    notes: str = '',
    allow_duplicate: bool = False,
    dry_run: bool = False,
    read: Callable = agent.gog_json,
    events_override: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """Execute dual Tasks + Calendar registration.

    Validates duplicates first, identifies an open calendar slot, registers on Google Calendar,
    and creates the Google Tasks entry linked to the calendar event.
    """
    today = now.date()
    title = ' '.join((title or '').split())
    if not title or len(title) > 1024:
        raise ValueError('title_must_be_1_to_1024_characters')

    target_date, period, exact_time, duration = parse_schedule_expression(when, today, duration_minutes)

    # Step 1: Pre-flight check for duplicate task before doing any calendar writes
    if not allow_duplicate:
        lists = secretary.task_lists(cfg, read)
        target_list = secretary.pick_list(lists, list_name or cfg.get('capture_list', ''))
        rows = read(cfg, ['tasks', 'list', target_list['id'], '--all', '--max', '100']).get('tasks') or []
        twins = [
            {'key': f"{target_list['id']}/{row['id']}", 'title': row.get('title', ''),
             'list_title': target_list.get('title', ''), 'due': (row.get('due') or '')[:10],
             'updated': row.get('updated', '')}
            for row in rows if isinstance(row, dict) and row.get('id') and row.get('status') == 'needsAction'
        ]
        for existing in twins:
            if secretary.normal(existing['title']) == secretary.normal(title):
                raise tiers.Decision({
                    'status': 'duplicate',
                    'existing': secretary.describe(existing, today),
                    'hint': '같은 제목의 미완료 작업이 이미 있습니다. 일정을 변경하려면 due, 새로 등록하려면 --allow-duplicate.'
                })

    # Step 2: Query events and locate free slot
    if events_override is not None:
        calendar_events = events_override
    else:
        calendar_events = sources.fetch_events(cfg, now, read, days=14) or []

    start_dt, end_dt, conflicts = find_free_slot(
        calendar_events,
        target_date,
        period=period or 'afternoon',
        duration_minutes=duration,
        exact_time=exact_time,
        tz=now.tzinfo
    )

    if start_dt is None or end_dt is None:
        conflict_summaries = [
            f"{c.get('title', '(제목없음)')} ({c.get('start', now).strftime('%H:%M')}~{c.get('end', now).strftime('%H:%M')})"
            for c in conflicts
        ]
        raise tiers.Decision({
            'status': 'slot_conflict',
            'day': target_date.isoformat(),
            'when': when,
            'conflicts': conflict_summaries,
            'hint': f"{target_date.isoformat()} 해당 시간대에 기존 일정이 있습니다 ({', '.join(conflict_summaries)}). 다른 시간을 지정해주세요."
        })

    # Step 3: Calendar write (or dry-run)
    slot_label = f"{target_date.month}/{target_date.day}({tiers.WEEKDAYS[target_date.weekday()]}) {start_dt.strftime('%H:%M')}~{end_dt.strftime('%H:%M')}"
    cal_desc = f"📋 Google Tasks 연동: {title}\n등록 시각: {now.strftime('%Y-%m-%d %H:%M')}"

    cal_command = [
        'calendar', 'create', calendar_id,
        '--summary', title,
        '--from', start_dt.isoformat(),
        '--to', end_dt.isoformat(),
        '--description', cal_desc
    ]
    if dry_run:
        cal_command.append('--dry-run')

    cal_res = read(cfg, cal_command)
    cal_event = cal_res.get('event', cal_res) if not dry_run else {'id': 'dry-run-cal-id', 'htmlLink': ''}
    cal_id = cal_event.get('id', '')
    cal_link = cal_event.get('htmlLink', '')

    # Step 4: Google Tasks write
    task_notes = f"📅 캘린더 일정: {slot_label}\n"
    if cal_link:
        task_notes += f"🔗 {cal_link}\n"
    if notes:
        task_notes += f"\n{notes}"

    try:
        task_res = secretary.add(
            cfg, now,
            title=title,
            due=target_date.isoformat(),
            list_name=list_name,
            notes=task_notes,
            allow_duplicate=True,  # Already pre-flight checked
            allow_past=False,
            dry_run=dry_run,
            read=read
        )
    except Exception as exc:
        if not dry_run and cal_id:
            try:
                read(cfg, ['calendar', 'delete', calendar_id, cal_id, '--force'])
            except Exception:
                pass
        raise exc

    return {
        'status': 'dry_run' if dry_run else 'scheduled',
        'title': title,
        'slot': {
            'day': target_date.isoformat(),
            'start': start_dt.isoformat(),
            'end': end_dt.isoformat(),
            'label': slot_label,
            'duration_minutes': duration
        },
        'calendar': {
            'id': cal_id,
            'link': cal_link,
            'summary': title
        },
        'task': task_res
    }
