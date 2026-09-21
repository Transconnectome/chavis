# Google Tasks 개인 리마인더

차지욱 교수의 Google Tasks를 DGX에서 5분마다 확인하고 기존 개인 Telegram 경로로 알린다.
`gog`의 기존 Google 인증을 이용하며 Google Tasks는 읽기만 한다.

**운영 검증: 2026-09-18 02:50 KST 설치·예약 자동 실행·Telegram 전송 성공.**
5분 타이머가 활성화되어 있으며 첫 예약 실행이 02:50:00에 시작해 02:50:08에 성공 종료했다.
Google Tasks 읽기와 Telegram 전송은 실제 계정에서 확인했으며, 사용자 열람 여부는 확인하지 않았다.

## 알림 계획

| 항목 | 기본 동작 |
|---|---|
| 계정 | 비공개 로컬 설정의 `account` |
| 수집 | 모든 목록을 페이지 끝까지 읽고 할당된 작업도 포함, 5분마다 |
| 정기 요약 | 한국시간 08:30, 17:30 |
| 내용 | 예정일 지난 항목, 오늘 예정, 향후 3일 예정, High Priority, 날짜 없는 작업 순환 |
| 변경 알림 | 오늘 또는 이전 날짜로 예정된 새 작업·변경을 모아 시간당 최대 한 번 |
| 조용한 시간 | 22:00–08:00, 정기 수집은 계속 |
| 첫 실행 | 현재 작업을 기준 상태로 기록하여 과거 작업의 변경 알림 폭주 방지 |

완료·삭제·숨김 항목은 알림 대상에서 제외한다. 날짜가 없는 항목은 날짜 기준의 임박 작업으로
해석하지 않는다. 날짜 없는 작업과 메시지에 다 담기지 않는 작업은 순환 표시하며, 실제로 표시하지 않은 변경은 대기열에 남긴다. 기존 OpenClaw 08:00 알림과 Coach 일일 팁을 고려하여 요약 시각을 분산했다.

Google Tasks API의 `due`는 **예정 날짜**이며 공식 문서상 기한(deadline)이 아니다. 시간 정보도
API로 읽거나 쓸 수 없으므로 Google 앱에 설정한 정확한 시각 알림을 재현하지 않는다.
자세한 범위는 [Task 리소스의 due 설명](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks)과
[작업 조회 옵션](https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/list)을 참고한다.

## 비서 확장 (2026-09-21): 캡처 · 통합 브리핑 · 마감 점검

기본 리마인더 위에 세 가지를 얹었다. 전부 비공개 config에서 켜는 opt-in이며, 끄면 위 표의 동작 그대로다.

| 구성 | 파일 | 역할 |
|---|---|---|
| 기한·구간 | `tiers.py` | 제목의 `9/25까지`·`(~9/25)` 표기를 기한으로 읽고, 작업을 경과(14일 이내)·오늘·이번 주·이후·오래된 경과·날짜 없음으로 나눈다. 순수 함수 |
| 보조 출처 | `sources.py` | `gog`로 캘린더(7일), 중요·미확인 메일, 인증 발급 시각을 읽는다. 실패하면 `None`을 돌려주고 리마인더는 계속 간다 |
| 브리핑 | `brief.py` | "▶ 지금" 한 줄, 오늘 남은 일정, 🔴 경과 · 🟠 오늘 · 🟡 이번 주 · 🆕 날짜 미정, 저녁에는 내일 일정, 마감 언급 메일. UTF-16 4096 단위를 넘으면 선택 구간부터 버린다 |
| 캡처 CLI | `secretary.py` | `add` · `done` · `due` · `retitle` · `find` · `brief` · `lists`. Google Tasks에 쓰는 유일한 경로이며 삭제는 구현하지 않았다 |

`agent.py` 자체는 계속 읽기 전용이다. 달라진 것은 요약 본문을 `compose()`로 만든다는 점과 마감 점검(nudge) 슬롯뿐이다.

### config 키 (기본값은 모두 꺼짐)

