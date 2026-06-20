# SWTS Studio — 구조 및 동작 원리

> SWTS(SW Unit/Integration Test Studio)는 C/C++ 유닛의 구조적 커버리지
> (**Statement / Branch / MC-DC**)를 측정하고, MC-DC를 충족하는 테스트 벡터를
> **자동 역산**하는 도구입니다. ISO 26262 ASIL C/D의 구조적 커버리지 검증을
> 겨냥하며, 측정 엔진으로 LLVM(`clang -fcoverage-mcdc`)을 사용합니다.

이 문서는 **실제 구현된 동작**을 정확한 용어로 서술합니다. (마케팅적 과장 없이,
코드에 있는 그대로 — 예: 진리표는 BDD가 아닌 완전열거, "CFG"가 아닌 라인 기반
구조 분석.)

---

## 1. 한눈에 보기

```
┌─────────────────────────────────────────────────────────┐
│  브라우저 SPA (templates/index.html)                       │
│  소스트리 · 소스뷰어(커버리지 색칠) · 테스트케이스 · 리포트     │
└───────────────┬─────────────────────────────────────────┘
                │ HTTP (JSON)
┌───────────────▼─────────────────────────────────────────┐
│  Flask 서버 (app.py) — 라우트 + 측정 엔진 오케스트레이션      │
│   /api/scan  /api/source  /api/generate  /api/cov_select   │
│   /api/report  /api/browse                                 │
└──┬───────────────┬───────────────┬───────────────┬───────┘
   │               │               │               │
   ▼               ▼               ▼               ▼
swts_scan.py   swts_generate   swts_clang_cov   swts_mcdc_atg
(스캔: git+    (GCC+gcov 실측) (clang MC/DC 엔진 (Z3 미니-ATG:
 libclang)                     + 휴리스틱 생성)   입력 역산)
```

측정은 **다단계 폴백**으로 동작합니다. 도구 가용성에 따라 자동으로 최선의
엔진을 선택합니다(§4).

---

## 2. 모듈 구성

| 파일 | 역할 |
|------|------|
| `app.py` | Flask 서버. 모든 라우트, 측정 엔진 폴백 오케스트레이션(`api_generate`), 툴체인 없을 때의 정적 커버리지 추정(`_static_coverage`), VectorCAST 풍 HTML 리포트 생성(`_build_report_html`), 예제용 목업 데이터. |
| `swts_scan.py` | **스캔 엔진**. `git diff`로 변경 라인 집합을 뽑고, libclang AST로 각 `.c`의 함수 정의·라인범위·호출그래프·support 레벨을 추출해 **컴포넌트/유닛 트리**를 구성. |
| `swts_generate.py` | **GCC 실측 엔진**. 수기 명세(`TestSpec`)가 있는 유닛에 한해 C 하니스를 생성·`gcc -fprofile-arcs -ftest-coverage`로 빌드·서브프로세스 실행·`gcov`로 라인 히트 측정. (하니스 방식인 이유: 서브프로세스 종료 시 `.gcda`가 확실히 flush됨.) |
| `swts_clang_cov.py` | **Clang MC-DC 엔진** + **휴리스틱 벡터 생성기**. `clang -fcoverage-mcdc`로 실행 기반 MC-DC를 측정하고, 분기 조건을 라인 단위로 분석해 도달성 높은 테스트 벡터를 생성(`_smart_vectors`). 매크로/enum 값을 clang으로 해석(`probe_consts`). |
| `swts_mcdc_atg.py` | **SMT 기반 미니-ATG**. C 조건식을 파싱→진리표→MC-DC 독립쌍→**Z3로 입력값 역산**→강건성(한계값) 주입. `swts_clang_cov`가 휴리스틱 벡터에 **가산**으로 합침. |
| `templates/index.html` | 단일 파일 SPA(HTML/CSS/JS). 소스트리, 커버리지 색칠 소스뷰어, 테스트케이스 패널, 리포트 버튼. |
| `example/ecu_powertrain/` | 데모용 C 소스(BLDC 모터·전류센싱·진단·엔코더·열관리 ECU 예제). |
| `run.bat` / `run.sh` | 런처(의존성 확인 → 포트 정리 → 서버 기동). |

---

## 3. 데이터 흐름 (파이프라인)

