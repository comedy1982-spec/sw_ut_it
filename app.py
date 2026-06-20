"""
SWTS Studio — Flask 단일 파일 버전 (순수 Python + 서버렌더 HTML)
================================================================
손그림 레이아웃 그대로:
  [상단] 설정 버튼들
  [좌] 소스트리/유닛 선택       [우상] 소스뷰어 (F/T 분기 + hit수 + 커버리지 색칠)
                              [우하] 테스트케이스

기존 엔진 재사용:
  swts_scan.py     → /scan (git diff + Clang)
  swts_generate.py → /generate (GCC 하니스 + gcov 실측)

실행:
  pip install flask
  python app.py            # → http://localhost:5000
  (소스 루트는 env SWTS_ROOT, 기본 ../example/ecu_powertrain)
"""
from __future__ import annotations
import os
import sys
import re
import json

from flask import Flask, render_template, request, jsonify

# 엔진 모듈 경로 추가 (flask_app/ 의 부모 = backend 묶음 가정)
HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, ".."), os.path.join(HERE, "..", "backend")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

SWTS_ROOT = os.environ.get("SWTS_ROOT",
                           os.path.join(HERE, "example", "ecu_powertrain"))
SWTS_ROOT = os.path.abspath(SWTS_ROOT)

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

# 마지막 스캔 결과 캐시 {abs_root: scan_result}
_scan_cache: dict = {}
_scan_root_latest: str = SWTS_ROOT

# ---- 엔진 로드 (없으면 목업 모드) ----
try:
    import swts_scan
    import swts_generate
    ENGINES = True
except Exception as e:  # pragma: no cover
    print(f"[warn] 엔진 로드 실패 -> 목업 모드: {e}")
    ENGINES = False

try:
    import swts_clang_cov
    CLANG_COV = swts_clang_cov.tools_available()
    if CLANG_COV:
        print(f"[info] Clang MC/DC 엔진: {swts_clang_cov.find_clang()}")
    else:
        print("[info] Clang MC/DC 도구 미설치 (clang/llvm-profdata/llvm-cov) -> 정적 분석 모드")
except Exception as e:
    print(f"[warn] swts_clang_cov 로드 실패: {e}")
    CLANG_COV = False


# ============================================================
# 실제 빌드 명세 (어떤 유닛을 진짜 GCC+gcov 로 돌릴지)
# ============================================================
def _real_spec(unit_ref):
    if not ENGINES:
        return None
    specs = {
        "src/speed/speed_calc.c::calculate_speed": dict(
            func="calculate_speed", ret_type="int",
            arg_decls=["int sensor_id", "int* out_buf"],
            stubs="int g_mock_raw=0;\nint read_sensor(int id){return g_mock_raw;}\n",
            capture_globals=["g_last"],
            cases=[
                {"id": "TC-001", "desc": "정상 속도 (raw=50 → 180)",
                 "inputs": {"sensor_id": 5, "read_sensor()": 50},
                 "setup": ["g_mock_raw=50;", "int buf[4]={0};"], "call": "5, buf",
                 "capture": {"out_buf[0]": "buf[0]"}, "expected": {"return": 180, "g_last": 180}},
                {"id": "TC-002", "desc": "센서 음수 → 즉시 -1",
                 "inputs": {"sensor_id": 5, "read_sensor()": -1},
                 "setup": ["g_mock_raw=-1;", "int buf[4]={0};"], "call": "5, buf",
                 "expected": {"return": -1}},
                {"id": "TC-003", "desc": "최대속도 clamp (raw=100 → 360 → 200)",
                 "inputs": {"sensor_id": 9, "read_sensor()": 100},
                 "setup": ["g_mock_raw=100;", "int buf[4]={0};"], "call": "9, buf",
                 "capture": {"out_buf[0]": "buf[0]"}, "expected": {"return": 200}},
            ],
        ),
    }
    d = specs.get(unit_ref)
    if not d:
        return None
    rel = unit_ref.split("::")[0]
    return swts_generate.TestSpec(
        unit_ref=unit_ref, src_file=os.path.join(SWTS_ROOT, rel), **d)


# ============================================================
# 라우트
# ============================================================
@app.route("/")
def index():
    return render_template("index.html", root=SWTS_ROOT, engines=ENGINES)


@app.route("/api/scan")
def api_scan():
    """소스트리 + 컴포넌트/유닛 목록."""
    global _scan_root_latest
    root = request.args.get("root", "").strip()
    root = os.path.abspath(root) if root else SWTS_ROOT
    _scan_root_latest = root
    if ENGINES:
        try:
            data = swts_scan.scan_project(root, None, ["-DUNIT_TEST"])
            if any(c["units"] for c in data["components"].values()):
                data["root"] = root
                data["engines_used"] = True
                _scan_cache[root] = data
                return jsonify(data)
        except Exception as e:
            print(f"[scan] fallback: {e}")
    mock = _mock_scan()
    mock["root"] = root
    return jsonify(mock)


@app.route("/api/browse")
def api_browse():
    """디렉터리 존재 여부 확인 및 하위 폴더 목록."""
    path = request.args.get("path", "").strip()
    if not path:
        path = os.path.expanduser("~")
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return jsonify({"ok": False, "error": "경로가 존재하지 않습니다.", "path": path})
    try:
        dirs = sorted([
            d for d in os.listdir(path)
            if os.path.isdir(os.path.join(path, d)) and not d.startswith('.')
        ])
    except PermissionError:
        dirs = []
    parent = os.path.dirname(path)
    return jsonify({"ok": True, "path": path, "parent": parent, "dirs": dirs})


@app.route("/api/source")
def api_source():
    comp = request.args.get("component", "")
    root = request.args.get("root", _scan_root_latest)
    root = os.path.abspath(root)
    if ENGINES:
        file_rel = _get_comp_file(comp, root)
        if file_rel:
            abs_path = os.path.join(root, file_rel)
            if os.path.exists(abs_path):
                return jsonify(_real_component_source(comp, abs_path))
    return jsonify(_mock_component_source(comp))


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """유닛 → TC + 커버리지 (소스뷰어 F/T/hit 데이터 포함)."""
    body = request.json or {}
    unit_ref = body.get("unit_ref", "")
    root = body.get("root", _scan_root_latest)
    spec = _real_spec(unit_ref)
    if spec is not None:
        try:
            logs = []
            res = swts_generate.generate(spec, log=lambda lvl, t: logs.append({"level": lvl, "text": t}))
            res["logs"] = logs
            res["mode"] = "real"
            res = _annotate_branches(res, spec)
            return jsonify(res)
        except Exception as e:
            print(f"[generate] GCC 실패 → Clang 정적 분석 시도: {e}")
    # Clang 정적 분석 (GCC 없을 때)
    clang_res = _clang_generate(unit_ref, root)
    if clang_res:
        return jsonify(clang_res)
    # 목업 (EngineControl 예제 등)
    m = _mock_generate(unit_ref)
    if m:
        return jsonify(m)
    return jsonify({"ok": False, "error": f"unknown unit_ref: {unit_ref}"})


