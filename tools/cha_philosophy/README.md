# 차지욱 교수 철학 에이전트

직접 발언과 검토된 원칙을 연결해 연구 글쓰기·평가·리뷰에 적용하는 로컬 에이전트다. 원칙마다 정확한 발언, 적용 범위, 예외와 상태를 보존한다. 실제 교수 확인과 에이전트의 근거 해석은 구분한다.

연구 근거·실제 자료의 해석·전체 목표의 남은 조건은 [연구 보고서](../../docs/cha_philosophy/RESEARCH_REPORT.md), 검증 계획은 [평가 프로토콜](../../docs/cha_philosophy/EVALUATION_PROTOCOL.md)에 있다.

## 바로 사용하기

설치된 앱에서 **“차교수 철학을 적용해서 이 글을 고쳐줘”**, **“차교수의 검토 기준을 적용하되 이 rubric으로 평가해줘”**, **“차교수 원칙에 근거해서 이 원고를 리뷰해줘”**라고 요청한다. `cha-philosophy` 스킬은 기존 글쓰기·리뷰 스킬에 현재 근거를 제공한다. 스킬 목록을 시작 시 읽는 클라이언트에서는 다음 작업부터 새 스킬이 표시될 수 있다.

명령으로 현재 원칙과 수집 상태를 확인할 수 있다.

```bash
cha-philosophy status
cha-philosophy bundle writing '연구 제안서의 질문과 검증 계획을 다듬기' --limit 3
cha-philosophy bundle review '예측 모형의 추가 가치와 결론 검토' --limit 3
cha-philosophy principles --status candidate
```

현재 스토어는 `/home/juke/.local/share/cha-philosophy/`이며, 원문이나 생성 결과를 저장소에 커밋하지 않는다. `status`는 본문을 출력하지 않는다. `source`, `search`, `bundle`, `prepare`, `read-task-unit`, `apply`, `audit-task`에는 비공개 발언·과제 본문·산출물이 포함될 수 있으므로 공개 로그로 보내지 않는다.

## 원고와 rubric을 분리한 작업 실행

`prepare`는 모델 없이 원고·rubric·요청과 현재 원칙을 묶고, 원래 입력을 식별하는 비공개 receipt를 기록한다. 아래 파일은 사용자가 준비한 UTF-8 입력 경로 예시다.

```bash
cha-philosophy prepare evaluation --target app \
  --request /path/to/request.txt \
  --input rubric=/path/to/rubric.txt \
  --input manuscript=/path/to/submission.txt \
  --output /path/to/private-bundle.json
```

입력 종류는 `rubric`, `manuscript`, `new_evidence`, `reference`다. 입력은 현재 작업에서 제공된 자료이며 검증된 과학적 사실이나 교수의 직접 발언으로 취급하지 않는다. 동일한 종류·본문·과제 범위에서 안정적인 `task:` ID가 만들어진다. CLI는 원문을 UTF-8로 읽으며 CRLF를 LF로 바꾸거나 공백·인용·숫자를 정리하지 않는다. 문자 위치와 해시는 보존된 원문을 기준으로 한다. 동일 종류의 동일 본문을 반복 제공하면 하나의 출처로 취급한다.

| 실행 경로 | 입력·원칙 보존과 문맥 한도 |
| --- | --- |
| `prepare --target app` — 기본값 | 모든 입력과 선택한 최대 3개 원칙의 전체 근거·예외를 유지한다. 로컬 모델의 문맥 한도로 준비를 거절하지 않는다. |
| `prepare --target local` | 입력은 자르지 않는다. 문맥이 부족하면 원칙 전체 단위로 선택 수를 줄이고 제외 ID를 기록한다. 입력이나 남겨야 할 근거가 들어가지 않으면 `needs_chunking=true`로 실패한다. |
| `apply` | 항상 위의 로컬 준비 경로를 사용한 뒤 로컬 모델을 호출한다. 전체 원고의 자동 분할 추론·통합 기능은 아니다. |

준비 결과의 `preparation.full_task_inputs_supplied=true`는 파일 내용이 묶음에 온전히 담겼다는 뜻이다. `local_context_fit`은 현재 로컬 모델 경로의 보수적 한도 검사 결과다. 앱의 실제 문맥 적합성·읽기·작업 완료는 검사하지 않으며 `app_context_fit_verified`, `input_reading_verified`, `task_completion_verified`는 모두 `false`다. 출력 파일은 비공개 권한으로 새로 만들며 기존 원고나 결과 파일을 덮어쓰지 않는다.

로컬 모델이 켜져 있을 때 동일한 입력으로 `apply`를 호출할 수 있다.

```bash
cha-philosophy apply review \
  --request /path/to/request.txt \
  --input manuscript=/path/to/manuscript.txt \
  --output /path/to/private-result.json
```

기본 모델은 `qwen3.6:35b-ctx16k`, 주소는 `127.0.0.1:11434`다. 모델 서비스는 명령이 자동 시작하지 않는다. 이 설치 당시 서비스는 정상 종료된 상태였다. 모델이 없어도 기존 원칙의 검색·bundle·prepare·audit는 실행할 수 있다.

원칙 후보를 추출할 때에는 현재 메시지 본문의 명시적인 대필·외부 저자 표지 행을 정확한 위치와 함께 모든 본문 구간에 전달한다. 서명 구분자 뒤의 표지도 포함하며, 부정 표현이 잘리지 않도록 해당 행 전체를 보존한다. 표지는 해석용 문맥이고 인용 근거는 해당 `authored_text` 구간에서만 허용한다. 표지가 있다고 메시지 전체를 자동 제외하거나 AI 저작을 확정하지 않는다.

이 추가 문맥은 실제 JSON UTF-8 크기로 1,600바이트까지 허용한다. 넘으면 모델 호출 전에 검토 필요 오류로 보류한다. 호출 전체의 실제 직렬화 크기와 재시도 여유를 계산해 필요하면 본문 구간 크기를 줄이며 원문 전체는 유지한다. 추출 완료 기록에는 문맥 공급 범위와 구간 크기를 남긴다. 알려진 텍스트 인용 경계만 구분하므로 HTML 인용의 평탄화, 표지 미탐지, 다른 메시지의 후속 정정은 해결하지 못한다. 후보 활성화 전 전체 원문과 관련 스레드의 의미 검토가 필요하다.

앱의 에이전트가 `prepare` 결과를 읽고 만든 출력은 다음 구조로 저장하여 검사한다.

