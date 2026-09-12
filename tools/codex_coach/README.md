# Codex Coach

차지욱 교수의 Codex 작업에서 **결과물의 질·검증 정확도**를 개선하는 개인 코치.
2026-09-12 사용자 선택: 전체 프로젝트 공개 대화/작업 흐름 관찰, 작업 중 짧은 개입.
수업자료와 어려운 글쓰기의 수정 과정을 관찰하며, 매일의 코칭은 사용자가 확인한
**"결과가 기대에 못 미쳤다"**를 출발점으로 삼는다. 구체적인 실패 원인은 아직 확인하지 않았다.

## 사용

Codex 새 작업에서 `$codex-coach`, 기존 Telegram 봇에서 `/codex_coach`를 사용한다.

| 요청 | 예시 |
|---|---|
| 현재 상태 | `$codex-coach 상태 확인해줘` |
| 최근 조언 | `/codex_coach 최근 조언` |
| 오늘의 실천 팁 | `/codex_coach 오늘의 팁` |
| 주간 요약 | `/codex_coach 이번 주 요약` |
| 프롬프트 개선 | `$codex-coach 이 수업자료 수정 요청에 종료 기준을 넣어줘` |
| 브레인스토밍 | `/codex_coach 이 글을 더 고칠지 끝낼지 같이 판단하자` |
| 피드백 | `/codex_coach 방금 조언은 잘못 짚었어` |
| 일시정지 | `/codex_coach 관찰을 일시정지해줘` |

양쪽은 같은 설정·코칭 카드·피드백·질문 답변·연구 근거를 사용한다.
Telegram의 기존 명령 메뉴에 새 항목이 아직 표시되지 않아도 `/codex_coach`를 직접 입력할 수 있다.
Gateway의 별도 검증 세션에서 이 명령의 자연 호출과 실제 상태 조회를 확인했다.
Codex에서 설치한 새 스킬이 현재 목록에 없으면 새 작업에서 호출하거나
`/home/juke/.codex/skills/codex-coach/SKILL.md`를 읽도록 요청한다.

글쓰기 정체를 의심할 때 기본 제안:

> 현재 판본을 보존해줘. 최근 수정 전후를 목적·독자 이해·핵심 주장과 근거 기준으로 비교하고,
> 실제로 줄어든 결함과 표현만 바뀐 부분을 구분해줘. 아직 중요한 결함 하나가 확인될 때만
> 그 부분을 수정하고, 없으면 이번 반복을 끝내줘.

수정 횟수, 사용 토큰, 에이전트의 자체 점수만으로 품질 정체를 판정하지 않는다.
사용자 의도 변경·에이전트 실행 오류·지침 충돌·환경 장애를 구분하며,
한 번에 작은 개선 하나와 그 효과를 확인할 기준을 제안한다.

## 실제 동작과 한계

- 읽기 전용 관찰기는 이 DGX의 `~/.codex/sessions`, `~/.codex/archived_sessions`를 대상으로 한다.
  다른 Mac/클라우드에만 있는 세션은 보이지 않는다. 전 프로젝트 허용은 전 호스트 접근을 뜻하지 않는다.
- 관찰은 시작 후/이전 실행 종료 후 약2분 간격. 공개 사용자·assistant 메시지의 최근 표본을 읽는다.
  시스템/개발자 지침, 추론, 도구 본문, subagent 세션은 제외한다. 따라서 실제 파일·테스트·논문 근거의
  완전한 감사가 아니며, 로그에 검증이 보이지 않는다는 이유로 실패라고 단정하지 않는다.
- 반복 교정·수정·완료·검증 관련 신호가 있으면 기존 인증의 Codex 모델로 판단한다.
  분석 간격 최소5분, 하루 최대48회, 선제 알림 최대6회, 알림 간격 최소30분,
  23:00–08:00 KST 조용한 시간은 첫 설정이다. **사용자 서비스 할당량에 대한 추정이 아니라
  이 코치 자체의 운영 제한**이다. `status`에서 실제 가동/한도 상태를 확인한다.
- 선제 알림은 Telegram으로 보낸다. Codex에서는 공용 스킬로 조언을 조회/토론한다.
  현재 작업으로 메시지를 강제 주입하거나 원래 에이전트 작업을 자동 수정하지 않는다.
