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

    # 프로젝트 디렉터리(소스 폴더 + -I 인클루드 경로) — 시스템 헤더와 구분
    abs_src_norm = os.path.abspath(abs_path)
    proj_dirs = [os.path.normcase(os.path.dirname(abs_src_norm))]
    for fl in flags:
        if fl.startswith("-I") and len(fl) > 2:
            proj_dirs.append(os.path.normcase(os.path.abspath(fl[2:])))

    def _in_project(loc_file) -> bool:
        if not loc_file:
            return False
        p = os.path.normcase(os.path.abspath(loc_file.name))
        return any(p == d or p.startswith(d + os.sep) for d in proj_dirs)

    # 모든 함수 선언에서 시그니처 수집 (stub 생성용)
    #  - defined_funcs: 이 TU 에서 '정의'된 함수 (stub 금지 -> 중복정의)
    #  - proj_decls: 프로젝트 소스/헤더에 선언된 함수 (시스템 헤더 제외)
    #    -> 정의 안 된 것은 전부 stub (라이브러리 함수는 시스템 헤더라 제외됨)
    all_func_sigs: dict[str, dict] = {}
    defined_funcs: set = set()
    proj_decls: set = set()
    for node in tu.cursor.walk_preorder():
        if node.kind == cidx.CursorKind.FUNCTION_DECL and node.spelling:
            all_func_sigs[node.spelling] = {
                "ret": node.result_type.spelling,
                "params": [{"name": a.spelling or f"_p{i}",
                             "type": a.type.spelling}
                            for i, a in enumerate(node.get_arguments())],
            }
            if node.is_definition():
                defined_funcs.add(node.spelling)
            if _in_project(node.location.file):
                proj_decls.add(node.spelling)
    # 프로젝트에 선언됐으나 정의 안 된 함수 = 반드시 stub (직접/간접 호출 무관)
    stub_funcs = proj_decls - defined_funcs

    # 최상위 전역변수(외부 링키지) 수집 — 하니스에서 extern 으로 세팅 가능
    all_globals: dict[str, str] = {}
    for node in tu.cursor.get_children():
        if (node.kind == cidx.CursorKind.VAR_DECL and node.spelling
                and node.linkage == cidx.LinkageKind.EXTERNAL):
            all_globals[node.spelling] = node.type.spelling

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
                "defined_funcs": defined_funcs,
                "stub_funcs": stub_funcs,
                "all_globals": all_globals,
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
# 상수/매크로(대문자) 만 — 파생 스윕 값으로 안전하게 사용 가능
_SAFE_CONST = re.compile(r"^-?[A-Z0-9_][A-Z0-9_ +\-*/()]*$")


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


def _classify_lhs(lhs, tval, fval, ptr_name, struct_var, scalar_names, globals_map):
    lhs = lhs.strip()
    if ptr_name and lhs.startswith(ptr_name + "->"):
        field = lhs[len(ptr_name) + 2:].strip()
        if re.match(r"^[A-Za-z_]\w*$", field):
            return ("field", field, tval, fval)
        return None
    if lhs in scalar_names:
        return ("scalar", lhs, tval, fval)
    if lhs in globals_map:               # 전역변수 -> 하니스에서 extern 으로 세팅
        return ("global", lhs, tval, fval)
    return None  # 미지 -> 건너뜀 (설정 불가)


def _strip_outer_parens(s: str) -> str:
    """전체를 감싸는 균형 괄호 한 쌍만 제거 (중첩 괄호 보존).
       예) '(a & (B|C))' -> 'a & (B|C)' (안쪽 ')' 를 떼지 않음)"""
    s = s.strip()
    while len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        depth = 0
        wraps = True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    wraps = False
                    break
        if wraps:
            s = s[1:-1].strip()
        else:
            break
    return s


