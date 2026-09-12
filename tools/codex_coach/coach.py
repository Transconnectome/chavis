#!/usr/bin/env python3
"""Shared Codex/OpenClaw coach. Public logs in; private state and bounded advice out."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
STATE = Path(os.environ.get('CODEX_COACH_STATE', str(Path.home() / '.local/state/codex-coach')))
KST = ZoneInfo('Asia/Seoul')
DEFAULTS = {'enabled': True, 'content_scope': 'all_local_projects', 'priority': 'quality_and_verification',
            'intervention': 'brief_when_actionable', 'max_analyses_per_day': 48,
            'analysis_debounce_minutes': 5,
            'max_alerts_per_day': 6, 'cooldown_minutes': 30, 'quiet_start': 23, 'quiet_end': 8,
            'retention_days': 30, 'lookback_hours': 0.5}
CATEGORIES = {
 'completion_evidence': ('완료 확인', '요청한 산출물별 실제 파일과 확인 결과를 연결해줘. 확인하지 못한 항목은 미확인으로 표시하고 남은 일을 처리해줘.'),
 'source_claim_gap': ('주장과 근거', '핵심 주장마다 원출처의 방법·결과·한계를 확인해줘. 관찰, 예측, 인과를 구분하고 근거를 넘는 표현은 줄여줘.'),
 'scope_drift': ('요청 범위', '현재 작업을 처음 요청한 결과물과 대조해줘. 빠진 요구를 우선 처리하고 추가 작업은 목적에 필요한 것만 진행해줘.'),
 'instruction_conflict': ('지침 충돌', '진행을 막는 지침이 있다면 해당 파일과 문장을 짚어줘. 현재 내 요청과 이미 허용한 범위에 맞춰 충돌을 해결하고 계속해줘.'),
 'unclear_acceptance': ('완료 기준', '이 작업의 완료를 판정할 구체적인 결과물과 확인 방법을 짧게 정리해줘. 기존 맥락으로 정할 수 있는 것은 직접 정하고 중요한 빈칸만 질문해줘.'),
 'repeated_rework': ('반복 수정', '최근 수정 요청의 공통 원인을 짚고 이번 결과물에서 그 원인이 해결됐는지 확인해줘. 내 의도 변경과 실행 오류는 구분해줘.'),
 'writing_plateau': ('글쓰기 반복 점검', '현재 판본을 보존해줘. 최근 수정 전후를 목적·독자 이해·핵심 주장과 근거 기준으로 비교하고, 실제로 줄어든 결함과 표현만 바뀐 부분을 구분해줘. 아직 중요한 결함 하나가 확인될 때만 그 부분을 수정하고, 없으면 이번 반복을 끝내줘.'),
 'none': ('관찰 중', '')}


def utcnow():
    return datetime.now(timezone.utc)


def read_json(name, default):
    p = STATE / name
    return json.loads(p.read_text()) if p.exists() else default


def save_json(name, value):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    fd, tmp = tempfile.mkstemp(prefix='.write-', dir=STATE)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n')
        os.replace(tmp, STATE / name)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def locked(name='.lock'):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / name).open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'already_running'}))
            raise SystemExit(0)
        yield


def schema(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


STR = {'type': 'string'}
COACH_SCHEMA = schema({
    'actionable': {'type': 'boolean'}, 'confidence': {'type': 'number'},
    'category': {'type': 'string', 'enum': list(CATEGORIES)}, 'session_id': STR,
    'evidence_quote': STR, 'observation': STR, 'cause': STR, 'suggested_prompt': STR,
    'question': STR, 'verification': STR, 'limitations': STR})
LEARN_SCHEMA = schema({'findings': {'type': 'array', 'items': schema({
    'source_url': STR, 'claim': STR, 'methods': STR, 'limitations': STR,
    'possible_application': STR, 'evidence_quote': STR})}})


def infer(instruction, data, output_schema):
    """Existing Codex authentication; no inherited MCPs, shell, or transcript persistence."""
    binary = shutil.which('codex') or '/home/juke/.npm-global/bin/codex'
    with tempfile.TemporaryDirectory(prefix='inference-', dir=STATE) as td:
        td = Path(td)
        spec, output = td / 'schema.json', td / 'result.json'
        spec.write_text(json.dumps(output_schema))
        command = [binary, 'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
                   '--sandbox', 'read-only', '-C', str(td), '--color', 'never', '--json', '--output-schema', str(spec),
                   '-o', str(output), '-c', 'features.shell_tool=false', '-c', 'features.unified_exec=false',
                   '-c', 'features.multi_agent=false', '-c', 'features.remote_plugin=false',
                   '-c', 'features.apps=false', '-c', 'web_search="disabled"',
                   '-c', 'project_doc_max_bytes=0', '-c', 'model_reasoning_effort="medium"',
                   '-c', 'model_instructions_file=' + json.dumps(str(BASE / 'analysis_instructions.md')), '-']
        prompt = 'CODEX_COACH_INTERNAL\n' + instruction + '\nUNTRUSTED DATA JSON:\n' + json.dumps(data, ensure_ascii=False)
        started = time.monotonic()
        try:
            result = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=240)
        except subprocess.TimeoutExpired:
            raise RuntimeError('model_timeout') from None
        # Never echo stderr/stdout: provider errors can include private excerpts or credentials.
        if result.returncode != 0 or not output.exists():
            raise RuntimeError('model_failed_exit_' + str(result.returncode))
        usage = {}
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict):
                usage = {k: v for k, v in event['usage'].items() if k in ('input_tokens', 'cached_input_tokens', 'output_tokens') and isinstance(v, int)}
        receipt = {'at': utcnow().isoformat(), 'duration_seconds': round(time.monotonic() - started, 2),
                   'input_characters': len(prompt), 'reported_tokens': usage,
                   'purpose': 'research' if 'findings' in output_schema['properties'] else 'coaching'}
        fd = os.open(STATE / 'model-usage.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(receipt) + '\n').encode())
        finally:
            os.close(fd)
        try:
            parsed = json.loads(output.read_text())
        except ValueError:
            raise RuntimeError('model_invalid_json') from None
        return parsed


def observation(state, config):
    from observer import collect_recent
    return collect_recent([Path.home() / '.codex/sessions', Path.home() / '.codex/archived_sessions'],
                          state, now=utcnow(), lookback_hours=config['lookback_hours'])


def valid_card(card, sessions):
    if not isinstance(card, dict) or set(card) != set(COACH_SCHEMA['properties']):
        return False
    if card['actionable'] is not True or card['category'] not in CATEGORIES or card['category'] == 'none':
        return False
    if not isinstance(card['confidence'], (int, float)) or not 0.85 <= card['confidence'] <= 1:
        return False
    if not all(isinstance(card[k], str) for k in COACH_SCHEMA['properties'] if k not in ('actionable', 'confidence')):
        return False
    quote = card['evidence_quote'].strip()
    if len(quote) < 12 or len(quote) > 800:
        return False
    return any(s['session_id'] == card['session_id'] and
               any(quote in m['text'] for m in s['messages']) for s in sessions)


def alert_text(card):
    # Fixed, reviewed templates: no research/administrative content, quotes or LLM output transmitted.
    label, prompt = CATEGORIES[card['category']]
    return (f'Codex 코치 · {label}\n작업 기록에서 확인할 지점이 보입니다. 일부 맥락에 근거한 제안입니다.\n\n'
            f'바로 쓸 지시: “{prompt}”\n\n'
            f'근거와 맞춤 지시: /codex_coach 최근 조언 {card["id"]}\n'
            '잘못 짚었으면 “잘못 짚음”, 유용했으면 “도움됨”이라고 알려주세요.')


def send_alert(card):
    route = read_json('telegram-route.json', {})
    if route.get('channel') != 'telegram' or not route.get('target'):
        return 'route_unconfigured'
    command = [shutil.which('openclaw') or '/home/juke/.npm-global/bin/openclaw', 'message', 'send',
               '--channel', 'telegram', '--target', route['target'], '--message', alert_text(card), '--json']
    if route.get('account'):
        command += ['--account', route['account']]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=40)
    except subprocess.TimeoutExpired:
        return 'delivery_unknown_timeout'
    if result.returncode != 0:
        return 'delivery_failed'
    try:
        receipt = json.loads(result.stdout[result.stdout.index('{'):])
        payload = receipt.get('payload', receipt)
        success = payload.get('ok') is True or bool(payload.get('messageId'))
    except (ValueError, TypeError):
        success = False
    return 'sent' if success else 'delivery_unknown_receipt'


def prune(cards, config, now):
    cutoff = (now - timedelta(days=config['retention_days'])).isoformat()
    return [c for c in cards if c.get('created_at', '') >= cutoff]


def relevant_signal(sessions):
    """Cheap trigger, never a quality verdict. Commentary alone need not call a model."""
    correction = r'다시|반복|고쳐|수정|다듬|부족|별로|토큰|퀄리티|퀄러티|품질|아니|왜|틀렸|정체|rewrite|revise|incorrect'
    completion = r'완료|검증|최종|출처|근거|테스트.*통과|모두.*반영|점수|done|completed|verified|final version'
    return any(re.search(correction if m['role'] == 'user' else completion, m['text'], re.I)
               for s in sessions for m in s['messages'])


def contextualize(sessions, now):
    """Small redacted rolling excerpt cache, so an update keeps its request context."""
    cache = read_json('context.json', {})
    cutoff = (now - timedelta(hours=2)).isoformat()
    cache = {k: v for k, v in cache.items() if v.get('updated_at', '') >= cutoff}
    output = []
    for session in sessions:
        old = cache.get(session['session_id'], {}).get('session', {})
        merged = []; seen = set()
        for message in old.get('messages', []) + session['messages']:
            key = (message['role'], message.get('timestamp'), message['text'])
            if key not in seen:
                seen.add(key); merged.append(message)
        first_user = next((m for m in merged if m['role'] == 'user'), None)
        merged = merged[-12:]
        if first_user and first_user not in merged:
            merged = [first_user] + merged[-11:]
        # Preserve the request plus newest evidence, within3000chars/session.
        request = {**first_user, 'text': first_user['text'][:500]} if first_user else None
        budget = 3000 - (len(request['text']) if request else 0); tail = []
        for m in reversed(merged):
            if budget <= 0: break
            if first_user and m == first_user: continue
            text = m['text'][:min(1000, budget)]; budget -= len(text)
            tail.append({**m, 'text': text})
        bounded = ([request] if request else []) + list(reversed(tail))
        current = {**session, 'messages': bounded}
        cache[session['session_id']] = {'updated_at': now.isoformat(), 'session': current}
        output.append(current)
    cache = dict(sorted(cache.items(), key=lambda kv: kv[1]['updated_at'], reverse=True)[:6])
    save_json('context.json', cache)
    return output


def tick(*, dry_run=False):
    now = utcnow()
    config = {**DEFAULTS, **read_json('config.json', {})}
    state = read_json('monitor.json', {})
    state['last_tick'] = now.isoformat()
    cards = prune(read_json('cards.json', []), config, now)
    if not dry_run:
        save_json('cards.json', cards)
        contextualize([], now)  # Expire cached excerpts even during quiet periods.
    if not config['enabled']:
        state['status'] = 'paused'
        save_json('monitor.json', state)
        return {'status': 'paused'}
    day = now.astimezone(KST).date().isoformat()
    if state.get('day') != day:
        state.update(day=day, analyses_today=0, alerts_today=0)
    batch = observation(state, config)
    state['coverage'] = batch['coverage']
    sessions = batch['sessions']
    if not sessions:
        state.update(cursor=batch['cursor'], status='no_new_messages')
        save_json('monitor.json', state)
        return {'status': state['status'], 'coverage': state['coverage']}
    if dry_run:
        return {'status': 'observed_no_model_no_delivery', 'sessions': len(sessions),
                'messages': sum(len(s['messages']) for s in sessions), 'coverage': batch['coverage']}
    if not relevant_signal(sessions):
        contextualize(sessions, now)
        state.update(cursor=batch['cursor'], status='no_intervention_signal')
        save_json('monitor.json', state)
        return {'status': state['status']}
    if state.get('last_analysis', '') > (now - timedelta(minutes=config['analysis_debounce_minutes'])).isoformat():
        state['status'] = 'analysis_debounce'
        save_json('monitor.json', state)
        return {'status': state['status']}
    if state.get('analyses_today', 0) >= config['max_analyses_per_day']:
        state['status'] = 'daily_analysis_limit'
        save_json('monitor.json', state)
        return {'status': state['status']}
    sessions = contextualize(sessions, now)
    state['analyses_today'] = state.get('analyses_today', 0) + 1
    state.update(status='analyzing', analysis_started_at=now.isoformat())
    save_json('monitor.json', state)  # Failed/timeout attempts count against budget too.
    sources = json.loads((BASE / 'sources.json').read_text())
    data = {'sessions': sessions, 'priority': config['priority'], 'reviewed_sources': sources,
            'user_examples_and_answers': read_json('answers.json', [])[-8:],
            'learned_findings': read_json('learning.json', {}).get('findings', [])[:8],
            'previous_advice': [{k: c.get(k) for k in ('category', 'session_id', 'observation', 'feedback')} for c in cards[-12:]]}
    instruction = ('현재 작업에서 결과물의 질/검증을 개선하는 조언이 꼭 필요한지 판단하라. '
        '근거가 약하거나 이미 해결됐거나 최근 조언과 같으면 actionable=false. '
        '로그에 도구 결과와 저장소 지침이 없으므로 검증이 안 보이는 것만으로 검증 실패를 추정하지 말라. '
        '계속 정상 진행 중인 작업, 이 코치 구축 작업, 사용자의 정상적 범위 확장은 개입하지 말라. '
        '명백한 반복 교정, 근거와 주장 간 모순, 요청 미충족 완료 보고 등 구체적 근거를 우선하라. '
        '첫 핵심 대상은 수업자료·어려운 글쓰기의 반복 수정 정체다. 최근 수정에서 어떤 결함이 줄었는지 '
        '확인하지 않은 채 비슷한 다듬기·자기평가·리뷰를 반복하는 경우 writing_plateau 후보로 본다. '
        '판본 비교/사용자 판단이 없으면 실제 품질이 정체했다고 단정하지 말고 비교와 종료기준을 제안한다. '
        '관찰된 문장을 evidence_quote에 원문 그대로 12~800자로 적고 session_id를 정확히 사용하라. '
        '한국어로 observation, possible cause, 복사 가능한 suggested_prompt, 확인 방법 verification을 쓴다. '
        'cause는 원인 가설이며 단정하지 않는다. 질문이 유용하면 question에 하나만, 아니면 빈 문자열. '
        'confidence>=0.85만 알림 대상이다. 이 점수는 교정된 확률이 아니다.')
    try:
        card = infer(instruction, data, COACH_SCHEMA)
    except RuntimeError as e:
        state.update(status=str(e), last_error_at=now.isoformat())
        save_json('monitor.json', state)
        return {'status': state['status']}
    state.update(cursor=batch['cursor'], last_analysis=now.isoformat(), status='no_actionable_finding')
    if valid_card(card, sessions):
        prior = next((c for c in reversed(cards) if c['session_id'] == card['session_id'] and c['category'] == card['category'] and
                      c.get('created_at', '') >= (now - timedelta(hours=24)).isoformat()), None)
        duplicate = prior and prior.get('delivery') not in ('quiet_hours', 'cooldown_or_daily_limit', 'local_only', 'route_unconfigured')
        if not duplicate:
            card['id'] = hashlib.sha256((card['session_id'] + card['category'] + now.isoformat()).encode()).hexdigest()[:10]
            card['created_at'] = now.isoformat()
            card['source_file'] = next(s['source_file'] for s in sessions if s['session_id'] == card['session_id'])
            card['feedback'] = []
            if prior:
                card['id'] = prior['id']
                card['feedback'] = prior.get('feedback', [])
                cards.remove(prior)
            card['delivery'] = 'local_only'
            local_hour = now.astimezone(KST).hour
            quiet = local_hour >= config['quiet_start'] or local_hour < config['quiet_end']
            cooldown = state.get('last_alert', '') > (now - timedelta(minutes=config['cooldown_minutes'])).isoformat()
            still_enabled = read_json('config.json', {}).get('enabled', True)
            if still_enabled and not quiet and not cooldown and state.get('alerts_today', 0) < config['max_alerts_per_day']:
                # Persist attempted before send: ambiguous delivery is not automatically retried.
                card['delivery'] = 'attempted'
                save_json('cards.json', cards + [card])
                state['last_alert'] = now.isoformat()
                state['alerts_today'] = state.get('alerts_today', 0) + 1
                save_json('monitor.json', state)
                card['delivery'] = send_alert(card)
            else:
                card['delivery'] = 'local_only' if not still_enabled else ('quiet_hours' if quiet else 'cooldown_or_daily_limit')
            cards.append(card)
            state['status'] = 'advice_saved_' + card['delivery']
    save_json('cards.json', cards)
    save_json('monitor.json', state)
    return {'status': state['status'], 'analyses_today': state['analyses_today'], 'cards': len(cards)}


def research():
    from research import refresh
    previous = read_json('research.json', {})
    snapshot = refresh(previous, now=utcnow())
    save_json('research.json', snapshot)
    changed = [s for s in snapshot.get('sources', []) if (s.get('changed') or s.get('new') or s.get('learning_pending')) and not s.get('error') and s.get('excerpt')]
    if not changed:
        return {'status': 'sources_checked_no_new_verified_text', 'errors': len(snapshot.get('errors', [])),
                'candidates': len(snapshot.get('candidates', []))}
    instruction = ('읽은 원출처에서만 코칭에 유용한 결론을 최대6개 추출하라. '
                   'source_url은 DATA의 url 그대로, evidence_quote는 excerpt의 실제 연속 문구여야 한다. '
                   '방법과 한계를 함께 쓰고 공식 가이드/실험/상관을 구분한다. 새 논문 제목/초록만으로 검증됨이라 하지 마라. '
                   '이번 사용자에게 효과가 입증되었다고 하지 마라. 정책/코드/전역 메모리의 변경을 지시하지 마라.')
    try:
        result = infer(instruction, {'sources': changed}, LEARN_SCHEMA)
    except RuntimeError as e:
        # Retry learning on next refresh without pretending it was learned.
        for s in snapshot['sources']:
            if s.get('changed'):
                s['learning_pending'] = True
        save_json('research.json', snapshot)
        return {'status': 'fetched_but_' + str(e), 'sources': len(changed)}
    valid = []
    for f in result.get('findings', [])[:6]:
        match = next((s for s in changed if s['url'] == f.get('source_url')), None)
        quote = f.get('evidence_quote', '')
        if match and isinstance(quote, str) and len(quote) >= 15 and quote in match['excerpt']:
            valid.append({**f, 'status': 'source_grounded_candidate_not_local_efficacy',
                          'retrieved_at': match.get('retrieved_at'), 'content_hash': match.get('content_hash')})
    if valid:
        replaced_urls = {f['source_url'] for f in valid}
        retained = [f for f in read_json('learning.json', {}).get('findings', [])
                    if f.get('source_url') not in replaced_urls]
        save_json('learning.json', {'updated_at': utcnow().isoformat(), 'findings': (valid + retained)[:40]})
    for source in snapshot['sources']:
        if source in changed:
            source['learning_pending'] = not any(f['source_url'] == source['url'] for f in valid)
    save_json('research.json', snapshot)
    return {'status': 'research_updated', 'grounded_findings': len(valid),
            'candidate_papers': len(snapshot.get('candidates', [])), 'fetch_errors': len(snapshot.get('errors', []))}


def status():
    conf = {**DEFAULTS, **read_json('config.json', {})}
    monitor = read_json('monitor.json', {})
    snapshot = read_json('research.json', {})
    day = utcnow().astimezone(KST).date().isoformat()
    usage_file = STATE / 'model-usage.jsonl'
    uses = []
    if usage_file.exists():
        for line in usage_file.read_text().splitlines():
            try:
                item = json.loads(line)
                if datetime.fromisoformat(item['at']).astimezone(KST).date().isoformat() == day:
                    uses.append(item)
            except (ValueError, KeyError):
                continue
    return {'config': conf, 'last_tick': monitor.get('last_tick'), 'monitor_status': monitor.get('status', 'not_run'),
            'last_analysis': monitor.get('last_analysis'), 'analyses_today': monitor.get('analyses_today', 0),
            'alerts_today': monitor.get('alerts_today', 0), 'coverage': monitor.get('coverage'),
            'cards': len(read_json('cards.json', [])), 'telegram_route_configured': bool(read_json('telegram-route.json', {}).get('target')),
            'research_last_check': snapshot.get('checked_at'), 'research_errors': snapshot.get('errors', []),
            'learning_updated': read_json('learning.json', {}).get('updated_at'),
            'coach_model_usage_today': {'recorded_calls': len(uses),
                 'reported_tokens': {k: sum(u['reported_tokens'].get(k, 0) for u in uses) for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')},
                 'note': 'Only coach calls with reported usage; not user task totals or monetary cost.'},
            'state_directory': str(STATE), 'limitations': ['DGX에 실제 저장된 세션만 관찰; 다른 호스트/클라우드 세션은 확인 불가',
                '도구 원문과 실제 산출물을 자동 감사하지 않음; 공개 대화의 표본으로 개입 후보 판단',
                '개입은 2분 주기 관찰과 모델 처리 후; 예산/중복/야간 제한 적용',
                'Codex에서는 스킬 호출로 조회; 기존 작업에 강제 메시지 주입 없음']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('tick'); p.add_argument('--dry-run', action='store_true')
    for name in ('status', 'research', 'pause', 'resume', 'brainstorm', 'context'):
        sub.add_parser(name)
    p = sub.add_parser('report'); p.add_argument('--id'); p.add_argument('--limit', type=int, default=3)
    p = sub.add_parser('feedback'); p.add_argument('id'); p.add_argument('rating', choices=['도움됨','잘못 짚음','불필요','적용함','보류'])
    p.add_argument('--note', default='')
    p = sub.add_parser('answer'); p.add_argument('text')
    args = parser.parse_args()
    read_commands = ('status', 'pause', 'resume', 'report', 'brainstorm', 'context', 'answer')
    guard = nullcontext() if args.command in read_commands else locked('.research.lock' if args.command == 'research' else '.lock')
    with guard:
        if args.command == 'tick': out = tick(dry_run=args.dry_run)
        elif args.command == 'research': out = research()
        elif args.command == 'status': out = status()
        elif args.command == 'context':
            # On-demand, transient context for the requesting agent; not saved or sent to Telegram.
            batch = observation({}, {**DEFAULTS, **read_json('config.json', {})})
            out = {'sessions': batch['sessions'], 'coverage': batch['coverage']}
        elif args.command in ('pause', 'resume'):
            conf = {**DEFAULTS, **read_json('config.json', {}), 'enabled': args.command == 'resume'}
            save_json('config.json', conf); out = {'monitor_enabled': conf['enabled']}
        elif args.command == 'report':
            cards = prune(read_json('cards.json', []), {**DEFAULTS, **read_json('config.json', {})}, utcnow())
            out = [c for c in cards if not args.id or c['id'] == args.id][-max(1,min(args.limit,20)):]
        elif args.command == 'feedback':
            cards = read_json('cards.json', []); card = next((c for c in cards if c['id'] == args.id), None)
            if card is None: raise SystemExit('unknown_advice_id')
            from observer import redact
            card['feedback'].append({'rating': args.rating, 'note': redact(args.note[:1000]), 'at': utcnow().isoformat()})
            save_json('cards.json', cards); out = {'recorded': args.rating, 'id': args.id, 'efficacy': 'not_inferred_from_rating'}
        elif args.command == 'answer':
            from observer import redact
            answers = read_json('answers.json', []); answers.append({'at': utcnow().isoformat(), 'text': redact(args.text[:2000])})
            save_json('answers.json', answers[-50:]); out = {'saved': True}
        else:
            out = {'known_preferences': DEFAULTS, 'answers': read_json('answers.json', []),
                   'next_question': '최근 결과를 다시 고치게 된 사례 하나를 고른다면, 어떤 결과를 기대했고 무엇이 부족했나요?'}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'status': 'error', 'error_type': type(exc).__name__}))
        raise SystemExit(1)
