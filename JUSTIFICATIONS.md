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
| 분류 | **likely code defect** (dead code) — *도구 한계 아님* |
| 조치 | **소스 리뷰 필요** — 결함 수정 권장. 미수정 시 잠정 제외 |

> ⚠️ **정정**: 초기 분석은 이를 "외부에서 단언되는 방어 코드(통합 테스트 대상)"로
> 적었으나 이는 잘못이었습니다. `static`은 외부 접근을 막으므로 "외부에서 셋"이
> 성립하지 않습니다. 정확한 진단은 아래와 같이 **코드 결함**입니다.

**근거(rationale) — 왜 코드 결함인가**
- `g_estop` 은 `static int g_estop = 0;` (motor_ctrl.c:8, **내부 링키지**)이며,
  파일 전체에서 **읽기(L17) 1회 외에 어떤 대입(setter)도 없습니다**(초기화 `=0` 뿐).
- `static`(파일 내부)이므로 **외부 TU도 접근/대입 불가** — 주석의 "외부에서 셋"과
  **모순**됩니다(외부에서 셋하려면 `static`이면 안 됨).
- 따라서 `g_estop` 은 **누구도 1로 만들 수 없어 영원히 0** → `g_estop ||` 항은
  **죽은 코드(dead code)** 이고, **비상정지(e-stop)가 실제로 절대 발동하지 않습니다.**
  안전 필수 코드에서 이는 **결함**입니다. MC/DC `C1✗(missed)`는 이 도달 불가
  조건을 정확히 신고한 것 — MC/DC의 본래 목적입니다.

**권고 (resolution)**

| 의도 | 조치 | 결과 |
|------|------|------|
| e-stop이 동작해야 함(정상) | `static` 제거 + 헤더에 `extern int g_estop;` 또는 setter 함수 추가 | L17 도달 가능 → 도구가 입력으로 설정 → **MC-DC 100% 정당 달성** |
| 의도적 데모/플레이스홀더 | 죽은 코드임을 명시 후 제외 | 잠정 제외(아래 문구) |

**잠정 제외 문구 (수정 전, 코드 리뷰로 결정될 때까지)**
> motor_ctrl.c:17, 조건 `g_estop`: **DEAD CODE (probable defect).** `g_estop`
> (motor_ctrl.c:8)은 `static`이고 TU 내 setter가 없어 불변으로 0 → TRUE 항은
> 도달 불가. 비상정지가 발동되지 않으므로 코드 리뷰에서 (a) `static` 제거+setter
> 추가로 수정하거나 (b) 의도적 비활성 코드로 확정해야 한다. 수정 전까지 MC-DC
> 대상에서 잠정 제외하되, **정당화가 아니라 미해결 결함으로 추적한다.**

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
