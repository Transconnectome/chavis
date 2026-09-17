# Chavis 인수인계

갱신: 2026-09-18. 이번 작업은 Google Tasks 개인 리마인더의 구현·운영과 Git 인계다.
저장소 `/home/juke/git/chavis`, 브랜치 `main`, 대상 `origin/main` (`Transconnectome/chavis`, 공개 저장소).
이전 철학 에이전트와 Codex Coach 기록은 아래에 당시 상태로 보존한다.

## Google Tasks 에이전트: 바로 시작할 곳

1. [실행 README](tools/google_tasks_agent/README.md)를 읽는다. 구현은 `agent.py`, 설치는 `install.py`, 검사는 같은 디렉터리의 `test_*.py`다.
2. `python3 tools/google_tasks_agent/install.py --check`와 `python3 tools/google_tasks_agent/agent.py status`로 현재 설치·조회·전송 상태를 확인한다. 상태 출력에는 실제 작업 수와 전달 영수증이 있으므로 전체를 공개 문서에 복사하지 않는다.
3. `systemctl --user list-timers google-tasks-agent.timer --no-pager`로 다음 실행을 확인한다. 중지·재개 방법은 실행 README에 있다.
4. 다음 정기 요약 후 비공개 SQLite의 날짜별 `digest` 영수증을 확인한다. 최초 수동 전송 성공과 미래 정기 전송 성공을 혼동하지 않는다.

## 이번에 완료한 변경과 유지할 동작

- `tools/google_tasks_agent/agent.py`: 기존 `gog` 인증으로 전체 목록·페이지·할당된 작업을 읽고, 기존 개인 OpenClaw Telegram 경로로 요약한다. Google Tasks 쓰기나 LLM 추론은 하지 않는다.
- 5분 주기 수집, 한국시간 08:30·17:30 요약, 22:00–08:00 전송 보류. 오늘·이전 예정일의 추가·변경은 시간당 최대 한 번 묶어서 알린다. High Priority와 날짜 없는 항목도 순환 표시한다.
- 전체 조회가 성공했을 때만 snapshot을 교체한다. 완료·삭제·숨김 작업을 제외하고, 중복 실행 잠금 및 SQLite의 작업 버전별 발송 기록으로 재시작·시간 초과 뒤 중복을 억제한다. 메시지에서 빠진 변경은 대기열에 유지한다.
- `install.py`: 자신의 service/timer만 설치하며 기존 config와 수동 수정 unit을 보존한다. 공개 코드에 개인 계정을 내장하지 않으며, 신규 설치에만 `--account`를 요구한다. 설치된 개인 config는 변경하지 않았다.
- API의 `due`는 예정 날짜이며 정확한 시각·실제 업무 마감 여부는 제공하지 않는다. 앱 시각 알림 재현, Telegram 양방향 명령·완료 처리, 작업 내용의 의미적 우선순위 판단은 구현 범위가 아니다.

## 확인한 검증과 운영 경계

- 최초 구현의 오프라인 회귀 검사 24개 통과, 독립 검토 `Proceed`. 공개본 계정 설정 검사 4개를 더해 현재 총 28개 통과. 아래 명령으로 재현한다.
- 실제 Google 전체 조회, 첫 Telegram 요약 전송 영수증, 동일 날짜 재실행 시 추가 전송 없음 확인. 사용자 열람 여부는 확인하지 않았다.
- 2026-09-18 02:50:00 KST 타이머가 직접 서비스 시작, 02:50:08 정상 종료 (`Result=success`, `ExecMainStatus=0`). 이후 인계 시작 시 source/delivery `healthy`, timer enabled/active, 설치 drift 없음 확인.
- `systemd-analyze --user verify` 통과, `Linger=yes`. 서버·네트워크가 동작할 때 로그인 종료 후에도 예약 실행한다. 서버 중단 자체를 알리는 외부 감시는 없다.
- 이번 인계에서는 새 알림을 발송하거나 Google 작업을 변경하지 않는다. 관련 없는 유료 sycophancy 벤치마크는 변경 범위 밖이어서 실행하지 않는다.

