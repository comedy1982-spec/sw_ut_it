"""
SWTS Phase 0 — 실제 스캔 엔진 (git diff + Clang AST)
====================================================
/scan 엔드포인트의 실제 구현. 목업 _mock_scan() 을 대체한다.

흐름:
  1. git diff {base} → 변경된 .c 파일 + 변경 라인 집합 추출
  2. 각 .c 를 Clang 으로 파싱(동일 전처리 플래그) → 함수 정의 추출
  3. 함수 라인범위 ∩ 변경라인 → changed 판정
  4. CallGraph 1-depth → caller 추적
  5. 파라미터/본문 분석 → support 레벨(A/B/C) 산정
  6. 디렉터리 트리 구성

의존: pip install libclang
"""
from __future__ import annotations
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

import clang.cindex as cidx

# libclang 바인딩이 동봉한 .so 경로를 명시 (환경에 따라 자동 탐색 실패 방지)
_LIBCLANG = "/usr/local/lib/python3.12/dist-packages/clang/native/libclang.so"
if os.path.exists(_LIBCLANG):
    try:
        cidx.Config.set_library_file(_LIBCLANG)
    except Exception:
        pass


# ============================================================
# 1. git diff → 변경 파일/라인
# ============================================================
def git_changed_lines(root: str, base: Optional[str]) -> dict[str, set[int]]:
    """{상대경로: {변경된 라인번호}}. base 가 없으면 빈 dict(전체 스캔 의미)."""
    if not base:
        return {}
    try:
        out = subprocess.run(
            ["git", "-C", root, "diff", "--unified=0", base, "--", "*.c"],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return {}

    changed: dict[str, set[int]] = {}
    cur: Optional[str] = None
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:].strip()
            changed.setdefault(cur, set())
        elif line.startswith("@@") and cur is not None:
            # @@ -a,b +c,d @@  →  변경 후(c..c+d-1) 라인
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                for n in range(start, start + max(count, 1)):
                    changed[cur].add(n)
    return {k: v for k, v in changed.items() if v}


# ============================================================
# 2~5. Clang AST 분석
# ============================================================
@dataclass
class UnitInfo:
    name: str
    file_rel: str
    start: int
    end: int
    branches: int = 0
    max_depth: int = 0
    params: list[str] = field(default_factory=list)
    has_struct_ptr: bool = False
    has_bitop: bool = False
    callees: set[str] = field(default_factory=set)


def _support_level(u: UnitInfo) -> str:
    """A: primitive만 / B: 포인터·구조체 / C: 깊은중첩·비트연산·구조체포인터."""
    if u.max_depth >= 3 or u.has_bitop or u.has_struct_ptr:
        return "C"
    if any("*" in p for p in u.params):
        return "B"
    return "A"


def _walk_function(node, file_rel: str) -> UnitInfo:
    """함수 정의 노드 → UnitInfo (분기수·중첩깊이·비트연산·호출 추출)."""
    extent = node.extent
    u = UnitInfo(
        name=node.spelling,
        file_rel=file_rel,
        start=extent.start.line,
        end=extent.end.line,
        params=[_param_type(a) for a in node.get_arguments()],
    )
    if any("*" in p and _looks_struct(p) for p in u.params):
        u.has_struct_ptr = True

    def visit(n, depth):
        k = n.kind
        if k in (cidx.CursorKind.IF_STMT, cidx.CursorKind.WHILE_STMT,
                 cidx.CursorKind.FOR_STMT, cidx.CursorKind.CASE_STMT):
            u.branches += 1
            u.max_depth = max(u.max_depth, depth + 1)
            for c in n.get_children():
                visit(c, depth + 1)
        else:
            if k == cidx.CursorKind.BINARY_OPERATOR and _is_bitop(n):
                u.has_bitop = True
            if k == cidx.CursorKind.CALL_EXPR:
                callee = _callee_name(n)
                if callee:
                    u.callees.add(callee)
            for c in n.get_children():
                visit(c, depth)

    visit(node, 0)
    return u


def _param_type(arg) -> str:
    return arg.type.spelling


def _looks_struct(type_str: str) -> bool:
    # 휴리스틱: 기본형 포인터(char*, int* 등)가 아니면 구조체 포인터로 간주
    base = type_str.replace("*", "").replace("const", "").strip()
    return base not in {"char", "int", "short", "long", "float", "double",
                        "unsigned", "void", "unsigned char", "unsigned int"}


def _is_bitop(node) -> bool:
    toks = [t.spelling for t in node.get_tokens()]
    return any(t in {"&", "|", "^", "<<", ">>"} for t in toks)


def _callee_name(call_node) -> Optional[str]:
    """CALL_EXPR 에서 함수명 추출. spelling 이 비면 자식 DECL_REF_EXPR 토큰에서."""
    if call_node.spelling:
        return call_node.spelling
    if call_node.referenced and call_node.referenced.spelling:
        return call_node.referenced.spelling
    for c in call_node.get_children():
        if c.kind in (cidx.CursorKind.DECL_REF_EXPR, cidx.CursorKind.UNEXPOSED_EXPR):
            if c.spelling:
                return c.spelling
            toks = [t.spelling for t in c.get_tokens()]
            if toks:
                return toks[0]
    return None


