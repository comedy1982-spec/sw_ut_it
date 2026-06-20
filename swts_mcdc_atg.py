"""LDRA식 미니 ATG (Automatic Test-vector Generation)
====================================================
결정(decision) → 원자조건 → 진리표 → MC/DC 독립쌍 → Z3 로 입력 역산.

직접 설정 가능한 입력(파라미터 스칼라 / 구조체 필드 / 전역 / 스텁 반환)에 대한
정수 선형비교·비트마스크 조건을 Z3 제약 솔버로 풀어, MC/DC 를 충족하는
구체 입력 벡터를 생성한다.

풀 수 없는 조건(배열 파생, 루프 본문 변형, 복잡 결합 등)은 건너뛴다.
출력은 _smart_vectors 와 동일한 assignment 형식(fields/scalars/globals/srets/null)
이라, 기존 벡터에 '추가'만 되므로 커버리지 회귀가 발생하지 않는다(가산식).
"""
from __future__ import annotations
import re
from itertools import product

try:
    import z3
    Z3_OK = True
except Exception:
    Z3_OK = False


# ============================================================
# C 식 토크나이저 / 파서 (조건식·로컬 대입 우변 해석용)
# ============================================================
_OPS2 = ("->", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||")
_OPS1 = set("-+*/%&|^~!<>()[],")


def _tokenize(s):
    toks, i, n = [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch.isspace():
            i += 1; continue
        if ch.isdigit():
            j = i
            if s[i:i + 2].lower() == "0x":
                j = i + 2
                while j < n and s[j] in "0123456789abcdefABCDEF":
                    j += 1
                val = int(s[i:j], 16)
            else:
                while j < n and s[j].isdigit():
                    j += 1
                val = int(s[i:j])
            while j < n and s[j] in "uUlL":
                j += 1
            toks.append(("num", val)); i = j; continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            toks.append(("id", s[i:j])); i = j; continue
        if s[i:i + 2] in _OPS2:
            toks.append(("op", s[i:i + 2])); i += 2; continue
        if ch in _OPS1:
            toks.append(("op", ch)); i += 1; continue
        return None      # 미지원 문자 -> 파싱 포기
    return toks


_PREC = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5, "==": 6, "!=": 6,
         "<": 7, "<=": 7, ">": 7, ">=": 7, "<<": 8, ">>": 8,
         "+": 9, "-": 9, "*": 10, "/": 10, "%": 10}


class _Parser:
    def __init__(self, toks):
        self.t = toks; self.i = 0

    def _peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def _next(self):
        tok = self._peek(); self.i += 1; return tok

    def parse(self):
        node = self._expr(0)
        if self.i != len(self.t):
            raise ValueError("trailing tokens")
        return node

    def _expr(self, minp):
        left = self._unary()
        while True:
            k, v = self._peek()
            if k == "op" and v in _PREC and _PREC[v] >= minp:
                self._next()
                left = ("bin", v, left, self._expr(_PREC[v] + 1))
            else:
                return left

    def _unary(self):
        k, v = self._peek()
        if k == "op" and v in ("!", "~", "-", "+"):
            self._next(); return ("un", v, self._unary())
        return self._postfix()

    def _postfix(self):
        node = self._primary()
        while True:
            k, v = self._peek()
            if k == "op" and v == "->":
                self._next(); kk, nm = self._next()
                if kk != "id":
                    raise ValueError("field name")
                node = ("field", node, nm)
            elif k == "op" and v == "[":
                self._next(); idx = self._expr(0)
                if self._next() != ("op", "]"):
                    raise ValueError("]")
                node = ("index", node, idx)
            elif k == "op" and v == "(":
                self._next(); args = []
                if self._peek() != ("op", ")"):
                    args.append(self._expr(0))
                    while self._peek() == ("op", ","):
                        self._next(); args.append(self._expr(0))
                if self._next() != ("op", ")"):
                    raise ValueError(")")
                node = ("call", node, args)
            else:
                return node

    def _primary(self):
        k, v = self._next()
        if k == "num":
            return ("num", v)
        if k == "id":
            return ("id", v)
        if (k, v) == ("op", "("):
            node = self._expr(0)
            if self._next() != ("op", ")"):
                raise ValueError(")")
            return node
        raise ValueError("unexpected")


def _parse(text):
    toks = _tokenize(text.strip())
    if not toks:
        return None
    try:
        return _Parser(toks).parse()
    except ValueError:
        return None


# ============================================================
# AST -> Z3 (32-bit BitVec, C int 부호 의미)
# ============================================================
_CMP = {"==", "!=", "<", "<=", ">", ">="}


class _Z3Ctx:
    """입력 변수(field/scalar/global/sret)를 BitVec 로 등록하며 AST 를 번역."""
    def __init__(self, const_map, ptr_name, scalar_names, globals_map, stub_funcs):
        self.const = const_map; self.ptr = ptr_name
        self.scalars = scalar_names; self.globs = globals_map
        self.stubs = stub_funcs
        self.env = {}            # local name -> z3 bv
        self.inputs = {}         # (kind, target) -> bv
        self._free = 0

    def _inp(self, kind, target):
        key = (kind, target)
        if key not in self.inputs:
            self.inputs[key] = z3.BitVec(f"{kind}__{target}", 32)
        return self.inputs[key]

    def _fresh(self):
        self._free += 1
        return z3.BitVec(f"_free{self._free}", 32)

    def add_local(self, name, ast):
        self.env[name] = self.bv(ast)

    def bv(self, node):
        t = node[0]
        if t == "num":
            return z3.BitVecVal(node[1] & 0xFFFFFFFF, 32)
        if t == "id":
            nm = node[1]
            if nm in self.env:
                return self.env[nm]
            if nm in self.const:
                return z3.BitVecVal(self.const[nm] & 0xFFFFFFFF, 32)
            if nm in self.scalars:
                return self._inp("scalar", nm)
            if nm in self.globs:
                return self._inp("global", nm)
            return self._fresh()
        if t == "field":
            base, fld = node[1], node[2]
            if base[0] == "id" and self.ptr and base[1] == self.ptr:
                return self._inp("field", fld)
            return self._fresh()
        if t == "call":
            fn = node[1]
            if fn[0] == "id" and fn[1] in self.stubs:
                return self._inp("sret", f"__sret_{fn[1]}")
            return self._fresh()
        if t == "index":
            return self._fresh()
        if t == "un":
            op, a = node[1], self.bv(node[2])
            if op == "-":
                return -a
            if op == "~":
                return ~a
            if op == "+":
                return a
            if op == "!":
                return z3.If(a == 0, z3.BitVecVal(1, 32), z3.BitVecVal(0, 32))
        if t == "bin":
            op = node[1]
            if op in _CMP:
                return z3.If(self.boolean(node), z3.BitVecVal(1, 32),
                             z3.BitVecVal(0, 32))
            a, b = self.bv(node[2]), self.bv(node[3])
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                return z3.If(b == 0, z3.BitVecVal(0, 32), a / b)
            if op == "%":
                return z3.If(b == 0, z3.BitVecVal(0, 32), z3.SRem(a, b))
            if op == "&":
                return a & b
            if op == "|":
                return a | b
            if op == "^":
                return a ^ b
            if op == "<<":
                return a << b
            if op == ">>":
                return a >> b
        return self._fresh()

    def boolean(self, node):
        """AST -> z3 Bool (조건 진리값)."""
        t = node[0]
        if t == "bin" and node[1] == "&&":
            return z3.And(self.boolean(node[2]), self.boolean(node[3]))
        if t == "bin" and node[1] == "||":
            return z3.Or(self.boolean(node[2]), self.boolean(node[3]))
        if t == "un" and node[1] == "!":
            return z3.Not(self.boolean(node[2]))
        if t == "bin" and node[1] in _CMP:
            a, b = self.bv(node[2]), self.bv(node[3])
            return {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b,
                    ">": a > b, ">=": a >= b}[node[1]]
        return self.bv(node) != 0      # 비교 아님 -> truthiness


# ============================================================
# 원자조건 추출 + MC/DC 진리표 / 독립쌍
# ============================================================
def _atoms(node, acc):
    t = node[0]
    if t == "bin" and node[1] in ("&&", "||"):
        _atoms(node[2], acc); _atoms(node[3], acc)
    elif t == "un" and node[1] == "!":
        _atoms(node[2], acc)
    else:
        acc.append(node)


def _eval(node, idx, bits):
    t = node[0]
    if t == "bin" and node[1] == "&&":
        return _eval(node[2], idx, bits) and _eval(node[3], idx, bits)
    if t == "bin" and node[1] == "||":
        return _eval(node[2], idx, bits) or _eval(node[3], idx, bits)
    if t == "un" and node[1] == "!":
        return not _eval(node[2], idx, bits)
    return bits[idx[id(node)]]


def _mcdc_rows(atoms, ast, idx):
    """MC/DC 독립쌍을 덮는 최소 행 집합(unique-cause)."""
    n = len(atoms)
    rows = list(product([False, True], repeat=n))
    out = {r: _eval(ast, idx, r) for r in rows}
    chosen = set()
    for i in range(n):
        for r in rows:
            r2 = list(r); r2[i] = not r2[i]; r2 = tuple(r2)
            if out[r] != out[r2]:        # i 만 바꿔 결과가 바뀌는 독립쌍
                chosen.add(r); chosen.add(r2)
                break
    return chosen


# ============================================================
# 함수 본문 -> 결정 목록(로컬 정의 + 경로 제약 포함)
# ============================================================
_IF_RE = re.compile(r"^\}?\s*(?:else\s+if|if)\s*\((.*)\)\s*\{?\s*$")
_WH_RE = re.compile(r"^\s*while\s*\((.*)\)\s*\{?\s*$")
_DOWH_RE = re.compile(r"^\}?\s*while\s*\((.*)\)\s*;\s*$")
_SW_RE = re.compile(r"^switch\s*\((.*)\)\s*\{?\s*$")
_CASE_RE = re.compile(r"^case\s+(.+?)\s*:.*$")
_ASSIGN_RE = re.compile(r"^(?:[A-Za-z_][\w\s\*]*?\s)?([a-z_]\w*)\s*=\s*([^=].*?);")


def _collect_decisions(body):
    """[(decision_ast, local_defs[(name,ast)], path[(ast,bool)])] 반환."""
    decisions = []
    local_defs = []            # 누적(정의 순서)
    stack = []                 # (indent, kind, payload) — kind: 'if'/'sw'/'case'
    sw_expr = []               # switch 식 스택
    for _ln, text in body:
        st = text.strip()
        indent = len(text) - len(text.lstrip())
        while stack and stack[-1][0] >= indent and stack[-1][1] != "sw_open":
            stack.pop()

        # 로컬 대입 수집
        am = _ASSIGN_RE.match(st)
        if am and not st.startswith(("if", "while", "for", "return")):
            rast = _parse(am.group(2))
            if rast is not None:
                local_defs.append((am.group(1), rast))

        # 경로 제약(현재 스택)
        path = [(a, b) for (_i, k, a, b) in
                ((f[0], f[1], f[2], f[3]) for f in stack if len(f) == 4)]

        m = _SW_RE.match(st)
        if m:
            sw_expr.append(_parse(m.group(1)))
            stack.append((indent, "sw_open"))
            continue
        m = _CASE_RE.match(st)
        if m and sw_expr and sw_expr[-1] is not None:
            cv = _parse(m.group(1))
            if cv is not None:
                # m->mode == CASE  형태 제약
                stack.append((indent, "case", ("bin", "==", sw_expr[-1], cv), True))
            continue

        for rgx in (_IF_RE, _WH_RE, _DOWH_RE):
            m = rgx.match(st)
            if m:
                dast = _parse(m.group(1))
                if dast is not None:
                    decisions.append((dast, list(local_defs),
                                      [p for p in path if p[0] is not None]))
                    if rgx is _IF_RE:    # if 본문 진입 = 조건 True 경로
                        stack.append((indent, "if", dast, True))
                break
    return decisions


# ============================================================
# 메인: MC/DC 충족 입력 벡터 생성
# ============================================================
def _blank():
    return {"fields": {}, "scalars": {}, "globals": {},
            "srets": {}, "null": False}


def collect_symbols(body):
    """본문에서 상수 후보(대문자 식별자) 수집 — 호출측이 clang 으로 값 해석.
    주석/문자열은 제거해 코드에 없는 낱말(예: 주석 속 단어)을 배제한다."""
    text = "\n".join(t for _l, t in body)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)   # 블록 주석
    text = re.sub(r"//[^\n]*", " ", text)                # 라인 주석
    text = re.sub(r'"(?:\\.|[^"\\])*"', " ", text)       # 문자열 리터럴
    syms = set()
    for m in re.finditer(r"[A-Za-z_]\w*", text):
        w = m.group(0)
        if re.match(r"^[A-Z][A-Z0-9_]*$", w):
            syms.add(w)
    return syms