```bash
python3 -m unittest discover -s tools/google_tasks_agent -p 'test_*.py'
python3 tools/google_tasks_agent/install.py --check
python3 tools/google_tasks_agent/agent.py status
```

## 비공개 자료와 Git 범위

- 운영 설정: `~/.config/google-tasks-agent/config.json`. 계정은 여기에서만 공급한다.
- 상태·스냅샷·전송 영수증·설치 manifest: `~/.local/state/google-tasks-agent/`. 최초 상세 README는 같은 디렉터리의 `handoff-local-README-20260918.md`에 보존한다.
- 개인 Telegram 경로: `~/.local/state/codex-coach/telegram-route.json`. Google/OpenClaw 자격증명과 실제 task 본문·개인 식별자는 커밋하지 않는다. Git push는 이 비공개 자료와 설치된 user unit의 백업이 아니다.
- 인계 시작 시 fetch 뒤 `HEAD...origin/main`은 0/0, index는 비어 있었다. 커밋 범위는 이 `HANDOFF.md`와 `tools/google_tasks_agent/`의 소스·합성 테스트·README만이다.
- 기존 루트 `README.md`, `tests/test_notion_sync.py`, `tools/codex_coach/`의 수정, `skills/explore/`, `tools/audio_powerlaw/`, `tools/local_explore/`, `tools/codex_capabilities/`, 대시보드 PNG는 제외한다.
- 이 문서를 담을 커밋과 push의 성공 여부·최종 SHA는 완료 응답에서 실제 원격 조회 결과로 보고한다.

---

## 이전 작업 기록 — 차교수 철학 에이전트, 2026-09-14

아래 “현재 작업”, “이번 인계”와 날짜별 상태는 당시 기록이다.

갱신: 2026-09-14. 현재 작업은 차지욱 교수 철학 에이전트다. 저장소 `/home/juke/git/chavis`, 브랜치 `main`, push 대상 `origin/main` (`Transconnectome/chavis`, 공개 저장소). 아래 Codex Coach 인계는 이전 작업의 역사적 기록으로 보존한다.

## 차교수 철학 에이전트: 지금 시작할 곳

1. [공개 구현 보고서](docs/cha_philosophy/RESEARCH_REPORT.md), [실행 README](tools/cha_philosophy/README.md), [미적용 수정안](tools/cha_philosophy/pending/README.md)을 읽는다.
2. `cha-philosophy status` 및 `systemctl --user show cha-philosophy-refresh.service cha-philosophy-refresh.timer -p Id -p LoadState -p ActiveState -p SubState -p MainPID -p ExecMainStatus`로 현재 상태를 다시 확인한다. 비공개 JSON 전체를 공개 로그로 복사하지 않는다.
3. 운영 버전과 미적용 패치를 혼동하지 않는다. 운영 코드는 전체 검사 824개 통과 상태다. Gmail 스레드 문맥 수정안은 별도 사본에서 883개 통과했고 아래 이관 준비도 별도 사본에서만 수행했다.
4. 새로운 코드 확장보다, 검토 manifest와 현재 원문·원칙을 대조하고 코드와 검토 revision을 함께 반영하는 작업을 먼저 끝낸다. 그 뒤 실제 TLS handshake timeout의 좁은 재시도, Drive 남은 수집·내용 gap, Teams 인증을 처리한다.

## 완료한 작업과 산출물

- `tools/cha_philosophy/`: 비공개 원문·귀속·해석 등록부, 읽기 전용 수집·재개, 후보 추출·근거 감사, 과제 원본 보존·구간 계획·통합 선언 검사, CLI와 설치기.
- `skills/cha-philosophy/`, `agents/cha-philosophy.md`: 현재 요청과 근거에 맞는 글쓰기·평가·리뷰 적용. 세 클라이언트에 설치한 링크와 기존 스킬 연결은 로컬 환경에 별도로 존재한다.
- `docs/cha_philosophy/research_memory.md`, `research_connectors.md`, `research_evaluation.md`: 공개 1차 문헌·공식 API의 세 병렬 심층 조사. `EVALUATION_PROTOCOL.md`는 구조·의미 검증과 실제 비교의 경계를 정의한다.
- `tools/cha_philosophy/pending/thread-context.patch`: 운영 버전에 추가할 21개 파일의 코드·합성 검사·설명 수정. 인계에서 독립 사본에 적용하여 883개 검사를 통과한 Python snapshot과 모든 해시가 일치함을 확인했다. 운영 적용은 하지 않았다.