def parse_c_file(abs_path: str, file_rel: str, flags: list[str]) -> list[UnitInfo]:
    index = cidx.Index.create()
    # 누락된 시스템 헤더에 견디도록: 에러 한계 해제 + NULL 등 최소 정의 주입.
    # (실제 빌드가 아니라 AST 구조 분석이 목적이므로 타입 완전성은 불필요)
    args = ["-x", "c", "-ferror-limit=0", "-Wno-everything",
            "-D__SWTS_SCAN__=1", "-DNULL=((void*)0)"] + flags
    opts = (cidx.TranslationUnit.PARSE_INCOMPLETE
            | cidx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
    try:
        tu = index.parse(abs_path, args=args, options=opts)
    except cidx.TranslationUnitLoadError:
        return []

    units: list[UnitInfo] = []
    for node in tu.cursor.get_children():
        if (node.kind == cidx.CursorKind.FUNCTION_DECL
                and node.is_definition()
                and node.location.file
                and os.path.abspath(node.location.file.name) == os.path.abspath(abs_path)):
            units.append(_walk_function(node, file_rel))
    return units


# ============================================================
# 6. 트리 + 컴포넌트 구성
# ============================================================
def _component_of(file_rel: str) -> Optional[str]:
    """경로 규칙으로 컴포넌트 추정. 프로젝트 규약에 맞게 교체 가능."""
    parts = file_rel.split("/")
    if "src" in parts:
        i = parts.index("src")
        if i + 1 < len(parts) - 1:
            seg = parts[i + 1]
            return {"speed": "SpeedControl", "diag": "Diagnostics"}.get(seg, seg.capitalize())
    return None


def build_tree(root: str) -> list[dict]:
    """디렉터리 트리(.c/.h 파일만, hidden/build 제외)."""
    def rec(d: str) -> list[dict]:
        out = []
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            return out
        for name in entries:
            if name.startswith(".") or name in {"build", "node_modules", "out"}:
                continue
            full = os.path.join(d, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if os.path.isdir(full):
                kids = rec(full)
                if kids:
                    out.append({"type": "folder", "name": name, "children": kids})
            elif name.endswith((".c", ".h")):
                out.append({"type": "file", "name": name, "path": rel,
                            "component": _component_of(rel)})
        return out
    return rec(root)


# ============================================================
# 진입점 — /scan 이 호출
# ============================================================
def scan_project(root: str, base: Optional[str], flags: list[str]) -> dict:
    root = os.path.abspath(root)
    # include/ 가 있으면 자동으로 -I 추가 (타입 해석 향상)
    flags = list(flags)
    inc = os.path.join(root, "include")
    if os.path.isdir(inc) and f"-I{inc}" not in flags:
        flags.append(f"-I{inc}")
    changed_map = git_changed_lines(root, base)

    # 모든 .c 수집
    c_files: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in {"build", "node_modules", "out"}]
        for f in files:
            if f.endswith(".c"):
                c_files.append(os.path.join(dirpath, f))

    components: dict[str, dict] = {}
    all_units: list[UnitInfo] = []
    unit_index: dict[str, UnitInfo] = {}

    for abs_path in c_files:
        rel = os.path.relpath(abs_path, root).replace("\\", "/")
        units = parse_c_file(abs_path, rel, flags)
        for u in units:
            all_units.append(u)
            unit_index[u.name] = u

    # caller 역추적: 누가 이 함수를 호출하나
    callers_of: dict[str, list[str]] = {u.name: [] for u in all_units}
    for u in all_units:
        for callee in u.callees:
            if callee in callers_of and callee != u.name:
                callers_of[callee].append(u.name)

    for u in all_units:
        comp = _component_of(u.file_rel) or "Uncategorized"
        changed_lines = changed_map.get(u.file_rel, set())
        is_changed = bool(changed_lines & set(range(u.start, u.end + 1)))
        unit_ref = f"{u.file_rel}::{u.name}"
        entry = {
            "unit_ref": unit_ref,
            "name": u.name,
            "lines": f"{u.start}-{u.end}",
            "changed": is_changed,
            "callers": sorted(set(callers_of.get(u.name, []))),
            "branches": u.branches,
            "max_depth": u.max_depth,
            "support": _support_level(u),
        }
        components.setdefault(comp, {"file": u.file_rel, "units": []})
        components[comp]["units"].append(entry)

    return {
        "ok": True,
        "project": os.path.basename(root),
        "tree": build_tree(root),
        "components": components,
    }


if __name__ == "__main__":
    import json, sys
    r = sys.argv[1] if len(sys.argv) > 1 else "."
    base = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(scan_project(r, base, ["-DUNIT_TEST"]), ensure_ascii=False, indent=2))
