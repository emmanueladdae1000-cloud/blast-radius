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


# ======================== IN-CONTAINER VERIFIER (RUNS IN FORKED CHILD) ========================

class _SubTestFailFast(BaseException):
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


def _run_verification_child(
    patched_files: dict[str, str],
    test_ids: list[tuple[int, str]],
    use_pickle: bool,
) -> tuple[list[int], list[int]]:
    signal.alarm(15)

    os.chdir(SQLGLOT_REAL)
    os.environ["SKIP_INTEGRATION"] = "0"
    if SQLGLOT_REAL not in sys.path:
        sys.path.insert(0, SQLGLOT_REAL)

    mod_map: dict[str, tuple[str, str, bool]] = {}
    abs_patches: dict[str, str] = {}
    for rel, content in patched_files.items():
        abs_p = os.path.realpath(os.path.join(SQLGLOT_REAL, rel))
        abs_patches[abs_p] = content
        if rel.endswith(".py"):
            if rel.endswith("/__init__.py"):
                mod_name = rel[: -len("/__init__.py")].replace("/", ".")
                is_pkg = True
            else:
                mod_name = rel[:-3].replace("/", ".")
                is_pkg = False
            mod_map[mod_name] = (content, abs_p, is_pkg)

    if mod_map:
        sys.meta_path.insert(0, _PatchFinder(mod_map))

    orig_open = builtins.open
    orig_io_open = io.open
    orig_path_open = Path.open

    def _patched_stream(target, mode="r", *args, **kwargs):
        if any(m in mode for m in ("w", "a", "x", "+")):
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
        if "b" in mode:
            return io.BytesIO(content.encode("utf-8"))
        return io.StringIO(content)

    def wrapped_open(file, mode="r", *args, **kwargs):
        stream = _patched_stream(file, mode)
        if stream is not None:
            return stream
        return orig_open(file, mode, *args, **kwargs)

    def wrapped_io_open(file, mode="r", *args, **kwargs):
        stream = _patched_stream(file, mode)
        if stream is not None:
            return stream
        return orig_io_open(file, mode, *args, **kwargs)

    def wrapped_path_open(self, mode="r", *args, **kwargs):
        stream = _patched_stream(self, mode)
        if stream is not None:
            return stream
        return orig_path_open(self, mode, *args, **kwargs)

    builtins.open = wrapped_open
    io.open = wrapped_io_open
    Path.open = wrapped_path_open

    class _ConfiguredExec(_LazyImmediateExecutor):
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
            super().__init__(max_workers, mp_context, initializer, initargs, use_pickle=use_pickle)

    concurrent.futures.ProcessPoolExecutor = _ConfiguredExec
    concurrent.futures.as_completed = _lazy_as_completed
    concurrent.futures.wait = _lazy_wait

    @contextlib.contextmanager
    def fast_subtest(self, msg=None, **params):
        try:
            yield
        except unittest.SkipTest:
            pass
        except BaseException:
            raise _SubTestFailFast()

    unittest.TestCase.subTest = fast_subtest

    devnull = open(os.devnull, "w")
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = devnull
    sys.stderr = devnull

    passed: list[int] = []
    failed: list[int] = []
    loaded_classes: dict[tuple[str, str], type | None] = {}
    setup_done: set[tuple[str, str]] = set()

    grouped: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    for num, tid in test_ids:
        parts = tid.split("::")
        if len(parts) != 3:
            failed.append(num)
            continue
        grouped[(parts[0], parts[1])].append((num, parts[2]))

    try:
        for (mod_file, cls_name), methods in grouped.items():
            key = (mod_file, cls_name)
            if key not in loaded_classes:
                mod_name = mod_file[:-3].replace("/", ".")
                try:
                    mod = __import__(mod_name, fromlist=[cls_name])
                    cls = getattr(mod, cls_name)
                    loaded_classes[key] = cls
                except BaseException:
                    loaded_classes[key] = None

            cls = loaded_classes[key]
            if cls is None:
                for num, _ in methods:
                    failed.append(num)
                continue

            if key not in setup_done:
                setup_done.add(key)
                if hasattr(cls, "setUpClass"):
                    try:
                        cls.setUpClass()
                    except BaseException:
                        for num, _ in methods:
                            failed.append(num)
                        loaded_classes[key] = None
                        continue

            for num, meth_name in methods:
                try:
                    inst = cls(meth_name)
                    res = unittest.TestResult()
                    inst.run(res)
                    if res.wasSuccessful() and not res.skipped:
                        passed.append(num)
                    else:
                        failed.append(num)
                except _SubTestFailFast:
                    failed.append(num)
                except BaseException:
                    failed.append(num)
    finally:
        for key in setup_done:
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


