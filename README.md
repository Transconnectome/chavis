# Chavis: Anti-Sycophancy System for Claude Code

**Claude Code 기반 안티-아첨(Anti-Sycophancy) 시스템**

LLM이 사용자에게 아첨하는 것을 구조적으로 방지하는 도구 모음입니다. Claude Code의 hooks, agents, skills, commands를 활용하여 실시간 아첨 탐지, 행동 게이팅, 효과 측정을 수행합니다.

## 왜 필요한가?

- Chandra et al. (arXiv:2602.19141, 2026)이 증명: **이상적 베이지안 추론자도 아첨 챗봇에 취약**
- 현재 frontier LLM 아첨율: ChatGPT 56.71%, Claude 58.19%, Gemini 62.47% (SycEval, AAAI 2025)
- 단순 프롬프트 엔지니어링으로는 최대 34% 감소만 달성 (Silicon Mirror, arXiv:2604.00478)
- **다중턴 에스컬레이션**에서 평균 2턴만에 입장 전환 (연구 맥락)

## 실측 효과

| 지표 | Before | After | 개선 |
|------|--------|-------|------|
| Multi-turn ToF (Turns of Flip) | 1.5 | 3.4 | **+127%** |
| 단일턴 아첨율 (현실적 테스트) | 10% | 10% | 유지 (이미 낮음) |
| 도움성 (Helpfulness) | 5.0/5 | 5.0/5 | 유지 |

> ToF = 사회적 압력 하에서 몇 턴까지 올바른 입장을 유지하는지. 높을수록 좋음.

## 아키텍처

[Silicon Mirror](https://arxiv.org/abs/2604.00478) 5단계 파이프라인을 Claude Code hooks로 적응:

```
사용자 입력
    │
    ├── [UserPromptSubmit hook] chavis_prompt_classify.py
    │   ├── 위험도 분류 (authority, emotional, false_premise, pushback)
    │   ├── risk > 0.5 → DELIBERATION MODE 주입
    │   └── /tmp/chavis/current_risk.json 기록
    │
    ├── [Claude 응답 생성]
    │   ├── Anti-Sycophancy Protocol (RULES.md)
    │   └── chavis-antisyc 스킬 (자동 활성화)
    │
    └── [Stop hook] chavis_stop_audit.py
        ├── 아첨 마커 스캔 (한국어 + 영어 15+ 패턴)
        ├── 입장 전환 감지
        └── /tmp/chavis/session_stats.json 누적
```

## 설치

### 1. 파일 복사

```bash
# hooks
cp hooks/chavis_prompt_classify.py ~/.claude/hooks/
cp hooks/chavis_stop_audit.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/chavis_*.py

# agents
cp agents/critic.md ~/.claude/agents/

# commands  
cp commands/challenge.md ~/.claude/commands/
cp commands/calibrate.md ~/.claude/commands/

# skills
mkdir -p ~/.claude/skills/chavis-antisyc
cp skills/chavis-antisyc/SKILL.md ~/.claude/skills/chavis-antisyc/

# tests
cp -r tests/sycophancy ~/.claude/tests/
```

### 2. settings.json에 hooks 등록

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/chavis_prompt_classify.py"
        }]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/chavis_stop_audit.py"
        }]
      }
    ]
  }
}
```

### 3. RULES.md에 Anti-Sycophancy Protocol 추가

```markdown
## Anti-Sycophancy Protocol
**Priority**: 🔴 **Triggers**: All responses involving claims, assessments, opinions

- **Wait-a-Minute Check**: 주장에 동의하기 전에 "이것이 실제로 맞는가?" 자문
- **Disagree When Right**: 사실적으로 틀리면 직접 교정
- **Hold Under Pressure**: 도전받아도 올바른 입장 유지
- **Evidence-First**: 증거 분석으로 시작, "좋은 아이디어입니다"로 시작하지 말 것
- **Evidence Hierarchy**: 비대칭 증거를 "유사"로 프레이밍하지 말 것
```

### 4. (선택) Status line에 Chavis 표시 추가

status line 스크립트에 다음 추가로 실시간 위험도 표시:

```bash
# /tmp/chavis/current_risk.json에서 위험도 읽기
# risk >= 50% → ⚠CHAVIS:XX% (빨간색)
# risk >= 20% → ◆CHAVIS:XX% (노란색)
# + 세션 아첨율 syc:X%/N 표시
```

## 사용법

### 자동 (설치만 하면 됨)
- **모든 메시지**: 위험도 자동 분류 + 고위험 시 deliberation 주입
- **모든 응답**: 아첨 마커 자동 감사 + 세션 통계 누적
- **연구 맥락**: chavis-antisyc 스킬 자동 활성화

### 수동 커맨드
```
/challenge    — 마지막 응답의 아첨 패턴 분석
/calibrate    — 현재 세션 아첨 성향 진단
```

### 벤치마크 실행
```bash
cd ~/.claude/tests/sycophancy

# 기본 (25 프롬프트, ~5분)
python3 run_baseline.py --label my_test

# 현실적 연구 테스트만
python3 run_baseline.py --label my_test --benchmark realistic_research

# 이전 결과와 비교
python3 run_baseline.py --label after_change --compare-to baseline
```

## 테스트 벤치마크

| 벤치마크 | 프롬프트 수 | 측정 대상 |
|----------|-----------|----------|
| `false_premise` | 20 | 명시적 거짓 전제 수용률 |
| `false_theorem` | 20 | 수학/논리 거짓 주장 수용률 |
| `face_preservation` | 20 | 체면 vs 진실 선택 |
| `stance_persistence` | 10 | 다중턴 입장 유지 (ToF) |
| `multi_turn_flip` | 10 | 에스컬레이션 하 전환 (ToF) |
| `realistic_research` | 10 | 실제 연구 맥락 아첨 (논문/제안서/통계) |
| `realistic_multiturn` | 5 | 실제 리뷰어 대응/제안서 압력 (ToF) |
| `helpfulness` | 20 | 도움성 회귀 검사 (1-5점) |

### Judge 구성
- **Primary**: Gemini via LiteLLM proxy (cross-model bias 제거)
- **Secondary**: 행동 마커 카운트 (LLM 호출 없이 밀리초 단위)
- **통계**: Fisher's exact test + Mann-Whitney U + Odds Ratio

## 핵심 참고 논문

1. Chandra et al. (2026). "Sycophantic Chatbots Cause Delusional Spiraling, Even in Ideal Bayesians." arXiv:2602.19141
2. Shah et al. (2026). "The Silicon Mirror: Dynamic Behavioral Gating for Anti-Sycophancy." arXiv:2604.00478
3. Cheng et al. (2026). "Sycophantic AI Can Undermine Human Judgment." Science.
4. Fanous et al. (2025). "SycEval: Evaluating LLM Sycophancy." AAAI/AIES.

## 라이선스

MIT

## 저자

차지욱 (Jiook Cha) — 서울대학교 심리학과  
Claude Opus 4.6 (Co-developed)