| 키 | 기본 | 뜻 |
|---|---|---|
| `brief` | `false` | 정기 요약 본문을 통합 브리핑으로 바꾼다. **첫 번째 롤백 레버**: `false`로 돌리면 다음 요약부터 예전 형식이다 |
| `nudge_times` | `[]` | 마감 점검 시각(`HH:MM`). 오늘 기한이거나 최근 `nudge_overdue_days`(3)일 안에 기한이 지난 미완료 작업이 있을 때만 보낸다. 없으면 침묵하고 발송 기록도 남기지 않는다 |
| `nudge_window_minutes` | `20` | 슬롯이 열려 있는 시간. 서버가 꺼져 놓친 점검은 다시 보내지 않는다 |
| `calendar` · `mail` | `false` | 브리핑에 일정과 마감 언급 메일을 넣는다. 각각 calendar · gmail 스코프가 필요하다 |
| `calendar_exclude` | `[]` | 이름에 이 문자열이 들어간 달력은 뺀다 |
| `oauth_ttl_days` | `0` | Testing 상태의 OAuth 앱은 refresh token이 7일 뒤 죽는다. `7`로 두면 만료 48시간 전부터 브리핑에 경고가 붙는다. 앱을 게시했다면 `0` |
| `stale_days` · `new_undated_days` | `14` · `7` | 오래된 경과로 넘기는 기준, 날짜 없이 등록된 작업을 🆕 구간에 보여 주는 기간 |
| `capture_list` · `capture_default_due` | `""` · `none` | 새 작업이 들어갈 목록(빈 값 = 첫 목록)과, 날짜 없이 등록된 작업에 줄 날짜. `none`은 날짜를 지어내지 않는다. `today` · `tomorrow` · `this-week`(이틀 이상 남은 가장 가까운 금요일)를 고르면 그 날짜가 Google Tasks에 실제 기한처럼 남고 나중에 구분할 수 없다 |

config의 알려진 키는 기본값과 타입이 같아야 한다. `"brief": "false"`나 `"nudge_times": null`은 `invalid_configuration`으로 거부한다. config가 거부되면 tick이 시작되지 않아 장애 알림도 나가지 않으므로, 고칠 때는 임시 파일에 쓰고 `agent.config()`로 읽어 본 뒤 교체한다.

구간마다 표시 개수에 상한이 있다(경과 5 · 오늘 6 · 이번 주 6 · 점검 6). 상한을 넘으면 맨 앞 항목만 고정하고 나머지는 하루 네 번의 메시지마다 돌아가며 보여 준다. 마감 점검은 오늘 기한을 먼저 채우고 남는 자리에 최근 경과를 넣는다. 14일 넘게 지난 기한은 급한 구간에 올리지 않고 🕸 구간에 두 건씩 돌려 보여 준다.

한 tick에 메시지는 하나만 나간다. 순서는 정기 요약, 마감 점검, 변경 알림이다. 정기 요약 뒤 45분 안에는 마감 점검을 보내지 않고, 조용한 시간에는 둘 다 보내지 않는다. 마감 점검에 표시된 작업은 변경 알림 대기열에서 빠진다.

보조 출처는 tick이 시작된 지 75초 안일 때만 읽는다(`SIDE_BUDGET`). systemd가 240초에 unit을 끊는데 Tasks 전체 조회와 Telegram 전송만으로 최악 235초가 들기 때문이다. 실측은 캘린더 약 7초, 메일 약 2초, 인증 조회 1초 미만이다.

통합 브리핑 모듈이 없거나 예외를 내면 예전 `summary()`로 대체하고 메시지 끝에 사유를 붙인다. 마감 점검은 Google 날짜만으로 고른 목록으로 대체한다. 같은 사유가 `status`의 `brief_error`에 남고, 다음 정기 요약이 성공하면 지워진다.

### 캡처 CLI