확인한 원칙은 검토자의 해석 18개, 후보 4개, 교수 명시 확인 0개다. 활성 인용은 56개다. 교수 계정에서 보냈다는 사실을 독자 집필·문장별 AI 기여·과학적 진실·교수의 에이전트 승인으로 바꾸지 않는다. 현재 rubric과 객관적 근거가 개인 선호보다 우선한다.

## 운영 상태와 남은 문제

2026-09-14 09:28 KST readback에서 timer는 active/waiting, 마지막 bounded cycle은 09:08:34 KST 종료 코드 0이었다. 이는 모든 플랫폼 성공이나 전체 corpus 완료가 아니다.

- Gmail: 현재 설정 query의 첫 전체 열거는 2026-09-13 08:52:17 KST에 6,337스레드·64페이지로 완료됐다. 이후 overlap 주기의 작은 숫자를 전체 수로 해석하지 않는다. 과거 관계 기간·별칭·그룹·의미 검토 전체 범위는 미완이다.
- Drive: 현재 query는 2,376파일·24페이지 처리, 현재 페이지 대기 28개·다음 페이지 있음·내용 gap 274개다. 전일 일회성 작업은 2,146파일에서 TLS handshake timeout으로 오류 종료했으나 다음 timer들이 checkpoint를 이어갔다. 전체 잔여 수를 현재 페이지 대기 수로 대체하지 않는다. 수거된 transient unit의 `LoadState=not-found`에서 보이는 기본 성공 값은 종료 증거가 아니다.
- Teams: native 수집의 active 메시지 5,323개와 별도 아카이브 1,469개는 전체 발견이나 최신 권한의 증명이 아니다. 연결 재인증과 전용 Entra public-client ID·로그인·silent 갱신의 실제 검증이 남았다.
- Ollama: 시스템 `ollama.service` inactive/dead, MainPID 0. 재시작에 관한 사용자 응답은 아직 없다. 대체 GPU 서비스나 외부 모델로 우회하지 않는다. 검색·bundle·prepare·감사는 기존 자료로 가능하다.
- 효과: 실제 과거 업무 세 사례의 제한된 대조는 여섯 판정 중 동률 5·기본 조건 선호 1·개인화 선호 0이었다. 원고 리뷰·실제 채점·시간 절감·교수의 새 독립 판단 일치는 미측정이다.

Gmail 전체 스레드 22개·171개 메시지에 대해 후속 정정·조건·인용 경계를 별도로 의미 검토했다. 이 근거를 사용하는 활성 원칙은 17개이며, Drive만 근거인 나머지 1개는 기존 검토를 유지한다. 현재 문장·예외를 유지할 수 있다는 검토 결과와 원격 재조회 후 별도 DB 검증을 보존했다. 이것을 운영 반영 완료나 교수 확인으로 표시하지 않는다.

## 다음 이관을 위한 비공개 위치

원문·개인 식별자·개별 사례 세부는 Git에 포함하지 않는다.