```json
{
  "content": "실제 작업 산출물",
  "applications": [
    {"principle_id": "bundle에 있는 ID", "applied_to": "산출물에 실제로 있는 문장", "rationale": "어떤 판단에 어떻게 적용했는지"}
  ],
  "claims": [
    {"text": "산출물에 실제로 있는 주장", "source_ids": ["bundle에 있는 ID"], "claim_type": "inference"}
  ],
  "uncertainties": ["아직 확인하지 못한 사항"]
}
```

```bash
cha-philosophy audit-task /path/to/output.json /path/to/private-bundle.json
```

`structural_valid`는 출처·인용·적용 기록의 구조 검사 통과다. 사실 정확성, 올바른 rubric 채점, 교수의 판단 재현은 별도 의미 검토를 요구한다. 의도적으로 잘못된 점수도 형식이 맞으면 구조 검사를 통과할 수 있다는 반례를 회귀 테스트에 포함했다. 검증기는 이런 결과를 의미 검증 완료로 표시하지 않는다.

## 전체 원고의 입력 보존과 구간 기록

준비 시 스토어의 비공개 `task_receipts/`에 원래 요청·출처·원칙의 ID와 스냅샷 해시, 순서를 원자적으로 보존하고 `task_receipt_id`를 반환한다. 이 기록에는 원고나 rubric 본문을 넣지 않는다. 이후 전체 rubric을 빼거나 원고를 짧은 발췌로 바꾸고 해시를 다시 계산해도 원래 준비 기록과 다르면 감사가 실패한다. 예전 버전에서 만든 receipt 없는 bundle은 원래 전체 입력으로 다시 준비해야 한다. 동일 사용자가 로컬 검증 기록 자체를 변조하는 행위나 실제 입력 제공자의 신원까지 인증하는 기능은 아니다.

긴 원고는 원본을 그대로 둔 채 구간별로 읽는다. `plan-task`는 요청·rubric·원고·참고자료를 포함한 모든 `task:` 출처를 원래 순서대로 분할한다. 기본 구간은 최대 6,000 UTF-8바이트이며 Unicode 문자를 중간에서 자르지 않는다. 원본 source ID, 원문 전체에서의 문자 시작·끝, 구간 해시를 유지하고 공백·줄바꿈도 보존한다. 문단이나 문장 경계에서 끝난다는 보장은 없다. 앱은 요청·rubric의 모든 구간을 먼저 확인하고 나머지 자료를 읽은 뒤, 방법·결과·결론 및 평가 기준을 함께 검토한다.

```bash
cha-philosophy plan-task /path/to/private-bundle.json --output /path/to/private-plan.json
cha-philosophy read-task-unit /path/to/private-bundle.json UNIT_ID
cha-philosophy audit-task /path/to/output.json /path/to/private-bundle.json \
  --coverage /path/to/private-coverage.json
```

`plan-task` 결과의 `coverage_schema`가 해당 계획에 사용할 정확한 JSON Schema다. `UNIT_ID`에는 같은 결과의 `units[].unit_id`를 사용한다. `read-task-unit`은 receipt를 검사하고 원문 구간을 반환하지만, 누가 실제로 읽었는지 기록하거나 확인하지 않는다.

coverage 파일에는 다음 필드가 필요하다. `unit_notes[].citations`만 선택 사항이며 생략하거나 `[]`로 둘 수 있다.

- `task_receipt_id`, `plan_id`, `max_unit_bytes`: 원래 계획에서 그대로 복사한다.
- `unit_notes`: 요청과 rubric을 포함한 **모든 unit을 중복 없이 한 번씩** 기록하고 각 `notes`를 채운다. 인용을 넣으면 그 unit과 같은 `source_id` 및 해당 구간 안의 원문 위치를 사용한다. `char_start`는 0부터 시작하고 `char_end`는 포함하지 않는 Unicode 문자 위치이며 바이트 위치가 아니다. `quote`는 이 원문 범위와 정확히 같아야 한다.
- `synthesis`: `text`에 통합 검토를 쓰고 `addressed_unit_ids`에 전체 unit ID를, `rubric_source_ids`에 전체 rubric 출처 ID를 중복 없이 넣는다. rubric이 없으면 후자는 `[]`다. `cross_unit_checks`의 각 항목은 서로 다른 두 개 이상의 unit과 대조 메모를 담아야 하며, 모든 항목을 합쳐 전체 unit을 다뤄야 한다. unit이 하나뿐이면 이 목록은 `[]`다.

아래는 빈 원칙 저장소에서 `evaluation` 작업으로 준비한 세 입력의 구조 예시다. 입력은 끝 줄바꿈 없이 각각 `Apply the supplied rubric.`, `Question: 40%. Methods: 60%.`, `Question stated. Methods described.`이며, 각 입력이 한 unit이다. 이 JSON은 해당 계획의 `coverage_schema`와 원문 위치 검사를 통과한다. 실제 작업에서는 아래 ID를 재사용하지 말고 **자신의 plan에 있는 ID와 모든 구간에 대한 실제 검토 내용**을 사용한다.

```json
{
  "schema_version": 1,
  "task_receipt_id": "e63e0290a7bacbd5afbde926e3fd1f660f898b03653750031eaa2fa208c6dfab",
  "plan_id": "73de57655566367f71188e117b426d8a8725337d8ca5f6cddd176efcfb431828",
  "max_unit_bytes": 6000,
  "unit_notes": [
    {
      "unit_id": "task-unit:6000:ad95cca6fbbaf76280a11e22be466a974df031858f1241e61efa5760414cd371",
      "notes": "제공된 rubric을 적용하라는 요청이다."
    },
    {
      "unit_id": "task-unit:6000:04471ed7997f275e5c681eccc4a912fd6a657732a6964dc632e238eecf7110d8",
      "notes": "Question 40%, Methods 60%이며 다른 기준은 제시되지 않았다.",
      "citations": [
        {
          "source_id": "task:rubric:6754b11c2119e7633315595a656425a2598c808a2ff09dd2714ea46ca6f6e20f",
          "char_start": 0,
          "char_end": 28,
          "quote": "Question: 40%. Methods: 60%."
        }
      ]
    },
    {
      "unit_id": "task-unit:6000:7c37826008c80359f18e80791a0d195377931931e4c39ff1840be55a356c1e88",
      "notes": "질문과 방법이 기술되었다는 짧은 진술만 있어 구체적 수준을 확인할 수 없다."
    }
  ],
  "synthesis": {
    "text": "요청에 따라 질문과 방법을 rubric의 두 기준에 연결했다. 원고에 세부 내용이 없고 rubric에도 성취 수준별 배점 규칙이 없어 정확한 점수는 확인 불가다.",
    "addressed_unit_ids": [
      "task-unit:6000:ad95cca6fbbaf76280a11e22be466a974df031858f1241e61efa5760414cd371",
      "task-unit:6000:04471ed7997f275e5c681eccc4a912fd6a657732a6964dc632e238eecf7110d8",
      "task-unit:6000:7c37826008c80359f18e80791a0d195377931931e4c39ff1840be55a356c1e88"
    ],
    "cross_unit_checks": [
      {
        "unit_ids": [
          "task-unit:6000:ad95cca6fbbaf76280a11e22be466a974df031858f1241e61efa5760414cd371",
          "task-unit:6000:04471ed7997f275e5c681eccc4a912fd6a657732a6964dc632e238eecf7110d8",
          "task-unit:6000:7c37826008c80359f18e80791a0d195377931931e4c39ff1840be55a356c1e88"
        ],
        "notes": "요청·원고·rubric을 함께 대조했다. 두 기준의 비중은 보존하되 성취 수준이나 점수 계산 규칙을 새로 만들지 않았다."
      }
    ],
    "rubric_source_ids": [
      "task:rubric:6754b11c2119e7633315595a656425a2598c808a2ff09dd2714ea46ca6f6e20f"
    ]
  }
}
```