- 근거가 충분하지 않으면 조용히 유지한다. 인용이 실제 로그에 존재하는지는 코드로 검사하지만,
  그 인용이 해석을 뒷받침하는지는 모델 판단과 사용자 피드백에 의존한다. 신뢰 점수는 교정된 확률이 아니다.

## 연구 갱신

매일 **07:00 Asia/Seoul**에 공식 가이드와 연구 원문을 읽는다. 최초 검토 근거는
[RESEARCH.md](RESEARCH.md)와 [sources.json](sources.json)에 있다.

공식 가이드·기존 연구6건 + 최신 관련 arXiv 후보 최대5건을 찾고, 신규 원문 최대2건을 읽는다.
Atom 검색 오류 시 arXiv 공식 cs.AI RSS를 한 번 읽는 대체 경로를 쓴다. RSS 발표 시각을 최초
출판일로 바꾸지 않는다. 방법·결과·한계 발췌, 버전, 해시, 마지막 성공 시각을 저장한다.
원문을 못 읽은 후보는 초록만 본 상태로 남으며 오류가 마지막 성공 기록을 덮지 않는다.

모델이 만든 학습 항목의 인용문이 실제 읽은 원문 발췌에 있는지 검사한다. 누적 학습은 최대40개,
코칭 분석에는 최근8개를 제공한다. 새 근거는 제한적 적용 후보이며, 실제 사용자 작업에서 효과가
입증되었다는 뜻이 아니다. 전역 지침·스킬·코드를 자동으로 고치지 않는다. 모든 최신 문헌을
빠짐없이 검토한다는 보장은 없다.

## 일일 팁과 주간 요약

사용자 승인 일정은 한국시간 기준 매일 오전8시 일일 팁, 일요일 오후6시 주간 요약이다.
서버에서 아래 일정을 실행하며, 설치·활성화 여부는 `install.py --check`로 확인한다.

| 실행 | 한국시간 | 내용 |
|---|---|---|
| 연구 갱신 | 매일07:00 | 공식 자료·연구 원문과 새 후보 확인 |
| 일일 준비 | 매일07:20 | 오늘의 팁 작성·검증·비공개 보관 후 Notion 동기화 |
| 일일 전달 | 매일08:00 | Notion 상태 확인·필요시 재시도 후 오늘의 팁을 Telegram에 전달 |
| 주간 종합 | 일요일18:00 | 기록·피드백을 종합한 뒤 전달·동기화 |

주간 요약은18시에 생성을 시작하므로 실제 도착은 작성·검증·연결 처리만큼 늦어질 수 있다.
일일 사전 준비가 실패하여 오늘 기록이 없으면 전달도 실패 상태로 남는다. 준비와 전달을 분리하여
오전8시의 전달 작업이 긴 연구·생성 작업으로 이어지지 않게 한다.
서버가 꺼져 있으면 실행할 수 없다. 재시작 시 systemd가 놓친 일정을 재개할 수 있지만,
실행기는 현재 날짜/주간의 산출물과 전달 기록을 기준으로 처리하여 과거 날짜의 팁을 몰아서 보내지 않는다.

범위는 수업자료(`teaching`), 논문(`research`), 학생 평가(`student_assessment`),
교수 평가(`faculty_assessment`), 학과 행정(`administration`)이다. 일일 팁은 한 가지 실천에
집중한다. 오늘 바꿀 행동, 업무와의 연결, 바로 쓰는 지시문, 근거·한계, 효과를 확인할 방법을 담는다.
주간 요약은 저장된 팁과 실제 피드백을 구분하며, 피드백이 없으면 적용 여부·효과를 확인 불가로 남긴다.

논문의 방법·결과·한계와 공식 가이드를 우선 검토한다. 파워유저 YouTube 사례는 접근한 자막이나
영상 구간을 명시하고 개인 시연과 실험 근거를 구분한다. 새롭고 유용한 근거가 없으면 그 사실을
밝히고 이전 실천의 점검으로 연결한다. 새로운 팁이나 성과를 날짜에 맞춰 만들어내지 않는다.
학생·교수의 원본 평가 자료나 개인별 점수는 이 연구·팁 데이터베이스의 수집 대상이 아니다.

```bash
python3 /home/juke/git/chavis/tools/codex_coach/digest.py status
python3 /home/juke/git/chavis/tools/codex_coach/digest.py report --limit 3
python3 /home/juke/git/chavis/tools/codex_coach/digest.py report --id <shown-id>
python3 /home/juke/git/chavis/tools/codex_coach/digest.py feedback <shown-id> 적용함 --note '핵심 근거 누락이 줄었음' --quality 4 --review-minutes 12 --rework-count 1
```