- 운영 저장소: `/home/juke/.local/share/cha-philosophy/evidence.sqlite3`; 설정은 같은 디렉터리의 `config.json`, 최신 주기 결과는 `refresh_status.json`.
- 스레드 수정 작업 사본: `/home/juke/.local/share/cha-philosophy/thread-context-stage-20260913/`.
- 같은 사본의 `reviewed_thread_migration_plan_20260913.json`: 실제 원격 재조회, 22개 검토 family manifest, 17개 검토된 원칙의 revision 연결, 76개 자료의 좁은 편집 링크 가림 계획. 운영 변경 0인 별도 사본 검증 결과다.
- 같은 사본의 `reviewed-thread-final-canary/evidence.sqlite3`, `small_family_semantic_review.json`, `middle_family_semantic_review.json`, `large_family_semantic_review.json`: 검토 결과와 원문 대응. 직접 읽기 허용 범위 안에서만 사용하고 새 모델·외부 서비스로 보내지 않는다.
- 같은 사본의 `prepare_reviewed_migration.py`: 당시 준비 과정의 로컬 스크립트. 기존 결과를 덮어쓰지 않도록 되어 있으며 운영 적용 스크립트가 아니다. 무작정 재실행하지 말고 현재 source hash·membership·원칙·제외 상태를 다시 비교한다.
- 이관 전 자료를 다시 읽어 명시적 revision review를 준비한다. 자동 context binding을 실제 읽기 표시로 대체하지 않는다. 순수 로컬 가림으로 `verified_at`을 갱신하지 말고, 원격 확인 시각·재개 위치·무관한 자료를 보존한다.
- 이번 인계 백업: `/home/juke/.local/share/cha-philosophy/handoff-push-20260914/`. 기존 상세 `RESEARCH_REPORT.md`·`PLAN.md`, 이전 `HANDOFF.md`, 운영 readback, 패치 재구성 검증을 보존했다. 현재 공개 보고서는 개인 사례를 제외한 요약이다.

추가 TLS handshake timeout 처리는 검토 요청 뒤 실행이 중단되어 코드에 반영되지 않았다. 위 패치의 883개 검사 결과를 이 미구현 처리의 검증으로 인용하지 않는다. 오래된 검토 snapshot의 freshness가 계속 유효하다고 가정하지 않는다.

## 검사·Git 경계

이번 인계에서 운영 버전의 `python3 -m pytest -q tools/cha_philosophy/tests`가 **824 passed (49.15s)**였다. anyio 플러그인 재작성 경고 1개가 있었고 실패는 없었다. 패치 적용 사본은 이전 **883 passed (60.61s)** 결과와 모든 Python 해시가 일치했다. 실제 네트워크·추론·의미 정확도·미래 성능을 이 검사 수로 입증하지 않는다. 기존 유료 sycophancy 벤치마크는 변경 범위 밖이라 실행하지 않았다.

인계 시작 시 `git fetch origin` 뒤 `HEAD...origin/main`은 0/0, staged 변경은 없었다. 이번 커밋은 이 HANDOFF와 철학 에이전트 경로에 한정한다. 기존 루트 `README.md`, `tests/test_notion_sync.py`, `tools/codex_coach/`의 변경, `tools/audio_powerlaw/`, `tools/local_explore/`, `skills/explore/`, 나머지 `docs/`, 대시보드 PNG는 제외한다. 기존 작업을 정리·삭제·일괄 stage하지 않는다.

이 요청은 인계·커밋·해당 브랜치 push를 승인한다. 배포·모델 재시작·원격 문서 게시·비공개 원자료 공개는 추가하지 않는다. Git은 비공개 DB·원문·평가 산출물·모델·인증·설치된 서비스 및 로컬 이관 계획을 백업하지 않는다. 이번 커밋의 SHA와 실제 원격 검증은 완료 응답에서 보고한다.

---

## 이전 작업 기록 — Codex Coach, 2026-09-12

아래 내용의 “이번 인계”와 날짜별 운영 상태는 이전 Codex Coach 작업 당시 기록이다. 현재 철학 에이전트 인계 상태는 위 내용을 따른다.

# Codex Coach 인수인계

갱신: 2026-09-12. 대상 저장소 `/home/juke/git/chavis`, 브랜치 `main`, 원격 `origin/main` (`Transconnectome/chavis`, 공개 저장소).

## 다음 작업 시작

1. `tools/codex_coach/README.md`와 `skills/codex-coach/SKILL.md`를 읽는다.
2. `python3 tools/codex_coach/install.py --check`로 설치 파일과 다섯 timer의 실제 상태를 확인한다.
3. `python3 tools/codex_coach/coach.py status`, `python3 tools/codex_coach/digest.py status`로 관찰·연구·전달 상태를 확인한다. 후자는 비공개 대상 설정을 포함할 수 있으므로 출력 전체를 공개 기록에 복사하지 않는다.
4. 다음 일일/주간 실행 뒤 날짜·채널별 전달 결과와 중복 억제를 확인한다. 실행기가 성공했다는 사실만으로 품질 개선이나 모든 채널 전달을 인정하지 않는다.