def _local_deps(body, ptr_name, scalar_names, stub_names):
    """로컬변수 -> 제어 가능한 base 집합. base=(kind,target):
       'scalar'(param) / 'field'(struct) / 'sret'(스텁 반환).
       예) int raw=read_sensor(); int kmh=raw*36/10; -> kmh 는 read_sensor 반환에 의존."""
    assigns_of: dict = {}    # local -> [rhs, ...] (모든 대입)
    assign_re = re.compile(r"^(?:[A-Za-z_][\w\s\*]*?\s)?([A-Za-z_]\w*)\s*=\s*([^=].*?);")
    for _ln, text in body:
        m = assign_re.match(text.strip())
        if m:
            assigns_of.setdefault(m.group(1), []).append(m.group(2))

    ident_re = re.compile(r"[A-Za-z_]\w*")
    field_re = re.compile(rf"{re.escape(ptr_name)}\s*->\s*(\w+)") if ptr_name else None
    call_re  = re.compile(r"([A-Za-z_]\w*)\s*\(")
    cache: dict = {}

    def resolve(local, seen):
        if local in cache:
            return cache[local]
        if local not in assigns_of or local in seen:
            return set()
        seen.add(local)
        bases: set = set()
        for rhs in assigns_of[local]:
            for cm in call_re.finditer(rhs):
                if cm.group(1) in stub_names:
                    bases.add(("sret", f"__sret_{cm.group(1)}"))
            # 함수호출 인자목록 제거 -> 호출 인자(sensor_id 등)를 의존성으로 오인 방지
            work = re.sub(r"[A-Za-z_]\w*\s*\([^()]*\)", " ", rhs)
            if field_re:
                for fm in field_re.finditer(work):
                    bases.add(("field", fm.group(1)))
            for im in ident_re.finditer(work):
                nm = im.group(0)
                if nm in scalar_names:
                    bases.add(("scalar", nm))
                elif nm in assigns_of and nm != local:
                    bases |= resolve(nm, seen)
        cache[local] = bases
        return bases

    return {lc: resolve(lc, set()) for lc in assigns_of}


def _atom_assignment(atom, ptr_name, struct_var, scalar_names, globals_map):
    """원자 조건 1개 -> (kind, target, true_value, false_value)."""
    a = _strip_outer_parens(atom.strip())
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
        return _classify_lhs(lhs, tval, fval, ptr_name, struct_var, scalar_names, globals_map)
    m = re.match(r"^(.+?)\s*&\s*(.+)$", a)        # 비트 AND: lhs & mask
    if m:
        return _classify_lhs(m.group(1).strip(), m.group(2).strip(), "0",
                             ptr_name, struct_var, scalar_names, globals_map)
    if a.startswith("!"):                          # !x  -> true:0 false:1
        return _classify_lhs(a[1:].strip(), "0", "1", ptr_name, struct_var, scalar_names, globals_map)
    return _classify_lhs(a, "1", "0", ptr_name, struct_var, scalar_names, globals_map)  # x -> true:1