```
[1] 스캔            /api/scan
    git diff + libclang → 컴포넌트/유닛 트리 (변경유닛 표시)
        │
[2] 유닛 선택        (프론트엔드 체크박스)
        │
[3] TC 생성/측정     /api/generate  (유닛별)
    ├─ 테스트 벡터 자동 생성 (휴리스틱 + SMT ATG)
    ├─ C 하니스 생성 → clang -fcoverage-mcdc 빌드
    ├─ 실행 → run.profraw → llvm-profdata merge
    └─ llvm-cov export(JSON) → {STMT%, BR%, MC-DC%, mcdc_records, source}
        │
[4] 표시            소스뷰어 색칠 + 라인↔TC 매핑 + 유닛별 Result(%)
        │
[5] TC 토글          /api/cov_select  (clang-mcdc: 선택 TC만 재측정)
        │
[6] 리포트           /api/report → VectorCAST 풍 자체완결 HTML
```

---

## 4. 측정 엔진 — 다단계 폴백

`app.py::api_generate`는 유닛마다 아래 순서로 **가능한 최선의 엔진**을 자동 선택합니다.

| 우선순위 | 모드 | 조건 | 특성 |
|---------|------|------|------|
| 1 | **real (GCC)** | 해당 유닛에 수기 `TestSpec`이 있고 `gcc` 설치됨 | gcc+gcov 실측, 라인 hit 수 제공 |
| 2 | **clang-mcdc** | `clang`+`llvm-profdata`+`llvm-cov` 설치됨 (Clang 18+) | **실행 기반 MC-DC 실측**(권장). TC 토글 시 재측정 |
| 3 | **clang-static** | 위 도구 없음 → libclang AST만 사용 | **정적 추정**(실행 안 함). 색칠은 "TC가 노리는 라인" 표시용 — *실측 아님* |
| 4 | **mock** | 엔진 미적재 + 예제 유닛 | 고정 예제 데이터 |

> ⚠️ **clang-static의 100%는 측정값이 아니라 추정값**입니다. 실행을 안 하므로
> "이 TC가 이 라인을 노린다"는 정적 매핑만 보여줍니다. 신뢰 가능한 수치는
> **clang-mcdc(LIVE)** 에서 나옵니다. 상단 배지로 모드를 확인하세요(`MC/DC`=실측).

### 4.1 clang-mcdc 측정 절차 (`swts_clang_cov.generate`)

```
1. libclang AST → 함수 시그니처 · 호출 stub · 전역변수 수집
2. 테스트 벡터 생성   = 휴리스틱(_smart_vectors) ∪ SMT-ATG(swts_mcdc_atg)
3. C 하니스(main + stub) 생성
4. clang -O0 -fprofile-instr-generate -fcoverage-mapping -fcoverage-mcdc
5. 실행(LLVM_PROFILE_FILE=run.profraw)  +  TC별 개별 profraw 수집
6. llvm-profdata merge → llvm-cov export(JSON)
7. JSON 파싱 → STMT/BR/MC-DC % + 조건별 mcdc_records
```

---

## 5. 핵심 작동 원리 (Core Principles)

테스트 벡터 자동 추출은 **무작위 퍼징이 아니라 소스의 구조적 분석 + 수학적
역산**으로 이루어집니다. 아래는 `swts_mcdc_atg.py`의 실제 구현입니다.

### 5.1 구조화 — AST 및 구조적 분기 분석
- **AST**: 두 겹으로 사용. ①libclang이 함수 시그니처·호출·전역변수를 추출,
  ②자체 C식 토크나이저/재귀하강 파서가 조건식을 AST로 변환
  (`m->flags & MASK` → `('bin','&', ('field', ('id','m'),'flags'), ('id','MASK'))`).
- **분기/경로 식별**: 결정(decision)과 진입 경로를 **소스 라인 + 들여쓰기 추적**
  으로 추출(`_collect_decisions`). `if/else if/while/do-while/switch-case`를 인식하고
  enclosing 조건을 경로 제약으로 누적.
- **else-if 형제 부정**: `if(cmd==0){} else if(C){...}` 같은 체인에서, else-if 본문에
  도달하려면 앞선 형제(`cmd==0`)가 **거짓**이어야 함을 경로 제약으로 주입한다
  (`chains` 추적). 이게 없으면 Z3가 `cmd`를 0으로 채워 else-if가 런타임 도달 불가가
  된다 — switch 상태머신의 깊은 분기 커버에 핵심.
  > 정밀 표현: 형식적 CFG(제어흐름그래프) 자료구조를 만드는 것이 아니라,
  > **라인 기반 구조 분석**으로 결정·경로를 근사합니다. (결과는 평탄한 함수에서
  > 동등하지만, 깊은 중첩/비정형 제어흐름에서는 한계가 있습니다.)

