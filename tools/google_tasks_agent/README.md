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
