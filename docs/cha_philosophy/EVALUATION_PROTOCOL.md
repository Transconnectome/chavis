# 철학 전사 평가 실행 계약

상태: 2026-09-13 구현 및 실제 사례 세 건의 제한적 대조. 이 문서는 실제 교수 철학의 충실도나 모델 성능을 실측했다고 주장하지 않는다. 근거와 설계의 상세 내용은 [독립 연구 보고서](research_evaluation.md)에 있다.

## 현재 구현이 하는 일

`tools.cha_philosophy.evaluation.build_task_instructions(task)`는 writing, evaluation, review, mentoring, research별 정적 지침을 돌려준다. 원문·원칙 카드 내용을 명령으로 보간하지 않는다. 작업 지침에는 객관적 사실과 현재 요청의 우선성, 공식 rubric 보존, 원본과 수치 보존, 원문에 연결된 리뷰, 압력과 새로운 증거의 구분이 포함된다.

`audit_output(output, bundle)`는 API 호출 없이 구조와 참조 무결성을 검사한다. 적용한 원칙의 활성 상태와 task 범위, 직접 작성자로 검증된 원문, source hash, authored span 안의 인용, 산출물에 존재하는 적용 위치, 등록된 주장과 출처를 확인한다. 감사 직전에 Store에서 현재 bundle을 다시 생성해야 한다. 오래된 bundle 안의 hash가 서로 같다는 사실은 현재 원문의 이용 가능성을 보장하지 않는다.

반환 `status`는 `invalid` 또는 `structural_valid`다. `semantic_review_required`는 항상 true이고, `entailment_verified`, `objective_correctness_verified`, `professor_fidelity_measured`는 항상 false다. 허위 해석도 실제 원문 ID를 달면 구조상 유효할 수 있다. 이 모듈의 성공을 의미 판정 통과나 교수님 승인으로 바꾸면 안 된다.

실제 원고·rubric 입력을 쓰는 `prepare --target app`은 전체 입력과 원래 개행을 보존하고 비공개 receipt로 최초 snapshot을 고정한다. `plan-task`와 `read-task-unit`은 모든 입력의 원래 source ID·문자 위치를 유지한다. `audit-task --coverage`는 모든 구간의 메모·정확한 인용·구간 사이 통합 기록·전체 rubric 목록을 검사한다. 원고를 짧게 바꾸고 새 해시를 만들거나 rubric을 통째로 빼면 원래 receipt와 불일치하여 실패한다. 중복 JSON 키와 명시적 null coverage도 실패한다.

`input_coverage_accounted=true`는 완전한 선언 형식만 뜻한다. 의미 없는 메모가 구조적으로 통과하는 경우도 검사에 포함했다. `input_reading_verified=false`, `whole_task_completion_verified=false`를 유지하며 실제 읽기·이해·과제 완수와 구분한다. 긴 합성 원고의 prepare→plan→모든 unit 읽기→원문 재구성→audit 왕복 검증을 실제 논문 심사 품질 실험으로 계산하지 않는다. coverage가 없는 `audit-task`는 입력 구간의 처리 범위를 검증하지 않는다.

## 출력 형식

```json
{
  "content": "완전한 산출물",
  "applications": [
    {"principle_id": "실제 bundle의 ID", "applied_to": "산출물의 실제 부분 문자열", "rationale": "적용 이유"}
  ],
  "claims": [
    {"text": "산출물의 실제 주장 문자열", "source_ids": ["실제 source ID"], "claim_type": "professor"}
  ],
  "uncertainties": []
}
```

`claim_type`은 professor, objective, inference, ordinary이며 생략하면 professor다. 교수님에게 귀속하는 모든 주장과 bundle에 의존하는 주장을 등록한다. 일반 인사·연결 문장·통상적인 산출물 문장은 claims 밖에 둘 수 있다. 명시적 교수 귀속을 ordinary로 표시해 인용 요구를 피하려는 사례는 차단한다. 다만 모든 묵시적·우회적 귀속을 regex로 발견할 수 없으므로 전체 산출물의 의미 검토는 남는다.

교수님의 철학에 관한 주장은 active/direct 원문과 검증된 author_id를 요구한다. 객관적 과학 주장도 적절한 근거가 필요하지만 교수님의 발언만으로 과학적 진실이 증명되지는 않는다. 현재 bundle에 외부 과학 근거가 없으면 실제 사실 검증을 별도로 수행하고 검증 범위를 uncertainties에 기록한다. 이 감사기는 외부 출처의 타당성을 자동 판정하지 않는다.

필요하면 `decision_change`를 추가한다.

```json
{
  "changed": true,
  "basis": "new_evidence",
  "reason": "새 관찰이 기존 해석을 바꾸는 이유",
  "new_evidence_source_ids": ["현재 bundle의 근거 ID"]
}
```