### 5.2 진리표 기반 독립영향 분석 (MC-DC)
- 복합 조건문(예: `if (A && (B || C))`)을 부울 AST로 추출하고, **원자 조건**으로 분해.
- 조건 N개에 대해 **진리표를 완전열거**(`2^N`행, N≤6 캡)하고, 각 조건 Cᵢ에 대해
  **다른 조건이 고정된 채 Cᵢ만 결과를 바꾸는 독립쌍(unique-cause)** 을 도출(`_mcdc_rows`).
  > 정밀 표현: BDD(Binary Decision Diagram) 자료구조를 쓰지 않고, **진리표
  > 완전열거**로 독립쌍을 구합니다(소규모 조건에서 정확·충분).

### 5.3 SMT Solver 기반 제약 해결 (Z3)
- 도출된 참/거짓 논리식을 **실제 변수 제약식으로 치환**(예: `A=True` → `speed > 100`).
- 입력변수(파라미터 스칼라 / 구조체 필드 / 전역 / 스텁 반환)를 **32-bit BitVec**
  (C `int` 부호의미)으로 모델링하고, **Z3 SMT 엔진**으로 제약을 만족하는
  **구체 입력값(테스트 벡터)을 역산**(`_Z3Ctx`, `generate`).
- 매크로/enum 상수는 `clang` 컴파일·실행으로 정확히 해석(`probe_consts`).

### 5.4 경계값 및 강건성(Robustness) 데이터 주입
- SMT 해를 고른 뒤, **MC-DC를 만족하는 한도 내에서** 각 입력변수를 우선순위로 핀:
  **`[INT_MAX, INT_MIN]`(오버/언더플로 유발) → 비교상수 경계 `(C-1, C, C+1)` → `0,1,-1`**
  (`_robust_cands`, `_bias_robust`).
- **기본 해 + 강건성 해를 둘 다 방출**(가산)하여 분기 커버리지 손실 없이
  예외 입력을 함께 검증. 예: `speed > 100` 참→`INT_MAX`, 거짓→`INT_MIN`,
  `== 상수`는 정확값 강제.

### 5.5 휴리스틱 생성기 (보완)
SMT가 못 푸는 형태(루프 파생, 스텁 반환 의존 등)를 위해 `_smart_vectors`가
조건을 라인 분석해 트리거 값/극값(±99999)/관계비교 경계(±2배)를 **추가**합니다.
SMT-ATG와 합쳐(가산) 커버리지를 끌어올립니다.

---

## 6. 커버리지 의미와 색칠

| 지표 | 의미 |
|------|------|
| **Statement** | 실행된 구문 라인 비율 |
| **Branch (Decision)** | 각 분기의 참/거짓 양방향 도달 비율 |
| **MC-DC** | 각 조건이 결과에 **독립적으로 영향**을 줌을 보인 비율(ASIL D 권고) |

소스뷰어 라인 색: 초록=covered, 빨강=not covered, T/F=분기 양방향 마커.
프론트엔드는 라인별 `tcs`(라인↔TC 매핑)로 색을 칠하며, **TC 체크박스 토글 시**
색이 갱신됩니다(clang-mcdc는 백엔드 재측정, 그 외는 프론트 즉시 반영).

**STMT 측정 정밀도(`parse_cov`)** — llvm-cov export JSON의 segment를 라인 카운트로
변환할 때 llvm-cov의 실제 의미를 따른다: 라인 카운트 = `max(그 라인 region-entry
카운트, 활성 wrapped 리전 카운트)`이며, **gap-region(폴스루 0-count) 세그먼트는
자체값으로 라인을 올리지 않는다**. 또 **공백 라인은 실행문이 아니므로** 소스
텍스트로 판정해 통계에서 제외한다. 이로써 `switch case`의 `break`/`switch` 라인이나
케이스 사이 빈 줄이 0-count로 잘못 집계되는 아티팩트가 제거되어 STMT%가 정직하다.

---

## 7. API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/` | SPA |
| GET | `/api/scan?root=` | 컴포넌트/유닛 트리 |
| GET | `/api/browse?path=` | 폴더 브라우저 |
| GET | `/api/source?component=&root=` | 컴포넌트 전체 소스 |
| POST | `/api/generate` | 유닛 → 커버리지 + TC (다단계 폴백) |
| POST | `/api/cov_select` | 선택 TC만 재측정(clang-mcdc) |
| POST | `/api/report` | VectorCAST 풍 HTML 리포트 |

