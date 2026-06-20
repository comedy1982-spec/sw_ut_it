# SWTS Studio

C/C++ 유닛의 구조적 커버리지(**Statement / Branch / MC-DC**)를 측정하고,
MC-DC를 충족하는 테스트 벡터를 **자동 역산**하는 도구. ISO 26262 ASIL C/D의
구조적 커버리지 검증을 겨냥하며, 측정 엔진으로 LLVM(`clang -fcoverage-mcdc`)을
사용합니다. React 없이 Flask + HTML/CSS/JS로 구현.

```
┌──────────┬──────────────────────────┐
│ 소스트리   │  소스뷰어 (분기 T/F · 색칠)  │
│ 유닛 선택  ├──────────────────────────┤
│ +Result%  │  테스트케이스 (+커버리지 칩)  │
└──────────┴──────────────────────────┘
```

> 📖 **구조·동작 원리 상세는 [ARCHITECTURE.md](ARCHITECTURE.md)** 참고.

## 구성

| 파일 | 역할 |
|------|------|
| `app.py` | Flask 서버. 라우트 + 측정 엔진 폴백 + 정적 추정 + HTML 리포트 |
| `swts_scan.py` | 스캔 엔진 (git diff + libclang) → 컴포넌트/유닛 트리 |
| `swts_generate.py` | GCC+gcov 실측 엔진 (수기 명세 유닛) |
| `swts_clang_cov.py` | Clang MC-DC 엔진 + 휴리스틱 벡터 생성 |
| `swts_mcdc_atg.py` | SMT(Z3) 미니-ATG: 진리표→독립쌍→입력 역산→강건성 |
| `templates/index.html` | 단일 파일 SPA |

## 실행

```bash
pip install -r requirements.txt     # flask, libclang, gcovr, z3-solver(선택)
python app.py                       # → http://localhost:5000
# SWTS_ROOT 로 대상 소스 루트 지정 (기본: example/ecu_powertrain)
```

**실측(LIVE/MC-DC) 모드**는 PATH에 `clang`(18+) · `llvm-profdata` · `llvm-cov`가
있어야 동작합니다. Windows는 링크용 **VS Build Tools(C++)** 가 추가로 필요(없으면
정적 추정 폴백). 콘솔에 `[info] Clang MC/DC 엔진: clang`이 뜨면 LIVE.

## 측정 엔진 (다단계 폴백)

| 모드 | 조건 | 비고 |
|------|------|------|
| real (GCC) | 수기 명세 + gcc | gcov 실측 |
| **clang-mcdc** | clang+llvm 도구 | **실행 기반 MC-DC 실측(권장)** |
| clang-static | 도구 없음 | 정적 추정 — *실측 아님*(전 유닛 100%는 추정값) |
| mock | 엔진 미적재 | 예제 고정 데이터 |

## 동작 흐름

1. 좌측 트리에서 컴포넌트/유닛 확인 (펼치기·접기·전체선택·해제)
2. 유닛 체크 → 상단 **▶ TC 생성** → 자동 벡터 생성 + clang 빌드/실행 측정
3. 소스뷰어 색칠 + 유닛별 **Result(STMT/BR/MC-DC %)** + 하단 테스트케이스
4. TC 체크박스 토글 → 커버리지 즉시 갱신
5. **📊 리포트** → VectorCAST 풍 HTML 출력

## API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/` | SPA |
| GET | `/api/scan` | 컴포넌트/유닛 트리 |
| GET | `/api/source` | 컴포넌트 소스 |
| POST | `/api/generate` | 유닛 → 커버리지 + TC |
| POST | `/api/cov_select` | 선택 TC만 재측정 |
| POST | `/api/report` | HTML 리포트 |