def _smart_vectors(params: list[dict], body: list[tuple],
                   globals_map: dict, stub_rets: dict,
                   const_map: dict | None = None) -> tuple:
    """함수 본문 분기 조건을 분석해 도달성 높은 테스트 벡터 생성.
       stub_rets: {정수형 스텁함수: 반환타입} — 반환값을 극단값으로 쓸어
       스텁이 만든 로컬변수에 의존하는 분기(raw<0, kmh>MAX 등)를 커버.
       const_map: {매크로/enum: 정수값} — 주어지면 Z3 미니-ATG 로 MC/DC
       독립쌍을 풀어 입력 벡터를 추가(가산식).
       반환: (vectors, used_globals{name:type}) — 실패 시 (None, {})."""
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
    if ptr_name is None and not scalar_names and not stub_rets:
        return None, {}
    struct_var = f"_s{ptr_idx}" if ptr_idx >= 0 else None
    sret_names = [f"__sret_{f}" for f in stub_rets]
    # 로컬변수 의존성 (delta=t-duty, hottest=read_temp_raw() 등 추적)
    local_deps = _local_deps(body, ptr_name or "", scalar_names, set(stub_rets))

    branch_re  = re.compile(r"^\}?\s*(?:else\s+if|if|while|for)\s*\((.*)\)\s*\{?\s*$")
    dowhile_re = re.compile(r"^\}?\s*while\s*\((.*)\)\s*;\s*$")   # do-while 종료조건
    switch_re  = re.compile(r"^switch\s*\((.*)\)\s*\{?\s*$")
    case_re    = re.compile(r"^case\s+(.+?)\s*:.*$")
    default_re = re.compile(r"^default\s*:.*$")

    exit_re = re.compile(r"\b(return|break|continue|goto)\b")
    texts = [t for _l, t in body]

    def _is_guard(i):
        """body[i] 의 if 가 가드(본문이 return/break 등으로 탈출)인지."""
        if exit_re.search(texts[i]):       # 한 줄 가드: if (G) return x;
            return True
        base = len(texts[i]) - len(texts[i].lstrip())
        for j in range(i + 1, min(i + 4, len(texts))):
            s = texts[j].strip()
            if not s or s in ("{", "}"):
                continue
            ind = len(texts[j]) - len(texts[j].lstrip())
            if ind <= base:                # 본문 끝(같거나 얕은 들여쓰기)
                return False
            return bool(exit_re.search(s))
        return False

    events = []   # (indent, kind, payload, is_guard)
    case_vals: dict = {}   # switch expr -> set(case values) (default 회피용)
    for i, text in enumerate(texts):
        st = text.strip()
        indent = len(text) - len(text.lstrip())
        m = branch_re.match(st)
        if m:
            is_loop = st.startswith(("while", "for"))
            events.append((indent, "branch", m.group(1).strip(),
                           _is_guard(i) and not is_loop))
            continue
        m = dowhile_re.match(st)        # } while(cond); -> 분기로 취급(루프 종료 결정)
        if m:
            events.append((indent, "branch", m.group(1).strip(), False))
            continue
        m = switch_re.match(st)
        if m:
            events.append((indent, "switch", m.group(1).strip(), False)); continue
        m = case_re.match(st)
        if m:
            events.append((indent, "case", m.group(1).strip(), False)); continue
        if default_re.match(st):
            events.append((indent, "default", None, False)); continue
    if not events:
        return None, {}

    used_globals: dict = {}
    _bucket = {"field": "fields", "scalar": "scalars",
               "global": "globals", "sret": "srets"}

    def _blank():
        return {"fields": {}, "scalars": {}, "globals": {}, "srets": {}, "null": False}

    def _mk(base, overrides):
        v = {"fields": dict(base["fields"]), "scalars": dict(base["scalars"]),
             "globals": dict(base["globals"]), "srets": dict(base.get("srets", {})),
             "null": False}
        for kind, tgt, val in overrides:
            v[_bucket[kind]][tgt] = val
        return v

    def _frame(indent, assigns_dict=None, sw=None):
        fr = {"indent": indent, "fields": {}, "scalars": {},
              "globals": {}, "srets": {}, "sw": sw}
        if assigns_dict:
            for kk in ("fields", "scalars", "globals", "srets"):
                fr[kk].update(assigns_dict.get(kk, {}))
        return fr

    def _accum(stk):
        ctx = {"fields": {}, "scalars": {}, "globals": {}, "srets": {}}
        for fr in stk:
            for kk in ctx:
                ctx[kk].update(fr[kk])
        return ctx

    raw = [_blank()]                       # baseline
    if ptr_name:
        nullv = _blank(); nullv["null"] = True
        raw.append(nullv)
    # 스텁 반환값 스윕: 큰 음수/큰 양수로 -> 부호·임계 양방향 파생 분기 커버
    for sname in sret_names:
        for val in ("-99999", "99999"):
            sv = _blank(); sv["srets"][sname] = val
            raw.append(sv)

    # 가드 통과 전제조건 (early-return 가드의 부정) — 이후 모든 벡터의 base
    precond = {"fields": {}, "scalars": {}, "globals": {}, "srets": {}}

    rel_seeds: list = []   # (base_ctx, kind, target, rhs) — 관계비교 경계값 시딩용
    stack: list = []   # list of frames
    for indent, kind, payload, is_guard in events:
        # case/default 는 같은 들여쓰기의 감싸는 switch 프레임을 보존
        if kind in ("case", "default"):
            while stack and stack[-1]["indent"] >= indent:
                if stack[-1].get("sw") and stack[-1]["indent"] <= indent:
                    break
                stack.pop()
        else:
            while stack and stack[-1]["indent"] >= indent:
                stack.pop()
        ctx = {kk: dict(precond[kk]) for kk in precond}    # 전제조건을 base 로
        acc = _accum(stack)
        for kk in ctx:
            ctx[kk].update(acc[kk])

        # ── switch: 자식 case 가 세팅할 변수(field/scalar/global) 결정 ──
        if kind == "switch":
            sw = None
            cl = _classify_lhs(payload, "0", "0", ptr_name or "",
                               struct_var or "", scalar_names, globals_map)
            if cl and cl[0] in _bucket:
                sw = (cl[0], cl[1])
                if cl[0] == "global":
                    used_globals[cl[1]] = globals_map.get(cl[1], "int")
            stack.append(_frame(indent, sw=sw))
            continue

        # ── case / default: switch 변수를 해당 값으로 세팅 ──
        if kind in ("case", "default"):
            sw = next((fr["sw"] for fr in reversed(stack) if fr.get("sw")), None)
            ov = []
            if sw:
                if kind == "case" and payload:
                    ov = [(sw[0], sw[1], payload)]
                    case_vals.setdefault((sw[0], sw[1]), set()).add(payload)
                elif kind == "default":
                    ov = [(sw[0], sw[1], "0x7FFF")]   # 어떤 case 와도 안 겹치게
            raw.append(_mk(ctx, ov))                  # 케이스 진입 벡터
            fr = _frame(indent)
            for k, t, val in ov:
                fr[_bucket[k]][t] = val
            stack.append(fr)
            continue

        # ── branch (if / else if / while / for) ──
        cond = payload
        has_or = "||" in cond
        assigns, null_atom, derived = [], False, []
        for atom in re.split(r"\|\||&&", cond):
            # 관계비교(field/scalar/global op CONST) -> 경계값 시딩 대상으로 수집
            _cm = _CMP_RE.match(_strip_outer_parens(atom.strip()))
            if _cm and _cm.group(2) in ("<", ">", "<=", ">="):
                _rhs = _cm.group(3).strip()
                _cl = _classify_lhs(_cm.group(1).strip(), "", "", ptr_name or "",
                                    struct_var or "", scalar_names, globals_map)
                if _cl and _cl[0] in _bucket and _SAFE_CONST.match(_rhs):
                    rel_seeds.append((ctx, _cl[0], _cl[1], _rhs))
            res = _atom_assignment(atom, ptr_name or "", struct_var or "",
                                   scalar_names, globals_map)
            if res:
                if res[0] == "null":
                    null_atom = True
                elif res[0] != "notnull":
                    if res[0] == "global":
                        used_globals[res[1]] = globals_map.get(res[1], "int")
                    assigns.append(res)   # (kind, target, tval, fval)
                continue
            # 직접 분류 실패 -> 로컬변수에서 파생된 조건인지 확인
            cm = _CMP_RE.match(_strip_outer_parens(atom.strip()))
            if cm and cm.group(1).strip() in local_deps:
                rrhs = cm.group(3).strip()
                for (bk, bt) in local_deps[cm.group(1).strip()]:
                    derived.append((bk, bt, rrhs))
                    if bk == "global":
                        used_globals[bt] = globals_map.get(bt, "int")

        merged = _mk(ctx, [(k, t, tv) for (k, t, tv, fv) in assigns])
        stack.append(_frame(indent, merged))

        if null_atom:
            nv = _mk(ctx, []); nv["null"] = True
            raw.append(nv)
        if has_or:
            for k, t, tv, fv in assigns:
                raw.append(_mk(ctx, [(k, t, tv)]))
            raw.append(_mk(ctx, [(k, t, fv) for (k, t, tv, fv) in assigns]))
        else:
            for i, (k, t, tv, fv) in enumerate(assigns):
                ov = [(kk, tt, (fv2 if j == i else tv2))
                      for j, (kk, tt, tv2, fv2) in enumerate(assigns)]
                raw.append(_mk(ctx, ov))
        # 파생 로컬: base 변수를 극단값 + (안전한)조건 임계값으로 스윕 (양방향 도달)
        # rrhs 는 상수/매크로일 때만 사용 (m->x, 파라미터 참조는 하니스 스코프에 없음)
        for bk, bt, rrhs in derived:
            vals = ["-99999", "99999"]
            if _SAFE_CONST.match(rrhs):
                vals.append(rrhs)
            for val in vals:
                raw.append(_mk(ctx, [(bk, bt, val)]))
        raw.append(_mk(merged, []))

        # early-return 가드면, 그 부정(통과 전제조건)을 이후 벡터 base 에 누적.
        # OR/단일조건 가드만(부정이 '모든 원자 거짓'으로 정확) — AND 가드는 모호해 제외.
        if is_guard and "&&" not in cond and assigns:
            for k, t, tv, fv in assigns:
                precond[_bucket[k]][t] = fv

    # ── 관계비교 변수 경계값 시딩 ──
    # 단일 if 뿐 아니라 do-while/복합조건의 재평가(본문이 값을 변형한 뒤 재검사)까지
    # 커버하도록, 비교 상수의 ±2배 너머 값을 함께 주입한다(래핑 루프 등).
    for base_ctx, kind, target, rhs in rel_seeds:
        for val in (f"((2*({rhs}))+9)", f"(({rhs})+1)",
                    f"(({rhs})-1)", f"((-2*({rhs}))-9)"):
            raw.append(_mk(base_ctx, [(kind, target, val)]))

    # ── LDRA식 Z3 미니-ATG: 결정→진리표→MC/DC 독립쌍→Z3 입력 역산 (가산) ──
    agg_globals: set = set()   # 집계(struct/union/array) 전역 base — 0 리셋 제외
    try:
        import swts_mcdc_atg
        if swts_mcdc_atg.Z3_OK:
            for a in swts_mcdc_atg.generate(body, const_map or {}, ptr_name or "",
                                            scalar_names, globals_map,
                                            set(stub_rets)):
                for g in a.get("globals", {}):
                    used_globals.setdefault(g, globals_map.get(g, "int"))
                for base, gtype in a.get("_extern", {}).items():
                    used_globals.setdefault(base, gtype)
                    agg_globals.add(base)
                raw.append(a)
    except Exception:
        pass

    vectors, seen = [], set()
    for rv in raw:
        setup, args = [], []
        # 전역: 스칼라는 매 TC 0 리셋(누수 방지), 집계 전역은 멤버 경로로만 대입
        for g in used_globals:
            if g in rv["globals"]:
                setup.append(f"{g} = {rv['globals'][g]};")
            elif g not in agg_globals:
                setup.append(f"{g} = 0;")
        for gpath, gval in rv.get("global_lv", {}).items():
            setup.append(f"{gpath} = {gval};")
        for sname in sret_names:
            setup.append(f"{sname} = {rv.get('srets', {}).get(sname, '0')};")
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
        # 스텁 반환값(기본 0 아님)을 입력 표시용으로 노출
        srt = {f"{f}()": rv["srets"].get(s, "0")
               for f, s in ((g, f"__sret_{g}") for g in stub_rets)
               if rv["srets"].get(s, "0") != "0"}
        vectors.append({"setup": setup, "args": args, "srets": srt})
        if len(vectors) >= 96:
            break
    return (vectors or None), used_globals