허용 basis는 unchanged, new_evidence, correction, pressure다. pressure에 의한 변경은 invalid이며, new_evidence는 출처가 필요하다. correction은 구체적 수정 이유를 요구한다. 이 필드도 모델의 선언이다. 실제 새 증거가 결론을 바꿀 만큼 강한지, 압력을 새 증거로 잘못 표현했는지는 독립 의미 판정이 필요하다.

## 실행과 해석

```bash
python3 -m pytest tools/cha_philosophy/tests/test_evaluation.py -q
```

합성 시험은 잘못된 작성자, 답장 인용에 삽입된 지침, 철회·접근 불가·revision 변경, 비활성 원칙, 실제 산출물에 없는 인용 위치, 모름, 무인용 일반 문장, 교수 선호와 객관적 정확성의 분리, 선언된 압력과 새 증거의 차이를 점검한다. 의미를 검증하지 못하는 경우에 `pass`라고 주장하지 않는 것도 시험한다. 이 결과는 실제 교수님의 자료를 모델이 이해하거나 적용했다는 실증 결과가 아니다.

## 모델 및 사람 평가로 남은 일

1. 실제 직접 발언의 작성자·맥락·독립 사건을 표본 검수한다. Gmail의 인용된 답장, Teams reply, Drive 복제·revision은 동일 계열로 묶는다.
2. 시간과 대화/문서 계열 단위로 개발·검증 자료를 분리한다. 평가 시점 이후의 발언·정정·원칙 카드는 숨긴다.
3. 같은 모델과 사실 자료에서 일반 지침(B0), 스타일(B1), 원문 검색(B2), 원칙+task adapter(B3)를 비교한다.
4. 교수님은 소수 경계 사례에서 A/B/tie/neither와 선택 이유·반증 조건을 확인한다. 자기보고 철학 요약을 정답으로 재사용하지 않는다.
5. 독립 judge에 전체 산출물·원문·평가 기준을 제공하고 A/B 순서를 반전한다. 호출 오류와 불안정 판정을 성공에서 분리한다. 모델 계열을 바꿨다는 사실만으로 편향 제거를 주장하지 않는다.
6. 말투, 이름·명성·관계, 압력만 바뀌는 불변성 시험과 실제 새 근거·철회에 반응하는 방향성 시험을 함께 실시한다. 올바른 입장 유지와 올바른 수정은 별도 지표다.
7. task별 사실 정확성, 인용 지지, 중요한 정보의 누락, 허위 비판, 교수님 선택과의 일치, 수정 시간, 오류/미판정을 함께 보고한다. 단일 철학 점수를 만들지 않는다.

사람이 검증한 판정과 실제 미래 사례를 포함한 평가 전에는 운영 준비 완료, 교수님과 동등한 평가, 또는 전체 말뭉치 파악 완료라고 보고하지 않는다.

## 실행한 첫 실제 사례 대조의 범위

원칙 근거와 겹치지 않는 실제 Gmail 세 계열을 Store에서 예약했다. 그 뒤에 작성된 교수 답변을 숨기고 이전 대화만 가명 처리해 제공했다. 새 대화의 두 생성 에이전트는 같은 모델 조건에서 기본 지침과 현재 원칙 bundle 제공 조건으로 각각 3개 답안을 만들었다. 입력·출력·참조·원칙 snapshot을 고정했으며, 참조를 본 뒤 답안을 다시 만들지 않았다.

사례와 채점 항목은 과거 답변을 읽고 선별한 것이므로 완전히 사전 등록된 벤치마크가 아니다. 입력 밖의 맥락에 의존하는 과거 세부 사항은 채점하지 않았다. 교수 계정에서 발송된 과거 답변이 과학적·평가적 정답이나 단독 저작임을 가정하지 않았다.

Gemini 3.1 Pro Preview가 조건 이름을 숨긴 A/B 답안을 순서별로 판정했다. 첫 방식은 6개 중 4개가 구조 검증에 실패했으며 오류 결과를 보존했다. 두 번째 방식은 원문의 고정 구간 ID를 모델이 선택하고 코드가 실제 인용을 붙였다. 6개 모두 구조 검증에 통과했지만 인용의 의미를 자동 입증한 것은 아니다. 5개 동률·1개 기본 답안 선호·철학 적용 답안 선호 0개였다. 순서를 바꾸면 평가 사례의 기본 답안 선호가 동률로 바뀌었다.

현재 확인한 것은 작은 업무 답장 과제에서 관찰된 차이뿐이다. 원고 전체 리뷰, 실제 채점 정확성, 시간 절감, 교수의 선택 일치, 개인화의 일반적 우위는 미측정이다. 같은 세 사례를 개발에 맞춘 뒤 미관측 성능처럼 보고하지 않는다. 다음 검증은 별도 미래 사례와 교수의 비교 판단이 필요하다.