빠진 구간·중복 구간·잘못된 인용·통합 기록 누락은 실패다. CLI의 `audit-task` 입력 JSON과 `plan-task`·`read-task-unit`의 bundle JSON에서 중복 키나 `NaN`·`Infinity`도 거절한다. 임의의 완료 플래그를 coverage에 추가할 수 없다.

`audit-task --coverage`의 `input_coverage_accounted=true`는 구간 메모와 통합 검토 **선언의 구조**가 완전하다는 뜻이다. 메모를 지어내도 형식이 맞으면 통과할 수 있으며, 실제 읽기·올바른 통합·rubric 준수·산출물의 전체 작업 완료를 입증하지 않는다. 최상위 `input_reading_verified`와 `whole_task_completion_verified`는 `false`로 남고, 상세 `coverage_accounting.task_completion_verified`도 `false`다. `--coverage` 없는 감사는 `input_coverage_accounted=false`로 구간 처리 범위를 미검증으로 둔다. 구간 메모를 이어 붙인 것만으로 전체 원고 검토를 마쳤다고 보고하지 않는다.

계획·coverage에는 로컬 자원 한도가 있다. 출처당 8 MiB, 전체 입력 32 MiB, 최대 256개 출처·8,192개 구간이며, `--unit-bytes`는 256~65,536 범위에서 지정한다. 각 메모 16 KiB, 통합문 64 KiB, coverage JSON 전체 8 MiB 한도를 넘으면 자르지 않고 명시적으로 실패한다. 전체 입력의 앱 준비가 성공해도 이 별도 단계의 처리 한도가 사라지는 것은 아니다.

## 정기 갱신

설치기는 사용자 systemd timer를 추가한다. 활성화 후 약 5분에 첫 실행, 종료 후 약 1시간마다 다시 실행한다. 지연은 최대 2분이며 서비스 quota 추정과 무관한 로컬 일정이다. 기본 한 주기는 Gmail 100스레드, Drive 10파일, Graph Teams 3채널, 추출 3자료까지 처리한다. 과거 수집은 checkpoint에서 이어간다.

Gmail·Drive·인증이 설정된 Teams는 같은 주기에서 각각 수집한다. 긴 Drive 읽기가 다른 서비스의 시작을 지연시키지 않으며, 완료한 서비스의 checkpoint와 coverage는 전체 주기 종료 전에 저장된다. worker마다 별도 SQLite 연결을 사용하고 짧은 쓰기 transaction을 직렬화한다. `refresh.lock`은 주기 중복을 막고, `sync.lock` 공유 잠금과 플랫폼별 배타 잠금은 다른 플랫폼의 동시 진행과 같은 플랫폼의 중복 차단을 함께 보장한다. 유지보수는 기존처럼 `sync.lock`을 배타적으로 잡는다.

Drive 파일별 정합 처리에서는 SQL의 정확한 platform·scope 조건으로 해당 파일의 출처만 읽는다. 이웃 파일을 먼저 모두 읽고 걸러내지 않으며, 전체 inventory가 끝났을 때 누락 파일을 확인하는 전체 조회는 유지한다. 기존 계정·본문 보기·댓글 조건도 유지한다.

Google 읽기 호출에서 `dial tcp`와 확인된 연결 시간 초과 표현이 함께 발생하면 2초 뒤 정확히 같은 읽기를 한 번 재시도한다. 두 번째에도 실패하면 `gog_connect_timeout`으로 보고하고 checkpoint를 보존한다. 인증 거절·권한·rate/quota 응답이나 일반적인 미분류 오류는 이 재시도의 대상이 아니다. 다운로드는 전용 임시 디렉터리가 비어 있을 때만 재시도하며 부분 파일을 삭제하거나 덮어쓰지 않는다.

터미널에서 CLI를 `Ctrl-C`로 중단하면 종료 코드 130으로 끝나며 이미 commit한 checkpoint는 남는다. 실행 중인 네트워크 호출을 개별 취소하는 기능은 아니다. 라이브러리의 `refresh()`는 `KeyboardInterrupt`를 호출자에게 전달하고, 호출자가 예외를 잡아 프로세스를 유지하면 잔여 worker가 끝날 때까지 주기 잠금도 유지한다. systemd 서비스 중단은 해당 control group을 종료한다.

```bash
systemctl --user list-timers cha-philosophy-refresh.timer
systemctl --user status cha-philosophy-refresh.service
cha-philosophy refresh --gmail-limit 100 --drive-limit 10 --distill-limit 3
```

수집·추출 결과는 비공개 `/home/juke/.local/share/cha-philosophy/refresh_status.json`에 원자적으로 저장한다. 해당 파일은 플랫폼별 성공·실패, 과거 자료의 남은 범위, 모델 준비 상태, 후보 검토 대기 수를 분리한다. 외부 알림이나 메일은 보내지 않는다. `state=finished_bounded_cycle`은 한 주기 종료를 뜻한다. 자료나 추출의 실제 실패가 있으면 CLI는 실패 종료 코드를 내고 `health=source_error` 등으로 구분한다. 일부 경로만 실패했거나 사용 중인 근거 재확인에 실패한 경우도 성공으로 숨기지 않는다. 예산 때문에 남은 정상적인 부분 진행과 모델 중단은 별도로 표시한다.