# ============================================================
# 4. harness.c 생성
# ============================================================
def _emit_harness(func_name: str, detail: dict, vectors: list[dict],
                  globals_used: dict, include_dirs: list[str], abs_src: str) -> str:
    L = ["/* SWTS auto-generated harness for Clang MC/DC */",
         "#include <stdio.h>", "#include <stddef.h>", "#include <string.h>",
         "#include <stdlib.h>", "#include <stdint.h>", ""]

    # 소스에 대응하는 헤더 include
    src_stem = os.path.splitext(os.path.basename(abs_src))[0]
    for inc_dir in include_dirs:
        h = os.path.join(inc_dir, src_stem + ".h")
        if os.path.exists(h):
            L.append(f'#include "{h}"')
            break
    L.append("")

    # 소스 파일의 object-like 매크로를 하니스에도 정의
    # (파생 스윕 값이 .c 내부 #define 매크로일 수 있음 — 별도 TU 라 안 보임)
    try:
        with open(abs_src, encoding="utf-8", errors="replace") as _f:
            for _line in _f:
                dm = re.match(r"\s*#\s*define\s+([A-Za-z_]\w*)\s+(\S.*?)\s*$", _line)
                if dm:   # object-like (이름 뒤 공백) 만; 함수형 매크로는 미매치
                    L.append(f"#ifndef {dm.group(1)}")
                    L.append(f"#define {dm.group(1)} {dm.group(2)}")
                    L.append("#endif")
        L.append("")
    except OSError:
        pass

    # 조건에서 참조된 전역변수 — extern 으로 선언해 하니스에서 세팅
    for g, gtype in (globals_used or {}).items():
        L.append(f"extern {gtype} {g};")
    if globals_used:
        L.append("")

    # 미정의 함수 stub — 소스에 선언만 되고 정의 안 된 함수 전부
    # (간접 호출 대비). 정의된 함수는 소스에서 링크되므로 제외.
    sigs = detail["all_sigs"]
    stub_targets = detail.get("stub_funcs")
    if stub_targets is None:   # 폴백: 직접 callee 중 미정의
        stub_targets = detail["callees"] - detail.get("defined_funcs", set())
    for callee in sorted(stub_targets):
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
        elif "*" in ret:
            # 포인터 반환 스텁 -> NULL 고정 (잘못된 포인터 역참조 방지)
            L.append(f"{ret} {callee}({plist}) {{ return ({ret})0; }}")
        else:
            # 정수형 반환 스텁 -> 세팅 가능한 전역 반환 (분기 제어용)
            L.append(f"{ret} __sret_{callee} = ({ret})0;")
            L.append(f"{ret} {callee}({plist}) {{ return __sret_{callee}; }}")
    L.append("")

    # main() — 테스트 벡터 기반 (각 TC 를 블록으로 감싸 변수 스코프 분리)
    # 반환값을 stdout 으로 출력 -> 상위에서 expected 실측값으로 파싱
    ret_type = (detail.get("ret_type") or "int").strip()
    is_void  = ret_type == "void"
    # argv[1] 이 주어지면 해당 TC 만 실행(TC별 커버리지 측정용), 없으면 전체
    L.append("int main(int argc, char** argv) {")
    L.append("  int __only = (argc > 1) ? atoi(argv[1]) : 0;")
    for i, vec in enumerate(vectors, 1):
        L.append(f"  /* TC-{i:03d} */")
        L.append(f"  if (__only == 0 || __only == {i}) {{")
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


