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
