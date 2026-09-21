"""Render the integrated brief: what to do now, before the end of today, and this week.

`render` and `nudge` return (text, members). `members` maps every task actually printed
to its fingerprint, which is what the caller records as delivered. Pure: no IO, no clock.
"""
from __future__ import annotations

from datetime import timedelta

import sources
import tiers


LIMIT = 4096          # Telegram counts UTF-16 code units.
TITLE_UNITS = 84
LINK = 'https://tasks.google.com/'


def units(text):
    return len(text.encode('utf-16-le')) // 2


def clip(value, limit=TITLE_UNITS):
    text = ' '.join(str(value).split())
    return text.encode('utf-16-le')[:limit * 2].decode('utf-16-le', errors='ignore')


def clock(moment):
    return f'{moment:%H:%M}'


def event_text(event):
    origin = f' · {clip(event["calendar"], 24)}' if event['calendar'] else ''
    if event['all_day']:
        return f'(종일) {clip(event["title"])}{origin}'
    return f'{clock(event["start"])}–{clock(event["end"])} {clip(event["title"])}{origin}'


def task_text(task, today):
    if not task.get('eff_due'):
        return clip(task['title'])
    mark = ' · 제목의 기한' if task.get('due_source') == 'title' else ''
    return f'{clip(task["title"])} ({tiers.day_label(task["eff_due"], today)}{mark})'


def rotate(group, cap, cycle, fixed=0):
    """First `fixed` rows always show; the rest take turns so nothing past the cap stays hidden."""
    if len(group) <= cap:
        return group
    rest = group[fixed:]
    offset = (cycle * (cap - fixed)) % len(rest)
    return group[:fixed] + (rest[offset:] + rest[:offset])[:cap - fixed]


def turn(now):
    """Changes with every message of the day: morning brief, two checks, evening brief."""
    return now.date().toordinal() * 4 + sum(now.hour >= hour for hour in (12, 15, 17))