def probe_consts(symbols, abs_src: str, include_dirs: list[str],
                 clang: str) -> dict:
    """조건에 등장하는 대문자 상수/매크로/enum 값을 clang 컴파일+실행으로 해석.
    {SYM: int} 반환. 실패 시 {} (ATG 는 그래도 일부 조건은 풀거나 건너뜀)."""
    syms = sorted(symbols or [])
    if not clang or not syms:
        return {}
    wd = tempfile.mkdtemp(prefix="swts_probe_")
    try:
        L = ["#include <stdio.h>", "#include <stdint.h>"]
        src_stem = os.path.splitext(os.path.basename(abs_src))[0]
        seen_h = set()
        for inc in include_dirs:
            for h in (src_stem + ".h", "hw_api.h"):
                hp = os.path.join(inc, h)
                if hp not in seen_h and os.path.exists(hp):
                    L.append(f'#include "{hp}"')
                    seen_h.add(hp)
        # .c 의 object-like 매크로도 주입(헤더에 없는 .c 로컬 매크로 대비)
        try:
            with open(abs_src, encoding="utf-8", errors="replace") as f:
                for line in f:
                    dm = re.match(r"\s*#\s*define\s+([A-Za-z_]\w*)\s+(\S.*?)\s*$", line)
                    if dm:
                        L.append(f"#ifndef {dm.group(1)}")
                        L.append(f"#define {dm.group(1)} {dm.group(2)}")
                        L.append("#endif")
        except OSError:
            pass
        head = "\n".join(L)
        probe_c = os.path.join(wd, "probe.c")
        exe = os.path.join(wd, "probe.exe" if os.name == "nt" else "probe")
        cmd = [clang, "-O0", "-o", exe, probe_c, "-Wno-everything"]
        for inc in include_dirs:
            cmd += ["-I", inc]
        # 미정의 식별자(enum 아닌 비상수 등)는 컴파일러 에러에서 추출해 제거 후 재시도
        cur = list(syms)
        r = None
        for _attempt in range(4):
            prog = head + "\nint main(void){\n" + "".join(
                f'  printf("{s}=%lld\\n",(long long)({s}));\n' for s in cur
            ) + "  return 0; }\n"
            with open(probe_c, "w", encoding="utf-8") as f:
                f.write(prog)
            try:
                p = subprocess.run(cmd, cwd=wd, capture_output=True, text=True, timeout=40)
            except Exception:
                return {}
            if p.returncode == 0:
                try:
                    r = subprocess.run([exe], cwd=wd, capture_output=True,
                                       text=True, timeout=10)
                except Exception:
                    return {}
                break
            bad = set(re.findall(r"undeclared identifier '([A-Za-z_]\w*)'", p.stderr))
            bad |= set(re.findall(r"undeclared\b.*?'([A-Za-z_]\w*)'", p.stderr))
            cur = [s for s in cur if s not in bad]
            if not bad or not cur:
                return {}
        if r is None:
            return {}
        out = {}
        for line in r.stdout.splitlines():
            m = re.match(r"^([A-Za-z_]\w*)=(-?\d+)$", line.strip())
            if m:
                out[m.group(1)] = int(m.group(2))
        return out
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ============================================================
# 6. 실행 + 프로파일 수집
# ============================================================
def run_collect(exe: str, workdir: str, tc: Optional[int] = None,
                profname: str = "run.profraw") -> tuple[str, str]:
    profraw = os.path.join(workdir, profname)
    env = os.environ.copy()
    env["LLVM_PROFILE_FILE"] = profraw
    cmd = [exe] + ([str(tc)] if tc else [])
    proc = subprocess.run(cmd, cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=30)
    return profraw, (proc.stdout or "")


