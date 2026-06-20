# SWTS Studio — Flask 버전 (순수 Python + 서버렌더 HTML)

손그림 레이아웃 그대로의 4분할 UI. React 없이 Flask + HTML/CSS/JS 로만 구현.

```
┌─────────────────────────────────────┐
│  설정 버튼들 (로고 · LIVE/MOCK · 생성)  │
├──────────┬──────────────────────────┤
│ 소스트리   │  소스뷰어                  │
│ 유닛 선택  │  (F/T 분기 · hit수 · 색칠)  │
│          ├──────────────────────────┤
│          │  테스트케이스 (+커버리지 칩)  │
└──────────┴──────────────────────────┘
```

## 구성
```
flask_app/
├── app.py               Flask 서버 (라우트 + 엔진 연동 + 목업 폴백)
├── templates/index.html 4분할 UI (HTML/CSS/JS 한 파일)
├── swts_scan.py         Phase 0 엔진 (git+Clang) — 재사용
└── swts_generate.py     Phase 1 엔진 (GCC+gcov) — 재사용
```

## 실행
```bash
pip install flask libclang gcovr
export SWTS_ROOT=/path/to/your/c-project   # 기본: ../example/ecu_powertrain
python app.py                               # → http://localhost:5000
```
> gcc·git 이 있으면 LIVE(실측), 없으면 MOCK 모드로 동작.

## 핵심 — 소스뷰어 커버리지 거터
각 소스 라인 왼쪽에 3가지 정보를 표시 (손그림의 "F,T,및 hit수"):
- **색칠 바**: full(초록) / partial(노랑) / none(빨강)
- **F/T**: if·while·for 분기 라인의 참/거짓 도달 여부
- **hit수**: 해당 라인 실행 횟수 (gcov 실측, 예: `3×`)

## 동작 흐름
1. 좌측 트리에서 `.c` 파일 클릭 → 컴포넌트의 유닛 목록
2. 유닛 선택 → 상단 "테스트 케이스 생성"
3. 실제 GCC 빌드 + gcov 실행 → 소스뷰어에 커버리지, 하단에 TC + 칩

## API
| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/` | 4분할 UI |
| GET | `/api/scan` | 트리 + 유닛 목록 |
| POST | `/api/generate` | 유닛 → 커버리지 + TC |

## 현황
- `calculate_speed` → 실제 엔진 (100% 실측)
- `check_fault` → 목업 (Z3 단계에서 실제화 예정)