def right_now(groups, events, now, urgent=None):
    """One line: the meeting in progress or about to start, otherwise the most urgent task.

    `urgent` is the caller's own ordering; its head is always pinned in the printed block,
    so the task named here is one the reader can find below.
    """
    timed = [e for e in sources.events_on(events, now.date(), now) if not e['all_day']]
    for event in timed:
        if event['start'] <= now < event['end']:
            return f'▶ 지금: 진행 중 · {event_text(event)}'
    upcoming = next((e for e in timed if e['start'] > now), None)
    if upcoming and upcoming['start'] - now <= timedelta(minutes=30):
        minutes = int((upcoming['start'] - now).total_seconds() // 60)
        return f'▶ 지금: {minutes}분 뒤 시작 · {event_text(upcoming)}'
    urgent = tiers.critical(groups) if urgent is None else urgent
    if urgent:
        room = ''
        if upcoming:
            free = int((upcoming['start'] - now).total_seconds() // 60)
            room = f' — 다음 일정 {clock(upcoming["start"])}까지 {free // 60}시간 {free % 60}분'
        return f'▶ 지금: {clip(urgent[0]["title"], 60)}{room}'
    if groups['week']:
        return f'▶ 지금: 오늘 마감 없음 · 이번 주 먼저 볼 일 — {clip(groups["week"][0]["title"], 60)}'
    return '▶ 지금: 날짜가 정해진 급한 일이 없습니다.'


def credential_warning(issued, cfg, now):
    days = cfg.get('oauth_ttl_days', 7)
    if not issued or not days:
        return ''
    expires = issued + timedelta(days=days)
    remaining = expires - now
    if remaining > timedelta(hours=cfg.get('oauth_warn_hours', 48)):
        return ''
    if remaining <= timedelta(0):
        return '⚠️ Google 인증이 만료 예상 시각을 지났습니다. 조회가 실패하면 재인증이 필요합니다.'
    return (f'⚠️ Google 인증 만료 예상: {expires:%m/%d %H:%M} (약 {int(remaining.total_seconds() // 3600)}시간 후). '
            '그 전에 재인증하지 않으면 리마인드가 멈춥니다.')


def block(header, items, droppable=False, footer=()):
    """items are (text, task_or_None). Droppable blocks are cut first when the message overflows."""
    return {'header': header, 'items': list(items), 'droppable': droppable, 'footer': list(footer)}


def block_lines(entry):
    return ['', entry['header']] + [f'• {text}' for text, _ in entry['items']] + entry['footer']


def task_block(label, group, cap, today, cycle, droppable=False, hint='', fixed=0, shown=None):
    shown = rotate(group, cap, cycle, fixed) if shown is None else shown
    footer = [f'  외 {len(group) - len(shown)}개'] if len(group) > len(shown) else []
    return block(f'{label} ({len(group)})', [(task_text(t, today), t) for t in shown], droppable,
                 footer + ([hint] if hint else []))


def event_block(header, rows, cap, droppable=False):
    footer = [f'  외 {len(rows) - cap}개'] if len(rows) > cap else []
    return block(header, [(event_text(e), None) for e in rows[:cap]], droppable, footer)


def build(tasks, cfg, now, events=None, mail=None):
    today = now.date()
    groups = tiers.classify(tasks.values(), today, cfg.get('upcoming_days', 3), cfg.get('stale_days', 14))
    cycle = turn(now)
    fresh = tiers.recently_added_undated(groups, now, cfg.get('new_undated_days', 7))
    blocks = []

    if events is not None:
        rows = sources.events_on(events, today, now)
        blocks.append(event_block('🗓 오늘 남은 일정', rows, 7) if rows else block('🗓 오늘 남은 일정: 없음', []))
    elif cfg.get('calendar'):
        blocks.append(block('🗓 일정: 캘린더를 읽지 못했습니다.', []))

    # The head of each urgent block is pinned (latest overdue, soonest this week); the overflow rotates.
    for label, name, cap, fixed in (('🔴 기한 경과 · 아직 미완료', 'overdue', 5, 2),
                                    ('🟠 오늘 일과 종료 전 반드시', 'today', 6, 1),
                                    ('🟡 이번 주 안에 반드시', 'week', 6, 2)):
        if groups[name]:
            blocks.append(task_block(label, groups[name], cap, today, cycle, fixed=fixed))
    if fresh:
        blocks.append(task_block('🆕 최근 등록 · 날짜 미정', fresh, 3, today, cycle, fixed=1,
                                 hint='  → 마감일을 정하면 위 단계로 올라가 리마인드됩니다.'))

    if events is not None and now.hour >= 15:
        rows = sources.events_on(events, today + timedelta(days=1))
        if rows:
            blocks.append(event_block('🗓 내일 일정', rows, 4, droppable=True))

    if mail is not None and mail['threads']:
        flagged = [m for m in mail['threads'] if m['flagged']]
        total = f'{len(mail["threads"])}{"+" if mail["more"] else ""}'
        items = []
        for item in flagged[:3]:
            when = f' ({tiers.day_label(item["deadline"], today)})' if item['deadline'] else ''
            items.append((f'{clip(item["subject"], 64)}{when} — {clip(item["from"], 20)}', None))
        blocks.append(block(f'📧 중요·미확인 메일 {total}건 · 마감 언급 {len(flagged)}건', items, droppable=True))

    seen = {t['key'] for t in fresh}
    undated = [t for t in groups['undated'] if t['key'] not in seen]
    starred = [t for t in undated if t['list_title'].casefold() == 'high priority']
    rest = [t for t in undated if t['list_title'].casefold() != 'high priority']
    if groups['stale']:
        blocks.append(task_block(f'🕸 {cfg.get("stale_days", 14)}일 넘게 지난 기한 · 정리 필요', groups['stale'], 2,
                                 today, cycle, droppable=True))
    if starred:
        blocks.append(task_block('⭐ High Priority · 날짜 없음', starred, 3, today, cycle, droppable=True))
    if rest:
        blocks.append(task_block('🗂 날짜 없는 작업 돌아보기', rest, 2, today, cycle, droppable=True))
    return groups, blocks


def fit(head, blocks, tail):
    """Drop optional blocks from the bottom, then trim mandatory ones, until the text fits."""
    kept = list(blocks)

    def text():
        return '\n'.join(head + [line for entry in kept for line in block_lines(entry)] + tail)

    while units(text()) > LIMIT:
        optional = [index for index, entry in enumerate(kept) if entry['droppable']]
        if optional:
            kept.pop(optional[-1])
            continue
        longest = max(kept, key=lambda entry: len(entry['items']), default=None)
        if not longest or len(longest['items']) <= 1:
            break
        longest['items'].pop()
        if '  (일부 생략)' not in longest['footer']:
            longest['footer'].append('  (일부 생략)')
    return text(), kept


def members_of(blocks):
    return {task['key']: task['fingerprint'] for entry in blocks for _, task in entry['items'] if task}


def render(tasks, cfg, now, events=None, mail=None, issued=None, heading='오늘의 브리핑'):
    groups, blocks = build(tasks, cfg, now, events, mail)
    head = [f'📋 {heading} · {now:%m/%d}({tiers.WEEKDAYS[now.weekday()]}) {now:%H:%M}',
            right_now(groups, events, now)]
    tail = ['', f'미완료 {len(tasks)} · 날짜 없음 {len(groups["undated"])} · 오래된 경과 {len(groups["stale"])}']
    warning = credential_warning(issued, cfg, now)
    if warning:
        tail.append(warning)
    tail += ['완료·날짜 변경은 Google Tasks에서 하면 다음 확인에 반영됩니다.', LINK]
    text, kept = fit(head, blocks, tail)
    return text, members_of(kept)


def nudge(tasks, cfg, now, events=None, issued=None):
    """Short end-of-day check. Returns ('', {}) when nothing critical is open."""
    today = now.date()
    groups = tiers.classify(tasks.values(), today, cfg.get('upcoming_days', 3), cfg.get('stale_days', 14))
    # Digests keep every recent overdue item; a nudge repeats only the last few days so it stays worth reading.
    floor = (today - timedelta(days=cfg.get('nudge_overdue_days', 3))).isoformat()
    # Today's deadlines lead: a pile of recent overdue items must not push them out of an end-of-day check.
    todays, late = groups['today'], [t for t in groups['overdue'] if t['eff_due'] >= floor]
    urgent = todays + late
    if not urgent:
        return '', {}
    cap, cycle = 6, turn(now)
    if len(todays) >= cap:
        shown = rotate(todays, cap, cycle, fixed=1)
    else:
        shown = todays + rotate(late, cap - len(todays), cycle, fixed=0 if todays else 1)
    head = [f'⏰ 오늘 마감 점검 · {now:%H:%M}',
            f'오늘 일과 종료 전에 끝내야 할 일 {len(urgent)}건이 아직 열려 있습니다.',
            right_now(groups, events, now, urgent)]
    tail = ['']
    warning = credential_warning(issued, cfg, now)
    if warning:
        tail += [warning, '']
    tail += ['끝냈다면 Google Tasks에서 완료 처리하세요. 미루려면 날짜를 바꾸면 됩니다.', LINK]
    text, kept = fit(head, [task_block('아직 열려 있는 일', urgent, cap, today, cycle, shown=shown)], tail)
    return text, members_of(kept)