def generate(body, const_map, ptr_name, scalar_names, globals_map,
             stub_funcs, max_vectors=64):
    """MC/DC 독립쌍을 Z3 로 풀어 assignment 벡터 리스트 반환(가산용)."""
    if not Z3_OK:
        return []
    decisions = _collect_decisions(body)
    out, seen = [], set()
    for dast, local_defs, path in decisions:
        atoms = []
        _atoms(dast, atoms)
        if not atoms or len(atoms) > 6:
            continue
        idx = {id(a): i for i, a in enumerate(atoms)}
        rows = _mcdc_rows(atoms, dast, idx)
        for row in rows:
            ctx = _Z3Ctx(const_map, ptr_name, scalar_names, globals_map, stub_funcs)
            try:
                for nm, rast in local_defs:
                    ctx.add_local(nm, rast)
                s = z3.Solver()
                s.set("timeout", 2000)
                for i, atom in enumerate(atoms):
                    b = ctx.boolean(atom)
                    s.add(b if row[i] else z3.Not(b))
                for pa, pb in path:           # 경로 도달성(근사)
                    try:
                        cb = ctx.boolean(pa)
                        s.add(cb if pb else z3.Not(cb))
                    except Exception:
                        pass
                if s.check() != z3.sat:
                    continue
                model = s.model()
            except Exception:
                continue
            assign = _blank()
            bucket = {"field": "fields", "scalar": "scalars",
                      "global": "globals", "sret": "srets"}
            for (kind, target), var in ctx.inputs.items():
                try:
                    val = model.eval(var, model_completion=True).as_signed_long()
                except Exception:
                    val = 0
                assign[bucket[kind]][target] = str(val)
            key = (tuple(sorted(assign["fields"].items())),
                   tuple(sorted(assign["scalars"].items())),
                   tuple(sorted(assign["globals"].items())),
                   tuple(sorted(assign["srets"].items())))
            if key in seen:
                continue
            seen.add(key)
            out.append(assign)
            if len(out) >= max_vectors:
                return out
    return out