---

## 8. 설치 및 실행

```bash
pip install -r requirements.txt        # flask, libclang, gcovr, z3-solver(선택)
python app.py                          # → http://localhost:5000
# SWTS_ROOT 환경변수로 대상 소스 루트 지정(기본: example/ecu_powertrain)
```

**실측(LIVE) 모드 조건** — 아래가 PATH에 있어야 `clang-mcdc`로 동작:
- `clang` (**18+**, `-fcoverage-mcdc` 지원), `llvm-profdata`, `llvm-cov`
- Windows: clang의 링크에 **MSVC 빌드도구**(VS Build Tools, C++)가 추가로 필요
  (없으면 빌드 실패 → 정적 폴백). 또는 WSL/Linux에서 구동.
- 콘솔에 `[info] Clang MC/DC 엔진: clang`이 떠야 LIVE. 미설치 시 `정적 분석 모드`.

---

## 9. 측정 결과 (예제 ecu_powertrain)

`clang -fcoverage-mcdc` 실측 기준 **12개 유닛 중 11개가
STMT/BR/MC-DC = 100%/100%/100%** 입니다. 남은 1건(`motor_set_duty`)은
도구 한계가 아니라 **소스 코드 결함**입니다.

주요 개선(이 저장소 커밋 기준):
- **2.4 강건성 주입** — 한계값(INT_MAX/MIN) 입력이 배열·결합 경로로 전파되며
  `current_protect`(MC-DC 0%→100%), `motor_fault_mask`(67%→100%)까지 충족.
- **A1 else-if 형제 부정** — `motor_state_step`(MC-DC 75%→100%, 깊은 switch 분기 도달).
- **A2 / A2b parse_cov 보정** — `thermal_step`(STMT 93%→100%), gap-region·공백 라인
  아티팩트 제거.

### `motor_set_duty` L17 — 코드 결함 (도구 한계 아님)
`if (g_estop || (m->flags & ...))`의 `g_estop`은 `static int`(파일 내부 링키지)이고
**TU 안에 setter가 전혀 없습니다**(주석은 "외부에서 셋"이라 하지만 `static`이라
외부 접근 불가). 즉 `g_estop`은 **영원히 0** → 비상정지 분기가 **죽은 코드**이고
MC-DC는 이를 정확히 신고한 것입니다. 자세한 내용·권고는 [JUSTIFICATIONS.md](JUSTIFICATIONS.md)(J-001) 참고.

## 10. 한계 (정직한 경계)

SMT-ATG가 입력을 못 만드는 형태 → 해당 결정은 미달로 남을 수 있음. 100%가
목표가 아니라 **정당한 미달은 ISO 26262 근거로 제외**하는 것이 정석.

| 형태 | 상태 |
|------|------|
| 스칼라/전역(스칼라)/포인터 1단계 필드/스텁 반환 | ✅ 지원 |
| 중첩 struct(`a.b`)·전역 struct·유니온 단일멤버·상수인덱스 배열 | ✅ 지원 |
| else-if/switch 깊은 분기 도달성 | ✅ 지원(A1 형제 부정) |
| 배열 파생 로컬(`avg`)·클램프 변형 로컬(`t`)·누적 마스크 결합 | ⚠️ 부분 — 강건성 극값이 우연히 충족시킬 때만(정밀 모델링 아님) |
| **다단계 포인터**(`m->next->id`)·유니온 별칭(cross-member) | ❌ 미지원(필드 타입 분석 선행 필요) |
| `static`(파일 내부) 전역 | ❌ 별도 TU 하니스가 접근 불가 — 단, 보통 *코드 결함* 신호 |

> 미달 유닛 진단법: 화면 MC-DC 패널에서 미충족 조건을 보고 소스를 확인 →
> ①손으로 입력을 만들 수 있으면 *도구 한계*(개선 가능), ②어떤 입력으로도
> 불가하면 *도달불가/방어코드*(정당화 제외) 또는 *코드 문제*(리뷰 대상).

---

## 11. 리포트 (VectorCAST 풍)

`📊 리포트` 버튼 → `/api/report`가 현재 화면(선택 TC 기준)을 **자체완결 HTML**로
생성합니다(새 탭에서 미리보기·저장·인쇄). 구성: Report Configuration / Overall
Results / Units Summary / 유닛별 커버리지 바·테스트케이스 표·주석 소스·MC-DC 조건표.
라인 색상은 화면과 동일 규칙(선택 TC 반영)으로 산출됩니다.
```
