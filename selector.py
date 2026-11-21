from __future__ import annotations

import ast
import builtins
import concurrent.futures
import contextlib
import gzip
import importlib.abc
import importlib.machinery
import io
import json
import math
import os
import pickle
import re
import resource
import signal
import sys
import unittest
from collections import defaultdict
from pathlib import Path

DATA = Path("/data")
SQLGLOT = Path("/sqlglot")
SQLGLOT_REAL = os.path.realpath("/sqlglot")
MAP_PATH = Path("/app/coverage_map.json.gz")

WORKER_COST_MS = 2000
MAX_WORKERS = 8

NEVER_VERIFY_IDS = {
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/dialects/test_dialect.py::TestDialect::test_lazy_load",
}
NEVER_VERIFY_FILES = {
    "tests/test_sqlglotc.py",
}


def duration_ms(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def pack_workers(test_nums: set[int], dur_by_num: dict[int, int]) -> tuple[list[list[int]], int]:
    """Find worker partition (1..8 workers) minimizing max(load) + 2000 * k."""
    if not test_nums:
        return [], 0
    items = sorted(test_nums, key=lambda n: dur_by_num[n], reverse=True)
    durs = [dur_by_num[n] for n in items]
    total_ms = sum(durs)
    max_item = durs[0]

    best_cost = total_ms + WORKER_COST_MS
    best_workers: list[list[int]] = [items]

    max_k = min(MAX_WORKERS, len(items))
    for k in range(2, max_k + 1):
        lb = max(max_item, (total_ms + k - 1) // k) + WORKER_COST_MS * k
        if lb >= best_cost:
            continue

        workers: list[list[int]] = [[] for _ in range(k)]
        loads = [0] * k
        for n, d in zip(items, durs):
            idx = loads.index(min(loads))
            workers[idx].append(n)
            loads[idx] += d

        improved = True
        while improved:
            improved = False
            max_load = max(loads)
            hi = loads.index(max_load)
            for pos, n in enumerate(workers[hi]):
                d = dur_by_num[n]
                lo = loads.index(min(loads))
                if lo != hi and loads[lo] + d < max_load:
                    workers[hi].pop(pos)
                    loads[hi] -= d
                    workers[lo].append(n)
                    loads[lo] += d
                    improved = True
                    break

        occupied = [w for w in workers if w]
        cost = max(sum(dur_by_num[n] for n in w) for w in occupied) + WORKER_COST_MS * len(occupied)
        if cost < best_cost:
            best_cost = cost
            best_workers = occupied

    return best_workers, best_cost


def parse_unified_diff(patch_text: str) -> dict[str, dict]:
    files: dict[str, dict] = {}
    lines = patch_text.splitlines(keepends=True)
    i = 0
    n = len(lines)
    cur_file = None

    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+?)\r?\n?$", line)
            if m:
                old_path, new_path = m.group(1), m.group(2)
                cur_file = new_path if new_path != "/dev/null" else old_path
                files[cur_file] = {
                    "old_path": old_path,
                    "new_path": new_path,
                    "hunks": [],
                    "old_lines": set(),
                    "is_new": False,
                    "is_deleted": False,
                    "is_binary": False,
                }
            i += 1
            continue

        if cur_file is None:
            i += 1
            continue

        info = files[cur_file]
        if line.startswith("new file mode"):
            info["is_new"] = True
            i += 1
        elif line.startswith("deleted file mode"):
            info["is_deleted"] = True
            i += 1
        elif line.startswith("Binary files "):
            info["is_binary"] = True
            i += 1
        elif line.startswith("--- "):
            if line.strip() == "--- /dev/null":
                info["is_new"] = True
            i += 1
        elif line.startswith("+++ "):
            if line.strip() == "+++ /dev/null":
                info["is_deleted"] = True
            i += 1
        elif line.startswith("@@ "):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                i += 1
                continue
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            i += 1
            hunk_lines = []
            old_ln = old_start
            saw_minus_in_block = False
            while i < n and not lines[i].startswith(("diff --git ", "@@ ")):
                hl = lines[i]
                if hl.startswith("\\ No newline"):
                    hunk_lines.append(hl)
                    i += 1
                    continue
                if hl.startswith("-"):
                    hunk_lines.append(hl)
                    info["old_lines"].add(old_ln)
                    old_ln += 1
                    saw_minus_in_block = True
                elif hl.startswith("+"):
                    hunk_lines.append(hl)
                    if not saw_minus_in_block:
                        info["old_lines"].add(max(1, old_ln - 1))
                        info["old_lines"].add(old_ln)
                elif hl.startswith(" "):
                    hunk_lines.append(hl)
                    old_ln += 1
                    saw_minus_in_block = False
                else:
                    hunk_lines.append(" " + hl)
                    old_ln += 1
                    saw_minus_in_block = False
                i += 1
            info["hunks"].append((old_start, old_count, new_start, new_count, hunk_lines))
        else:
            i += 1

    return files


def apply_hunks(old_text: str, hunks: list) -> str | None:
    old_lines = old_text.splitlines(keepends=True)
    out: list[str] = []
    pos = 0

    for old_start, old_count, _new_start, _new_count, hunk_lines in hunks:
        target = max(0, old_start - 1) if old_count > 0 else old_start
        if target < pos or target > len(old_lines):
            return None
        out.extend(old_lines[pos:target])
        pos = target

        for hl in hunk_lines:
            if hl.startswith("\\ No newline"):
                continue
            tag = hl[0]
            body = hl[1:]
            if tag == " ":
                if pos >= len(old_lines):
                    return None
                out.append(old_lines[pos])
                pos += 1
            elif tag == "-":
                if pos >= len(old_lines):
                    return None
                pos += 1
            elif tag == "+":
                out.append(body)

    out.extend(old_lines[pos:])
    return "".join(out)


def is_harmless_py_change(old_text: str, new_text: str) -> tuple[bool, bool]:
    """Return (is_ast_identical_harmless, has_syntax_error)."""
    old_head = old_text.splitlines()[:2]
    new_head = new_text.splitlines()[:2]
    coding_re = re.compile(r"coding[:=]\s*[-\w.]+")
    if any(coding_re.search(l) for l in old_head) != any(coding_re.search(l) for l in new_head):
        return False, False
    if any(coding_re.search(l) for l in new_head) and old_head != new_head:
        return False, False

    try:
        old_tree = ast.parse(old_text)
    except SyntaxError:
        return False, False

    try:
        new_tree = ast.parse(new_text)
    except SyntaxError:
        return False, True

    if ast.dump(old_tree, include_attributes=False) == ast.dump(new_tree, include_attributes=False):
        return True, False

    return False, False


def is_pure_literal_expr(node: ast.AST | None) -> bool:
    """True if AST expression cannot fail or execute side effects at import time."""
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
        return is_pure_literal_expr(node.operand)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(is_pure_literal_expr(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(is_pure_literal_expr(k) and is_pure_literal_expr(v) for k, v in zip(node.keys, node.values))
    return False


def build_expr_subclasses_from_ast() -> dict[str, set[str]]:
    children: dict[str, set[str]] = defaultdict(set)
    all_classes: set[str] = set()
    expr_dir = SQLGLOT / "sqlglot" / "expressions"
    for p in expr_dir.glob("*.py"):
        try:
            tree = ast.parse(p.read_text("utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                all_classes.add(node.name)
                for base in node.bases:
                    bname = None
                    if isinstance(base, ast.Name):
                        bname = base.id
                    elif isinstance(base, ast.Attribute):
                        bname = base.attr
                    if bname:
                        children[bname].add(node.name)

    closure: dict[str, set[str]] = {}

    def dfs(name: str, seen: set[str]) -> set[str]:
        if name in closure:
            return closure[name]
        res = {name}
        seen.add(name)
        for ch in children.get(name, ()):
            if ch not in seen:
                res.update(dfs(ch, seen))
        closure[name] = res
        return res

    for c in all_classes:
        dfs(c, set())
    return closure


ATTR_INDEX_PATH = Path("/app/attr_index.json")
_attr_index_cache: list = []


def load_attr_index():
    if not _attr_index_cache:
        try:
            _attr_index_cache.append(json.loads(ATTR_INDEX_PATH.read_text("utf-8")))
            _attr_index_cache[0]["dynamic"] = set(_attr_index_cache[0]["dynamic"])
        except Exception:
            _attr_index_cache.append(None)
    return _attr_index_cache[0]


def build_attr_index() -> None:
    """Build-time index: for every class-level attribute name assigned in a class of sqlglot, every runtime
    Load read (file, line, innermost enclosing callable start or -1).  Names that appear as string
    constants anywhere are 'dynamic' (getattr) and never narrowed."""
    names: set[str] = set()
    trees = {}
    for p in sorted((SQLGLOT / "sqlglot").rglob("*.py")):
        rel = str(p.relative_to(SQLGLOT))
        try:
            tree = ast.parse(p.read_text("utf-8", errors="ignore"))
        except SyntaxError:
            continue
        trees[rel] = tree
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                        names.add(st.targets[0].id)
                    elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                        names.add(st.target.id)
    reads: dict[str, list] = {n: [] for n in names}
    dynamic: set[str] = set()
    for rel, tree in trees.items():
        callables = [(n.lineno, n.end_lineno or n.lineno) for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names:
                dynamic.add(node.value)
                continue
            nm = None
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                nm = node.attr
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                nm = node.id
            if nm is None or nm not in names:
                continue
            ln = node.lineno
            enc = [(a, b) for a, b in callables if a <= ln <= b]
            if not enc:
                reads[nm].append((rel, ln, -1))
            else:
                inner_s, _ = min(enc, key=lambda x: (x[1] - x[0], -x[0]))
                reads[nm].append((rel, ln, inner_s))
    ATTR_INDEX_PATH.write_text(json.dumps({"reads": reads, "dynamic": sorted(dynamic)}), "utf-8")


class StaticAnalyzer:
    def __init__(self, cov: dict) -> None:
        self.line_map: dict[str, dict[str, list[int]]] = cov["line_to_tests"]
        self.func_map: dict[str, dict[str, list[int]]] = cov["func_to_tests"]
        self.open_map: dict[str, list[int]] = cov["open_to_tests"]
        self.dial_map: dict[str, list[int]] = cov["dialect_to_tests"]
        self.expr_map: dict[str, list[int]] = cov.get("expr_to_tests", {})
        self.imp_map: dict[str, list[str]] = cov.get("import_to_testfiles", {})

        self.file_all_tests: dict[str, set[int]] = {}
        for f, lmap in self.line_map.items():
            s: set[int] = set()
            for ts in lmap.values():
                s.update(ts)
            self.file_all_tests[f] = s
        for f, fmap in self.func_map.items():
            s = self.file_all_tests.setdefault(f, set())
            for ts in fmap.values():
                s.update(ts)
        for f, ts in self.open_map.items():
            self.file_all_tests.setdefault(f, set()).update(ts)

        self.expr_subclasses = build_expr_subclasses_from_ast()
        self._ast_cache: dict[str, ast.Module | None] = {}
        self._attr_read_cache: dict[str, set[int] | None] = {}
        self._py_sources: dict[str, str] | None = None

    def get_ast(self, rel_path: str) -> ast.Module | None:
        if rel_path in self._ast_cache:
            return self._ast_cache[rel_path]
        p = SQLGLOT / rel_path
        if not p.exists() or not rel_path.endswith(".py"):
            self._ast_cache[rel_path] = None
            return None
        try:
            tree = ast.parse(p.read_text("utf-8", errors="ignore"))
        except SyntaxError:
            tree = None
        self._ast_cache[rel_path] = tree
        return tree

    def _ensure_sources(self) -> dict[str, str]:
        if self._py_sources is None:
            srcs: dict[str, str] = {}
            for p in (SQLGLOT / "sqlglot").rglob("*.py"):
                srcs[str(p.relative_to(SQLGLOT))] = p.read_text("utf-8", errors="ignore")
            self._py_sources = srcs
        return self._py_sources

    def runtime_attr_reads(self, attr_name: str) -> set[int] | None:
        """Return exact tests executing runtime Load reads of attr_name, or None if unsafe to narrow."""
        if attr_name in self._attr_read_cache:
            return self._attr_read_cache[attr_name]
        idx = load_attr_index()
        if idx is not None:
            if attr_name in idx["dynamic"]:
                self._attr_read_cache[attr_name] = None
                return None
            locs = idx["reads"].get(attr_name)
            if locs is not None:
                hits_i: set[int] = set()
                for rel, ln, enc in locs:
                    if enc < 0:
                        self._attr_read_cache[attr_name] = None
                        return None
                    hits_i.update(self.line_map.get(rel, {}).get(str(ln), ()))
                    hits_i.update(self.func_map.get(rel, {}).get(str(enc), ()))
                res_i = hits_i if hits_i else None
                self._attr_read_cache[attr_name] = res_i
                return res_i

        hits: set[int] = set()
        srcs = self._ensure_sources()
        for rel, txt in srcs.items():
            if attr_name not in txt:
                continue
            tree = self.get_ast(rel)
            if tree is None:
                continue
            callables = [
                (n.lineno, n.end_lineno or n.lineno)
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            ]
            f_lines = self.line_map.get(rel, {})
            f_funcs = self.func_map.get(rel, {})
            for node in ast.walk(tree):
                # Guard dynamic getattr(obj, "ATTR_NAME")
                if isinstance(node, ast.Constant) and node.value == attr_name:
                    self._attr_read_cache[attr_name] = None
                    return None
                is_read = (
                    (isinstance(node, ast.Attribute) and node.attr == attr_name and isinstance(node.ctx, ast.Load))
                    or (isinstance(node, ast.Name) and node.id == attr_name and isinstance(node.ctx, ast.Load))
                )
                if not is_read:
                    continue
                ln = node.lineno
                enc = [(s, e) for s, e in callables if s <= ln <= e]
                if not enc:
                    self._attr_read_cache[attr_name] = None
                    return None
                hits.update(f_lines.get(str(ln), ()))
                inner_s, _ = min(enc, key=lambda x: (x[1] - x[0], -x[0]))
                hits.update(f_funcs.get(str(inner_s), ()))

        res = hits if hits else None
        self._attr_read_cache[attr_name] = res
        return res


# ======================== IN-CONTAINER VERIFIER (v3: warm parent + forked children) ========================
#
# The parent imports sqlglot, every sqlglot module and every test module once ("warm").  For each commit a
# child is forked.  In mode A the child swaps the __code__ of the changed functions in place (cheap); in
# mode B it drops every sqlglot/tests module and re-imports them with the patched sources.  Mode A is used
# only when every change is inside function bodies, no changed line ran during the warm import, and the
# live functions are found unambiguously.  A line monitor checks that each passing test really executed
# the changed code whenever the coverage map says it does; otherwise the test is reported as failing.

CO_OPTIMIZED = 0x0001
DEBUG2 = os.environ.get("BLAST_DEBUG") == "2"
IMPORT_LINES_PATH = Path("/app/import_lines.json.gz")


class _SubTestFailFast(BaseException):
    pass


class _TestTimeout(BaseException):
    pass


class _PatchLoader(importlib.abc.Loader):
    def __init__(self, source: str, filepath: str, is_pkg: bool) -> None:
        self.source = source
        self.filepath = filepath
        self.is_pkg = is_pkg

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = self.filepath
        if self.is_pkg:
            module.__path__ = [os.path.dirname(self.filepath)]
        code = compile(self.source, self.filepath, "exec")
        exec(code, module.__dict__)


class _PatchFinder(importlib.abc.MetaPathFinder):
    def __init__(self, mod_map: dict[str, tuple[str, str, bool]]) -> None:
        self.mod_map = mod_map

    def find_spec(self, fullname, path, target=None):
        entry = self.mod_map.get(fullname)
        if entry is None:
            return None
        source, filepath, is_pkg = entry
        loader = _PatchLoader(source, filepath, is_pkg)
        spec = importlib.machinery.ModuleSpec(fullname, loader, origin=filepath, is_package=is_pkg)
        if is_pkg:
            spec.submodule_search_locations = [os.path.dirname(filepath)]
        return spec


class _LazyImmediateExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), use_pickle=False):
        self._init = initializer
        self._initargs = initargs
        self._inited = False
        self._use_pickle = use_pickle

    def submit(self, fn, /, *args, **kwargs):
        fut = concurrent.futures.Future()
        orig_result = fut.result

        def lazy_result(timeout=None):
            if not fut.done():
                if self._init is not None and not self._inited:
                    self._inited = True
                    if self._use_pickle:
                        pi, pa = pickle.loads(pickle.dumps((self._init, self._initargs)))
                        pi(*pa)
                    else:
                        self._init(*self._initargs)
                try:
                    if self._use_pickle:
                        pf, pa, pk = pickle.loads(pickle.dumps((fn, args, kwargs)))
                        res = pf(*pa, **pk)
                        res = pickle.loads(pickle.dumps(res))
                    else:
                        res = fn(*args, **kwargs)
                    fut.set_result(res)
                except BaseException as exc:
                    fut.set_exception(exc)
            return orig_result(timeout)

        fut.result = lazy_result
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return map(fn, *iterables)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False


def _lazy_as_completed(fs, timeout=None):
    for f in fs:
        yield f


def _lazy_wait(fs, timeout=None, return_when=concurrent.futures.ALL_COMPLETED):
    fs_list = list(fs)
    for f in fs_list:
        try:
            f.result()
        except BaseException:
            pass
    return set(fs_list), set()


def _module_name_for(rel: str) -> tuple[str, bool] | None:
    if not rel.endswith(".py"):
        return None
    if rel.endswith("/__init__.py"):
        return rel[: -len("/__init__.py")].replace("/", "."), True
    return rel[:-3].replace("/", "."), False


def _code_sig(co) -> tuple:
    consts = []
    for c in co.co_consts:
        if hasattr(c, "co_code"):
            consts.append(("<code>", c.co_name))
        else:
            consts.append((type(c).__name__, repr(c)))
    return (
        co.co_code, co.co_names, co.co_varnames, co.co_freevars, co.co_cellvars, tuple(consts),
        co.co_flags, co.co_argcount, co.co_kwonlyargcount, co.co_posonlyargcount, co.co_name, co.co_qualname,
    )


def _children(co) -> list:
    return [c for c in co.co_consts if hasattr(c, "co_code")]


def _subtree_equal(a, b) -> bool:
    if _code_sig(a) != _code_sig(b):
        return False
    ca, cb = _children(a), _children(b)
    return len(ca) == len(cb) and all(_subtree_equal(x, y) for x, y in zip(ca, cb))


def diff_code_trees(old_mod, new_mod) -> list[tuple] | None:
    """Pairs (old_top_func_code, new_top_func_code) whose subtree changed, or None when anything outside a
    function body changed (module code, class bodies, defaults, decorators...)."""
    changed: list[tuple] = []

    def walk(a, b) -> bool:
        is_func = bool(a.co_flags & CO_OPTIMIZED)
        if is_func:
            if not _subtree_equal(a, b):
                # pair this function and every nested code object (closures made at import time are live
                # function objects of the nested code and must be swapped too)
                return pair(a, b, True)
            return True
        if _code_sig(a) != _code_sig(b):
            return False
        ca, cb = _children(a), _children(b)
        if len(ca) != len(cb):
            return False
        return all(walk(x, y) for x, y in zip(ca, cb))

    def pair(a, b, top: bool) -> bool:
        if a.co_freevars != b.co_freevars or a.co_name != b.co_name or a.co_qualname != b.co_qualname:
            return False
        if a.co_firstlineno != b.co_firstlineno and not top:
            pass
        ca, cb = _children(a), _children(b)
        if len(ca) != len(cb):
            return False
        if top or not _subtree_equal(a, b):
            changed.append((a, b, top))
        return all(pair(x, y, False) for x, y in zip(ca, cb))

    def _unused(a, b) -> bool:
        is_func = bool(a.co_flags & CO_OPTIMIZED)
        if is_func:
            return True
        if _code_sig(a) != _code_sig(b):
            return False
        ca, cb = _children(a), _children(b)
        if len(ca) != len(cb):
            return False
        return all(walk(x, y) for x, y in zip(ca, cb))

    if not walk(old_mod, new_mod):
        return None
    return changed


def _new_changed_lines(hunks: list) -> set[int]:
    """New-file line numbers of added lines (or the insertion point of a pure deletion)."""
    out: set[int] = set()
    for _os, _oc, new_start, _nc, hunk_lines in hunks:
        ln = new_start
        pending_del = False
        for hl in hunk_lines:
            if hl.startswith("\\"):
                continue
            tag = hl[0]
            if tag == "+":
                out.add(ln)
                ln += 1
                pending_del = False
            elif tag == "-":
                pending_del = True
            else:
                if pending_del:
                    out.add(ln)
                    out.add(max(1, ln - 1))
                pending_del = False
                ln += 1
        if pending_del:
            out.add(max(1, ln - 1))
    return out


def _all_codes(co) -> list:
    res = [co]
    for c in _children(co):
        res.extend(_all_codes(c))
    return res


class WarmRunner:
    def __init__(self, debug: bool = False) -> None:
        self.ready = False
        self.debug = debug
        self.func_index: dict[tuple[str, str, int], list] = {}
        self.import_opened: set[str] = set()
        self.import_lines: dict[str, set[int]] = {}
        self.warm_failed = False
        self.exec_setup_done = False
        self.exec_setup_failed = False
        self.setup_lines: dict[str, set[int]] = {}
        self.last_cpu = 0.0
        self.why = ""

    # ---------------------------------------------------------------- warmup
    def warm(self) -> None:
        if self.ready or self.warm_failed:
            return
        try:
            self._warm()
            self.ready = True
        except BaseException as e:  # pragma: no cover
            self.warm_failed = True
            print(f"warmup failed: {e!r}", file=sys.stderr)

    def _warm(self) -> None:
        os.chdir(SQLGLOT_REAL)
        os.environ["SKIP_INTEGRATION"] = "0"
        if SQLGLOT_REAL not in sys.path:
            sys.path.insert(0, SQLGLOT_REAL)
        try:
            with gzip.open(IMPORT_LINES_PATH, "rt", encoding="utf-8") as gz:
                self.import_lines = {f: set(ls) for f, ls in json.load(gz).items()}
        except Exception:
            self.import_lines = {}
            self.no_import_lines = True

        orig_open = builtins.open
        orig_io_open = io.open
        orig_path_open = Path.open
        opened = self.import_opened

        def rec(target):
            try:
                s = os.fspath(target)
                if isinstance(s, bytes):
                    s = s.decode("utf-8", "ignore")
                rp = os.path.realpath(s)
                if rp.startswith(SQLGLOT_REAL + "/"):
                    opened.add(rp[len(SQLGLOT_REAL) + 1:])
            except Exception:
                pass

        def w_open(file, *a, **k):
            rec(file)
            return orig_open(file, *a, **k)

        def w_io_open(file, *a, **k):
            rec(file)
            return orig_io_open(file, *a, **k)

        def w_path_open(self_, *a, **k):
            rec(self_)
            return orig_path_open(self_, *a, **k)

        builtins.open, io.open, Path.open = w_open, w_io_open, w_path_open
        devnull = orig_open(os.devnull, "w")
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = devnull
        try:
            for p in sorted((SQLGLOT / "sqlglot").rglob("*.py")):
                rel = str(p.relative_to(SQLGLOT))
                if "__pycache__" in rel or rel.endswith("__main__.py"):
                    continue
                mn = _module_name_for(rel)
                if mn is None:
                    continue
                try:
                    __import__(mn[0])
                except BaseException:
                    pass
            for p in sorted((SQLGLOT / "tests").rglob("test_*.py")):
                rel = str(p.relative_to(SQLGLOT))
                if rel in NEVER_VERIFY_FILES:
                    continue
                try:
                    __import__(rel[:-3].replace("/", "."))
                except BaseException:
                    pass
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            devnull.close()
            builtins.open, io.open, Path.open = orig_open, orig_io_open, orig_path_open

        import gc
        import types
        prefix = SQLGLOT_REAL + "/sqlglot/"
        idx: dict[tuple[str, str, int], list] = defaultdict(list)
        for o in gc.get_objects():
            if type(o) is types.FunctionType:
                co = o.__code__
                if co.co_filename.startswith(prefix):
                    idx[(co.co_filename, co.co_qualname, co.co_firstlineno)].append(o)
        self.func_index = dict(idx)

    def ensure_exec_setup(self) -> None:
        """Run TestExecutor.setUpClass once in the warm parent (CSV loading, ~2 s) and record which sqlglot
        lines it executed; mode-A children then reuse the loaded tables."""
        if self.exec_setup_done or self.exec_setup_failed or not self.ready:
            return
        T = 5
        mon = sys.monitoring
        lines: dict[str, set[int]] = {}
        P = SQLGLOT_REAL + "/sqlglot/"
        try:
            mod = sys.modules.get("tests.test_executor") or __import__("tests.test_executor", fromlist=["TestExecutor"])
            cls = mod.TestExecutor

            def on_line(code, ln):
                f = code.co_filename
                if f.startswith(P):
                    lines.setdefault(f[len(SQLGLOT_REAL) + 1:], set()).add(ln)
                return mon.DISABLE

            mon.use_tool_id(T, "br_setup")
            mon.register_callback(T, mon.events.LINE, on_line)
            mon.set_events(T, mon.events.LINE)
            devnull = open(os.devnull, "w")
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = devnull
            try:
                cls.setUpClass()
            finally:
                sys.stdout, sys.stderr = old_out, old_err
                devnull.close()
                mon.set_events(T, 0)
                mon.register_callback(T, mon.events.LINE, None)
                mon.free_tool_id(T)
            self.setup_lines = lines
            self.exec_setup_done = True
        except BaseException as e:
            self.exec_setup_failed = True
            print(f"exec setup failed: {e!r}", file=sys.stderr)

    def exec_setup_conflict(self, touched: dict) -> bool:
        if not self.exec_setup_done:
            return False
        for rel, info in touched.items():
            if not rel.endswith(".py"):
                return True
            if info["old_lines"] & self.setup_lines.get(rel, set()):
                return True
        return False

    # ---------------------------------------------------------------- planning
    def plan_mode_a(self, touched: dict, patched_contents: dict[str, str]) -> list | None:
        """Returns a list of swaps [(funcs, new_code, codes_to_monitor)] or None if mode A is unsafe."""
        if not self.ready or not self.import_lines:
            return None
        swaps = []
        for rel, info in touched.items():
            if not rel.endswith(".py"):
                if rel in self.import_opened:
                    return None
                continue
            if not rel.startswith("sqlglot/"):
                return None
            new_src = patched_contents.get(rel)
            if new_src is None:
                return None
            abs_p = os.path.realpath(os.path.join(SQLGLOT_REAL, rel))
            try:
                old_src = (SQLGLOT / rel).read_text("utf-8")
                old_code = compile(old_src, abs_p, "exec", dont_inherit=True)
                new_code = compile(new_src, abs_p, "exec", dont_inherit=True)
            except SyntaxError:
                return None
            changed = diff_code_trees(old_code, new_code)
            if changed is None:
                self.why = f"non-body change {rel}"
                return None
            imp = self.import_lines.get(rel, set())
            if info["old_lines"] & imp:
                self.why = f"import-line {rel}"
                return None
            for oc, nc, top in changed:
                funcs = self.func_index.get((abs_p, oc.co_qualname, oc.co_firstlineno))
                if not funcs:
                    if not top:
                        continue
                    self.why = f"no live func {oc.co_qualname}"
                    return None
                live_codes = {id(f.__code__): f.__code__ for f in funcs}
                if len(live_codes) != 1:
                    return None
                live = next(iter(live_codes.values()))
                if not _subtree_equal(live, oc):
                    return None
                for f in funcs:
                    if len(f.__code__.co_freevars) != len(nc.co_freevars):
                        return None
                swaps.append((rel, funcs, nc))
        return swaps

    # ---------------------------------------------------------------- running
    def verify(self, mode: str, swaps, patched_files: dict[str, str], test_ids: list[tuple[int, str, float]],
               guard: dict[int, bool], new_lines: dict[str, set[int]], use_pickle: bool, budget_s: float) -> tuple[set[int], set[int]]:
        r_fd, w_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r_fd)
            try:
                passed, failed = self._child(mode, swaps, patched_files, test_ids, guard, new_lines, use_pickle, budget_s)
                payload = json.dumps({"p": passed, "f": failed}).encode("utf-8")
                with os.fdopen(w_fd, "wb") as wf:
                    wf.write(payload)
            except BaseException:
                try:
                    os.close(w_fd)
                except OSError:
                    pass
            os._exit(0)
        os.close(w_fd)
        chunks = []
        with os.fdopen(r_fd, "rb") as rf:
            while True:
                chunk = rf.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        _pid, _st, ru = os.wait4(pid, 0)
        self.last_cpu = ru.ru_utime + ru.ru_stime
        raw = b"".join(chunks)
        allnums = {n for n, _, _ in test_ids}
        if not raw:
            return set(), allnums
        try:
            data = json.loads(raw.decode("utf-8"))
            p = set(data["p"]) & allnums
            return p, allnums - p
        except Exception:
            return set(), allnums

    def _child(self, mode, swaps, patched_files, test_ids, guard, new_lines, use_pickle, budget_s):
        try:
            pass  # no address-space limit: numpy reserves large virtual areas
        except Exception:
            pass
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(max(5, int(budget_s) + 3))

        abs_patches: dict[str, str] = {}
        for rel, content in patched_files.items():
            abs_patches[os.path.realpath(os.path.join(SQLGLOT_REAL, rel))] = content

        executed: set[tuple[str, int]] = set()
        mon = sys.monitoring
        TOOL = 4

        if mode == "A":
            mon.use_tool_id(TOOL, "br_guard")
            code_file: dict[int, str] = {}

            def on_line(code, line):
                executed.add((code_file.get(id(code), ""), line))
                return mon.DISABLE

            mon.register_callback(TOOL, mon.events.LINE, on_line)
            for rel, funcs, nc in swaps:
                for f in funcs:
                    f.__code__ = nc
                for c in _all_codes(nc):
                    code_file[id(c)] = rel
                    mon.set_local_events(TOOL, c, mon.events.LINE)
        else:
            for name in list(sys.modules):
                if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests."):
                    del sys.modules[name]
            mod_map: dict[str, tuple[str, str, bool]] = {}
            for rel, content in patched_files.items():
                mn = _module_name_for(rel)
                if mn:
                    mod_map[mn[0]] = (content, os.path.realpath(os.path.join(SQLGLOT_REAL, rel)), mn[1])
            if mod_map:
                sys.meta_path.insert(0, _PatchFinder(mod_map))
            importlib.invalidate_caches()

        orig_open = builtins.open
        orig_io_open = io.open
        orig_path_open = Path.open

        def _patched_stream(target, mode_="r"):
            if any(m in mode_ for m in ("w", "a", "x", "+")):
                return None
            try:
                s = os.fspath(target)
                if isinstance(s, bytes):
                    s = s.decode("utf-8", errors="ignore")
                rp = os.path.realpath(s)
            except Exception:
                return None
            content = abs_patches.get(rp)
            if content is None:
                return None
            if "b" in mode_:
                return io.BytesIO(content.encode("utf-8"))
            return io.StringIO(content)

        def wrapped_open(file, mode_="r", *args, **kwargs):
            st = _patched_stream(file, mode_)
            return st if st is not None else orig_open(file, mode_, *args, **kwargs)

        def wrapped_io_open(file, mode_="r", *args, **kwargs):
            st = _patched_stream(file, mode_)
            return st if st is not None else orig_io_open(file, mode_, *args, **kwargs)

        def wrapped_path_open(self_, mode_="r", *args, **kwargs):
            st = _patched_stream(self_, mode_)
            return st if st is not None else orig_path_open(self_, mode_, *args, **kwargs)

        if abs_patches:
            builtins.open = wrapped_open
            io.open = wrapped_io_open
            Path.open = wrapped_path_open

        class _ConfiguredExec(_LazyImmediateExecutor):
            def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
                super().__init__(max_workers, mp_context, initializer, initargs, use_pickle=use_pickle)

        repl = {
            "ProcessPoolExecutor": (concurrent.futures.ProcessPoolExecutor, _ConfiguredExec),
            "as_completed": (concurrent.futures.as_completed, _lazy_as_completed),
            "wait": (concurrent.futures.wait, _lazy_wait),
        }
        for attr, (_orig, new) in repl.items():
            setattr(concurrent.futures, attr, new)
        # test modules imported by the warm parent hold their own references
        for mname, m in list(sys.modules.items()):
            if m is None or not (mname == "tests" or mname.startswith("tests.")):
                continue
            for attr, (orig, new) in repl.items():
                if getattr(m, attr, None) is orig:
                    setattr(m, attr, new)

        @contextlib.contextmanager
        def fast_subtest(self, msg=None, **params):
            try:
                yield
            except unittest.SkipTest:
                pass
            except _TestTimeout:
                raise
            except BaseException:
                raise _SubTestFailFast()

        unittest.TestCase.subTest = fast_subtest

        def on_timeout(signum, frame):
            raise _TestTimeout()

        devnull = orig_open(os.devnull, "w")
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = devnull
        sys.stderr = devnull

        passed: list[int] = []
        failed: list[int] = []
        loaded_classes: dict[tuple[str, str], type | None] = {}
        pre_setup: set[tuple[str, str]] = set()
        if mode == "A" and self.exec_setup_done:
            pre_setup.add(("tests/test_executor.py", "TestExecutor"))
        setup_done: set[tuple[str, str]] = set(pre_setup)
        grouped: dict[tuple[str, str], list[tuple[int, str, float]]] = defaultdict(list)
        for num, tid, sec in test_ids:
            parts = tid.split("::")
            if len(parts) != 3:
                failed.append(num)
                continue
            grouped[(parts[0], parts[1])].append((num, parts[2], sec))

        try:
            for (mod_file, cls_name), methods in grouped.items():
                key = (mod_file, cls_name)
                if key not in loaded_classes:
                    mod_name = mod_file[:-3].replace("/", ".")
                    try:
                        mod = __import__(mod_name, fromlist=[cls_name])
                        loaded_classes[key] = getattr(mod, cls_name)
                    except BaseException:
                        loaded_classes[key] = None
                cls = loaded_classes[key]
                if cls is None:
                    failed.extend(n for n, _, _ in methods)
                    continue
                if DEBUG2:
                    r_ = resource.getrusage(resource.RUSAGE_SELF)
                    print(f"    cls {cls_name} loaded cpu={r_.ru_utime + r_.ru_stime:.3f}", file=sys.__stderr__)
                if key not in setup_done:
                    setup_done.add(key)
                    if hasattr(cls, "setUpClass"):
                        try:
                            cls.setUpClass()
                        except BaseException:
                            failed.extend(n for n, _, _ in methods)
                            loaded_classes[key] = None
                            continue
                for num, meth_name, sec in methods:
                    if mode == "A":
                        executed.clear()
                        mon.set_events(TOOL, 0)
                        mon.restart_events()
                    ok = False
                    old_h = signal.signal(signal.SIGALRM, on_timeout)
                    signal.setitimer(signal.ITIMER_REAL, max(2.0, sec * 4 + 1.0))
                    try:
                        inst = cls(meth_name)
                        res = unittest.TestResult()
                        inst.run(res)
                        ok = res.wasSuccessful() and not res.skipped
                    except BaseException:
                        ok = False
                    finally:
                        signal.setitimer(signal.ITIMER_REAL, 0)
                        signal.signal(signal.SIGALRM, old_h)
                    if ok and mode == "A" and guard.get(num):
                        hit = any((f, ln) in executed for f, lns in new_lines.items() for ln in lns)
                        if not hit:
                            ok = False
                            if DEBUG2:
                                print(f"    guard-fail {num} {cls_name}.{meth_name}", file=sys.__stderr__)
                    elif not ok and DEBUG2:
                        print(f"    fail {num} {cls_name}.{meth_name} {res.failures[:1] or res.errors[:1] if 'res' in dir() else ''}"[:600], file=sys.__stderr__)
                    if DEBUG2:
                        r_ = resource.getrusage(resource.RUSAGE_SELF)
                        print(f"    t {num} {cls_name}.{meth_name} ok={ok} cpu={r_.ru_utime + r_.ru_stime:.3f} rec={sec}", file=sys.__stderr__)
                    (passed if ok else failed).append(num)
        finally:
            for key in setup_done - pre_setup:
                cls = loaded_classes.get(key)
                if cls is not None and hasattr(cls, "tearDownClass"):
                    try:
                        cls.tearDownClass()
                    except BaseException:
                        pass
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            devnull.close()
        return passed, failed


def estimate_verify_cpu_ms(test_nums, t_by_num: dict[int, dict], mode: str, warm_ready: bool) -> int:
    total = 30.0 if mode == "A" else 900.0
    if not warm_ready:
        total += 3500.0
    files_seen: set[str] = set()
    for n in test_nums:
        t = t_by_num[n]
        tf = t["file"]
        rec_ms = t["seconds"] * 1000.0
        if tf not in files_seen:
            files_seen.add(tf)
            if tf == "tests/test_executor.py":
                total += 1200.0
            elif mode == "B":
                total += 60.0
            else:
                total += 5.0
        if tf == "tests/test_optimizer.py":
            total += rec_ms * 1.15
        elif tf == "tests/test_executor.py":
            total += rec_ms * 1.00
        elif tf == "tests/test_generator.py":
            total += rec_ms * 0.50
        else:
            total += rec_ms * 0.65
    return int(total)



# ======================== COMPARISON-PROFILE PRUNING ========================
# cmp_profile.json.gz maps "file|line|col|op|alt" -> tests that executed that comparison (under two hash
# seeds) and for which the alternative operator never gave a different result.  When a change only swaps
# comparison operators, such tests execute exactly as before and cannot fail.

CMP_PROFILE_PATH = Path("/app/cmp_profile.json.gz")
_CMP_OPS = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Is: "is", ast.IsNot: "is not"}
_cmp_profile_cache: list = []


def load_cmp_profile() -> dict:
    if not _cmp_profile_cache:
        try:
            with gzip.open(CMP_PROFILE_PATH, "rt", encoding="utf-8") as g:
                _cmp_profile_cache.append(json.load(g))
        except Exception:
            _cmp_profile_cache.append({})
    return _cmp_profile_cache[0]


def _prim_fields(node) -> tuple:
    out = []
    for name, val in ast.iter_fields(node):
        if isinstance(val, ast.AST):
            continue
        if isinstance(val, list):
            out.append((name, len(val), tuple(type(v).__name__ for v in val if not isinstance(v, ast.AST))))
            continue
        out.append((name, type(val).__name__, repr(val)))
    return tuple(out)


def compare_only_changes(old_src: str, new_src: str) -> list[tuple[int, int, str, str]] | None:
    """[(line, col, old_op, new_op)] when the two sources differ only in single-operator comparisons."""
    try:
        a = ast.parse(old_src)
        b = ast.parse(new_src)
    except SyntaxError:
        return None
    wa, wb = list(ast.walk(a)), list(ast.walk(b))
    if len(wa) != len(wb):
        return None
    changes = []
    for x, y in zip(wa, wb):
        if type(x) is not type(y):
            if isinstance(x, ast.cmpop) and isinstance(y, ast.cmpop):
                continue  # handled at the Compare node
            return None
        if isinstance(x, ast.Compare):
            if len(x.ops) != len(y.ops):
                return None
            if [type(o) for o in x.ops] != [type(o) for o in y.ops]:
                if len(x.ops) != 1 or type(x.ops[0]) not in _CMP_OPS or type(y.ops[0]) not in _CMP_OPS:
                    return None
                changes.append((x.lineno, x.col_offset, _CMP_OPS[type(x.ops[0])], _CMP_OPS[type(y.ops[0])]))
            continue
        if isinstance(x, ast.cmpop):
            continue
        if _prim_fields(x) != _prim_fields(y):
            return None
    return changes


def removed_old_lines(hunks: list) -> set[int]:
    out: set[int] = set()
    for old_start, _oc, _ns, _nc, hunk_lines in hunks:
        ln = old_start
        for hl in hunk_lines:
            if hl.startswith("\\"):
                continue
            if hl[0] == "-":
                out.add(ln)
                ln += 1
            elif hl[0] == "+":
                pass
            else:
                ln += 1
    return out


def verify_overhead_ms(ver, t_by_num, mode: str, runner) -> int:
    files = {t_by_num[n]["file"] for n in ver}
    has_exec = "tests/test_executor.py" in files
    if mode == "A":
        total = 20
        if has_exec and not runner.exec_setup_done:
            total += 2200
    else:
        total = 900 + 45 * len(files)
        if has_exec:
            total += 2200
    return total


def main() -> None:
    debug = os.environ.get("BLAST_DEBUG") in ("1", "2")

    tests_json = json.loads((DATA / "tests.json").read_text("utf-8"))
    tests = tests_json["tests"]
    all_test_nums = {t["number"] for t in tests}
    t_by_num = {t["number"]: t for t in tests}
    dur_by_num = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    tests_by_file: dict[str, set[int]] = defaultdict(set)
    for t in tests:
        tests_by_file[t["file"]].add(t["number"])

    with gzip.open(MAP_PATH, "rt", encoding="utf-8") as gz:
        cov = json.load(gz)

    analyzer = StaticAnalyzer(cov)
    runner = WarmRunner(debug)
    try:
        with gzip.open(IMPORT_LINES_PATH, "rt", encoding="utf-8") as _gz:
            import_lines_all = {f: set(ls) for f, ls in json.load(_gz).items()}
    except Exception:
        import_lines_all = {}
    try:
        _tc = json.loads(Path("/app/test_costs.json").read_text("utf-8"))
        cost_table = {int(k): v for k, v in _tc["tests"].items()}
    except Exception:
        cost_table = {}

    def vcost_ms(n: int) -> int:
        c = cost_table.get(n)
        if c is None:
            c = int(t_by_num[n]["seconds"] * 1700) + 5
        return c + 2

    core_files = {
        "sqlglot/__init__.py",
        "sqlglot/errors.py",
        "sqlglot/helper.py",
        "sqlglot/tokens.py",
        "sqlglot/parser.py",
        "sqlglot/generator.py",
        "sqlglot/expressions/__init__.py",
        "sqlglot/expressions/core.py",
        "sqlglot/dialects/__init__.py",
        "sqlglot/dialects/dialect.py",
    }

    for patch_path in sorted((DATA / "commits").glob("*.patch")):
        commit_id = patch_path.stem
        patch_text = patch_path.read_text("utf-8", errors="replace")
        touched = parse_unified_diff(patch_text)

        selected: set[int] = set()
        fallback_all = False
        patched_contents: dict[str, str] = {}
        patch_clean = True
        has_py_semantic_change = False
        touched_serde = False

        for rel_path, info in touched.items():
            src_path = SQLGLOT / rel_path
            old_lines: set[int] = info["old_lines"]

            if rel_path in ("sqlglot/serde.py", "sqlglot/expressions/core.py"):
                touched_serde = True

            if not (info["is_deleted"] or info["is_new"] or info["is_binary"]) and src_path.exists() and info["hunks"]:
                old_text = src_path.read_text("utf-8", errors="replace")
                new_text = apply_hunks(old_text, info["hunks"])
                if new_text is not None:
                    patched_contents[rel_path] = new_text
                else:
                    patch_clean = False
            else:
                patch_clean = False

            if rel_path.startswith("tests/") and rel_path.endswith(".py"):
                has_py_semantic_change = True
                if rel_path in tests_by_file:
                    selected.update(tests_by_file[rel_path])
                else:
                    f_tests = analyzer.file_all_tests.get(rel_path)
                    if f_tests:
                        selected.update(f_tests)
                    else:
                        fallback_all = True
                continue

            if not rel_path.endswith(".py"):
                if rel_path in analyzer.open_map:
                    selected.update(analyzer.open_map[rel_path])
                elif rel_path.startswith("tests/fixtures/"):
                    for op, ts in analyzer.open_map.items():
                        if Path(op).parent == Path(rel_path).parent:
                            selected.update(ts)
                    if not selected:
                        fallback_all = True
                elif rel_path in ("pyproject.toml", "setup.py", "setup.cfg"):
                    selected.update(tests_by_file.get("tests/test_sqlglotc.py", set()))
                continue

            if info["is_deleted"] or info["is_new"] or info["is_binary"]:
                has_py_semantic_change = True
                if rel_path in analyzer.file_all_tests:
                    selected.update(analyzer.file_all_tests[rel_path])
                else:
                    fallback_all = True
                continue

            new_tree_for_file = None
            if rel_path in patched_contents and src_path.exists():
                old_text = src_path.read_text("utf-8", errors="replace")
                new_text = patched_contents[rel_path]
                harmless, has_syntax_err = is_harmless_py_change(old_text, new_text)
                if has_syntax_err:
                    has_py_semantic_change = True
                    if rel_path in core_files:
                        fallback_all = True
                        break
                    for tf in analyzer.imp_map.get(rel_path, ()):
                        selected.update(tests_by_file.get(tf, set()))
                    selected.update(analyzer.file_all_tests.get(rel_path, set()))
                    parts = Path(rel_path).parts
                    stem = Path(rel_path).stem.lower()
                    if len(parts) >= 3 and parts[0] == "sqlglot" and parts[1] in ("dialects", "parsers", "generators", "typing"):
                        selected.update(analyzer.dial_map.get(stem, ()))
                    if rel_path in analyzer.open_map:
                        selected.update(analyzer.open_map[rel_path])
                    if not selected:
                        fallback_all = True
                    continue
                if harmless:
                    continue
                try:
                    new_tree_for_file = ast.parse(new_text)
                except SyntaxError:
                    new_tree_for_file = None

            has_py_semantic_change = True

            if rel_path in analyzer.open_map:
                selected.update(analyzer.open_map[rel_path])

            f_lines = analyzer.line_map.get(rel_path, {})
            tree = analyzer.get_ast(rel_path)
            f_funcs = analyzer.func_map.get(rel_path, {})
            if tree is not None:
                callables = [
                    (n.lineno, n.end_lineno or n.lineno, n)
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                ]
                classes = [
                    (n.lineno, n.end_lineno or n.lineno, n)
                    for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef)
                ]
            else:
                callables = []
                classes = []

            for ln in old_lines:
                enc_c = [(s, e, n) for s, e, n in callables if s <= ln <= e]
                handled_as_body = False
                if enc_c:
                    inner_s, inner_e, inner_node = min(enc_c, key=lambda x: (x[1] - x[0], -x[0]))
                    if isinstance(inner_node, ast.Lambda) or (inner_node.body and ln >= inner_node.body[0].lineno):
                        selected.update(f_lines.get(str(ln), ()))
                        selected.update(f_funcs.get(str(inner_s), ()))
                        handled_as_body = True

                if not handled_as_body:
                    enc_cls = [(s, e, n) for s, e, n in classes if s <= ln <= e]
                    parts = Path(rel_path).parts
                    stem = Path(rel_path).stem.lower()

                    if len(parts) >= 3 and parts[0] == "sqlglot" and parts[1] == "expressions" and enc_cls:
                        cname = min(enc_cls, key=lambda x: x[1] - x[0])[2].name
                        expr_hits: set[int] = set()
                        for sub in analyzer.expr_subclasses.get(cname, {cname}):
                            expr_hits.update(analyzer.expr_map.get(sub, ()))
                        if expr_hits:
                            selected.update(expr_hits)
                        else:
                            selected.update(f_lines.get(str(ln), ()))
                            selected.update(analyzer.file_all_tests.get(rel_path, set()))
                    elif len(parts) >= 3 and parts[0] == "sqlglot" and parts[1] in ("dialects", "parsers", "generators", "typing"):
                        base_set = set(analyzer.dial_map.get(stem, ())) | analyzer.file_all_tests.get(rel_path, set())
                        attr_name = None
                        is_pure_const_attr = False
                        if enc_cls:
                            cls_node = min(enc_cls, key=lambda x: x[1] - x[0])[2]
                            for stmt in cls_node.body:
                                if stmt.lineno <= ln <= (stmt.end_lineno or stmt.lineno):
                                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                                        attr_name = stmt.targets[0].id
                                        is_pure_const_attr = is_pure_literal_expr(stmt.value)
                                    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                                        attr_name = stmt.target.id
                                        is_pure_const_attr = is_pure_literal_expr(stmt.value)

                        # Also confirm the new assignment value is a pure literal (cannot fail at import time)
                        if attr_name and is_pure_const_attr and new_tree_for_file is not None:
                            new_val_pure = False
                            for nnode in ast.walk(new_tree_for_file):
                                if isinstance(nnode, ast.Assign) and len(nnode.targets) == 1 and isinstance(nnode.targets[0], ast.Name) and nnode.targets[0].id == attr_name:
                                    new_val_pure = is_pure_literal_expr(nnode.value)
                                elif isinstance(nnode, ast.AnnAssign) and isinstance(nnode.target, ast.Name) and nnode.target.id == attr_name:
                                    new_val_pure = is_pure_literal_expr(nnode.value)
                            is_pure_const_attr = new_val_pure

                        if attr_name and is_pure_const_attr:
                            ref_tests = analyzer.runtime_attr_reads(attr_name)
                            if ref_tests is not None:
                                selected.update(base_set & ref_tests)
                            else:
                                selected.update(f_lines.get(str(ln), ()))
                                selected.update(base_set)
                        else:
                            selected.update(f_lines.get(str(ln), ()))
                            selected.update(base_set)
                    elif rel_path in analyzer.file_all_tests:
                        selected.update(f_lines.get(str(ln), ()))
                        selected.update(analyzer.file_all_tests[rel_path])
                    elif rel_path.startswith("sqlglotc/"):
                        selected.update(tests_by_file.get("tests/test_sqlglotc.py", set()))
                    else:
                        fallback_all = True

            if rel_path.startswith("sqlglotc/"):
                selected.update(tests_by_file.get("tests/test_sqlglotc.py", set()))

        if fallback_all:
            selected = set(all_test_nums)

        # comparison-operator swaps: drop tests that provably see identical results
        if selected and not fallback_all and patch_clean:
            cmp_safe: set[int] | None = None
            for rel, info in touched.items():
                if not rel.endswith(".py") or rel not in patched_contents or not rel.startswith("sqlglot/"):
                    cmp_safe = None
                    break
                old_src = (SQLGLOT / rel).read_text("utf-8", errors="replace")
                new_src = patched_contents[rel]
                chg = compare_only_changes(old_src, new_src)
                if chg is None:
                    cmp_safe = None
                    break
                if not chg:
                    continue
                prof = load_cmp_profile()
                imp = import_lines_all.get(rel, set())
                for line, col, op_old, op_new in chg:
                    if line in imp:
                        cmp_safe = set()
                        break
                    s = set(prof.get(f"{rel}|{line}|{col}|{op_old}|{op_new}", ()))
                    cmp_safe = s if cmp_safe is None else (cmp_safe & s)
                if cmp_safe is not None and not cmp_safe:
                    break
            if cmp_safe:
                before = len(selected)
                selected -= cmp_safe
                if debug:
                    print(f"  {commit_id}: cmp-profile pruned {before - len(selected)}", file=sys.stderr)

        verified_flag = False
        mode_used = "-"
        orig_count = len(selected)
        if selected and not fallback_all and patch_clean and patched_contents and has_py_semantic_change:
            kept_heavy = {
                n for n in selected
                if t_by_num[n]["id"] in NEVER_VERIFY_IDS
                or t_by_num[n]["file"] in NEVER_VERIFY_FILES
                or vcost_ms(n) > 6000
            }
            verifiable = selected - kept_heavy
            if verifiable:
                _, full_sim_ms = pack_workers(selected, dur_by_num)
                _, heavy_sim_ms = pack_workers(kept_heavy, dur_by_num)
                if full_sim_ms - heavy_sim_ms >= 300:
                    runner.warm()
                if runner.ready and full_sim_ms - heavy_sim_ms >= 300:
                    swaps = runner.plan_mode_a(touched, patched_contents)
                    mode = "A" if swaps is not None else "B"
                    # choose which light tests to verify: drop the ones whose verification costs much more
                    # than their share of the packed output
                    order = sorted(verifiable, key=lambda n: vcost_ms(n) - dur_by_num[n], reverse=True)
                    best = (full_sim_ms, None)
                    for k in range(0, min(6, len(order)) + 1):
                        keep = set(order[:k])
                        ver = verifiable - keep
                        if not ver:
                            break
                        est = verify_overhead_ms(ver, t_by_num, mode, runner) + sum(vcost_ms(n) for n in ver)
                        _, out_ms = pack_workers(kept_heavy | keep, dur_by_num)
                        if est + out_ms < best[0]:
                            best = (est + out_ms, ver, est)
                    if debug:
                        print(f"  {commit_id}: full={full_sim_ms} best={best[0]} mode={mode} why={getattr(runner, 'why', '')} nver={len(verifiable)} heavy={sorted(kept_heavy)}", file=sys.stderr)
                    runner.why = ""
                    if best[1] is not None and best[0] < full_sim_ms - 150:
                        ver = best[1]
                        est_cpu_ms = best[2]
                        if mode == "A" and any(t_by_num[n]["file"] == "tests/test_executor.py" for n in ver):
                            runner.ensure_exec_setup()
                            if runner.exec_setup_conflict(touched):
                                mode = "B"
                                swaps = None
                        guard: dict[int, bool] = {}
                        new_lines: dict[str, set[int]] = {}
                        if mode == "A":
                            for rel, info in touched.items():
                                if not rel.endswith(".py"):
                                    continue
                                new_lines[rel] = _new_changed_lines(info["hunks"])
                                rem = removed_old_lines(info["hunks"]) or info["old_lines"]
                                lm = analyzer.line_map.get(rel, {})
                                for ln in rem:
                                    for tnum in lm.get(str(ln), ()):
                                        guard[tnum] = True
                        verified_flag = True
                        mode_used = mode
                        test_id_list = [(n, t_by_num[n]["id"], t_by_num[n]["seconds"]) for n in sorted(ver, key=lambda x: (t_by_num[x]["file"], dur_by_num[x]))]
                        budget = est_cpu_ms / 1000.0 * 3 + 8
                        passed_set, _failed_set = runner.verify(mode, swaps, patched_contents, test_id_list, guard, new_lines, touched_serde, budget)
                        selected = selected - passed_set
                        if debug:
                            print(f"  {commit_id}: verify cpu={runner.last_cpu:.3f}s est={est_cpu_ms/1000:.3f}s passed={len(passed_set)}/{len(ver)}", file=sys.stderr)

        workers, final_cost_ms = pack_workers(selected, dur_by_num)
        if debug:
            print(
                f"DEBUG {commit_id}: orig={orig_count:3d} -> final={len(selected):3d} "
                f"verified={str(verified_flag):<5} mode={mode_used} sim={final_cost_ms/1000.0:6.3f}s",
                file=sys.stderr,
            )
        print(json.dumps({"commit": commit_id, "workers": workers}))

    if debug:
        ru_self = resource.getrusage(resource.RUSAGE_SELF)
        ru_child = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = ru_self.ru_utime + ru_self.ru_stime + ru_child.ru_utime + ru_child.ru_stime
        print(f"DEBUG TOTAL CPU: {cpu:.3f}s (self={ru_self.ru_utime+ru_self.ru_stime:.3f}s, child={ru_child.ru_utime+ru_child.ru_stime:.3f}s)", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1:] == ["--build-index"]:
        build_attr_index()
    else:
        main()
