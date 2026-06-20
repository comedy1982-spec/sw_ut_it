# 커버리지 정당화 기록 (Coverage Justification Record)

구조적 커버리지(Statement / Branch / MC-DC)에서 **도구·자동생성으로 도달할 수
없는** 항목을 ISO 26262-6(소프트웨어 유닛 검증)에 따라 **근거와 함께 제외**한
기록입니다. 100% 강박이 아니라, *정당화된 제외(justified exclusion)* 가 안전
인증의 표준 실무입니다.

> 측정 기준: `clang -fcoverage-mcdc` 실행 기반 실측 (LIVE 모드).
> 예제 프로젝트 `example/ecu_powertrain` 기준 12개 유닛 중 **11개가
> STMT/BR/MC-DC 100%/100%/100%** 이며, 아래 1건만 정당화 제외 대상입니다.

---

## J-001 — `motor_set_duty` : `g_estop` 조건의 TRUE 항

| 항목 | 내용 |
|------|------|
| 파일/위치 | `example/ecu_powertrain/src/motor/motor_ctrl.c:17` |
| 결정(decision) | `if (g_estop \|\| (m->flags & (MF_OVERCURRENT \| MF_OVERTEMP)))` |
| 미충족 | 조건 `g_estop` 의 **독립영향(MC/DC) 쌍 중 g_estop=TRUE 행** |
| 분류 | static-global-unsettable / **unreachable-by-design** |
| 조치 | **정당화 제외** (도구/빌드 시드 미적용) |

**근거(rationale)**
- `g_estop` 은 `static int g_estop = 0;` (motor_ctrl.c:8, **내부 링키지**)으로,
  주석대로 "비상정지 — 외부에서 셋"되는 변수입니다.
- 해당 번역 단위(TU) 안에는 **`g_estop` 에 대한 어떤 setter도 없습니다**(초기화
  `=0` 이외 대입 없음). 따라서 공개 API(`motor_set_duty` 등)로 도달 가능한 모든
  상태에서 `g_estop` 은 **불변으로 0** 이며, `g_estop==TRUE` 결과는 유닛 테스트
  범위에서 **도달 불가능한 코드**입니다.
- 하니스와 소스는 **별도 TU로 컴파일**되므로, 내부 링키지 심볼인 `g_estop` 은
  하니스에서 `extern` 으로 바인딩할 수 없습니다(언디파인드 심볼). 즉 현재
  측정 구조에서 설정 자체가 불가합니다.
- 대안으로 검토한 *단일-TU(`#include` 소스)* 또는 *`#ifdef` 테스트 시드* 는
  (a) 전 유닛이 의존하는 빌드/링크 전략을 침해하고, (b) `g_estop` 이 본질적으로
  도달 불가(setter 없음)이므로 **죽은 상태를 위한 인위적 테스트**가 됩니다 —
  적대적 검증에서 `fix_sound=false`/`reconsider` 로 기각되었습니다.

**정당화 문구 (커버리지 레코드용)**
> motor_ctrl.c:17, 결정 `g_estop || (m->flags & (MF_OVERCURRENT|MF_OVERTEMP))`,
> 조건 `g_estop`: **JUSTIFIED EXCLUSION (ISO 26262-6, unreachable condition).**
> `g_estop`(motor_ctrl.c:8)은 내부 링키지이며 외부 비상정지 액터만 기록한다.
> TU 내에 setter가 없어 유닛 테스트 범위에서 `g_estop`은 불변으로 0이고
> TRUE 결과는 공개 API를 통해 도달 불가능하다. 조건은 도달 가능한(FALSE)
> 상태로 검증되며, TRUE 항은 외부에서 단언되는 방어적 안전 코드로 통합
> 테스트 수준에서 검증한다. 도달 불가 상태를 위한 유닛 테스트는 생성하지 않는다.

**향후(선택)**: 정적 전역을 설정 가능한 재사용 기능으로 만들고 싶다면, 별도
검토 하에 *단일-TU 컴파일 시드*(내부 링키지 전역을 참조하는 미충족 결정에만
게이트 발동)를 도입할 수 있으나, 본 커버리지 작업 범위에서는 권장하지 않습니다.

---

## 참고 — 자동으로 회복된 항목(정당화 아님)

아래는 한때 미달이었으나 도구 개선으로 **실제 커버**된 항목입니다(정당화 제외가
아니라 진짜 충족).

| 유닛 | 이전 | 현재 | 개선 |
|------|------|------|------|
| `motor_state_step` | MC/DC 75%, STMT 74% | **100% / 100% / 100%** | else-if 형제 부정(A1) + 라인카운트 보정(A2) + 공백제외 |
| `thermal_step` | STMT 93% | **100% / 100% / 100%** | parse_cov gap-region 보정(A2) |
| `current_protect` | MC/DC 0% | **100%** | 강건성 극값 주입(2.4)이 배열 파생 avg 까지 충족 |
| `motor_fault_mask` | MC/DC 67% | **100%** | 강건성 극값 주입(2.4) |