초기 과거 자료의 수집을 앞당기는 임시 backfill은 한 주기에 Gmail 최대 1,000스레드와 Drive 최대 100파일을 읽으며, `refresh.lock`으로 정기 작업과의 중복을 막는다. 2026-09-13 01:17 KST에 시작한 `cha-philosophy-backfill-20260913b.service`는 Gmail 1,000스레드를 처리한 뒤 Drive의 해당 수집 주기에서 29개 파일까지 진행하고 다음 문서의 오류로 종료·수거되었다. 이 unit이 계속 실행 중인 것으로 해석하면 안 된다.

당시 기록의 `source_access_denied`는 대형 native Slides의 PPTX 내보내기에서 발생한 HTTP 403 `exportSizeLimitExceeded`를 잘못 분류한 것이었다. 실제 메타데이터와 Slides GET은 읽을 수 있었다. 수정 후 같은 문서를 저장소에 쓰지 않고 확인하여 **97개 슬라이드에서 추출한 텍스트 104,181문자**(본문·발표자 노트와 구간 표시 포함), 별도 **댓글 항목 50개와 완료된 페이지 조회**를 확인했다. 슬라이드 순서·전체 목록과 최종 수정 시각도 다시 검사했다. 이것은 해당 문서의 읽기 확인이며 과거 자료 전체 수집 완료를 뜻하지 않는다.

내보내기 크기 초과나 확인된 내보내기 시간 초과에만 권한이 허용된 native Slides GET 경로를 사용한다. 결과는 `partial_text=true`이며 그룹·표 안의 텍스트, 이미지·내장 파일, master·layout 텍스트, 일부 노트 도형 및 서식은 포함하지 못한다. 그 한계는 `document_read_gaps`에도 남는다. 일반 인증 실패·권한 거부·rate/quota 제한은 이 대체 경로를 실행하는 근거가 아니다. 내보내기나 문서 본문만 읽지 못했을 때는 해당 단계의 gap을 남기고 읽을 수 있는 댓글과 다른 파일을 계속 처리하며, 다음 전체 조회 주기에 다시 확인한다. 기존 본문의 검증 시각을 새로 갱신하지 않으며, 파일 메타데이터나 댓글 접근 상실이 확인되면 해당 범위의 기존 근거를 철회한다.

정기 Drive 수집의 native Slides 대체 읽기는 비공개 `document_cache/native_slides/`에 슬라이드 단위 진행을 저장한다. 계정·파일·MIME·수정 시각과 슬라이드 전체 순서가 맞아야 이어 읽으며, 각 응답을 검증한 다음 원자적으로 기록한다. 시작과 종료에 원격 메타데이터·전체 목록을 다시 확인한다. 바뀐 revision이나 순서의 본문을 섞지 않고, 중단된 진행 기록은 원문 DB·철학 원칙으로 등록하지 않는다. 모든 슬라이드와 최종 검증을 마치면 임시 본문 캐시를 제거한다. 이 경로도 위에 명시한 표·그룹·이미지 등의 누락을 유지한다.

실제 573장 파일에서 자식 프로세스가 302→303→307장을 계속 읽는 것을 확인했다. 파일 단위 checkpoint가 그대로여도 이처럼 내부 작업이 진행 중일 수 있다. 짧은 처리율 관측상 이 파일은 기본 서비스의 45분 제한을 넘을 수 있어, 제한 시간을 늘리는 대신 다음 실행에서 같은 revision의 읽은 구간을 재사용하도록 보강했다. 실행 중이던 `backfill-20260913c`는 이전 코드를 이미 적재했으므로 이 새 기능이 중간에 적용됐다고 보지 않는다.

파일 접근 상실·삭제가 확인되면 해당 계정·파일의 진행 캐시도 무효화한다. 다른 읽기 작업이 사용 중이면 무효화 요청을 남기고 다음 저장·완료 전에 검사한다. 댓글만 읽을 수 없는 경우는 별도로 처리한다. `document_resume_*` 오류는 해당 파일을 완료 처리하지 않고 pending 위치를 보존하며 정기 상태의 source 오류로 노출한다. 캐시가 없으면 첫 장부터 읽으며 진행을 기록한다. `GogClient`에 `document_cache_home`을 지정하지 않은 직접 호출은 진행을 저장하지 않는 기존 방식으로 동작한다.

별도로 검토한 `gog slides read-presentation-text` 실행 파일을 선택하려면 비공개 `config.json`의 `drive_slides_text_executable`에 그 파일의 절대 경로를 지정한다. 예: `"drive_slides_text_executable": "/absolute/path/to/reviewed-gog"`. 직접 호출에서는 `GogClient(..., slides_text_executable="/absolute/path/to/reviewed-gog")`를 사용한다. 옵션이 없으면 기존 설치된 gog와 위의 내보내기·슬라이드별 재개 경로를 그대로 쓴다. 옵션이 있으면 native Slides만 새 명령을 한 번 실행한다. 명령 내부의 Slides GET 두 번으로 shape·group·table·WordArt 및 모든 노트 페이지 도형의 텍스트와 슬라이드 순서를 읽고, 기존 gog로 최종 Drive 파일 ID·MIME·수정 시각·삭제 상태를 다시 확인한다. 인증·권한·변경·불완전 응답 실패를 기존 경로로 대체하지 않는다. 이전 슬라이드별 임시 캐시는 성공과 최종 검증 뒤에 폐기하며, 접근 상실·삭제·변경 시 무효화하고 일시적 실패 시 보존한다.