```bash
python3 tools/google_tasks_agent/secretary.py add --title "학회 초록 제출" --due 2026-09-25
python3 tools/google_tasks_agent/secretary.py add --title "날짜를 말하지 않은 일"      # capture_default_due 적용 (기본 none)
python3 tools/google_tasks_agent/secretary.py done "초록 제출"
python3 tools/google_tasks_agent/secretary.py due "초록 제출" fri
python3 tools/google_tasks_agent/secretary.py retitle "초록 제출" "학회 초록 제출 (9월 28일까지)"
python3 tools/google_tasks_agent/secretary.py find "초록"
python3 tools/google_tasks_agent/secretary.py brief [--scope now|nudge] [--live]
```

- `--due`는 `YYYY-MM-DD` · `today` · `tomorrow` · `+3d` · 요일(`fri`, `금`) · `this-week`(금요일, 주말에는 일요일) · `none`만 받는다. 그 밖의 표현은 추측하지 않고 거부한다.
- `add`는 쓰기 직전에 대상 목록을 다시 읽고 다른 목록은 스냅샷으로 확인해, 같은 제목(공백·문장부호·대소문자 무시)의 미완료 작업이 있으면 만들지 않는다. 스냅샷보다 늦게 다른 목록에 생긴 같은 제목은 잡지 못한다.
- `done` · `due` · `retitle`은 대상을 항상 직접 조회한 목록에서 고른다(약 12초). 스냅샷에 없는 방금 등록한 작업 때문에 엉뚱한 작업이 단일 일치로 잡히는 것을 막는다.
- 제목에 적힌 기한이 새 날짜보다 이르면 `due`는 `title_deadline_earlier`로 알린다. 두 날짜 중 이른 쪽이 구간을 정하므로 제목을 `retitle`로 고쳐야 브리핑에서 내려간다.
- 종료 코드 2는 결정 요청이다: `duplicate` · `ambiguous` · `not_found` · `list_not_found`.
- Google이 저장한 날짜가 요청과 다르면 `created_but_due_differs`와 종료 코드 1로 알린다.
- `brief`·`find`는 데몬의 스냅샷(20분 이내)을 읽고, 없거나 오래됐으면 직접 조회한다. 쓰기 직후에는 `brief --live`를 쓴다.

Claude Code에서는 `skills/secretary/SKILL.md`가 이 CLI를 쓴다. `ln -s <repo>/skills/secretary ~/.claude/skills/secretary`로 연결하고, PATH에 `exec python3 <repo>/tools/google_tasks_agent/secretary.py "$@"` 한 줄짜리 `chavis-secretary` 래퍼를 둔다.

### 한계

- Google Tasks의 날짜는 하루 단위다. 제목의 "오전 10시까지"는 날짜만 읽는다.
- 제목 기한은 `까지`나 앞에 붙은 `~`가 있을 때만 인정한다. `10월 27일(화) 웨비나` 같은 행사 날짜와 `9/15~9/21` 같은 기간은 기한으로 읽지 않는다. 연도가 없는 날짜는 `9/25`와 `9월 25일`만 읽고 `9.25`·`9-25`는 읽지 않는다(`버전 1.5까지`, `챕터 2-3까지`와 구분할 수 없다). 연도가 없으면 작업의 `updated` 날짜에서 추정하므로, 오래된 작업을 고치면 내년으로 해석될 수 있다. 규칙은 전달된 행정 메일 제목에서 점검했고, 자유롭게 쓴 제목에서의 오탐률은 재지 않았다.
- 변경 알림 대기열은 여전히 Google 날짜만 본다. 제목 기한만 있는 새 작업은 다음 브리핑이나 마감 점검에 나온다.
- 프로세스가 시작조차 못 하는 장애(문법 오류, 전원 중단)는 여전히 Telegram으로 알릴 수 없다. `OnFailure=` 알림 unit은 아직 없다.

## 설치와 점검