@app.route("/api/cov_select", methods=["POST"])
def api_cov_select():
    """선택된 TC 들만으로 커버리지 재측정 (clang-mcdc 모드 체크박스 토글)."""
    body = request.json or {}
    unit_ref = body.get("unit_ref", "")
    tc_ids = body.get("tc_ids", [])
    if not (ENGINES and CLANG_COV):
        return jsonify({"ok": False, "error": "clang 미사용"})
    try:
        res = swts_clang_cov.recompute(unit_ref, tc_ids)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    if not res:
        return jsonify({"ok": False, "error": "캐시 없음 (재생성 필요)"})
    return jsonify(res)


# ============================================================
# 실제 소스 파일 읽기 + Clang 정적 TC 생성
# ============================================================
_BRANCH_KW = ("if ", "} else", "else if", "while ", "for ", "switch ", "case ")


def _get_comp_file(comp: str, root: str) -> str | None:
    """스캔 캐시에서 컴포넌트 → 파일 경로 조회."""
    cache = _scan_cache.get(os.path.abspath(root), {})
    comp_data = cache.get("components", {}).get(comp)
    if comp_data:
        return comp_data.get("file")
    return None


def _real_component_source(comp: str, abs_path: str) -> dict:
    """실제 파일을 읽어 소스 오브젝트 반환."""
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return {"ok": False, "error": str(e), "source": []}
    source = []
    for i, text in enumerate(lines, 1):
        t = text.rstrip("\n")
        source.append({
            "n": i, "text": t, "cov": "raw", "hits": 0, "tcs": [],
            "is_branch": t.strip().startswith(_BRANCH_KW),
        })
    return {"ok": True, "component": comp, "source": source}


def _clang_generate(unit_ref: str, root: str) -> dict | None:
    """Clang MC/DC 실행 측정 또는 정적 분석 폴백."""
    if not ENGINES:
        return None
    root = os.path.abspath(root)
    cache = _scan_cache.get(root, {})
    if not cache:
        try:
            cache = swts_scan.scan_project(root, None, ["-DUNIT_TEST"])
            _scan_cache[root] = cache
        except Exception:
            return None

    # unit_ref 에서 unit_info 찾기
    unit_info = None
    file_rel = None
    for comp_data in cache.get("components", {}).values():
        for unit in comp_data.get("units", []):
            if unit["unit_ref"] == unit_ref:
                unit_info = unit
                file_rel = comp_data["file"]
                break
        if unit_info:
            break
    if not unit_info or not file_rel:
        return None

    abs_path = os.path.join(root, file_rel)
    if not os.path.exists(abs_path):
        return None

    start_ln, end_ln = [int(x) for x in unit_info["lines"].split("-")]
    func_name = unit_info["name"]

    # ── 1. Clang -fcoverage-mcdc 실행 기반 측정 (도구 있을 때) ──
    if CLANG_COV:
        include_dirs = []
        inc = os.path.join(root, "include")
        if os.path.isdir(inc):
            include_dirs.append(inc)
        logs: list[dict] = []
        result = swts_clang_cov.generate(
            unit_ref=unit_ref,
            abs_src=abs_path,
            func_name=func_name,
            start_ln=start_ln,
            end_ln=end_ln,
            flags=["-DUNIT_TEST"],
            include_dirs=include_dirs,
            log=lambda lvl, t: logs.append({"level": lvl, "text": t}),
        )
        if result:
            result["logs"] = logs + result.get("logs", [])
            return result
        # 빌드 실패 시 정적 분석으로 폴백
        print(f"[warn] Clang MC/DC 빌드 실패 -> 정적 분석 폴백")

    # ── 2. Clang 정적 분석 (도구 없거나 빌드 실패) ──
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return None

    source = []
    for i, text in enumerate(all_lines, 1):
        if i < start_ln or i > end_ln:
            continue
        t = text.rstrip("\n")
        source.append({
            "n": i, "text": t, "cov": "raw", "hits": 0, "tcs": [],
            "is_branch": t.strip().startswith(_BRANCH_KW),
        })

    cases = _gen_tc_stubs(func_name, source, unit_info["branches"],
                          abs_path=abs_path, flags=["-DUNIT_TEST"])
    return {
        "ok": True, "unit_ref": unit_ref, "mode": "clang-static",
        "coverage": {"statement": 0, "branch": 0, "mcdc": 0},
        "source": source,
        "cases": cases,
        "notes": [
            f"Clang AST 정적 분석: branches={unit_info['branches']}, "
            f"max_depth={unit_info['max_depth']}, support={unit_info['support']}",
            "clang + llvm-cov 설치 시 -fcoverage-mcdc 실행 기반 측정으로 자동 전환",
        ],
        "logs": [{"level": "info",
                  "text": f"[clang-static] {func_name}: {unit_info['branches']}개 분기"}],
    }


def _extract_cond(text: str) -> str:
    """if/while/for/else if 라인에서 조건식만 추출."""
    text = text.strip()
    for prefix in ("} else if (", "else if (", "if (", "while (", "for ("):
        if text.startswith(prefix):
            inner = text[len(prefix):]
            rp = inner.rfind(")")
            return inner[:rp] if rp >= 0 else inner
    return text


_ARROW = ""  # '->' 보호용 임시 토큰 (비교연산자 '>' 와 혼동 방지)


def _parse_condition(cond: str) -> dict:
    """C 조건식을 {좌변: 값힌트} 로 분해 (분기를 트리거하는 입력 추정).
       예) 's == NULL || s->id < 0' -> {'s':'NULL', 's->id':'< 0'}"""
    hints: dict = {}
    # '->' 의 '>' 가 비교연산자로 오인되지 않도록 negative lookbehind 사용
    op_re = re.compile(r"^(.+?)\s*(==|!=|<=|>=|<|(?<!-)>)\s*(.+)$")
    for part in re.split(r"\|\||&&", cond):
        part = part.strip().strip("()").strip()
        if not part:
            continue
        m = op_re.match(part)
        if m:
            lhs, op, rhs = m.group(1).strip(), m.group(2), m.group(3).strip()
            if op == "==":
                hints[lhs] = rhs
            elif op == "!=":
                hints[lhs] = f"!= {rhs}"
            else:
                hints[lhs] = f"{op} {rhs}"
        else:  # 단순 truthiness: 'flag' 또는 '!flag'
            if part.startswith("!"):
                hints[part[1:].strip()] = "0 (false)"
            else:
                hints[part] = "!= 0 (true)"
    return hints


def _branch_return(source: list, bi: int) -> str | None:
    """분기 라인(source[bi]) 블록 안에서 첫 return 값을 추출."""
    depth = 0
    seen_brace = False
    for k in range(bi, len(source)):
        t = source[k]["text"]
        depth += t.count("{") - t.count("}")
        if "{" in t:
            seen_brace = True
        st = t.strip()
        if k > bi and st.startswith("return"):
            val = st[len("return"):].strip().rstrip(";").strip()
            return val or "(void)"
        if seen_brace and k > bi and depth <= 0:
            break
    return None