`tools/codex_coach/PLAN.md`는 최초 버전의 역사적 기록이다. 그 안의 연구 09:10·timer 두 개·테스트29개는 당시 값이며, 현재 운영 기준은 아래 일정과 installer/README다.

## 목표와 현재 구현

차지욱 교수의 전체 프로젝트 Codex 공개 대화·작업 흐름에서 결과물의 질과 검증 정확도를 개선한다. 개선 기회가 있을 때 짧게 개입하고, Codex와 기존 Telegram 봇에서 같은 코칭 기록을 사용한다. 첫 실제 사용자 사례는 수업자료·어려운 글쓰기에서 품질 개선 없이 반복 수정과 토큰 사용이 이어지는 문제다.

- `coach.py`, `observer.py`: DGX 로컬 세션의 제한된 공개 메시지 관찰, 증분 cursor, 자격증명 마스킹, 코칭 근거 인용 일치, 중복/야간/한도 제어, 피드백·브레인스토밍.
- `research.py`, `sources.json`, `RESEARCH.md`: 공식 자료·논문 원문 갱신, arXiv Atom 실패 시 공식 RSS 대체, 버전·마지막 성공 시각·방법/결과/한계, 후보와 로컬 효과의 구분.
- `digest*.py`, `notion_sync.py`: 일일 팁·주간 요약, SQLite/Markdown 보관, 피드백, Telegram 전달과 Notion 동기화의 별도 영수증 및 중복 억제. 준비·전달을 분리한다.
- `install.py`: Codex/OpenClaw 공용 스킬 사본과 자신의 systemd timer만 관리하며 다른 파일 및 수동 변경을 보존한다.
- `demo.py`: 비공개 보관 기록을 읽어 로컬 시연 HTML을 만든다. 생성물은 Git 대상이 아니다.
- `CHATGPT_SCHEDULE_SETUP.md`: ChatGPT 자체 예약 등록용 템플릿. 실제 예약은 미등록 상태이며 서버 일정과 구분한다.

현재 코드에 포함된 일일/주간 확장은 최초 버전 이후 추가된 코치 기능이다. 이번 인계는 의존성이 연결된 현재 버전 전체를 대상으로 하며, 새로운 외부 발송·동기화·배포를 실행하지 않는다.

## 확인한 운영 일정

2026-09-12 23:54 KST 조회에서 설치 manifest drift 없이 아래 다섯 timer가 enabled/active였다.

| 작업 | Asia/Seoul 일정 |
|---|---|
| 관찰 | 이전 실행 종료 후 약2분; 모델 분석 최소5분 간격·하루48회 상한 |
| 연구 갱신 | 매일07:00 |
| 일일 팁 준비 | 매일07:20 |
| 일일 팁 전달 | 매일08:00 |
| 주간 요약 생성·전달 | 일요일18:00 |

다음 일일 전달은 2026-09-13 08:00, 주간 생성은 같은 날18:00으로 표시됐다. 이 미래 일정의 실제 실행 성공은 아직 확인하지 않았다. 선제 관찰 알림은 하루6회·30분 간격·23:00–08:00 조용한 시간 제한을 적용한다. 이 숫자는 코치 자체 설정이며 외부 서비스 할당량 추정이 아니다.

## 유지할 경계

