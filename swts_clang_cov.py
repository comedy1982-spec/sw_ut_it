"""
SWTS — Clang 19 MC/DC 커버리지 엔진 (ISO 26262 ASIL C/D)
==========================================================
-fcoverage-mcdc 플래그로 복합조건의 독립영향(MC/DC)을 조건별로 측정.
gcov 대비: if (a && (b || c)) 에서 각 조건이 결과를 독립으로 바꿨는지 판정.

흐름:
  1. Clang AST 분석 (libclang) -> 함수 시그니처 + 피호출 함수 stub 생성
  2. clang -O0 -fprofile-instr-generate -fcoverage-mapping -fcoverage-mcdc
  3. LLVM_PROFILE_FILE=run.profraw ./testbin
  4. llvm-profdata merge -sparse run.profraw -o run.profdata
  5. llvm-cov export testbin -instr-profile=run.profdata -> cov.json
  6. JSON 파싱 -> {stmt%, branch%, mcdc%, mcdc_records}

의존: clang[-19], llvm-profdata[-19], llvm-cov[-19]
없으면 generate() = None -> 상위에서 정적 분석 폴백.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import tempfile
from itertools import product
from typing import Optional

import clang.cindex as cidx


# ============================================================
# 1. 도구 탐색
# ============================================================
_CLANG_CANDS    = ["clang-19", "clang-20", "clang-18", "clang-17", "clang"]
_PROFDATA_CANDS = ["llvm-profdata-19", "llvm-profdata-20", "llvm-profdata-18", "llvm-profdata"]
_COV_CANDS      = ["llvm-cov-19", "llvm-cov-20", "llvm-cov-18", "llvm-cov"]


def _find(candidates: list[str]) -> Optional[str]:
    return next((c for c in candidates if shutil.which(c)), None)


def find_clang()        -> Optional[str]: return _find(_CLANG_CANDS)
def find_llvm_profdata()-> Optional[str]: return _find(_PROFDATA_CANDS)
def find_llvm_cov()     -> Optional[str]: return _find(_COV_CANDS)
def tools_available()   -> bool:
    return bool(find_clang() and find_llvm_profdata() and find_llvm_cov())


# ============================================================
# 2. libclang — 함수 상세 정보 추출
# ============================================================
def _get_func_detail(abs_path: str, func_name: str, flags: list[str]) -> Optional[dict]:
    """리턴타입, 파라미터(이름+타입), 피호출함수(이름+리턴타입+파라미터타입) 추출."""
    index = cidx.Index.create()
    args = ["-x", "c", "-ferror-limit=0", "-Wno-everything"] + flags
    opts = (cidx.TranslationUnit.PARSE_INCOMPLETE |
            cidx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
    try:
        tu = index.parse(abs_path, args=args, options=opts)
    except cidx.TranslationUnitLoadError:
        return None

    # 모든 함수 선언에서 시그니처 수집 (stub 생성용)
    all_func_sigs: dict[str, dict] = {}
    for node in tu.cursor.walk_preorder():
        if node.kind == cidx.CursorKind.FUNCTION_DECL and node.spelling:
            all_func_sigs[node.spelling] = {
                "ret": node.result_type.spelling,
                "params": [{"name": a.spelling or f"_p{i}",
                             "type": a.type.spelling}
                            for i, a in enumerate(node.get_arguments())],
            }

    # 대상 함수 찾기
    for node in tu.cursor.get_children():
        if (node.kind == cidx.CursorKind.FUNCTION_DECL
                and node.is_definition()
                and node.spelling == func_name
                and node.location.file
                and os.path.abspath(node.location.file.name) == os.path.abspath(abs_path)):

            params = [{"name": a.spelling or f"p{i}", "type": a.type.spelling}
                      for i, a in enumerate(node.get_arguments())]

            callees: set[str] = set()
            def _walk(n):
                if n.kind == cidx.CursorKind.CALL_EXPR:
                    nm = n.spelling or (n.referenced.spelling if n.referenced else None)
                    if nm and nm != func_name:
                        callees.add(nm)
                for c in n.get_children():
                    _walk(c)
            _walk(node)

            return {
                "ret_type": node.result_type.spelling,
                "params": params,
                "callees": callees,
                "all_sigs": all_func_sigs,
            }
    return None


# ============================================================
# 3. 테스트 벡터 자동 생성
# ============================================================
def _param_choices(p: dict, idx: int) -> list[tuple[list[str], str]]:
    """파라미터 1개의 (setup 문장 목록, call 인자식) 선택지 반환."""
    t = p["type"].replace("const", "").strip()
    n = p["name"]

    if t in ("int", "unsigned int", "short", "long", "unsigned char"):
        return [([], v) for v in ["0", "-1", "1", "100"]]
    if "*" in t:
        base = t.replace("*", "").strip()
        buf = f"_buf{idx}"
        if base in ("int", "char", "unsigned char", "short"):
            # 기본형 포인터 — 유효 버퍼만 사용 (NULL 역참조 방지)
            return [([f"int {buf}[8] = {{0}};"], buf)]
        else:
            # 구조체 포인터 — NULL 과 초기화된 구조체
            var = f"_s{idx}"
            return [
                ([f"{base} {var}; memset(&{var}, 0, sizeof({var}));"], f"&{var}"),
                ([], "NULL"),
            ]
    # enum / 기타 — 정수 0,1,2 시도 (C 에서 enum=int 호환)
    return [([], v) for v in ["0", "1", "2"]]


def _gen_vectors(params: list[dict]) -> list[dict]:
    """파라미터 조합으로 MC/DC 커버리지 향상 테스트 벡터 생성 (최대 8개)."""
    if not params:
        return [{"setup": [], "args": []}]
    choices = [_param_choices(p, i) for i, p in enumerate(params)]
    vectors = []
    for combo in list(product(*choices))[:8]:
        setup, args = [], []
        for stmts, arg in combo:
            setup.extend(stmts)
            args.append(arg)
        vectors.append({"setup": setup, "args": args})
    return vectors


# ============================================================
# 3-b. 조건 인식(condition-aware) 테스트 벡터 생성
#  - 분기 조건을 파싱해 구조체 필드/파라미터를 "트리거 값"으로 설정
#  - 들여쓰기로 상위 분기 조건을 누적해 깊은 분기까지 도달
# ============================================================
_CMP_RE = re.compile(r"^(.+?)\s*(==|!=|<=|>=|<|(?<!-)>)\s*(.+)$")


def _op_values(op: str, rhs: str) -> tuple:
    """조건 `lhs op rhs` 의 (참으로 만드는 값, 거짓으로 만드는 값)."""
    rhs = rhs.strip()
    plus, minus = f"(({rhs}) + 1)", f"(({rhs}) - 1)"
    return {
        "<":  (minus, rhs),
        ">":  (plus,  rhs),
        "<=": (rhs,   plus),
        ">=": (rhs,   minus),
        "==": (rhs,   plus),
        "!=": (plus,  rhs),
    }.get(op, (rhs, plus))


def _classify_lhs(lhs, tval, fval, ptr_name, struct_var, scalar_names):
    lhs = lhs.strip()
    if ptr_name and lhs.startswith(ptr_name + "->"):
        field = lhs[len(ptr_name) + 2:].strip()
        if re.match(r"^[A-Za-z_]\w*$", field):
            return ("field", field, tval, fval)
        return None
    if lhs in scalar_names:
        return ("scalar", lhs, tval, fval)
    return None  # 전역변수/미지 -> 건너뜀 (설정 불가)


def _atom_assignment(atom, ptr_name, struct_var, scalar_names):
    """원자 조건 1개 -> (kind, target, true_value, false_value)."""
    a = atom.strip().strip("()").strip()
    if not a:
        return None
    if ptr_name and re.match(rf"^{re.escape(ptr_name)}\s*==\s*NULL$", a):
        return ("null", ptr_name, "NULL", "")
    if ptr_name and re.match(rf"^{re.escape(ptr_name)}\s*!=\s*NULL$", a):
        return ("notnull", ptr_name, "", "")
    m = _CMP_RE.match(a)
    if m:
        lhs, op, rhs = m.group(1).strip(), m.group(2), m.group(3).strip()
        tval, fval = _op_values(op, rhs)
        return _classify_lhs(lhs, tval, fval, ptr_name, struct_var, scalar_names)
    m = re.match(r"^(.+?)\s*&\s*(.+)$", a)        # 비트 AND: lhs & mask
    if m:
        return _classify_lhs(m.group(1).strip(), m.group(2).strip(), "0",
                             ptr_name, struct_var, scalar_names)
    if a.startswith("!"):                          # !x  -> true:0 false:1
        return _classify_lhs(a[1:].strip(), "0", "1", ptr_name, struct_var, scalar_names)
    return _classify_lhs(a, "1", "0", ptr_name, struct_var, scalar_names)  # x -> true:1


def _smart_vectors(params: list[dict], body: list[tuple]) -> Optional[list[dict]]:
    """함수 본문 분기 조건을 분석해 도달성 높은 테스트 벡터 생성."""
    ptr_name = ptr_type = None
    ptr_idx = -1
    scalar_names: dict = {}
    basic_ptrs: dict = {}
    for idx, p in enumerate(params):
        t = p["type"].replace("const", "").strip()
        n = p["name"]
        if "*" in t:
            base = t.replace("*", "").strip()
            if base in ("int", "char", "unsigned char", "short"):
                basic_ptrs[idx] = n
            elif ptr_name is None:
                ptr_name, ptr_type, ptr_idx = n, base, idx
        else:
            scalar_names[n] = idx
    if ptr_name is None and not scalar_names:
        return None
    struct_var = f"_s{ptr_idx}" if ptr_idx >= 0 else None

    branch_re = re.compile(r"^\}?\s*(?:else\s+if|if|while|for)\s*\((.*)\)\s*\{?\s*$")
    parsed = []
    for _ln, text in body:
        m = branch_re.match(text.strip())
        if m:
            indent = len(text) - len(text.lstrip())
            parsed.append((indent, m.group(1).strip()))
    if not parsed:
        return None

    raw = [{"fields": {}, "scalars": {}, "null": False}]   # baseline
    if ptr_name:
        raw.append({"fields": {}, "scalars": {}, "null": True})

    stack: list = []   # (indent, ctx)
    for indent, cond in parsed:
        while stack and stack[-1][0] >= indent:
            stack.pop()
        ctx_f, ctx_s = {}, {}
        for _, c in stack:
            ctx_f.update(c["fields"]); ctx_s.update(c["scalars"])

        has_or = "||" in cond
        assigns, null_atom = [], False
        for atom in re.split(r"\|\||&&", cond):
            res = _atom_assignment(atom, ptr_name or "", struct_var or "", scalar_names)
            if not res:
                continue
            if res[0] == "null":
                null_atom = True
            elif res[0] != "notnull":
                assigns.append(res)   # (kind, target, tval, fval)

        def _mk(base_f, base_s, overrides):
            v = {"fields": dict(base_f), "scalars": dict(base_s), "null": False}
            for kind, tgt, val in overrides:
                (v["fields"] if kind == "field" else v["scalars"])[tgt] = val
            return v

        # 분기 진입(decision=참): 모든 원자 참
        all_true = [(k, t, tv) for (k, t, tv, fv) in assigns]
        merged_f, merged_s = dict(ctx_f), dict(ctx_s)
        for kind, tgt, val in all_true:
            (merged_f if kind == "field" else merged_s)[tgt] = val
        stack.append((indent, {"fields": merged_f, "scalars": merged_s}))

        if null_atom:
            raw.append({"fields": dict(ctx_f), "scalars": dict(ctx_s), "null": True})

        if has_or:
            # OR: 각 원자 단독 참(독립영향) + 모든 원자 거짓(decision=거짓)
            for k, t, tv, fv in assigns:
                raw.append(_mk(ctx_f, ctx_s, [(k, t, tv)]))
            raw.append(_mk(ctx_f, ctx_s, [(k, t, fv) for (k, t, tv, fv) in assigns]))
        else:
            # AND: 각 원자만 거짓(나머지 참, 독립영향)
            for i, (k, t, tv, fv) in enumerate(assigns):
                ov = [(kk, tt, (fv2 if j == i else tv2))
                      for j, (kk, tt, tv2, fv2) in enumerate(assigns)]
                raw.append(_mk(ctx_f, ctx_s, ov))
        raw.append({"fields": dict(merged_f), "scalars": dict(merged_s), "null": False})

    vectors, seen = [], set()
    for rv in raw:
        setup, args = [], []
        for idx, p in enumerate(params):
            n = p["name"]
            if idx == ptr_idx:
                if rv["null"]:
                    args.append("NULL")
                else:
                    setup.append(f"{ptr_type} {struct_var}; "
                                 f"memset(&{struct_var}, 0, sizeof({struct_var}));")
                    for field, val in rv["fields"].items():
                        setup.append(f"{struct_var}.{field} = {val};")
                    args.append(f"&{struct_var}")
            elif idx in basic_ptrs:
                buf = f"_buf{idx}"
                setup.append(f"int {buf}[8] = {{0}};")
                args.append(buf)
            else:
                args.append(str(rv["scalars"].get(n, "0")))
        key = (tuple(setup), tuple(args))
        if key in seen:
            continue
        seen.add(key)
        vectors.append({"setup": setup, "args": args})
        if len(vectors) >= 48:
            break
    return vectors or None


# ============================================================
# 4. harness.c 생성
# ============================================================
def _emit_harness(func_name: str, detail: dict, vectors: list[dict],
                  include_dirs: list[str], abs_src: str) -> str:
    L = ["/* SWTS auto-generated harness for Clang MC/DC */",
         "#include <stdio.h>", "#include <stddef.h>", "#include <string.h>", ""]

    # 소스에 대응하는 헤더 include
    src_stem = os.path.splitext(os.path.basename(abs_src))[0]
    for inc_dir in include_dirs:
        h = os.path.join(inc_dir, src_stem + ".h")
        if os.path.exists(h):
            L.append(f'#include "{h}"')
            break
    L.append("")

    # 피호출 함수 stub (올바른 시그니처 사용)
    sigs = detail["all_sigs"]
    for callee in sorted(detail["callees"]):
        sig = sigs.get(callee, {"ret": "int", "params": []})
        ret = sig["ret"]
        if not sig["params"]:
            plist = "void"
        else:
            parts = []
            for i, p in enumerate(sig["params"]):
                pname = p["name"] or f"_p{i}"
                parts.append(f"{p['type']} {pname}")
            plist = ", ".join(parts)
        if ret == "void":
            L.append(f"void {callee}({plist}) {{ }}")
        else:
            L.append(f"{ret} {callee}({plist}) {{ return ({ret})0; }}")
    L.append("")

    # main() — 테스트 벡터 기반 (각 TC 를 블록으로 감싸 변수 스코프 분리)
    # 반환값을 stdout 으로 출력 -> 상위에서 expected 실측값으로 파싱
    ret_type = (detail.get("ret_type") or "int").strip()
    is_void  = ret_type == "void"
    L.append("int main(void) {")
    for i, vec in enumerate(vectors, 1):
        L.append(f"  /* TC-{i:03d} */")
        L.append("  {")
        for stmt in vec["setup"]:
            L.append(f"    {stmt}")
        call_args = ", ".join(vec["args"])
        if is_void:
            L.append(f"    {func_name}({call_args});")
        else:
            L.append(f'    printf("__RET{i}=%lld\\n", '
                     f"(long long)({func_name}({call_args})));")
        L.append("  }")
    L.append("  return 0;")
    L.append("}")
    return "\n".join(L)


# ============================================================
# 5. 빌드
# ============================================================
def build(clang: str, harness: str, src_file: str,
          workdir: str, include_dirs: list[str]) -> str:
    # Windows 에서는 .exe 확장자를 명시해야 run/llvm-cov 가 같은 파일을 가리킴
    exe = os.path.join(workdir, "testbin.exe" if os.name == "nt" else "testbin")
    cmd = [
        clang, "-O0", "-g",
        "-fprofile-instr-generate",
        "-fcoverage-mapping",
        "-fcoverage-mcdc",
        "-o", exe,
        harness, src_file,
        "-Wno-everything",
    ]
    for inc in include_dirs:
        cmd += ["-I", inc]
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"clang build failed:\n{proc.stderr[-2000:]}")
    return exe


# ============================================================
# 6. 실행 + 프로파일 수집
# ============================================================
def run_collect(exe: str, workdir: str) -> tuple[str, str]:
    profraw = os.path.join(workdir, "run.profraw")
    env = os.environ.copy()
    env["LLVM_PROFILE_FILE"] = profraw
    proc = subprocess.run([exe], cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=30)
    return profraw, (proc.stdout or "")


# ============================================================
# 7. 프로파일 병합
# ============================================================
def merge_profile(llvm_profdata: str, profraw: str, workdir: str) -> str:
    profdata = os.path.join(workdir, "run.profdata")
    subprocess.run(
        [llvm_profdata, "merge", "-sparse", profraw, "-o", profdata],
        check=True, capture_output=True,
    )
    return profdata


# ============================================================
# 8. JSON 내보내기
# ============================================================
def export_cov(llvm_cov: str, exe: str, profdata: str) -> dict:
    proc = subprocess.run(
        [llvm_cov, "export", exe, f"-instr-profile={profdata}"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


# ============================================================
# 9. JSON 파싱 — MC/DC + branch + statement
# ============================================================
def parse_cov(cov_data: dict, src_basename: str) -> dict:
    files  = cov_data.get("data", [{}])[0].get("files", [])
    target = next(
        (f for f in files
         if os.path.basename(f.get("filename", "")) == src_basename),
        files[0] if files else {}
    )
    summ = target.get("summary", {})
    mcdc_sum  = summ.get("mcdc", {})
    br_sum    = summ.get("branches", {})
    stmt_sum  = summ.get("lines", {})

    # MC/DC 레코드 파싱 (어느 조건이 미충족인지)
    mcdc_records = []
    for r in target.get("mcdc_records", []):
        conds = r[-1] if isinstance(r[-1], list) else []
        mcdc_records.append({
            "line": r[0] if r else 0,
            "num_conditions": len(conds),
            "conditions_covered": conds,   # [True/False per condition]
            "covered": all(conds) if conds else False,
        })

    # 라인별 실행 횟수 (segments에서 추출)
    line_hits: dict[int, int] = {}
    prev_count = 0
    for seg in target.get("segments", []):
        # [line, col, count, has_count, is_region_entry, is_gap_region]
        if len(seg) >= 4:
            line, has_count, count = seg[0], seg[3], seg[2]
            if has_count:
                prev_count = count
            line_hits.setdefault(line, prev_count)

    mcdc_pct = round(mcdc_sum.get("percent", 0.0) if mcdc_sum.get("count", 0) > 0
                     else br_sum.get("percent", 0.0))
    return {
        "stmt_pct":       round(stmt_sum.get("percent", 0.0)),
        "branch_pct":     round(br_sum.get("percent", 0.0)),
        "mcdc_pct":       mcdc_pct,
        "mcdc_count":     mcdc_sum.get("count", 0),
        "mcdc_records":   mcdc_records,
        "line_hits":      line_hits,
    }


# ============================================================
# 10. 소스 라인 구성
# ============================================================
_BRANCH_KW = ("if ", "} else", "else if", "while ", "for ", "switch ", "case ")


def build_source(abs_src: str, start_ln: int, end_ln: int,
                 line_hits: dict[int, int]) -> list[dict]:
    try:
        with open(abs_src, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return []
    source = []
    for i, text in enumerate(all_lines, 1):
        if i < start_ln or i > end_ln:
            continue
        t = text.rstrip("\n")
        hits = line_hits.get(i)
        cov = "raw" if hits is None else ("full" if hits > 0 else "none")
        source.append({
            "n": i, "text": t, "cov": cov, "hits": max(hits or 0, 0), "tcs": [],
            "is_branch": t.strip().startswith(_BRANCH_KW),
        })
    return source


# ============================================================
# 11. 진입점
# ============================================================
def generate(unit_ref: str, abs_src: str, func_name: str,
             start_ln: int, end_ln: int,
             flags: list[str], include_dirs: list[str],
             log=lambda lvl, t: None) -> Optional[dict]:
    """
    Clang -fcoverage-mcdc 로 실행 기반 MC/DC 측정.
    도구 미설치 또는 빌드 실패 시 None 반환 (상위에서 정적 분석 폴백).
    """
    clang = find_clang()
    profdata_tool = find_llvm_profdata()
    cov_tool = find_llvm_cov()
    if not (clang and profdata_tool and cov_tool):
        missing = [n for n, v in [("clang", clang), ("llvm-profdata", profdata_tool),
                                   ("llvm-cov", cov_tool)] if not v]
        log("warn", f"[clang-mcdc] 미설치: {', '.join(missing)} -> 건너뜀")
        return None

    log("info", f"[clang-mcdc] 도구: {clang}")

    # AST 파싱에도 include 경로를 넘겨야 헤더 타입이 해석되어
    # 모든 분기/피호출 함수(callee)가 정확히 수집됨
    ast_flags = list(flags) + [f"-I{d}" for d in include_dirs]
    detail = _get_func_detail(abs_src, func_name, ast_flags)
    if not detail:
        log("warn", f"[clang-mcdc] {func_name} AST 추출 실패")
        return None

    # 함수 본문 라인 -> 조건 인식 벡터 생성 (실패 시 단순 조합으로 폴백)
    try:
        with open(abs_src, encoding="utf-8", errors="replace") as f:
            _all = f.readlines()
        body = [(i, _all[i - 1].rstrip("\n"))
                for i in range(start_ln, min(end_ln, len(_all)) + 1)]
    except OSError:
        body = []
    vectors = _smart_vectors(detail["params"], body) or _gen_vectors(detail["params"])
    log("info", f"[clang-mcdc] 테스트 벡터 {len(vectors)}개 생성")

    workdir = tempfile.mkdtemp(prefix="swts_mcdc_")
    try:
        # harness 생성
        harness_code = _emit_harness(func_name, detail, vectors, include_dirs, abs_src)
        harness_path = os.path.join(workdir, "harness.c")
        with open(harness_path, "w", encoding="utf-8") as f:
            f.write(harness_code)

        log("info", f"[clang-mcdc] 빌드: -fcoverage-mcdc + {os.path.basename(abs_src)}")
        exe = build(clang, harness_path, abs_src, workdir, include_dirs)

        log("info", "[clang-mcdc] 실행 + profraw 수집")
        profraw, stdout = run_collect(exe, workdir)
        # 하니스가 출력한 실측 반환값 파싱: __RET<i>=<value>
        ret_map: dict[int, int] = {}
        for line in stdout.splitlines():
            m = re.match(r"__RET(\d+)=(-?\d+)", line.strip())
            if m:
                ret_map[int(m.group(1))] = int(m.group(2))

        log("info", "[clang-mcdc] llvm-profdata merge")
        profdata = merge_profile(profdata_tool, profraw, workdir)

        log("info", "[clang-mcdc] llvm-cov export JSON")
        cov_data = export_cov(cov_tool, exe, profdata)

        src_basename = os.path.basename(abs_src)
        cov = parse_cov(cov_data, src_basename)
        log("info",
            f"[clang-mcdc] STMT {cov['stmt_pct']}% "
            f"| BR {cov['branch_pct']}% "
            f"| MC/DC {cov['mcdc_pct']}% ({cov['mcdc_count']} decisions)")

        source = build_source(abs_src, start_ln, end_ln, cov["line_hits"])

        # 자동 생성 TC 목록 (expected = 하니스 실측 반환값)
        cases = [
            {"id": f"TC-{i:03d}", "verdict": "manual",
             "desc": f"Auto vec #{i} - {', '.join(v['args'])}",
             "inputs": {p["name"]: a for p, a in zip(detail["params"], v["args"])},
             "expected": ({"return": ret_map[i]} if i in ret_map else {})}
            for i, v in enumerate(vectors, 1)
        ]

        uncovered = [r for r in cov["mcdc_records"] if not r["covered"]]
        return {
            "ok": True, "unit_ref": unit_ref, "mode": "clang-mcdc",
            "coverage": {
                "statement": cov["stmt_pct"],
                "branch":    cov["branch_pct"],
                "mcdc":      cov["mcdc_pct"],
            },
            "mcdc_records":  cov["mcdc_records"],
            "mcdc_count":    cov["mcdc_count"],
            "mcdc_uncovered": len(uncovered),
            "source": source,
            "cases": cases,
            "notes": [
                f"Clang {clang} -fcoverage-mcdc 실행 기반 측정 (ISO 26262 MC/DC)",
                f"MC/DC decisions: {cov['mcdc_count']}  |  "
                f"미충족: {len(uncovered)}개 -> 추가 TC 필요",
                "TC 입력값을 구체화하고 재생성하면 MC/DC% 향상 가능",
            ],
            "logs": [{"level": "info",
                      "text": (f"[clang-mcdc] {func_name}: "
                               f"STMT={cov['stmt_pct']}% "
                               f"BR={cov['branch_pct']}% "
                               f"MCDC={cov['mcdc_pct']}%")}],
        }
    except Exception as e:
        log("error", f"[clang-mcdc] 실패: {e}")
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