def _nominal_inputs(params: list) -> dict:
    """파라미터 타입별 정상 경로 기본 입력값."""
    out: dict = {}
    for p in params:
        t, n = p.get("type", ""), p.get("name", "")
        if "*" in t:
            out[n] = "buf" if any(b in t for b in ("char", "int", "short")) else "&valid"
        elif any(x in t for x in ("int", "long", "short", "char")):
            out[n] = "0"
        else:  # enum 등
            out[n] = "0"
    return out


def _gen_tc_stubs(func_name: str, source: list, branch_count: int,
                  abs_path: str | None = None, flags: list | None = None) -> list:
    """분기 + 함수 시그니처 분석으로 입력/기대값이 채워진 TC 생성."""
    # 1) libclang 으로 파라미터 추출 (런타임 도구 없이 AST 만으로 가능)
    params: list = []
    if abs_path:
        try:
            import swts_clang_cov
            detail = swts_clang_cov._get_func_detail(abs_path, func_name, flags or [])
            if detail:
                params = detail.get("params", [])
        except Exception:
            params = []
    nominal = _nominal_inputs(params)

    # 2) 함수의 마지막(fall-through) return → 정상 경로 기대값
    returns = [ln["text"].strip() for ln in source
               if ln["text"].strip().startswith("return")]
    final_ret = None
    if returns:
        final_ret = returns[-1][len("return"):].strip().rstrip(";").strip() or "(void)"

    # 3) TC-001: 정상 경로
    cases = [{
        "id": "TC-001", "verdict": "manual",
        "desc": f"{func_name} 정상 경로 (nominal)",
        "inputs": nominal or {"(args)": "none"},
        "expected": {"return": final_ret} if final_ret else {"return": "정상 동작"},
    }]

    # 4) 분기별 TC: 조건을 트리거하는 입력 + 해당 분기의 return
    branch_indices = [i for i, ln in enumerate(source) if ln["is_branch"]]
    for n, bi in enumerate(branch_indices[:9], 2):
        raw = source[bi]["text"].strip()
        ret = _branch_return(source, bi)
        inputs = dict(nominal)

        if raw.startswith("case "):                       # switch-case
            val  = raw[5:].rstrip(":").strip()
            desc = f"case {val}"
            inputs["(case)"] = val
        elif raw.startswith("default"):                   # switch-default
            desc = "default (기본 케이스)"
        elif "else" in raw and "if" not in raw:           # 순수 else
            desc = "else (기본 경로)"
        else:                                             # if / else if / while / for
            cond = _extract_cond(raw)
            inputs.update(_parse_condition(cond))         # 분기 트리거 입력으로 덮어쓰기
            desc = cond[:48]

        if not inputs:
            inputs = {"(args)": "none"}
        cases.append({
            "id": f"TC-{n:03d}", "verdict": "manual",
            "desc": f"L{source[bi]['n']}: {desc}",
            "inputs": inputs,
            "expected": {"return": ret} if ret else {"return": final_ret or "?"},
        })
    return cases


# ============================================================
# F/T 분기 라벨 + hit 주석 (소스뷰어 핵심 요구)
# ============================================================
def _annotate_branches(res, spec):
    """gcov 의 분기(branch) 정보를 소스 라인에 F/T 형태로 부착.
    gcov 원시 분기 데이터가 없으면, if 문 라인에 분기 마커만 표시."""
    # 간단화: 'if'/'else if' 포함 라인에 분기 플래그
    for ln in res.get("source", []):
        t = ln["text"].strip()
        ln["is_branch"] = t.startswith("if") or t.startswith("} else if") or t.startswith("else if") or t.startswith("while") or t.startswith("for")
    return res


# ============================================================
# 목업 (엔진 없거나 미등록 유닛)
# ============================================================
_TC_COVERED_LINES = {
    "engine_init": {
        "TC-001": [12, 13, 14],
        "TC-002": [12, 13, 15, 16, 17, 18, 19, 20, 22, 23, 24, 25, 26, 27, 28],
        "TC-003": [12, 13, 15, 16, 17, 18, 19, 20, 21, 22, 24, 25, 26, 27, 28],
    },
    "engine_start": {
        "TC-001": [30, 31, 32],
        "TC-002": [30, 31, 33, 34, 35],
        "TC-003": [30, 31, 33, 34, 36, 37, 38, 41, 42, 43, 44, 47, 48, 49, 50],
        "TC-004": [30, 31, 33, 34, 36, 37, 38, 39, 40],
    },
    "engine_stop": {
        "TC-001": [57, 58, 59],
        "TC-002": [57, 58, 60, 61, 62, 63, 64, 65, 66, 67, 71, 72, 73, 74],
        "TC-003": [57, 58, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70],
    },
    "set_throttle": {
        "TC-001": [76, 77, 78],
        "TC-002": [76, 77, 78],
        "TC-003": [76, 77, 79, 80, 82, 83, 84, 85, 86, 87, 89, 90, 91, 92, 93],
        "TC-004": [76, 77, 79, 80, 81],
    },
    "read_rpm": {
        "TC-001": [97, 98, 99, 100],
        "TC-002": [97, 98, 99, 101, 102, 103, 105, 106, 107],
        "TC-003": [97, 98, 99, 101, 102, 103, 104, 105, 106, 107],
    },
    "check_overrev": {
        "TC-001": [114, 115, 116, 117],
        "TC-002": [114, 115, 116, 118, 119, 120, 121],
        "TC-003": [114, 115, 116, 118, 119, 122, 123, 124, 125, 126, 127, 128],
        "TC-004": [114, 115, 116, 118, 119, 122, 123, 124, 125, 128, 129, 130, 131, 132],
    },
    "apply_fuel_cut": {
        "TC-001": [140, 141, 142],
        "TC-002": [140, 141, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154],
        "TC-003": [140, 141, 143, 144, 145, 146, 147, 148, 149, 150, 151, 154, 155, 156, 157],
    },
    "calc_torque": {
        "TC-001": [160, 161, 162],
        "TC-002": [160, 161, 163, 164, 165],
        "TC-003": [160, 161, 163, 164, 166, 167, 168, 169, 171, 172, 173, 175, 176, 177, 178],
        "TC-004": [160, 161, 163, 164, 166, 167, 168, 169, 170, 171, 172, 173, 174, 175, 176, 177, 178],
    },
    "update_status_led": {
        "TC-001": [183, 184, 185, 186, 187, 197, 198],
        "TC-002": [183, 184, 188, 189, 190, 197, 198],
        "TC-003": [183, 184, 191, 192, 193, 197, 198],
    },
    "engine_self_test": {
        "TC-001": [202, 203, 204, 205, 206, 207, 208, 230, 231, 232, 233],
        "TC-002": [202, 203, 204, 205, 209, 210, 211, 215, 216, 217, 221, 222, 223, 227, 228, 229, 230, 231, 232, 233],
        "TC-003": [202, 203, 204, 205, 209, 210, 211, 212, 213, 214, 230, 231, 232, 233],
        "TC-004": [202, 203, 204, 205, 209, 210, 211, 215, 216, 217, 218, 219, 220, 230, 231, 232, 233],
        "TC-005": [202, 203, 204, 205, 209, 210, 211, 215, 216, 217, 221, 222, 223, 224, 225, 226, 230, 231, 232, 233],
    },
    "check_fault": {
        "TC-001": [8, 9, 10],
        "TC-002": [8, 9, 11, 12, 13, 14, 19, 20, 21, 22],
        "TC-007": [8, 9, 11, 12, 13, 14, 19, 20, 21],
    },
}