이 명령의 재현 가능한 변경은 `vendor/gog-read-presentation-text.patch`와 `vendor/gog-slides-text-build.json`에 보존했다. 공식 [gogcli](https://github.com/steipete/gogcli)의 고정 커밋 `c18c58c8daadce2d6b4a6cf4fe95b1e3817bae1b`에 적용하며 Go 1.25.8을 사용한다. 기존 설치 파일과 분리한 새 checkout에서 다음처럼 빌드한다.

```bash
git clone --branch v0.12.0 https://github.com/steipete/gogcli.git /path/to/new/gogcli
git -C /path/to/new/gogcli checkout c18c58c8daadce2d6b4a6cf4fe95b1e3817bae1b
git -C /path/to/new/gogcli apply /home/juke/git/chavis/tools/cha_philosophy/vendor/gog-read-presentation-text.patch
go -C /path/to/new/gogcli test ./internal/cmd -run '^TestSlidesReadPresentationText' -count=1
go -C /path/to/new/gogcli build -trimpath -o /path/to/new/gog-slides-text ./cmd/gog
```

빌드만으로 설정·인증·서비스를 바꾸지 않는다. `revisionId`가 편집 권한이 있을 때만 제공된다는 조건은 [Google의 Presentation 계약](https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations)을 따른다.

선택적 구조 읽기도 `partial_text=true`이다. 이미지/OCR·차트·내장 미디어·layout/master·서식·링크 대상은 추출하지 않으며, 화면에 보이는 읽기 순서나 표 병합 배치를 확정하지 않는다. 읽기 전용 권한 때문에 Slides revision ID가 없으면 `revision_id_unavailable`을 남기고, Slides 순서 재확인과 Drive 메타데이터 대조만 기록한다. 이 검증은 교수 저자 확인을 뜻하지 않으며 문서의 `authorship=unverified`를 유지한다. `--max-bytes=33554432`는 고정된 텍스트·JSON 출력 한도다. SDK가 최초 API 응답을 해석할 때 사용하는 전체 메모리까지 제한하는 옵션은 아니다.

재개용 임시 `cha-philosophy-backfill-20260913c.service`는 01:59:49–03:26:37 KST에 실행되어 Gmail 1,000스레드와 Drive 수집 주기의 108번째 파일까지 처리한 뒤 다음 Docs 본문 오류로 종료됐다. 573장 파일의 본문 520,638자를 저장했지만 위에 명시한 텍스트 누락은 남겼다. 실행 중이던 c에는 내부 재개 새 코드가 중간 적용되지 않았다. 이전 Gmail 자료·checkpoint는 보존한다. 진행 중에는 `cha-philosophy status`의 checkpoint와 `systemctl --user list-units 'cha-philosophy-backfill*' --all`을 확인한다. 비공개 `refresh_status.json`은 한 주기가 끝날 때 갱신되므로 시작·종료 시각이 해당 실행과 일치하는지도 확인한다. 임시 unit은 종료 후 수거될 수 있다. 실행 중인 임시 작업을 중지하려면 확인된 해당 service를 별도로 stop한다.

Docs 본문에서 실제 재현한 `docs.cat`의 `timeout awaiting response headers`는 `document_native_doc_timeout` 내용 누락으로 기록한다. 해당 본문을 새로 읽었다고 표시하지 않고 댓글과 다음 파일은 계속 읽으며 다음 전체 inventory 주기에 재시도한다. 기존 본문과 검증 시각은 보존한다. 이 처리는 해당 명령의 확인된 응답 헤더 시간 초과에만 적용한다. 인증·권한·rate/quota 오류를 먼저 판별하며, 일반 명령 오류와 로컬 프로세스 시간 초과는 기존 실패·pending 보존 동작을 유지한다.

선택적 Docs 구조 읽기는 비공개 설정의 `drive_docs_text_executable`에 검토한 실행 파일의 절대 경로를 지정해 사용한다. 직접 호출 옵션은 `GogClient(..., docs_text_executable="/absolute/path/to/reviewed-gog")`이다. 이 실행 파일에는 고정된 `docs read-document-text DOCUMENT_ID --max-bytes=33554432` 명령만 전달한다. 이 명령의 바깥 실행 제한은 270초이며 다른 gog 명령의 기존 90초 제한을 바꾸지 않는다. 명령 내부에서는 같은 Docs 서비스로 기본 보기, 제안 포함 보기, 최종 작은 검증 응답을 세 번 읽는다. 문서 ID·제목·revision·전체 탭 계층을 대조한 뒤 기존 gog로 최종 Drive ID·MIME·수정 시각·삭제 상태를 다시 확인한다. 옵션이 없으면 기존 Docs 읽기 경로를 유지한다.

Docs·Slides를 함께 제공하는 누적 변경은 `vendor/gog-structured-document-text.patch`, 고정 빌드 환경과 명령은 `vendor/gog-document-text-build.json`에 있다. 같은 원본 커밋 `c18c58c8daadce2d6b4a6cf4fe95b1e3817bae1b`에 이 누적 패치를 직접 적용한다. 앞의 Slides 전용 패치와 중첩 적용하지 않는다. Go 1.25.8에서 `go test ./internal/cmd ./internal/googleapi -run '^Test(DocsReadDocumentText|DocsTextRead|SlidesReadPresentationText)' -count=1` 후 별도 경로에 빌드한다. Docs 전용 서비스만 기존 인증 TokenSource·재시도 설정을 복제해 응답 헤더 대기를 120초로 늘리고 전체 명령은 240초로 제한한다. 기존 서비스와 토큰 교환의 30초 제한은 유지한다. 두 보기의 의미와 view별 위치 차이는 [Google Docs 공식 설명](https://developers.google.com/workspace/docs/api/how-tos/suggestions)을 따른다.

`PREVIEW_WITHOUT_SUGGESTIONS`는 보류 중인 제안을 모두 거절한 상태로 **표시하는 기본 보기**다. 실제 문서의 제안을 승인하거나 거절하는 동작은 아니다. 그 텍스트는 기존 canonical 문서 source에 저장하고, `SUGGESTIONS_INLINE`은 `view_role=suggestions_inline_context`인 연결된 별도 source에 보존한다. 제안 포함본에는 삽입·삭제 표시와 상위 표·행·셀에서 내려온 제안 ID를 남긴다. 둘 다 `authorship=unverified`, `authored_text=""`이며 직접 철학 근거·자동 추출 대상이 아니다. 두 source는 한 파일의 대체 표현으로 연결되며, 한 transaction으로 저장하고 수집 파일 수는 하나로 센다. 제안만 있는 문서의 기본 본문은 빈 문자열 그대로 둔다.

제안 포함본만 읽을 권한이 없는 경우를 포함한 Docs 단계별 실패에서는 이전 두 본문과 검증 시각을 보존하고 gap을 남긴다. 최종 Drive 조회에서 파일 접근 상실·삭제가 확인되면 두 view를 함께 철회한다. 이전 기본 읽기 경로로 되돌리면 과거 INLINE 본문·검증 시각을 유지하되 `paired_view_status`를 `not_rechecked_with_canonical` 또는 `stale_for_canonical_revision`으로 표시한다. 원래 두 view를 함께 읽었던 사실과 지금도 같은 revision인지 여부를 구분한다.

각 view의 구조 레코드는 `extraction.view_records`에 따로 남긴다. `startIndex`·`endIndex`는 Google Docs의 UTF-16 code unit 위치이며 view마다 달라질 수 있다. Python 본문 문자열의 문자 위치나 근거 인용 범위로 재사용하지 않는다. 이미지/OCR·내장 그림·방정식·자동 페이지 번호·목록 배치·서식·링크 대상 등의 미추출 한계와 해당 레코드 위치를 보존한다. 텍스트 양옆에 미추출 요소가 있으면 표시를 넣어 별개의 글자가 하나의 문장으로 붙지 않게 한다. 고정 한도는 두 view를 합친 텍스트·JSON 출력 32 MiB, 탐색 노드 200,000개, 탭 1,024개, 탭 깊이 8·본문 깊이 16이다. SDK의 입력 응답 전체 메모리 한도를 보장하는 기능은 아니다. revision ID가 없으면 `revision_id_unavailable`과 Drive 메타데이터 재확인 범위를 명시한다.

### PDF의 이미지 텍스트와 Office 내부 경로

비공개 설정의 `drive_pdf_ocr_tessdata`에 `eng.traineddata`와 `kor.traineddata`가 있는 디렉터리의 절대 경로를 지정하면, `pdftotext`가 문서 전체에서 텍스트를 얻지 못했을 때 로컬 OCR을 사용한다. 직접 호출 옵션은 `GogClient(..., pdf_ocr_tessdata="/absolute/path/to/tessdata")`이다. 현재 환경에서는 설치된 PyMuPDF의 OCR 기능과 별도로 확인한 언어 자료로 실행되며, 서비스나 Ollama를 시작하거나 원문을 외부로 보내지 않는다. 엔진·언어 자료가 없으면 오류를 반환한다. 일부 페이지에만 이미지 글자가 있는 혼합 PDF는 이 조건에서 자동 OCR하지 않으며 기존 추출 한계를 유지한다. [PyMuPDF OCR 계약](https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html), [Tesseract 언어 자료](https://tesseract-ocr.github.io/tessdoc/Installation.html)

OCR은 고정된 Python 작업자 하나에서 CPU로 실행한다. 입력·추출 본문·JSON 출력은 각각 32 MiB, 최대 256페이지, 페이지당 1,200만 픽셀, 전체 300초·CPU 240초·주소 공간 2 GiB로 제한한다. 페이지마다 200 dpi 이하에서 실제 렌더 경계가 픽셀 한도에 들어가는 최대 정수 해상도를 선택하며, 1 dpi에서도 초과하면 렌더 전에 실패한다. 이 값은 로컬 처리 예산이다. 모든 페이지를 처리한 뒤에만 결과를 반환하며 중간 실패·시간 초과·전체 빈 결과를 성공으로 저장하지 않는다. 페이지 번호·실제 해상도·텍스트 바이트 수, 입력과 언어 자료 해시, PyMuPDF/MuPDF 버전을 보존한다. 마지막에 Drive 수정 시각·ID·MIME·크기를 다시 대조하며 실제 접근 상실이 확인되면 해당 파일의 기존 자료를 철회한다.

`requested_render_dpi=200`은 요청 상한이고 `pages[].render_dpi`는 실제 적용 값이다. 전체 `render_dpi`는 모든 페이지가 같은 해상도면 그 값, 혼합이면 `null`이다. 해상도를 낮춘 페이지 수와 인식 품질 미검증 한계도 기록한다. 작은 글자가 정확히 인식됐다는 보장은 없으며, worker 응답 schema 2는 실제 페이지별 해상도가 빠졌거나 잘못된 값을 거절한다.

결과의 `partial_text=true`와 `all_pages_processed=true`는 함께 존재한다. 전자는 OCR 오인식·누락과 표·수식·그림 의미의 한계를, 후자는 전체 페이지를 처리했다는 사실을 뜻한다. 숫자·이름·필기체·읽기 순서나 인식 정확도를 검증한 것으로 표현하지 않는다. 본문은 작성자 미확인 문맥이며 교수의 직접 발언으로 승격하지 않는다.

일부 Office ZIP의 `word\\document.xml` 같은 역슬래시 경로는 메모리 안의 이름 조회에서 슬래시로 대응한다. 정규화 후 서로 같은 이름이 되는 두 부품은 거절하고, 원래 ZIP 부품을 읽어 기존 크기·XML·관계 경로 제한을 유지한다. 원본 압축 파일은 바꾸거나 파일시스템에 풀지 않는다. `~$` 잠금 파일 형태의 비 ZIP 파일은 DOCX 본문으로 처리하지 않는다.

실제 Docs 실패 지점에서 2파일 수집을 재개해 Drive 주기를 110파일로 진행했고, 이전 빈 Markdown 한 건을 별도 재수집하여 크기 누락 기록을 해소했다. 03:39:53 KST에 `cha-philosophy-backfill-20260913d.service`를 시작해 다음 범위를 이어 읽는다. 완료·현재 실행 여부는 위의 service 및 상태 파일 확인 방법으로 판단한다.

d 백필은 댓글 문맥 해시 보강 코드를 배포하기 위해 명시적으로 종료했다. DB·checkpoint를 보존하고 구형 댓글·답글 518개의 해시를 현재 저장 본문으로 재계산했다. 본문·인용 문맥·`verified_at`은 변하지 않았으며 원격 조회로 표시하지 않는다. 실제로 다시 조회한 9개 출처만 원격 관측 시각으로 갱신했다. 03:58:05 KST에 `cha-philosophy-backfill-20260913e.service`가 보존된 위치에서 수집을 재개했다.

e 백필은 서비스별 동시 수집 코드를 적용하기 위해 명시적으로 종료했다. 해당 Gmail 범위 1,878스레드·Drive 범위 151파일의 처리 위치와 DB 무결성을 확인했다. 임시 f unit의 실행 시간 제한 설정을 바로잡은 뒤 04:24:39 KST에 `cha-philosophy-backfill-20260913g.service`를 시작했다. 실제 `Type=oneshot`, 시작 제한 2시간, 실행 PID를 확인했다. 종료·전체 수집 완료를 뜻하지 않는다. 코드 해시·586개 회귀·중단 및 재개 관측은 [동시 수집 검증 기록](/home/juke/.local/share/cha-philosophy/verification_parallel_refresh_20260913.json)에 있다.

모델이 꺼져 있으면 새 원문은 수집하고 추출을 보류한다. 이미 지원되는 원칙의 직접 Gmail 근거와 Drive 댓글·답글 근거는 24시간 이후 과거 자료 목록보다 먼저 재조회한다. Drive는 파일 메타데이터와 댓글의 모든 페이지가 확인되어야 기존 근거의 검증 시각을 갱신한다. 일부 페이지 실패로 오래된 근거를 새로 확인한 것으로 표시하지 않으며, 본문 확인 시각은 별도로 유지한다. 원문 변경·삭제·접근 상실 시 기존 원칙은 자동으로 사용 대상에서 빠진다. 새 후보는 근거 검토 없이 활성화하지 않는다.

### 인증의 현재 범위

- Gmail·Drive: 기존 gog 인증을 재사용한다. 설치기는 현재 세션에 있는 `GOG_KEYRING_PASSWORD`를 값 출력 없이 사용자 systemd manager의 환경으로 가져온다. 새 비밀 파일을 만들지 않는다.
- 사용자 manager 또는 호스트 재시작 후에는 해당 환경이 사라질 수 있다. 이미 인증된 셸에서 `systemctl --user import-environment GOG_KEYRING_PASSWORD`를 실행해 복구한다. 이 명령은 키 값을 인자로 적지 않는다. 재부팅까지 포함한 영구 무인 인증은 별도 검증해야 한다.
- Teams: 전용 Microsoft Entra public-client 앱을 사용하는 선택적 device-code 로그인과 MSAL silent 갱신 경로를 구현했다. **현재 실제 설정·로그인은 수행하지 않았다.** 앱 연결로 일부 메시지를 읽은 상태와 정기 작업의 Graph 인증은 별개다. `CHA_TEAMS_ACCESS_TOKEN` 방식도 남아 있지만 단일 access token만으로 장기 갱신이 되지는 않는다. channel·chat은 독립적인 목록·메시지 checkpoint를 사용한다.

### Teams 자동 인증 준비

승인된 전용 앱의 client ID를 확보한 후 비공개 `config.json`의 기존 설정에 아래 항목을 추가한다. 꺾쇠괄호는 실제 GUID로 교체해야 하는 설명용 자리다. 현재 설정을 이 예제로 통째로 교체하지 않는다.

```json
{
  "teams_tenant_id": "<조직 tenant GUID>",
  "teams_professor_ids": ["<교수 계정 object GUID 하나>"],
  "teams_auth": {
    "mode": "device_code",
    "client_id": "<승인된 전용 public-client 앱 GUID>"
  }
}
```

앱은 public-client flow와 delegated `User.Read`, `Chat.Read`, `Channel.ReadBasic.All`, `ChannelMessage.Read.All` 권한이 필요하다. `ChannelMessage.Read.All`은 관리자 동의가 필요하며 다른 권한도 조직 정책에 따라 별도 동의가 필요할 수 있다. 현재 수집기는 설정된 team 범위를 사용하며 모든 소속 team을 자동 발견하지 않는다. 근거와 설정 계약은 [인증 조사](../../docs/cha_philosophy/research_connectors.md)를 따른다.

```bash
cha-philosophy teams-auth status
# 직접 조작하는 터미널에서 최초 로그인 시에만 실행
cha-philosophy teams-auth login
```

`status`는 로컬 설정·계정 binding만 확인하며 네트워크나 keyring을 열지 않는다. 최초 로그인은 stdin·stdout·stderr가 모두 터미널일 때만 기기 코드를 표시한다. tenant·교수 object ID·허용 범위와 Graph `/me`를 검증한 뒤 해당 앱·tenant·교수 조합의 전용 Secret Service 항목에 MSAL 캐시를 저장한다. 잘못된 계정의 최초 로그인 결과는 영구 캐시에 쓰지 않는다.

정기 작업은 silent 인증만 시도한다. 잠긴 keyring을 해제하거나 대화상자를 띄우거나 평문 캐시로 전환하지 않는다. Secret Service 연결은 암호화된 전송 세션을 요구하지만, 이것이 운영체제 keyring의 디스크 암호화를 입증하지는 않는다. 최초 로그인 성공 이후에도 사용자 service 환경·재부팅·조직의 재인증 정책 아래 지속 동작하는지는 실제 검증이 필요하다.

Graph의 일반 401에는 silent 강제 갱신을 한 번 시도하고, claims challenge나 반복 401은 명시적인 재인증 필요 상태로 반환한다. `teams_auth`가 설정되어 있으면 설정·갱신 실패 시 다른 환경 토큰으로 바꾸지 않는다. 인증 성공도 전체 channel·chat 열거 또는 과거 메시지 수집 완료를 뜻하지 않는다.

수집 대상 주소와 자료 범위는 비공개 `config.json`에 있다. 교수 발신 alias, 연구실 수신 주소 44개, 우선 Drive 파일, Teams tenant/team/교수 ID를 사용한다. 이 주소 목록은 현재 구성원 명부가 아니며 과거 구성원의 주소도 포함한다. 추가한 주소 2개의 날짜별 원문 근거·한계와 설정 백업 위치는 비공개 `historical_roster_scope_expansion.json`에 있다. 메일 초대 참석만으로 구성원임을 확정하거나 완전한 과거 구성원 목록으로 가정하지 않는다.

교수 계정에서 보냈다는 사실과 문장의 작성자는 다를 수 있다. 명시적 자동 테스트·AI 에이전트 자기소개·타저자 전재문처럼 검토 결과 철학 근거에 부적합한 출처는 별도 `source_exclusions` 기록으로 관리한다. `exclude-source SOURCE_ID --source-hash HASH --reviewer NAME --reason REASON`은 실제로 읽은 revision과 일치할 때만 적용된다. 원문·형제 메시지·수집 checkpoint는 보존하고, 해당 자료의 자동 추출·직접 근거 검색·원칙 등록·bundle 사용을 막는다. 기존에 연결된 원칙은 stale로 바뀐다. 평가용 보류 자료와는 별도 기능이다.

`source-exclusions`는 제외 이유와 원문 revision 변경 여부를 보여준다. 재조회나 모델 출력은 제외를 해제하지 못한다. 현재 revision을 다시 읽은 뒤 `clear-source-exclusion SOURCE_ID --source-hash HASH --reviewer NAME --reason REASON`으로 해제할 수 있지만 원칙이 자동 활성화되지는 않는다. `search --include-context`와 `source`는 원문 확인용으로 계속 사용할 수 있다. 제외 판단을 다듬어진 문체나 AI 보조 가능성만으로 내리지 않으며, 교수의 실제 전달 지침과 복사된 타저자 글을 구분한다.

저장 전 비밀값 가림은 API 키·토큰·URL password 외에 명시적인 `Passcode`, 회의·Zoom·Teams password, 한국어 회의 암호 등의 라벨과 중첩 필드를 처리한다. 전화 한 번으로 참가하는 one-tap 문자열의 `#` 뒤 쉼표·세미콜론 대기 기호와 `*` 다음에 오는 숫자 접속 코드도 가리며 전화번호·회의 ID·접속 구분자는 유지한다. 본문·직접 발언·인용 문맥에 일관되게 적용하고 NBSP 공백도 처리한다. 라벨 없는 숫자나 모든 비밀값을 탐지하는 기능은 아니며, 날짜·통계량·일반 코드는 그대로 보존한다. 새 패턴을 과거 저장 자료에 반영하려면 명시적 재처리가 필요하다. 재처리를 원격 확인으로 표시하지 않고, 바뀐 근거의 해시는 정상적으로 무효화한다. 서비스 원본과 이미 만든 독립 스냅샷의 자동 정리를 뜻하지 않는다.

별도로 검토한 연구실 관련 프로젝트 주소는 선택적 `lab_project_routes` 목록에 저장한다. 현재 개인 주소 44개를 유지한 채 프로젝트 주소 1개를 활성화했다. 이 목록은 그룹 구성원 명부가 아니며 그룹을 자동 확장하지 않는다. 각 메시지의 정확한 교수 From·SENT·현재 To/Cc/Bcc 일치를 확인한다. 프로젝트 경로만 일치한 직접 발신은 `sent_verified_from_exact_lab_project_route`로 구별한다. 미설정 또는 빈 목록이면 이전 조회·정규화 결과를 유지하며, 이메일 형식이 아닌 값과 검색 연산자는 `gmail_lab_project_routes_invalid`로 거절한다. 새 범위는 새 전체 query를 시작하고 이전 자료·checkpoint를 보존한다. 실제 첫 페이지 조회와 3스레드 동기화를 확인했으나 전체 수집은 남아 있다. 범위 근거·설정 백업·활성화 결과는 비공개 `group_route_scope_review.md`와 `project_route_activation_20260913.json`에 있다.

Drive 댓글·답글의 해시에는 참조 문장 `quoted_file_content`와 문서 영역 `anchor`, 값의 부재도 포함된다. 댓글 본문이 같아도 이 문맥이 바뀌면 관련 원칙은 검토 전 사용 대상에서 빠진다. 저장 해시가 재계산 값과 다르면 `source integrity mismatch`로 거절하며, 조회나 검사가 구형 근거를 자동 재승인하지 않는다. 해결 상태·단순 수정 시각은 이 문맥 해시에서 제외한다.

## 후보 검토와 변화 반영

```bash
cha-philosophy distill --limit 3
cha-philosophy principles --status candidate
cha-philosophy source SOURCE_ID
cha-philosophy review PRINCIPLE_ID evidence_supported \
  --reviewer '실제 검토자' --reason '원문의 의미와 적용 범위·예외를 검토한 이유'
```

이미 검토된 원칙에 같은 해석을 뒷받침하는 새 직접 발언이 생기면 `support PRINCIPLE_ID EVIDENCE_JSON --reviewer NAME --reason REASON`으로 추가한다. 입력은 `source_id`, `source_hash`, `quote`만 가진 객체의 배열이다. 전체 기존·추가 근거를 원자적으로 다시 검사하며, `evidence_supported` 상태에서만 허용한다. 원칙 문장·영역·예외·상태는 바꾸지 않고 중복 인용은 추가하지 않는다. 검토 이유는 철회 시 지워지는 원칙의 검토 이력에, 감사 로그에는 이유 해시와 전후 원칙 해시를 남긴다. 근거가 바뀐 이전 bundle은 다시 준비해야 한다. 새로운 해석·예외 또는 상충하는 발언은 이 명령으로 조용히 덧붙이지 않고 별도 후보·충돌 검토를 거친다.

교수의 명시적 확인이 있는 경우에만 `professor_confirmed`와 `--professor-attestation`을 사용한다. 단지 에이전트가 그럴듯하다고 판단했거나 교수가 메시지를 보냈다는 사실은 확인의 근거가 아니다. 의견 충돌은 `conflict LEFT RIGHT`, 해소는 `resolve KEEP SUPERSEDE --reviewer ... --reason ...`로 기록한다. 해소 후에도 보존할 원칙은 검토 대기 상태로 돌아간다.

`revoke SOURCE_ID`는 로컬 출처와 파생 인용을 철회한다. 원본 서비스에는 쓰지 않는다. 이미 출력한 파일은 DB와 독립된 사본이므로 다시 쓰기 전에 검증하고, 보존·삭제 범위를 따로 관리한다.

## 원칙 자료와 평가 사례의 분리

실제 과거 답변을 평가 참조로 남길 때는 답변만 빼는 것으로 충분하지 않다. `hold-for-evaluation SOURCE_ID --reason ...`은 Gmail 계정·스레드 계열 전체와 그 뒤에 수집될 같은 계열의 메시지를 원칙 추출·근거 등록에서 제외한다. 이미 어떤 원칙의 근거로 사용된 계열은 사후에 미관측 평가 자료로 지정할 수 없다. 수집과 본문 보존은 계속되며 추출 완료로 허위 표시하지 않는다.

`release-evaluation SOURCE_ID --reason ...`은 제외를 해제한다. 이후 원칙 개발에 사용한 사례는 새로운 평가에서 미관측 사례로 주장할 수 없다. 현재 고정된 실제 사례 세 건과 생성·Gemini 대조 기록은 비공개 `evaluation_holdout/`에 있으며, 첫 소규모 비교에서는 철학 적용 답안의 우위가 관찰되지 않았다. 상세 범위와 한계는 연구 보고서를 따른다.

## 설치·검증·되돌리기

Python 3.12, `jsonschema`, `defusedxml`, gog, PDF 읽기용 `pdftotext`가 필요하다. 선택적 Teams 자동 인증에는 `msal`, `msal-extensions`, `secretstorage`, `jeepney`와 사용자 Secret Service가 추가로 필요하다. 로컬 추출·생성에는 Ollama와 지정 모델이 필요하다. 현재 머신의 도구로 실행·테스트했다.

```bash
cd /home/juke/git/chavis
python3 -m pytest tools/cha_philosophy/tests -q
python3 -m tools.cha_philosophy.install --enable-timer
```

설치기는 새 command·skill·agent 경로에 다른 파일이 있으면 덮어쓰지 않는다. 기존 관련 SKILL.md에는 표시된 연결 블록만 붙이며 원본 사본을 비공개 `installation_backups/`에 남긴다. 설치 기록은 `installation-*.json`이다. 기존 저장소 README와 다른 작업의 파일은 이 설치 대상이 아니다.

정기 작업은 `systemctl --user disable --now cha-philosophy-refresh.timer`로 중지할 수 있다. 진행 중인 수집까지 종료하려면 별도로 `systemctl --user stop cha-philosophy-refresh.service`를 사용한다. 스킬 되돌리기는 설치 기록을 확인해 추가한 `CHA-PHILOSOPHY` 블록과 이 설치의 symlink만 제거한다. 이후 수정된 기존 스킬을 과거 백업으로 통째로 덮어쓰지 않는다. 비공개 원문 DB 삭제는 설치 해제와 별개다.

현재의 전체 목표 미완 항목과 구체적인 완료 기준은 [연구 보고서](../../docs/cha_philosophy/RESEARCH_REPORT.md#7-다음-완료-조건)를 기준으로 한다.
