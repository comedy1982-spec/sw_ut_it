# VectorCAST 연동 플랜 (`.tst` 라운드트립 · 사전 검증 게이트)

> 상태: **설계 확정 · 구현 대기**. 본 도구를 VectorCAST **정식 검증의 사전 검증
> (pre-check) 게이트**로 사용하기 위한 `.tst` 라운드트립 연동 계획.

## 결정 사항 (확정)

| 항목 | 결정 | 영향 |
|------|------|------|
| `.tst` 샘플 | **없음** | 공개 VectorCAST `TEST.*` 문법 기준으로 설계, **관대한 파서**(미지원 directive는 스킵·로그). 실제 샘플 확보 시 방언 보정 |
| 타깃/실행 | **TriCore/AURIX (TASKING)** | VectorCAST는 시뮬/타깃 실행, 본 도구는 **호스트 clang-mcdc** → **근사 사전검증**(동일 보장 아님). 타깃 정확 측정은 향후 TASKING ISS 어댑터 |
| 착수 순서 | **Import 먼저 (P2)** | 기존 `.tst` 재현·측정 → 게이트 흐름의 시작점부터 구현 |

## 1. 목표 흐름

```
[코드 변경]
  ├─ 현재 VectorCAST .tst Import ──► clang-mcdc 재현(replay) ──► 커버리지 측정
  ├─ 미충족 ──► ATG로 부족분 자동 생성 ──► .tst Export(병합) ──► 재측정(충족까지)
  └─ 충족 ──► HTML 리포트 + 커밋용 추적문서(COVERAGE_STATUS.md) ──► 커밋
              └► (정식 검증 시 VectorCAST에 Import해 실행)
```

## 2. ⚠️ 동등성 한계 (반드시 명시)

본 도구는 **VectorCAST 결과의 대체가 아니라 근사 사전검증**입니다.

1. **엔진 차이** — VectorCAST 자체 계측 vs `clang -fcoverage-mcdc`. MC/DC 규칙
   (현재 Unique-Cause만; VectorCAST는 Masking 설정 가능)·statement 카운팅에서
   차이 가능.
2. **실행 환경 차이(핵심)** — `.tst` **입력값은 이식**되지만 실행은 타깃(TASKING
   ISS) vs 호스트(clang/x86). 이식성 있는 제어흐름·MC-DC 로직은 대체로 일치하나,
   타깃 의존(`sizeof`/엔디안/비트필드 레이아웃/내장함수)은 다를 수 있음.

→ 용도: **변경 시 빠른 커버리지 공백 탐지 + 보충 TC 자동 생성**. 최종 증빙은
VectorCAST에서 생성.

## 3. 컴포넌트

| 모듈 | 역할 | 단계 |
|------|------|------|
| `swts_tst_import.py` | `.tst` 파서 → 테스트케이스(unit/subprogram/values/expected/stub) | P2 |
| `swts_tst_map.py` | VectorCAST 값표기 ↔ 하니스 assignment 양방향 매핑 | P2/P1 |
| `swts_clang_cov`(재사용) | 주어진 벡터로 하니스 빌드·실행·측정 + expected 비교(pass/fail) | P2 |
| `swts_mcdc_atg`(재사용) | 공백난 부분만 보충 TC 생성 | P3 |
| `swts_tst_export.py` | 우리 케이스 → `.tst` directive 생성 | P1/P3 |
| `swts_gate.py` | 변경 감지 → Import→측정→보충→Export→리포트/문서 오케스트레이션 | P4 |

## 4. 단계별 플랜

### P2 — `.tst` Import + Replay (우선)

**파싱 대상(서브셋)** — 공개 `TEST.*` 문법:
```
TEST.UNIT:<unit>
TEST.SUBPROGRAM:<func>
TEST.NEW / TEST.NAME:<name> / TEST.END
TEST.VALUE:<unit>.<func>.<param-path>:<value>      // 파라미터(struct/array/pointer 경로)
TEST.VALUE:<unit>.<global>:<value>                 // 전역
TEST.VALUE:<unit>.<stub_func>.return:<value>       // 스텁 반환
TEST.EXPECTED:<unit>.<func>.return:<value>         // 기대 반환
TEST.STUB / TEST.SLOT(compound) 등 → 인식·스킵·로그
```

**매핑(`swts_tst_map`)** — VectorCAST 값표기 → 하니스 setup:
| `.tst` | 하니스 |
|--------|--------|
| `m[0].enabled:1` | `_s0.enabled = 1;`, arg `&_s0` (포인터 param 역참조) |
| `m[0].window[2]:50` | `_s0.window[2] = 50;` |
| `target_duty:100` | arg `100` (스칼라 param) |
| `<global>:1` | `g = 1;` (외부 링키지 전역) |
| `hall_read.return:0` | `__sret_hall_read = 0;` (스텁) |
| `EXPECTED ...return:-2` | 실행 반환과 비교 → pass/fail |

**Replay**: 매핑된 벡터를 기존 하니스(`_emit_harness`/`build`/`run`/`parse_cov`)에
**그대로 주입**(ATG 생성 대신) → 커버리지 측정 + (보너스) expected 대비 pass/fail.

**산출물**: "기존 VectorCAST 테스트의 (호스트 기준) 커버리지" 리포트. 미지원
directive·매핑 실패 항목은 명시적으로 보고(은폐 금지).

### P1 — `.tst` Export
ATG/Import 케이스 → `.tst` directive 출력. 내부 벡터가 이미 구조화돼 매핑 명확.
`/api/export_tst` + UI 버튼.

### P3 — Gap-fill 루프
Import 측정 → 미충족 결정에 ATG 보충 생성 → `.tst` 병합 → 재측정. 보강 `.tst`에
자동 TC는 `[AUTO]` 태그.

### P4 — 변경 게이트 + 추적 문서
변경 감지(git diff) → P2→P3 자동 → 충족 시 리포트 + `COVERAGE_STATUS.md` 갱신·커밋.

### P5 — 동등성 정렬(지속)
VectorCAST MC/DC 모드·statement 규칙 맞춤. 타깃 정확 측정이 필요하면
**TASKING ISS 어댑터**(별도 과제) — 호스트 clang 대신 TriCore 시뮬로 측정.

## 5. 커밋용 추적 문서 `COVERAGE_STATUS.md` (설계)

변경마다 갱신:
- 변경 식별(커밋 해시/날짜/대상 유닛)
- 유닛별 **before→after** STMT/BR/MC-DC
- TC 출처 태그: `[VC]`(기존 .tst) / `[AUTO]`(자동 보충)
- 정당화/결함 항목([JUSTIFICATIONS.md](JUSTIFICATIONS.md) 연동)
- HTML 리포트 링크 + `.tst` 경로
- 측정 환경 주석(호스트 clang 근사 — §2)

## 6. 리스크 / 미해결

| 리스크 | 대응 |
|--------|------|
| `.tst` 방언 차이(샘플 없음) | 서브셋 + 관대한 파서, 미지원은 로그. 샘플 확보 시 보정 |
| 호스트≠타깃 커버리지 | "근사 사전검증" 명시. 필요 시 TASKING ISS 어댑터(P5) |
| MC/DC 규칙 차이 | VectorCAST 모드 확인 후 정렬(P5) |
| struct/포인터 타입 복원 | libclang 타입 정보 활용; 미지원 형태는 보고 후 스킵 |

---
*다음 작업: P2(`swts_tst_import.py` + `swts_tst_map.py` + replay 측정) 착수.*