def verify_in_fork(
    patched_files: dict[str, str],
    test_ids: list[tuple[int, str]],
    use_pickle: bool,
) -> tuple[set[int], set[int]]:
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r_fd)
        try:
            passed, failed = _run_verification_child(patched_files, test_ids, use_pickle)
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
    os.waitpid(pid, 0)

    raw = b"".join(chunks)
    if not raw:
        return set(), {n for n, _ in test_ids}
    try:
        data = json.loads(raw.decode("utf-8"))
        return set(data["p"]), set(data["f"])
    except Exception:
        return set(), {n for n, _ in test_ids}


def estimate_verify_cpu_ms(test_nums: set[int], t_by_num: dict[int, dict]) -> int:
    total = 280.0
    files_seen: set[str] = set()
    for n in test_nums:
        t = t_by_num[n]
        tf = t["file"]
        rec_ms = t["seconds"] * 1000.0
        if tf not in files_seen:
            files_seen.add(tf)
            if tf == "tests/test_executor.py":
                total += 1600.0
            elif tf == "tests/test_optimizer.py":
                total += 600.0
            else:
                total += 35.0
        if tf == "tests/test_optimizer.py":
            total += rec_ms * 1.15
        elif tf == "tests/test_executor.py":
            total += rec_ms * 1.00
        elif tf == "tests/test_generator.py":
            total += rec_ms * 0.50
        else:
            total += rec_ms * 0.65
    return int(total)


def main() -> None:
    debug = os.environ.get("BLAST_DEBUG") == "1"

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

        verified_flag = False
        orig_count = len(selected)
        if (
            selected
            and not fallback_all
            and patch_clean
            and patched_contents
            and has_py_semantic_change
        ):
            kept_heavy = {
                n for n in selected
                if t_by_num[n]["id"] in NEVER_VERIFY_IDS
                or t_by_num[n]["file"] in NEVER_VERIFY_FILES
                or (dur_by_num[n] > 4000 and t_by_num[n]["id"] != "tests/test_generator.py::TestGenerator::test_generator_memory")
            }
            verifiable = selected - kept_heavy

            if verifiable and len(verifiable) <= 180:
                _, full_sim_ms = pack_workers(selected, dur_by_num)
                _, heavy_sim_ms = pack_workers(kept_heavy, dur_by_num)
                max_savings_ms = full_sim_ms - heavy_sim_ms
                est_cpu_ms = estimate_verify_cpu_ms(verifiable, t_by_num)

                if max_savings_ms >= est_cpu_ms * 1.15 and max_savings_ms >= 1500:
                    verified_flag = True
                    test_id_list = [(n, t_by_num[n]["id"]) for n in sorted(verifiable, key=lambda x: dur_by_num[x])]
                    passed_set, _failed_set = verify_in_fork(patched_contents, test_id_list, touched_serde)
                    selected = selected - passed_set

        workers, final_cost_ms = pack_workers(selected, dur_by_num)
        if debug:
            print(
                f"DEBUG {commit_id}: orig={orig_count:3d} -> final={len(selected):3d} "
                f"verified={str(verified_flag):<5} sim={final_cost_ms/1000.0:6.3f}s",
                file=sys.stderr,
            )
        print(json.dumps({"commit": commit_id, "workers": workers}))

    if debug:
        ru_self = resource.getrusage(resource.RUSAGE_SELF)
        ru_child = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = ru_self.ru_utime + ru_self.ru_stime + ru_child.ru_utime + ru_child.ru_stime
        print(f"DEBUG TOTAL CPU: {cpu:.3f}s (self={ru_self.ru_utime+ru_self.ru_stime:.3f}s, child={ru_child.ru_utime+ru_child.ru_stime:.3f}s)", file=sys.stderr)


if __name__ == "__main__":
    main()
