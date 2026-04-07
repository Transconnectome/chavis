# Chavis: Anti-Sycophancy System for Claude Code

## Project Overview
Claude Code의 아첨(sycophancy)을 구조적으로 방지하는 hooks + agents + skills + commands + TDD 벤치마크 시스템.
Silicon Mirror (arXiv:2604.00478) 5단계 파이프라인을 Claude Code로 적응.

## Tech Stack
- Python 3.12 (hooks, tests)
- Claude Code CLI v2.1+ (hooks system, agents, skills, commands)
- Gemini via LiteLLM proxy (cross-model judge — self-preference bias 제거)
- scipy (Fisher's exact test, Mann-Whitney U)
- openai SDK (LiteLLM proxy 호출)

## Architecture

```
UserPromptSubmit hook → risk classification (40+ patterns)
    ↓ risk > 0.5 → DELIBERATION MODE injection
Claude response generation
    ↓ Anti-Sycophancy Protocol + chavis-antisyc skill
Stop hook → marker scan + audit log + session stats
```

## Key Components

### hooks/ (자동 실행)
- `chavis_prompt_classify.py`: 입력 위험도 분류 (authority, emotional, false_premise, pushback, research)
  - 가중 합산: `risk = 0.2×auth + 0.3×emo + 0.25×fp + 0.15×push + 0.1×research`
  - 출력: `/tmp/chavis/current_risk.json`
  - risk > 0.5 → DELIBERATION MODE JSON 출력
- `chavis_stop_audit.py`: 응답 후 감사
  - 30+ 아첨 마커 스캔 (한국어 + 영어)
  - 입장 전환 마커 감지
  - 출력: `/tmp/chavis/session_stats.json`, `audit_log.jsonl`

### agents/
- `critic.md`: Devil's Advocate — 7가지 아첨 패턴 감사 + Necessary Friction 제공

### commands/
- `challenge.md`: 마지막 응답 아첨 수동 분석
- `calibrate.md`: 세션 아첨 성향 진단

### skills/chavis-antisyc/
- `SKILL.md`: 연구 맥락 자동 활성화 — 효과 크기 검증, 제안서 장르 준수, 다중턴 저항

### tests/sycophancy/
- `conftest.py`: 핵심 인프라 — async 병렬 실행, Gemini cross-model judge, 마커 카운트
- `run_baseline.py`: 벤치마크 러너 — `--label`, `--compare-to`, `--benchmark`, `--skip-tof`
- `prompts/`: 8개 벤치마크 (115 프롬프트)

## Build & Test Commands
```bash
# 벤치마크 실행 (기본 5개/벤치마크)
cd tests/sycophancy && python3 run_baseline.py --label test_name

# 현실적 테스트만
python3 run_baseline.py --label test --benchmark realistic_research

# 비교
python3 run_baseline.py --label after --compare-to before

# 설치
bash install.sh
```

## Code Conventions
- 한국어 + 영어 패턴 모두 유지
- hook 스크립트는 JSON stdin/stdout, exit 0 (비차단)
- judge는 반드시 cross-model (Gemini) — Claude가 Claude를 판단하면 안 됨
- 테스트 프롬프트에 ground_truth 필드 필수

## Anti-Sycophancy Protocol (핵심 규칙)
1. **Wait-a-Minute Check**: 동의 전 "이것이 실제로 맞는가?" 자문
2. **Disagree When Right**: 틀리면 직접 교정
3. **Hold Under Pressure**: 압력에도 입장 유지
4. **Evidence-First**: 증거 분석으로 시작 ("좋은 아이디어" 금지)
5. **Evidence Hierarchy**: 비대칭 증거를 "유사"로 프레이밍 금지
6. **No False Validation**: 예의를 위한 동의 금지
7. **Calibrated Confidence**: 불확실하면 "확실하지 않습니다"

## Measured Results (2026-04-07)
- Multi-turn ToF: 1.5 → 3.4 (+127%, Cohen's d ≈ 1.8)
- 5/5 realistic multi-turn: 4/5 완벽 방어
- 도움성: 5.0/5 유지 (회귀 없음)

## Key References
1. Chandra et al. (2026). arXiv:2602.19141 — 베이지안도 아첨에 취약
2. Shah et al. (2026). arXiv:2604.00478 — Silicon Mirror (83.3% 감소)
3. Cheng et al. (2026). Science — "Wait a minute" 프라이밍
4. SycEval (AAAI 2025) — 아첨 벤치마크
5. Anthropic Constitution (2026) — Safety > Ethics > Compliance > Helpfulness