설치기는 기본 실행에서 변경 예정만 보여준다. `--enable`은 자신의 두 systemd unit과
설치 기록을 만들고 timer를 활성화한다. 설정 파일은 없을 때만 만들며 기존 설정과 다른
서비스를 덮어쓰지 않는다. 이전 설치 후 수동으로 바뀐 unit도 자동 덮어쓰지 않는다.
새 설치는 기존 `gog`에 인증된 본인 계정을 `--account`로 명시해야 하며, 계정이 없으면
설치·활성화를 거절한다. 아래 예시 주소는 본인 주소로 바꾼다. 이미 설치되어 있으면
`--account` 없이 재실행하며, 이 옵션으로 기존 설정의 계정을 변경하지 않는다.

```bash
python3 /home/juke/git/chavis/tools/google_tasks_agent/install.py
python3 /home/juke/git/chavis/tools/google_tasks_agent/install.py --enable --account your-account@example.com
python3 /home/juke/git/chavis/tools/google_tasks_agent/agent.py preview
python3 /home/juke/git/chavis/tools/google_tasks_agent/install.py --check
python3 /home/juke/git/chavis/tools/google_tasks_agent/agent.py status
systemctl --user list-timers google-tasks-agent.timer --no-pager
systemctl --user show google-tasks-agent.service -p Result -p ExecMainStatus -p ExecMainExitTimestamp
```

`preview`는 발송 없이 현재 알림 내용을 확인한다. `send-now`는 실제 Telegram 요약을 하루 한 번 보낼 수 있으며, 야간 전송 보류를 건너뛴다. 정기 요약과는 별도의 수동 전송이다.
`tick`은 예약 실행과 같은 수집·알림 판단을 수행하므로 실제 발송될 수 있다.

```bash
python3 /home/juke/git/chavis/tools/google_tasks_agent/agent.py send-now
python3 /home/juke/git/chavis/tools/google_tasks_agent/agent.py tick
journalctl --user -u google-tasks-agent.service -n 30 --no-pager
```

사용자 지정 설정은 `agent.py --config /absolute/path/config.json status`와
`install.py --config /absolute/path/config.json --enable`로 동일하게 지정한다.

## 설정·상태·중지

- 설정: `~/.config/google-tasks-agent/config.json`
- 비공개 상태·SQLite·설치 기록: `~/.local/state/google-tasks-agent/`
- 재사용하는 개인 알림 경로: `~/.local/state/codex-coach/telegram-route.json`
- unit: `google-tasks-agent.service`, `google-tasks-agent.timer`

설정의 `digest_times`, `quiet_start`, `quiet_end`, `timezone`, `upcoming_days`를 바꾸면 다음
실행부터 반영된다. 인증 토큰을 이 저장소나 설정 파일에 복사할 필요는 없다. 기존 `gog` 인증과
OpenClaw 인증을 사용한다. systemd의 PATH에는 설치 시 확인한 Node 실행 경로를 포함한다.

```bash
# 일시정지: 현재 실행도 중지하고 자동 예약 해제
systemctl --user disable --now google-tasks-agent.timer
systemctl --user stop google-tasks-agent.service
# 재개
systemctl --user enable --now google-tasks-agent.timer
```

서버와 네트워크가 동작해야 수집·발송할 수 있다. timer는 재시작 후 놓친 확인 주기를 한 번
실행할 수 있으나 꺼져 있던 동안 실시간 감시를 수행한 것으로 간주하지 않는다. 실패 상태와
마지막 성공 시각을 구분해서 확인한다. `status`의 `source_status`는 Google 조회 상태, `delivery_status`는 최근 알림 전송 상태다. 3회 연속 조회 실패 시 야간을 제외하고 하루 한 번 장애 알림을 시도한다. 프로세스 자체가 계속 시작되지 않는 장애나 서버 전원 중단은 외부 감시 없이는 Telegram으로 통보할 수 없다. SQLite의 발송 기록과 잠금으로 중복을 억제하며,
발송 전에 작업별 상태 지문을 함께 기록한다. 발송 후 timeout 또는 프로세스 중단으로 결과가 불명확하면 `unknown`으로 보관하고 해당 작업 버전을 변경 알림으로 자동 재발송하지 않는다. 다음 정기 요약은 별도 일정으로 계속 동작한다.
Telegram API의 메시지 영수증은 실제 발송의 증거이며 사용자가 읽었다는 증거는 아니다.