def _mock_component_source(comp):
    if comp == "EngineControl":
        lines = list(_ENGINE_FILE_HEADER)
        unit_keys = sorted(_ENGINE_MOCK.keys(), key=lambda k: _ENGINE_MOCK[k]["src"][0][0])
        for key in unit_keys:
            for (n, t, c, h) in _ENGINE_MOCK[key]["src"]:
                lines.append((n, t, 0))
            last_n = _ENGINE_MOCK[key]["src"][-1][0]
            lines.append((last_n + 1, '', 0))
        source = [
            {"n": n, "text": t, "cov": "raw", "hits": 0, "tcs": [],
             "is_branch": t.strip().startswith(("if ", "} else", "else if", "while ", "for ", "switch "))}
            for (n, t, *_) in lines
        ]
        return {"ok": True, "component": comp, "source": source}
    if comp == "Diagnostics":
        src_lines = [
            (1,  '#include "diag_monitor.h"',           0),
            (2,  '',                                     0),
            (3,  'static int g_fault_log[32];',         0),
            (4,  'static int g_log_idx = 0;',           0),
            (5,  '',                                     0),
            (6,  '/* FaultCode 정의 */',                 0),
            (7,  'typedef enum { FAULT_NONE=0, FAULT_NULL, FAULT_VOLT,', 0),
            (8,  'FaultCode check_fault(Sensor* s, Mode mode, int retries) {', 0),
            (9,  '    if (s == NULL || s->id < 0) {',  0),
            (10, '        return FAULT_NULL;',           0),
            (11, '    }',                                0),
            (12, '    if (mode == ACTIVE) {',           0),
            (13, '        if (s->volt < V_MIN || s->volt > V_MAX) {', 0),
            (14, '            if (s->temp > T_CRIT && retries <= 0) {', 0),
            (15, '                engine_stop();',       0),
            (16, '                return FAULT_THERMAL;',0),
            (17, '            }',                        0),
            (18, '            return FAULT_VOLT;',      0),
            (19, '        }',                            0),
            (20, '    }',                                0),
            (21, '    return FAULT_NONE;',              0),
            (22, '}',                                    0),
        ]
        source = [{"n": n, "text": t, "cov": "raw", "hits": 0, "tcs": [],
                   "is_branch": t.strip().startswith(("if ", "while ", "for "))}
                  for (n, t, *_) in src_lines]
        return {"ok": True, "component": comp, "source": source}
    if comp == "SpeedControl":
        src_lines = [
            (1,  '#include "speed_calc.h"',              0),
            (2,  '',                                      0),
            (3,  'static int g_last = 0;',               0),
            (4,  '',                                      0),
            (5,  'int calculate_speed(int sensor_id, int* out_buf) {', 0),
            (6,  '    int raw = read_sensor(sensor_id);', 0),
            (7,  '    if (raw < 0) return -1;',          0),
            (8,  '    int speed = raw * 3.6;',           0),
            (9,  '    if (speed > 200) speed = 200;',   0),
            (10, '    g_last = speed;',                  0),
            (11, '    if (out_buf) out_buf[0] = speed;', 0),
            (12, '    return speed;',                    0),
            (13, '}',                                    0),
        ]
        source = [{"n": n, "text": t, "cov": "raw", "hits": 0, "tcs": [],
                   "is_branch": t.strip().startswith(("if ", "while ", "for "))}
                  for (n, t, *_) in src_lines]
        return {"ok": True, "component": comp, "source": source}
    return {"ok": False, "source": []}


def _mock_scan():
    return {
        "ok": True, "project": "ecu_powertrain",
        "tree": [
            {"type": "folder", "name": "src", "children": [
                {"type": "folder", "name": "speed", "children": [
                    {"type": "file", "name": "speed_calc.c",
                     "path": "src/speed/speed_calc.c", "component": "SpeedControl"}]},
                {"type": "folder", "name": "diag", "children": [
                    {"type": "file", "name": "diag_monitor.c",
                     "path": "src/diag/diag_monitor.c", "component": "Diagnostics"}]},
                {"type": "folder", "name": "engine", "children": [
                    {"type": "file", "name": "engine_ctrl.c",
                     "path": "src/engine/engine_ctrl.c", "component": "EngineControl"}]},
            ]},
        ],
        "components": {
            "SpeedControl": {"file": "src/speed/speed_calc.c", "units": [
                {"unit_ref": "src/speed/speed_calc.c::calculate_speed", "name": "calculate_speed",
                 "lines": "6-18", "changed": False, "callers": [], "branches": 2,
                 "max_depth": 1, "support": "B"}]},
            "Diagnostics": {"file": "src/diag/diag_monitor.c", "units": [
                {"unit_ref": "src/diag/diag_monitor.c::check_fault", "name": "check_fault",
                 "lines": "8-44", "changed": True, "callers": ["log_event"], "branches": 16,
                 "max_depth": 4, "support": "C"}]},
            "EngineControl": {"file": "src/engine/engine_ctrl.c", "units": [
                {"unit_ref": "src/engine/engine_ctrl.c::engine_init",       "name": "engine_init",
                 "lines": "12-28",  "changed": False, "callers": [],                  "branches": 3,  "max_depth": 1, "support": "A"},
                {"unit_ref": "src/engine/engine_ctrl.c::engine_start",      "name": "engine_start",
                 "lines": "30-55",  "changed": True,  "callers": ["engine_init"],     "branches": 6,  "max_depth": 2, "support": "B"},
                {"unit_ref": "src/engine/engine_ctrl.c::engine_stop",       "name": "engine_stop",
                 "lines": "57-74",  "changed": False, "callers": [],                  "branches": 4,  "max_depth": 2, "support": "A"},
                {"unit_ref": "src/engine/engine_ctrl.c::set_throttle",      "name": "set_throttle",
                 "lines": "76-95",  "changed": True,  "callers": ["engine_start"],    "branches": 5,  "max_depth": 2, "support": "B"},
                {"unit_ref": "src/engine/engine_ctrl.c::read_rpm",          "name": "read_rpm",
                 "lines": "97-112", "changed": False, "callers": ["engine_start"],    "branches": 2,  "max_depth": 1, "support": "A"},
                {"unit_ref": "src/engine/engine_ctrl.c::check_overrev",     "name": "check_overrev",
                 "lines": "114-138","changed": True,  "callers": ["engine_start"],    "branches": 8,  "max_depth": 3, "support": "C"},
                {"unit_ref": "src/engine/engine_ctrl.c::apply_fuel_cut",    "name": "apply_fuel_cut",
                 "lines": "140-158","changed": False, "callers": ["check_overrev"],   "branches": 4,  "max_depth": 2, "support": "B"},
                {"unit_ref": "src/engine/engine_ctrl.c::calc_torque",       "name": "calc_torque",
                 "lines": "160-181","changed": False, "callers": ["set_throttle"],    "branches": 5,  "max_depth": 2, "support": "B"},
                {"unit_ref": "src/engine/engine_ctrl.c::update_status_led", "name": "update_status_led",
                 "lines": "183-200","changed": False, "callers": [],                  "branches": 3,  "max_depth": 1, "support": "A"},
                {"unit_ref": "src/engine/engine_ctrl.c::engine_self_test",  "name": "engine_self_test",
                 "lines": "202-240","changed": True,  "callers": ["engine_init"],     "branches": 12, "max_depth": 4, "support": "C"},
            ]},
        },
    }