피드백은 `도움됨`, `잘못 짚음`, `불필요`, `적용함`, `보류` 중 실제 사용자 답변을 저장한다.
`--note`, `--quality`(사용자가 제공한1–5점), `--review-minutes`, `--rework-count`는 선택 항목이다. 단순 만족이나 적용을
품질·생산성 개선의 증거로 바꾸지 않는다. 검토 시간과 재작업 횟수는 사용자가 제공한 값만 기록한다.

ChatGPT 자체의 예약 작업은 서버 systemd 일정과 별개다. 이 작업에서 네이티브 예약 도구를
사용할 수 없으면 ChatGPT 예약은 미등록으로 명시한다. 서버 일정이나 알림 성공을 ChatGPT 예약
성공으로 보고하지 않는다.

## 상태와 데이터

상태 위치: `/home/juke/.local/state/codex-coach/` (디렉터리0700, 데이터0600).

- `config.json`: 우선순위·개입·한도. 없으면 코드의 검증된 기본 설정을 사용한다.
- `monitor.json`: 처리 cursor, 마지막 실행/분석, 실제 상태와 표본 범위.
- `context.json`: 마스킹한 최근 발췌. 최대6세션×3000자, 최근2시간만 유지.
- `cards.json`: 근거/조언/피드백. 기본30일 보존. 사용자 답변은 `answers.json`에 최근50건.
- `research.json`, `learning.json`: 원문 갱신과 근거 일치 검사를 통과한 적용 후보.
- `model-usage.jsonl`: 코치 자신의 실제 모델 호출 시간·보고된 토큰. 사용자 전체 작업 비용이나 절약액이 아니다.
- `telegram-route.json`: 기존 allowlisted 사용자와 기존 수신 경로의 일치를 확인한 비공개 설정.
- `digest-config.json`: 일일·주간 설정과 Notion 대상 식별자. 접근 토큰은 저장소에 넣지 않는다.
- `digest.sqlite3`: 출처·일일 팁·주간 요약·피드백·전달 상태의 누적 데이터베이스.
- `digests/daily/`, `digests/weekly/`: 날짜별 Markdown 보관본. 전체 구조화 데이터는 SQLite 안의 JSON에 저장한다.
- `digest-research/`: 날짜별 검색·본문 확인 기록의 JSON. 연구 근거와 사용자 적용 결과를 구분한다.

일일·주간 요약은 공개 출처에 근거한 코칭 내용을 Telegram과 지정 Notion 대상에 전달한다.
각 전달 채널의 성공·실패는 따로 확인한다. 로컬 보관 성공만으로 Telegram 수신이나 Notion 저장
완료를 주장하지 않는다. 관찰기 원문 발췌는 일일·주간 전달물에 복사하지 않는다.

원본 로그는 변경하지 않는다. 자격증명과 이메일을 마스킹하지만 모든 민감정보를 자동 식별할 수는
없다. 제한된 공개 대화 발췌를 기존 Codex 모델 서비스로 보내 분석하므로 완전한 오프라인 처리는
아니다. Telegram 선제 알림은 검토된 일반 코칭 템플릿만 보내며 원문 인용·연구내용·프로젝트 경로를
넣지 않는다. 사용자가 후속 조회를 요청하면 스킬은 필요한 작업 흐름만 요약한다.

## 설치·중지·점검

```bash
python3 /home/juke/git/chavis/tools/codex_coach/install.py --enable
python3 /home/juke/git/chavis/tools/codex_coach/install.py --check
python3 /home/juke/git/chavis/tools/codex_coach/coach.py status
python3 /home/juke/git/chavis/tools/codex_coach/coach.py tick --dry-run
```

설치기는 자신의 파일만 설치하고 다른 파일의 변경을 보존한다. Codex 스킬, OpenClaw 공용/워크스페이스
스킬, 자신의 다섯 systemd timer만 관리한다. 기존 Telegram 수신기는 그대로 사용한다.