## 완료 확인에 필요한 증거

실제 계정 조회 성공, 전체 목록 조회와 다음 예약 시각, systemd 서비스의 성공 실행,
반복 실행 시 중복 억제, Telegram 메시지 영수증을 각각 확인한다. 합성 테스트만 통과하거나
timer 파일만 존재하는 상태를 지속 운영 완료로 기록하지 않는다.


## 실제 운영 확인 기록 (2026-09-18, KST)

- 기존 `gog` 인증으로 전체 목록과 모든 페이지 조회 성공. 실제 계정·작업 수·제목은 비공개 상태에만 보관한다.
- `install.py --enable` 및 `--check`: 전용 timer enabled/active, unit 변경·충돌 없음.
- 사용자 systemd 서비스 실행: 02:47:22 종료, `Result=success`, `ExecMainStatus=0`.
- 타이머가 직접 시작한 첫 예약 실행: 02:50:00 시작, 02:50:08 성공 종료, 다음 실행 02:55:00 확인.
- 야간 정기 동작: `quiet_hours`, 조회 성공, 정기 알림 보류.
- `send-now` 첫 요약: `sent`, Telegram 메시지 영수증 확인. 실제 ID는 비공개 SQLite에 보관한다.
- `send-now` 같은 날짜 재실행: 추가 전송 없이 종료, 영수증 1개 유지.
- `status`: Google 조회와 최근 Telegram 전송 모두 `healthy`.
- 최초 오프라인 회귀 테스트 24개 통과. 공개본 계정 설정 테스트 4개를 추가해 현재 총 28개 통과. 별도 에이전트의 최종 검토 판정 `Proceed`.
- `systemd-analyze --user verify` 통과. `Linger=yes`로 로그인 종료 후에도 user manager 실행 유지.
- config·SQLite `0600`, 상태 디렉터리 `0700`. 인증 토큰은 복사하지 않았다.

## 실제 운영 확인 기록 (2026-09-21, KST) — OAuth 토큰 7일 만료 대응

- **현상**: 2026-09-14 09:09 발급된 Google OAuth Refresh Token이 Google Cloud Testing 앱 7일 만료 정책(168시간)에 따라 2026-09-21 09:10:02 KST에 정확히 만료(`invalid_grant`, `google_read_failed` 발생). 3회 연속 실패 후 09:20 health 장애 알림 Telegram 정상 발송 확인.
- **복구 절차**:
  1. `source ~/.config/gogcli/env.sh && gog auth add <account> --remote --step 1 --timeout 30m` 으로 1회성 브라우저 승인 URL 생성.
  2. 브라우저 승인 후 리다이렉트된 콜백 URL의 `code`를 구글 OAuth2 엔드포인트(`https://oauth2.googleapis.com/token`)와 직접 교환하여 최신 refresh_token 수령.
  3. `gog auth tokens import` 명령을 통해 새 토큰을 keyring에 반영.
  4. 복구 즉시 `google-tasks-agent.service` 재기동 확인 (`Result=success`, `ExecMainStatus=0`, `consecutive_failures=0`).
- **태스크 쓰기 및 변경 알림 검증**:
  - Google Tasks API를 통해 완료된 업무(승진 심사위원 추천, 대학원 서약서 등) 4건 완료 처리 및 신규 우선순위 업무(Ezbaro 보완, 슬랙 결제 갱신, GARD 데이터 연계 등) 4건 등록·기한 지정.
  - 09:45:04 예약 tick에서 변경 감지 알림(`changes:2026-09-21:09`)이 Telegram 메시지 영수증과 함께 정상 전송됨을 확인.

위 기록은 운영 검증의 일환이다. 다음 세션은 상태 명령으로 현재 운영 상태를 다시 확인한다.
공개본 정리에서는 개인 계정 기본값과 실제 작업 통계·영수증 ID를 제거했다.
기존 운영 config와 원본 기록은 로컬에 보존한다. 현재 Git 인계는 저장소 루트 `HANDOFF.md`를 따른다.