_ENGINE_FILE_HEADER = [
    (1,  '#include "engine_ctrl.h"',           0),
    (2,  '#include "hw_api.h"',                0),
    (3,  '#include "log.h"',                   0),
    (4,  '',                                    0),
    (5,  '/* EngineControl — ECU Powertrain */',0),
    (6,  '',                                    0),
    (7,  'static EngineState  g_state;',        0),
    (8,  'static EngineConfig g_cfg;',          0),
    (9,  '',                                    0),
    (10, '#define RPM_SCALE        10',         0),
    (11, '#define RPM_DIV          1',          0),
    (12, '',                                    0),
]

_ENGINE_MOCK = {
    "src/engine/engine_ctrl.c::engine_init": {
        "coverage": {"statement": 100, "branch": 100, "mcdc": 100},
        "src": [
            (12, "EngineState engine_init(EngineConfig* cfg) {",       "full",    4),
            (13, "    if (cfg == NULL) {",                              "full",    4),
            (14, "        return ENGINE_ERR_NULL;",                     "full",    1),
            (15, "    }",                                               "full",    4),
            (16, "    g_state.rpm      = 0;",                          "full",    3),
            (17, "    g_state.throttle = 0;",                          "full",    3),
            (18, "    g_state.running  = 0;",                          "full",    3),
            (19, "    g_state.fuel_cut = 0;",                          "full",    3),
            (20, "    if (cfg->mode == ENGINE_MODE_TEST) {",           "full",    3),
            (21, "        g_state.max_rpm = RPM_TEST_LIMIT;",          "full",    1),
            (22, "    } else {",                                        "full",    3),
            (23, "        g_state.max_rpm = cfg->max_rpm;",            "full",    2),
            (24, "    }",                                               "full",    3),
            (25, "    engine_self_test();",                             "full",    3),
            (26, "    update_status_led(LED_INIT);",                   "full",    3),
            (27, "    return ENGINE_OK;",                               "full",    3),
            (28, "}",                                                   "full",    4),
        ],
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "cfg==NULL → ERR_NULL",
             "inputs": {"cfg": "NULL"}, "expected": {"return": "ENGINE_ERR_NULL"}},
            {"id": "TC-002", "verdict": "pass", "desc": "일반 모드 초기화",
             "inputs": {"cfg->mode": "NORMAL", "cfg->max_rpm": 6000},
             "expected": {"return": "ENGINE_OK", "g_state.max_rpm": 6000}},
            {"id": "TC-003", "verdict": "pass", "desc": "TEST 모드 → max_rpm=RPM_TEST_LIMIT",
             "inputs": {"cfg->mode": "TEST"}, "expected": {"g_state.max_rpm": "RPM_TEST_LIMIT"}},
        ],
    },
    "src/engine/engine_ctrl.c::engine_start": {
        "coverage": {"statement": 83, "branch": 75, "mcdc": 68},
        "src": [
            (30, "int engine_start(int key_pos) {",                     "full",   5),
            (31, "    if (g_state.running) {",                          "full",   5),
            (32, "        return ENGINE_ERR_ALREADY;",                  "full",   2),
            (33, "    }",                                               "full",   5),
            (34, "    if (key_pos < KEY_ON) {",                        "full",   3),
            (35, "        return ENGINE_ERR_KEY;",                      "full",   1),
            (36, "    }",                                               "full",   3),
            (37, "    int rpm = read_rpm();",                           "full",   3),
            (38, "    if (rpm > RPM_CRANK_LIMIT) {",                   "full",   3),
            (39, "        log_event(EVT_OVER_CRANK);",                 "none",   0),
            (40, "        return ENGINE_ERR_RPM;",                     "none",   0),
            (41, "    }",                                               "full",   3),
            (42, "    set_throttle(THROTTLE_IDLE);",                   "full",   3),
            (43, "    g_state.running = 1;",                           "full",   3),
            (44, "    if (check_overrev() != OVERREV_OK) {",           "full",   3),
            (45, "        engine_stop();",                              "none",   0),
            (46, "        return ENGINE_ERR_OVERREV;",                 "none",   0),
            (47, "    }",                                               "full",   3),
            (48, "    update_status_led(LED_RUN);",                    "full",   3),
            (49, "    return ENGINE_OK;",                               "full",   3),
            (50, "}",                                                   "full",   5),
        ],
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "이미 running → ERR_ALREADY",
             "inputs": {"g_state.running": 1}, "expected": {"return": "ENGINE_ERR_ALREADY"}},
            {"id": "TC-002", "verdict": "pass", "desc": "key_pos < KEY_ON → ERR_KEY",
             "inputs": {"key_pos": 0}, "expected": {"return": "ENGINE_ERR_KEY"}},
            {"id": "TC-003", "verdict": "pass", "desc": "정상 시동",
             "inputs": {"key_pos": "KEY_ON", "rpm": 0},
             "expected": {"return": "ENGINE_OK", "g_state.running": 1}},
            {"id": "TC-004", "verdict": "manual", "desc": "rpm > RPM_CRANK_LIMIT (수동 확인 필요)",
             "inputs": {"rpm": "RPM_CRANK_LIMIT+1"}, "expected": {"return": "ENGINE_ERR_RPM"}},
        ],
    },
    "src/engine/engine_ctrl.c::engine_stop": {
        "coverage": {"statement": 100, "branch": 100, "mcdc": 88},
        "src": [
            (57, "int engine_stop(void) {",                             "full",   4),
            (58, "    if (!g_state.running) {",                        "full",   4),
            (59, "        return ENGINE_ERR_NOT_RUNNING;",             "full",   1),
            (60, "    }",                                               "full",   4),
            (61, "    set_throttle(0);",                               "full",   3),
            (62, "    g_state.fuel_cut = 1;",                          "full",   3),
            (63, "    int timeout = STOP_TIMEOUT;",                    "full",   3),
            (64, "    while (read_rpm() > RPM_IDLE && timeout > 0) {", "full",   3),
            (65, "        timeout--;",                                  "full",   8),
            (66, "    }",                                               "full",   3),
            (67, "    if (timeout <= 0) {",                            "full",   3),
            (68, "        log_event(EVT_STOP_TIMEOUT);",               "full",   1),
            (69, "        return ENGINE_WARN_TIMEOUT;",                "full",   1),
            (70, "    }",                                               "full",   3),
            (71, "    g_state.running = 0;",                           "full",   2),
            (72, "    update_status_led(LED_OFF);",                    "full",   2),
            (73, "    return ENGINE_OK;",                               "full",   2),
            (74, "}",                                                   "full",   4),
        ],
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "not running → ERR_NOT_RUNNING",
             "inputs": {"g_state.running": 0}, "expected": {"return": "ENGINE_ERR_NOT_RUNNING"}},
            {"id": "TC-002", "verdict": "pass", "desc": "정상 정지",
             "inputs": {"g_state.running": 1, "rpm": 0},
             "expected": {"return": "ENGINE_OK", "g_state.running": 0}},
            {"id": "TC-003", "verdict": "pass", "desc": "타임아웃 → WARN_TIMEOUT",
             "inputs": {"rpm": "항상 > IDLE"}, "expected": {"return": "ENGINE_WARN_TIMEOUT"}},
        ],
    },
    "src/engine/engine_ctrl.c::set_throttle": {
        "coverage": {"statement": 90, "branch": 80, "mcdc": 72},
        "src": [
            (76, "int set_throttle(int pct) {",                        "full",   6),
            (77, "    if (pct < 0 || pct > 100) {",                   "full",   6),
            (78, "        return ENGINE_ERR_RANGE;",                   "full",   2),
            (79, "    }",                                               "full",   6),
            (80, "    if (!g_state.running && pct > 0) {",            "full",   4),
            (81, "        return ENGINE_ERR_NOT_RUNNING;",             "none",   0),
            (82, "    }",                                               "full",   4),
            (83, "    if (g_state.fuel_cut) {",                       "full",   4),
            (84, "        pct = 0;",                                   "full",   1),
            (85, "    }",                                               "full",   4),
            (86, "    int delta = pct - g_state.throttle;",           "full",   4),
            (87, "    if (delta > THROTTLE_SLEW_MAX) {",              "full",   4),
            (88, "        pct = g_state.throttle + THROTTLE_SLEW_MAX;","full",  2),
            (89, "    }",                                               "full",   4),
            (90, "    g_state.throttle = pct;",                       "full",   4),
            (91, "    hw_write_throttle(pct);",                       "full",   4),
            (92, "    return ENGINE_OK;",                               "full",   4),
            (93, "}",                                                   "full",   6),
        ],
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "pct < 0 → ERR_RANGE",
             "inputs": {"pct": -1}, "expected": {"return": "ENGINE_ERR_RANGE"}},
            {"id": "TC-002", "verdict": "pass", "desc": "pct > 100 → ERR_RANGE",
             "inputs": {"pct": 101}, "expected": {"return": "ENGINE_ERR_RANGE"}},
            {"id": "TC-003", "verdict": "pass", "desc": "fuel_cut 중 → pct=0 강제",
             "inputs": {"pct": 50, "fuel_cut": 1}, "expected": {"g_state.throttle": 0}},
            {"id": "TC-004", "verdict": "manual", "desc": "not running + pct>0 경로",
             "inputs": {"pct": 30, "g_state.running": 0},
             "expected": {"return": "ENGINE_ERR_NOT_RUNNING"}},
        ],
    },
    "src/engine/engine_ctrl.c::read_rpm": {
        "coverage": {"statement": 100, "branch": 100, "mcdc": 100},
        "src": [
            (97,  "int read_rpm(void) {",                              "full",   8),
            (98,  "    int raw = hw_read_rpm_sensor();",               "full",   8),
            (99,  "    if (raw < 0) {",                                "full",   8),
            (100, "        return 0;",                                  "full",   2),
            (101, "    }",                                              "full",   8),
            (102, "    int rpm = (raw * RPM_SCALE) / RPM_DIV;",       "full",   6),
            (103, "    if (rpm > RPM_MAX_PHYSICAL) {",                 "full",   6),
            (104, "        rpm = RPM_MAX_PHYSICAL;",                   "full",   1),
            (105, "    }",                                              "full",   6),
            (106, "    return rpm;",                                    "full",   6),
            (107, "}",                                                  "full",   8),
        ],
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "raw < 0 → 0 반환",
             "inputs": {"hw_read_rpm_sensor()": -1}, "expected": {"return": 0}},
            {"id": "TC-002", "verdict": "pass", "desc": "정상 rpm 계산",
             "inputs": {"hw_read_rpm_sensor()": 300}, "expected": {"return": 3000}},
            {"id": "TC-003", "verdict": "pass", "desc": "clamp to RPM_MAX_PHYSICAL",
             "inputs": {"hw_read_rpm_sensor()": 99999},
             "expected": {"return": "RPM_MAX_PHYSICAL"}},
        ],
    },
    "src/engine/engine_ctrl.c::check_overrev": {
        "coverage": {"statement": 62, "branch": 55, "mcdc": 48},
        "src": [
            (114, "OverrevState check_overrev(void) {",                "full",   5),
            (115, "    int rpm = read_rpm();",                         "full",   5),
            (116, "    if (rpm < 0) {",                                "full",   5),
            (117, "        return OVERREV_ERR;",                       "full",   1),
            (118, "    }",                                              "full",   5),
            (119, "    if (rpm <= g_state.max_rpm) {",                 "full",   5),
            (120, "        g_state.overrev_cnt = 0;",                  "full",   3),
            (121, "        return OVERREV_OK;",                        "full",   3),
            (122, "    }",                                              "full",   5),
            (123, "    g_state.overrev_cnt++;",                        "full",   2),
            (124, "    if (g_state.overrev_cnt >= OVERREV_THRESH) {",  "full",   2),
            (125, "        if (g_cfg.allow_fuel_cut) {",               "full",   2),
            (126, "            apply_fuel_cut();",                     "none",   0),
            (127, "            return OVERREV_FUEL_CUT;",              "none",   0),
            (128, "        }",                                          "none",   0),
            (129, "        if (g_cfg.allow_shutdown) {",               "none",   0),
            (130, "            engine_stop();",                        "none",   0),
            (131, "            return OVERREV_SHUTDOWN;",              "none",   0),
            (132, "        }",                                          "none",   0),
            (133, "        log_event(EVT_OVERREV_WARN);",              "none",   0),
            (134, "        return OVERREV_WARN;",                      "none",   0),
            (135, "    }",                                              "full",   2),
            (136, "    return OVERREV_OK;",                            "full",   2),
            (137, "}",                                                  "full",   5),
        ],
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "rpm < 0 → OVERREV_ERR",
             "inputs": {"rpm": -1}, "expected": {"return": "OVERREV_ERR"}},
            {"id": "TC-002", "verdict": "pass", "desc": "rpm 정상 범위 → OVERREV_OK",
             "inputs": {"rpm": 3000, "max_rpm": 6000}, "expected": {"return": "OVERREV_OK"}},
            {"id": "TC-003", "verdict": "manual", "desc": "overrev_cnt >= THRESH + fuel_cut 허용",
             "inputs": {"rpm": 7000, "overrev_cnt": 5, "allow_fuel_cut": 1},
             "expected": {"return": "OVERREV_FUEL_CUT"}},
            {"id": "TC-004", "verdict": "manual", "desc": "shutdown 경로 (수동 필요)",
             "inputs": {"allow_shutdown": 1}, "expected": {"return": "OVERREV_SHUTDOWN"}},
        ],
    },
    "src/engine/engine_ctrl.c::apply_fuel_cut": {
        "coverage": {"statement": 88, "branch": 75, "mcdc": 70},
        "src": [
            (140, "int apply_fuel_cut(void) {",                        "full",   4),
            (141, "    if (g_state.fuel_cut) {",                      "full",   4),
            (142, "        return ENGINE_ERR_ALREADY;",               "full",   1),
            (143, "    }",                                             "full",   4),
            (144, "    g_state.fuel_cut = 1;",                        "full",   3),
            (145, "    hw_write_throttle(0);",                        "full",   3),
            (146, "    int t = FUEL_CUT_HOLD_MS;",                    "full",   3),
            (147, "    while (t > 0 && read_rpm() > RPM_IDLE) {",     "full",   3),
            (148, "        hw_delay_ms(1);",                          "full",  10),
            (149, "        t--;",                                      "full",  10),
            (150, "    }",                                             "full",   3),
            (151, "    if (read_rpm() <= RPM_IDLE) {",               "full",   3),
            (152, "        g_state.fuel_cut = 0;",                   "full",   2),
            (153, "        return ENGINE_OK;",                        "full",   2),
            (154, "    }",                                             "full",   3),
            (155, "    log_event(EVT_FUEL_CUT_STUCK);",              "none",   0),
            (156, "    return ENGINE_WARN_FUEL_CUT;",                "none",   0),
            (157, "}",                                                 "full",   4),
        ],
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "이미 fuel_cut → ERR_ALREADY",
             "inputs": {"fuel_cut": 1}, "expected": {"return": "ENGINE_ERR_ALREADY"}},
            {"id": "TC-002", "verdict": "pass", "desc": "정상 fuel cut → rpm 하강 → OK",
             "inputs": {"rpm_after": "RPM_IDLE-1"}, "expected": {"return": "ENGINE_OK"}},
            {"id": "TC-003", "verdict": "manual", "desc": "rpm 안 내려감 → WARN_FUEL_CUT",
             "inputs": {"rpm": "항상 > RPM_IDLE"}, "expected": {"return": "ENGINE_WARN_FUEL_CUT"}},
        ],
    },
    "src/engine/engine_ctrl.c::calc_torque": {
        "coverage": {"statement": 95, "branch": 87, "mcdc": 80},
        "src": [
            (160, "int calc_torque(int throttle_pct, int rpm) {",      "full",   6),
            (161, "    if (throttle_pct < 0 || throttle_pct > 100) {", "full",   6),
            (162, "        return TORQUE_ERR;",                         "full",   1),
            (163, "    }",                                              "full",   6),
            (164, "    if (rpm <= 0) {",                               "full",   6),
            (165, "        return 0;",                                  "full",   1),
            (166, "    }",                                              "full",   6),
            (167, "    int base = (throttle_pct * TORQUE_COEFF) / 100;","full",  5),
            (168, "    int rpm_factor = rpm / RPM_TORQUE_DIV;",        "full",   5),
            (169, "    if (rpm_factor > TORQUE_RPM_MAX_F) {",         "full",   5),
            (170, "        rpm_factor = TORQUE_RPM_MAX_F;",            "full",   1),
            (171, "    }",                                              "full",   5),
            (172, "    int torque = base * rpm_factor;",               "full",   5),
            (173, "    if (torque > TORQUE_MAX) {",                    "full",   5),
            (174, "        torque = TORQUE_MAX;",                      "none",   0),
            (175, "    }",                                              "full",   5),
            (176, "    g_state.torque = torque;",                      "full",   5),
            (177, "    return torque;",                                 "full",   5),
            (178, "}",                                                  "full",   6),
        ],
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "throttle < 0 → TORQUE_ERR",
             "inputs": {"throttle_pct": -1}, "expected": {"return": "TORQUE_ERR"}},
            {"id": "TC-002", "verdict": "pass", "desc": "rpm = 0 → torque 0",
             "inputs": {"rpm": 0}, "expected": {"return": 0}},
            {"id": "TC-003", "verdict": "pass", "desc": "정상 토크 계산",
             "inputs": {"throttle_pct": 50, "rpm": 3000}, "expected": {"return": 150}},
            {"id": "TC-004", "verdict": "manual", "desc": "torque > TORQUE_MAX clamp",
             "inputs": {"throttle_pct": 100, "rpm": 9000},
             "expected": {"return": "TORQUE_MAX"}},
        ],
    },
    "src/engine/engine_ctrl.c::update_status_led": {
        "coverage": {"statement": 100, "branch": 100, "mcdc": 100},
        "src": [
            (183, "void update_status_led(LedState state) {",           "full",   5),
            (184, "    switch (state) {",                               "full",   5),
            (185, "        case LED_OFF:",                              "full",   2),
            (186, "            hw_led_set(0, 0, 0);",                  "full",   2),
            (187, "            break;",                                  "full",   2),
            (188, "        case LED_INIT:",                             "full",   1),
            (189, "            hw_led_set(0, 0, 255);",                "full",   1),
            (190, "            break;",                                  "full",   1),
            (191, "        case LED_RUN:",                              "full",   1),
            (192, "            hw_led_set(0, 255, 0);",                "full",   1),
            (193, "            break;",                                  "full",   1),
            (194, "        default:",                                    "full",   1),
            (195, "            hw_led_set(255, 0, 0);",                "full",   1),
            (196, "            break;",                                  "full",   1),
            (197, "    }",                                              "full",   5),
            (198, "}",                                                  "full",   5),
        ],
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "LED_OFF → rgb(0,0,0)",
             "inputs": {"state": "LED_OFF"}, "expected": {"hw_led": "(0,0,0)"}},
            {"id": "TC-002", "verdict": "pass", "desc": "LED_INIT → blue",
             "inputs": {"state": "LED_INIT"}, "expected": {"hw_led": "(0,0,255)"}},
            {"id": "TC-003", "verdict": "pass", "desc": "LED_RUN → green",
             "inputs": {"state": "LED_RUN"}, "expected": {"hw_led": "(0,255,0)"}},
        ],
    },
    "src/engine/engine_ctrl.c::engine_self_test": {
        "coverage": {"statement": 55, "branch": 48, "mcdc": 40},
        "src": [
            (202, "SelfTestResult engine_self_test(void) {",            "full",   3),
            (203, "    SelfTestResult res = {0};",                      "full",   3),
            (204, "    int rpm = read_rpm();",                          "full",   3),
            (205, "    if (rpm != 0) {",                               "full",   3),
            (206, "        res.rpm_ok = 0;",                           "full",   1),
            (207, "        res.error_code = SELF_TEST_RPM_NONZERO;",   "full",   1),
            (208, "        goto done;",                                 "full",   1),
            (209, "    }",                                              "full",   3),
            (210, "    res.rpm_ok = 1;",                               "full",   2),
            (211, "    if (hw_check_throttle_hw() != HW_OK) {",       "full",   2),
            (212, "        res.throttle_ok = 0;",                      "none",   0),
            (213, "        res.error_code = SELF_TEST_THR_HW;",       "none",   0),
            (214, "        goto done;",                                 "none",   0),
            (215, "    }",                                              "full",   2),
            (216, "    res.throttle_ok = 1;",                         "full",   2),
            (217, "    if (hw_check_fuel_hw() != HW_OK) {",           "none",   0),
            (218, "        res.fuel_ok = 0;",                          "none",   0),
            (219, "        res.error_code = SELF_TEST_FUEL_HW;",      "none",   0),
            (220, "        goto done;",                                 "none",   0),
            (221, "    }",                                              "none",   0),
            (222, "    res.fuel_ok = 1;",                              "none",   0),
            (223, "    if (hw_check_led_hw() != HW_OK) {",            "none",   0),
            (224, "        res.led_ok = 0;",                           "none",   0),
            (225, "        res.error_code = SELF_TEST_LED_HW;",       "none",   0),
            (226, "        goto done;",                                 "none",   0),
            (227, "    }",                                              "none",   0),
            (228, "    res.led_ok = 1;",                               "none",   0),
            (229, "    res.all_ok = 1;",                               "none",   0),
            (230, "done:",                                              "full",   3),
            (231, "    log_event(res.all_ok ? EVT_SELF_TEST_OK : EVT_SELF_TEST_FAIL);","full",3),
            (232, "    return res;",                                    "full",   3),
            (233, "}",                                                  "full",   3),
        ],
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "rpm != 0 → rpm_ok=0 즉시 종료",
             "inputs": {"rpm": 100}, "expected": {"error_code": "SELF_TEST_RPM_NONZERO"}},
            {"id": "TC-002", "verdict": "pass", "desc": "rpm=0, 모든 HW 정상",
             "inputs": {"rpm": 0, "hw_all": "OK"}, "expected": {"res.all_ok": 1}},
            {"id": "TC-003", "verdict": "manual", "desc": "throttle HW 불량 (수동 필요)",
             "inputs": {"hw_check_throttle_hw()": "HW_FAIL"},
             "expected": {"error_code": "SELF_TEST_THR_HW"}},
            {"id": "TC-004", "verdict": "manual", "desc": "fuel HW 불량",
             "inputs": {"hw_check_fuel_hw()": "HW_FAIL"},
             "expected": {"error_code": "SELF_TEST_FUEL_HW"}},
            {"id": "TC-005", "verdict": "manual", "desc": "LED HW 불량",
             "inputs": {"hw_check_led_hw()": "HW_FAIL"},
             "expected": {"error_code": "SELF_TEST_LED_HW"}},
        ],
    },
}