# ============================================================
# 7. 프로파일 병합
# ============================================================
def merge_profile(llvm_profdata: str, profraw, workdir: str,
                  out: str = "run.profdata") -> str:
    profdata = os.path.join(workdir, out)
    inputs = profraw if isinstance(profraw, (list, tuple)) else [profraw]
    subprocess.run(
        [llvm_profdata, "merge", "-sparse", *inputs, "-o", profdata],
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
def _parse_mcdc_records(raw_records: list) -> list:
    out = []
    for r in raw_records:
        # 레코드: [L1,C1,L2,C2,..,kind, [조건별 독립영향 bool], [테스트벡터 dict]]
        # 독립영향 리스트 = '불리언으로 이뤄진 리스트' (dict 리스트인 마지막과 구분)
        conds = []
        for el in r if isinstance(r, list) else []:
            if isinstance(el, list) and el and isinstance(el[0], bool):
                conds = el
                break
        out.append({
            "line": r[0] if r else 0,
            "num_conditions": len(conds),
            "conditions_covered": conds,
            "covered": bool(conds) and all(conds),
        })
    return out


def parse_cov(cov_data: dict, src_basename: str,
              func_name: Optional[str] = None,
              start_ln: Optional[int] = None,
              end_ln: Optional[int] = None) -> dict:
    data0  = cov_data.get("data", [{}])[0]
    files  = data0.get("files", [])
    target = next(
        (f for f in files
         if os.path.basename(f.get("filename", "")) == src_basename),
        files[0] if files else {}
    )

    # 라인별 실행 횟수 (segments에서 추출) — 소스뷰어 색칠용 (파일 전체)
    line_hits: dict[int, int] = {}
    prev_count = 0
    for seg in target.get("segments", []):
        # [line, col, count, has_count, is_region_entry, is_gap_region]
        if len(seg) >= 4:
            line, has_count, count = seg[0], seg[3], seg[2]
            if has_count:
                prev_count = count
            line_hits.setdefault(line, prev_count)

    # ── 함수 단위 스코핑 (가능하면) — 대상 함수만의 STMT/BR/MC/DC ──
    fn = None
    if func_name:
        fn = next((g for g in data0.get("functions", [])
                   if g.get("name") == func_name), None)
    if fn is not None:
        branches = fn.get("branches", [])
        # branch entry: [L1,C1,L2,C2, trueCount, falseCount, ...]
        br_total = len(branches)
        br_cov = sum(1 for b in branches if len(b) >= 6 and b[4] > 0 and b[5] > 0)
        branch_pct = round(100 * br_cov / br_total) if br_total else 100

        mcdc_records = _parse_mcdc_records(fn.get("mcdc_records", []))
        mcdc_total = len(mcdc_records)
        mcdc_cov = sum(1 for r in mcdc_records if r["covered"])
        mcdc_pct = round(100 * mcdc_cov / mcdc_total) if mcdc_total else branch_pct

        # STMT: 함수 라인 범위의 실행 라인 비율
        if start_ln is not None and end_ln is not None:
            rng = {ln: c for ln, c in line_hits.items() if start_ln <= ln <= end_ln}
        else:
            rng = line_hits
        stmt_total = len(rng)
        stmt_cov = sum(1 for c in rng.values() if c > 0)
        stmt_pct = round(100 * stmt_cov / stmt_total) if stmt_total else 100

        return {
            "stmt_pct": stmt_pct, "branch_pct": branch_pct, "mcdc_pct": mcdc_pct,
            "mcdc_count": mcdc_total, "mcdc_records": mcdc_records,
            "line_hits": line_hits,
        }

    # ── 폴백: 파일 전체 summary ──
    summ = target.get("summary", {})
    mcdc_sum, br_sum, stmt_sum = (summ.get("mcdc", {}), summ.get("branches", {}),
                                  summ.get("lines", {}))
    mcdc_pct = round(mcdc_sum.get("percent", 0.0) if mcdc_sum.get("count", 0) > 0
                     else br_sum.get("percent", 0.0))
    return {
        "stmt_pct":     round(stmt_sum.get("percent", 0.0)),
        "branch_pct":   round(br_sum.get("percent", 0.0)),
        "mcdc_pct":     mcdc_pct,
        "mcdc_count":   mcdc_sum.get("count", 0),
        "mcdc_records": _parse_mcdc_records(target.get("mcdc_records", [])),
        "line_hits":    line_hits,
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
# 10-b. 유닛별 측정 캐시 (체크박스 토글 재계산용)
# ============================================================
_COV_CACHE: dict = {}   # unit_ref -> {workdir, exe, ...}
_COV_CACHE_MAX = 8


def _cache_unit(unit_ref: str, meta: dict) -> None:
    """유닛 측정 결과 보존. 오래된 항목은 워크디렉터리 정리 후 제거."""
    old = _COV_CACHE.pop(unit_ref, None)
    if old and old.get("workdir"):
        shutil.rmtree(old["workdir"], ignore_errors=True)
    _COV_CACHE[unit_ref] = meta
    while len(_COV_CACHE) > _COV_CACHE_MAX:
        _k, _v = next(iter(_COV_CACHE.items()))
        _COV_CACHE.pop(_k, None)
        if _v.get("workdir"):
            shutil.rmtree(_v["workdir"], ignore_errors=True)


def recompute(unit_ref: str, tc_ids: list) -> Optional[dict]:
    """선택된 TC 들만 병합해 커버리지 재측정 (정확한 STMT/BR/MC/DC)."""
    meta = _COV_CACHE.get(unit_ref)
    if not meta:
        return None
    sel = sorted({int(re.sub(r"\D", "", str(t))) for t in tc_ids if re.search(r"\d", str(t))})
    sel = [i for i in sel if 1 <= i <= meta["n_tcs"]]
    wd = meta["workdir"]
    if not sel:   # 아무것도 선택 안 함 -> 0%
        src = build_source(meta["abs_src"], meta["start_ln"], meta["end_ln"], {})
        return {"ok": True, "coverage": {"statement": 0, "branch": 0, "mcdc": 0},
                "mcdc_records": [], "source": src}
    profraws = [os.path.join(wd, f"tc_{i}.profraw") for i in sel
                if os.path.exists(os.path.join(wd, f"tc_{i}.profraw"))]
    if not profraws:
        return None
    try:
        pd = merge_profile(meta["profdata_tool"], profraws, wd, out="sel.profdata")
        cdata = export_cov(meta["cov_tool"], meta["exe"], pd)
        cov = parse_cov(cdata, meta["src_basename"], meta.get("func_name"),
                        meta["start_ln"], meta["end_ln"])
    except Exception:
        return None
    source = build_source(meta["abs_src"], meta["start_ln"], meta["end_ln"], cov["line_hits"])
    return {
        "ok": True,
        "coverage": {"statement": cov["stmt_pct"], "branch": cov["branch_pct"],
                     "mcdc": cov["mcdc_pct"]},
        "mcdc_records": cov["mcdc_records"],
        "source": source,
    }


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
    # 정수형 반환 스텁 함수 중 '대상 함수가 실제 호출'하는 것만 스윕
    # (호출 안 하는 센서까지 스윕하면 불필요한 TC 가 생김)
    _sigs = detail.get("all_sigs", {})
    _sweep_set = detail.get("stub_funcs", set()) & detail.get("callees", set())
    stub_rets = {f: _sigs[f]["ret"] for f in _sweep_set
                 if f in _sigs and _sigs[f]["ret"] not in ("void",)
                 and "*" not in _sigs[f]["ret"]}
    # 조건에 쓰인 매크로/enum 값을 clang 으로 해석 -> Z3 미니-ATG 입력
    const_map = {}
    try:
        import swts_mcdc_atg
        if swts_mcdc_atg.Z3_OK:
            const_map = probe_consts(swts_mcdc_atg.collect_symbols(body),
                                     abs_src, include_dirs, clang)
    except Exception:
        const_map = {}
    vectors, globals_used = _smart_vectors(
        detail["params"], body, detail.get("all_globals", {}), stub_rets,
        const_map)
    if not vectors:
        vectors, globals_used = _gen_vectors(detail["params"]), {}
    log("info", f"[clang-mcdc] 테스트 벡터 {len(vectors)}개 생성"
                + (f" (전역 {len(globals_used)}개 세팅)" if globals_used else "")
                + (f" · Z3-ATG 상수 {len(const_map)}개" if const_map else ""))

    workdir = tempfile.mkdtemp(prefix="swts_mcdc_")
    keep_workdir = False
    try:
        # harness 생성
        harness_code = _emit_harness(func_name, detail, vectors,
                                     globals_used, include_dirs, abs_src)
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
        cov = parse_cov(cov_data, src_basename, func_name, start_ln, end_ln)
        log("info",
            f"[clang-mcdc] STMT {cov['stmt_pct']}% "
            f"| BR {cov['branch_pct']}% "
            f"| MC/DC {cov['mcdc_pct']}% ({cov['mcdc_count']} decisions)")

        source = build_source(abs_src, start_ln, end_ln, cov["line_hits"])

        # ── TC별 profraw 생성 (체크박스 토글 시 recompute 로 정확 재측정) ──
        log("info", f"[clang-mcdc] TC별 profraw 생성 ({len(vectors)}개)")
        for i in range(1, len(vectors) + 1):
            try:
                run_collect(exe, workdir, tc=i, profname=f"tc_{i}.profraw")
            except Exception:
                pass

        # 재계산(recompute)용으로 워크디렉터리/메타 캐시 보존
        keep_workdir = True
        _cache_unit(unit_ref, {
            "workdir": workdir, "exe": exe, "src_basename": src_basename,
            "func_name": func_name,
            "start_ln": start_ln, "end_ln": end_ln, "abs_src": abs_src,
            "n_tcs": len(vectors), "profdata_tool": profdata_tool,
            "cov_tool": cov_tool,
        })

        # 자동 생성 TC 목록 (expected = 하니스 실측 반환값)
        cases = [
            {"id": f"TC-{i:03d}", "verdict": "manual",
             "desc": f"Auto vec #{i} - {', '.join(v['args'])}",
             "inputs": {**{p["name"]: a for p, a in zip(detail["params"], v["args"])},
                        **v.get("srets", {})},
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
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