```bash
# 관찰만 일시정지/재개; 연구 갱신은 계속
python3 /home/juke/git/chavis/tools/codex_coach/coach.py pause
python3 /home/juke/git/chavis/tools/codex_coach/coach.py resume
# 일일·주간 팁만 중지 (원본/코칭 기록은 보존)
systemctl --user disable --now codex-coach-digest-prepare.timer codex-coach-digest-delivery.timer codex-coach-digest-weekly.timer
# 관찰·연구 갱신도 중지
systemctl --user disable --now codex-coach-monitor.timer codex-coach-research.timer
```

검증 명령:

```bash
cd /home/juke/git/chavis
python3 -m pytest -q tests/test_observer.py tools/codex_coach/tests tests/test_digest_install.py tests/test_notion_sync.py
python3 /home/juke/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/codex-coach
```

기존 관찰기 초기 검증 기록(일일·주간 확장 이전): 2026-09-12 핵심29개 테스트 통과,
독립 검토 Proceed, 실제 Codex 추론과 첫 로그 분석
성공(개입할 근거가 없어 알림0건), 원문8건 갱신+최신 후보5건+초기 인용 일치 학습6건 성공.
Codex CLI 실제 스킬 상태 조회, OpenClaw 별도 세션의 `/codex_coach` 상태 조회,
기존 Telegram 수신 경로의 연결 안내 발송 영수증, 두 timer enabled/active와 다음 실행을 확인했다.
이 호스트의 Codex CLI `read-only` shell 실행은 bwrap 권한 오류가 있어 스킬 호출 검증은 현재
앱과 같은 `danger-full-access` 환경에서 상태 조회만 수행했다. 백그라운드 추론은 shell/MCP를
제공하지 않는 read-only 실행으로 성공했다.
이 검증은 동작 확인이며 글쓰기 품질 개선 효과는 향후 실제 판본 비교/사용자 피드백으로 확인한다.

### 일일·주간 확장 실제 검증 (2026-09-12)

- 전체 관련 테스트 **78개 통과**, 스킬 형식 및 diff whitespace 검사 통과.
- 실제 웹 검색, 논문2건·공식 가이드2건 본문 발췌, YouTube1건 자막을 수집했다.
  첫 팁은 이 중 논문1건과 가이드1건을 근거로 사용했고, 인용의 원문 일치를 확인했다.
- 첫 팁 `daily:2026-09-12`를 SQLite/Markdown에 저장하고 Notion의 Record ID·내용 해시·본문을
  native query/fetch 결과로 다시 확인했다. 초기 탐색 메모와 후속 자막 수집 결과가 달랐던 문장을
  전송 전에 바로잡았으며, 수정 전 준비 기록은 private `setup-revisions/`에 보존했다.
- Notion: 차지욱 · 에이전틱 AI 실천 연구 (비공개 링크는 로컬 설정 참조).
  첫 팁 (비공개 링크는 로컬 설정 참조)의 Telegram 발송 영수증 확인.
  같은 전달 명령과 systemd 서비스를 다시 실행해도 전송 시도1회가 유지되어 중복 발송되지 않았다.
- 주간 요약은 일일 기록1건·피드백0건으로 실제 모델 생성까지 시험했다. 결과를
  `validation/weekly-preview.md`로 구분해 보관했으며 실제 주간 예약 결과로 배포하지 않았다.
- 다섯 timer enabled/active, 설치 manifest drift 없음. 전달 service 수동 실행 Result=success,
  ExecMainStatus=0, user Linger=yes. 다음 일일 전달은 **2026-09-13 08:00 KST**,
  주간 작업은 **2026-09-13 18:00 KST**. 아직 그 미래 예약의 실제 발화 성공을 주장하지 않는다.
- ChatGPT 자체 예약은 생성 도구 부재로 **미등록**. [등록용 완성 설정](CHATGPT_SCHEDULE_SETUP.md)에
  Notion의 같은 결과를 읽는 일일·주간 지침을 남겼다. 별도 조사 중복을 피한다.
- 공용 스킬로 입력한 피드백은 서버 DB에 누적되어 이후 팁/주간 요약에 반영된다.
  Notion의 피드백 속성을 직접 편집한 내용은 현재 서버 DB로 자동 역동기화되지 않는다.

내부 전달 실패는 채널별로 기록하며 process exit code도 실패로 반환한다. 준비되지 않은 일일 결과나
주간 생성 실패는 기존 Telegram 경로에 당일 한 번만 알린다. Notion의 오래된 inflight는20분 후
고유 Record ID 조회로 복구하지만, 결과가 불명확한 Telegram 전송은 확인 없이 재발송하지 않는다.