def _mock_generate(unit_ref):
    if unit_ref in _ENGINE_MOCK:
        d = _ENGINE_MOCK[unit_ref]
        unit_name = unit_ref.split("::")[-1]
        tc_lines = _TC_COVERED_LINES.get(unit_name, {})
        line_tcs: dict = {}
        for case in d["cases"]:
            for ln_no in tc_lines.get(case["id"], []):
                line_tcs.setdefault(ln_no, []).append(case["id"])
        source = [
            {"n": n, "text": t, "cov": c, "hits": h,
             "tcs": line_tcs.get(n, []),
             "is_branch": t.strip().startswith(("if ", "} else", "else if", "while ", "for ", "switch "))}
            for (n, t, c, h) in d["src"]
        ]
        return {
            "ok": True, "unit_ref": unit_ref, "mode": "mock",
            "coverage": d["coverage"],
            "source": source,
            "cases": d["cases"],
            "notes": ["목업 데이터 (EngineControl 예제)"],
            "logs": [{"level": "info", "text": f"[mock] {unit_name} 목업 응답"}],
        }
    if unit_ref != "src/diag/diag_monitor.c::check_fault":
        return None
    src = [
        (8,  "FaultCode check_fault(Sensor* s, Mode mode, int retries) {", "full", 6),
        (9,  "    if (s == NULL || s->id < 0) {", "full", 6),
        (10, "        return FAULT_NULL;", "full", 1),
        (12, "    if (mode == ACTIVE) {", "full", 5),
        (13, "        if (s->volt < V_MIN || s->volt > V_MAX) {", "full", 3),
        (14, "            if (s->temp > T_CRIT && retries <= 0) {", "full", 2),
        (19, "                return FAULT_THERMAL;", "none", 0),
        (23, "            } else {", "none", 0),
        (24, "                return FAULT_VOLT;", "none", 0),
        (36, "    return FAULT_NONE;", "full", 2),
        (37, "}", "full", 6),
    ]
    cf_lines = _TC_COVERED_LINES.get("check_fault", {})
    cf_line_tcs: dict = {}
    for tc_id, lns in cf_lines.items():
        for ln_no in lns:
            cf_line_tcs.setdefault(ln_no, []).append(tc_id)
    source = [{"n": n, "text": t, "cov": c, "hits": h,
               "tcs": cf_line_tcs.get(n, []),
               "is_branch": t.strip().startswith(("if", "} else", "else", "while", "for"))}
              for (n, t, c, h) in src]
    return {
        "ok": True, "unit_ref": unit_ref, "mode": "mock",
        "coverage": {"statement": 70, "branch": 61, "mcdc": 56},
        "source": source,
        "cases": [
            {"id": "TC-001", "verdict": "pass", "desc": "s==NULL (D1 true)",
             "inputs": {"s": "NULL"}, "expected": {"return": "FAULT_NULL"}},
            {"id": "TC-002", "verdict": "pass", "desc": "과전압·과열 → shutdown",
             "inputs": {"s->volt": 16, "s->temp": 130}, "expected": {"return": "FAULT_OVERHEAT"}},
            {"id": "TC-007", "verdict": "manual", "desc": "복구경로(D9) 수동 필요",
             "inputs": {"g_recover_en": "?"}, "expected": {"return": "FAULT_RECOVERED"}},
        ],
        "notes": ["목업 데이터 (check_fault 는 Z3 단계에서 실제화 예정)"],
        "logs": [{"level": "info", "text": "[mock] check_fault 목업 응답"}],
    }


if __name__ == "__main__":
    print(f"[SWTS] SWTS_ROOT = {SWTS_ROOT}")
    print(f"[SWTS] 엔진 = {'활성(real)' if ENGINES else '목업(mock)'}")
    print("[SWTS] http://localhost:5000")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port)