- 수정 횟수·토큰·자체 점수만으로 품질 정체를 판정하지 않는다. 목적·독자 이해·주장과 근거 기준의 판본 비교로 남은 결함 하나와 반복 종료 조건을 제안한다.
- 공개 대화의 표본은 실제 산출물/도구 결과의 전수 감사가 아니다. 사용자 프롬프트, 에이전트 오류, 지침 충돌, 의도 변경, 환경 문제를 구분한다.
- 다른 Mac/클라우드에만 있는 세션은 관찰하지 못한다. 모델 추론에는 제한된 마스킹 발췌가 전달되므로 완전한 오프라인 처리는 아니다.
- 선제 Telegram 관찰 알림에는 원문 인용·연구내용·프로젝트 경로를 넣지 않는다. 공개 출처 기반 일일/주간 팁은 별도 전달 경로다.
- 새 연구를 로컬 효과 입증으로 승격하거나 전역 지침·코드를 자동 수정하지 않는다. 만족/적용 피드백을 인과적 품질 개선으로 해석하지 않는다.
- 기존 Telegram 수신기를 재사용한다. 새 poller, 기존 세션 초기화, Gateway 재시작을 자동으로 수행하지 않는다.
- Notion 피드백 속성의 수동 편집은 서버 DB로 자동 역동기화되지 않는다. 공용 스킬의 feedback 경로를 사용한다.

## 검증과 남은 확인

이번 인계에서 현재 작업 트리의 관련 테스트78개, 스킬 형식 검증, 설치 readback(drift 없음·다섯 timer active), `git fetch origin` 뒤 HEAD/upstream 차이0/0을 확인했다. 공개 커밋용 식별자 치환본도 별도 snapshot에서 테스트78개·스킬 형식·Python 컴파일 검증을 통과했다. 기존 sycophancy의 유료 모델 벤치마크는 관련 코드가 변경되지 않아 실행하지 않았다.

```bash
python3 -m pytest -q tests/test_observer.py tools/codex_coach/tests tests/test_digest_install.py tests/test_notion_sync.py
python3 /home/juke/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/codex-coach
python3 tools/codex_coach/install.py --check
```

초기 구현 작업에서 실제 Codex/OpenClaw 스킬 상태 조회와 Telegram 연결 안내 발송을 확인했다. 현재 README에는 후속 일일 팁의 Notion·Telegram 영수증 및 주간 미리보기 검증 기록이 있다. 이번 Git 인계에서는 이를 새 외부 발송으로 반복하지 않는다. 실제 글쓰기 품질 개선 효과, 미래 예약의 실행, 실제 Telegram 사용자 입력부터 응답까지의 마지막 구간은 별도 확인 대상으로 유지한다.

이 호스트의 Codex CLI shell `read-only` 검증은 bwrap 권한 오류가 있었고, 초기 스킬 조회는 현재 앱의 full-access 환경에서 읽기만 수행했다. 도구를 제공하지 않는 백그라운드 추론은 read-only 실행에 성공했다. OpenClaw의 `--agent`와 `--session-id` 조합은 기존 main snapshot을 재사용할 수 있다. 격리 검증은 Gateway agent RPC에 별도 sessionKey와 deliver=false를 사용하고 sessionId는 생략한다. Telegram 메뉴에 없더라도 `/codex_coach` 직접 입력은 동적으로 해석된다.

## Git에 들어가지 않는 자료

- `/home/juke/.local/state/codex-coach/` 전체: 원문 발췌, 카드·답변, SQLite, 학습/검색 기록, 전달 영수증, route/config/manifest, 날짜별 자료, 데모·검증 산출물. Git 푸시는 이 자료를 백업하지 않는다.
- 홈 디렉터리에 설치된 스킬 사본·systemd unit·모델/연결 인증. 복구에는 기존 인증과 `codex`, `openclaw`, `systemctl`, PDF용 `pdftotext`, 자막용 `yt-dlp`가 필요하다. 선택적 `demo.py`에는 Python `markdown` 패키지도 필요하다.
- 비공개 Notion 대상 주소·실제 식별자는 공개본에서 일반 설정 안내와 합성 fixture로 치환한다. 로컬 원본은 수정하지 않아 해당 네 파일의 작업 트리 차이가 남을 수 있다. 실제 대상은 비공개 `digest-config.json`에서 찾는다.
- 기존 루트 `README.md`, `agents/cha-philosophy.md`, `skills/cha-philosophy/`, `skills/explore/`, `docs/`, 다른 `tools/` 하위 작업, 대시보드 PNG는 이번 커밋에서 제외한다.

이번 commit/push의 실제 SHA와 원격 확인 결과는 완료 응답에서 보고한다. 이 문서에 미래 푸시 성공이나 자기 자신의 SHA를 미리 기록하지 않는다.
