"""Run candidate tests for a patch inside this container, against the patched code.

Only used for patches whose every real edit sits inside function bodies that never
run at import time. The patched functions are swapped in place (func.__code__), so
every reference to them (dispatch tables, closures, decorators) sees the new code.
Each patch is checked in its own forked child of a parent that imported sqlglot and
the needed test modules once.
"""
import gc
from functools import lru_cache
import json
import os
import pickle
import signal
import struct
import sys
import tempfile
import time
import types
import weakref
import unittest


def _setup(src_root):
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, src_root)
    import concurrent.futures as cf

    def rt(x):
        return pickle.loads(pickle.dumps(x))

    class SyncExecutor(cf.Executor):
        """Process-pool stand-in: same pickling, but runs in this (patched) process."""

        def __init__(self, *a, initializer=None, initargs=(), **k):
            if initializer:
                initializer(*rt(initargs))

        def submit(self, fn, /, *args, **kwargs):
            f = cf.Future()
            try:
                f.set_result(rt(fn(*rt(args), **rt(kwargs))))
            except BaseException as e:
                f.set_exception(e)
            return f

        def shutdown(self, wait=True, *, cancel_futures=False):
            pass

    cf.ProcessPoolExecutor = SyncExecutor


def _own(code):
    consts = tuple(("<code>", c.co_qualname) if isinstance(c, types.CodeType) else _constant_key(c) for c in code.co_consts)
    return (code.co_code, code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars, consts,
            code.co_flags, code.co_argcount, code.co_kwonlyargcount, code.co_posonlyargcount,
            code.co_exceptiontable, code.co_nlocals)


def _constant_key(value):
    """Compiler constants need type-aware equality (True != 1, +0.0 != -0.0)."""
    if isinstance(value, types.CodeType):
        return (types.CodeType, _code_key(value))
    if type(value) in (tuple, frozenset):
        values = [_constant_key(v) for v in value]
        return (type(value), frozenset(values) if type(value) is frozenset else tuple(values))
    if type(value) is float:
        return (float, struct.pack("!d", value))
    if type(value) is complex:
        return (complex, struct.pack("!dd", value.real, value.imag))
    return (type(value), repr(value))


def _code_key(code):
    # Source positions do not affect execution. Every executable child, constant,
    # exception handler, name, calling convention and closure slot does.
    return (_own(code), code.co_name, code.co_qualname, code.co_stacksize,
            tuple(_constant_key(c) for c in code.co_consts))


@lru_cache(maxsize=64)
def _original_code(path, source):
    # Patches are written as UTF-8 source files. Compile bytes so a changed
    # coding cookie has the same effect here as when Python imports that file.
    return compile(source.encode("utf-8"), path, "exec", dont_inherit=True)


@lru_cache(maxsize=64)
def _original_key(path, source):
    return _code_key(_original_code(path, source))


def compiled_equivalent(path, old_source, new_code):
    """Compare complete compiled modules, including nested executable code."""
    return _original_key(path, old_source) == _code_key(new_code)


def _codes(code, out, top=True):
    out.setdefault(code.co_qualname if not top else "<module>", []).append(code)
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            _codes(c, out, False)
    return out


@lru_cache(maxsize=32)
def _old_code_tree(path, source):
    return _codes(_original_code(path, source), {})


def _plan_swaps(path, old_src, new_src):
    """Return [(qualname, old_code, new_code)] for changed function bodies, or None if
    the change touches module/class-body code (not swappable)."""
    old = _old_code_tree(path, old_src)
    new = _codes(compile(new_src.encode("utf-8"), path, "exec", dont_inherit=True), {})
    if set(old) != set(new):
        return None
    swaps = []
    for q, olist in old.items():
        nlist = new[q]
        if len(olist) != len(nlist):
            return None
        for oc, nc in zip(olist, nlist):
            if _own(oc) != _own(nc):
                if q == "<module>" or not (oc.co_flags & 0x02):  # 0x02 = CO_NEWLOCALS: a function
                    return None
                if oc.co_freevars != nc.co_freevars:
                    return None
                swaps.append((q, oc, nc))
    return swaps


def _function_index(paths):
    index = {}
    for obj in gc.get_objects():
        if type(obj) is types.FunctionType:
            code = obj.__code__
            if code.co_filename in paths:
                key = (code.co_filename, code.co_qualname, code.co_firstlineno)
                index.setdefault(key, []).append(weakref.ref(obj))
    return index

def _apply(path, swaps, function_index):
    """Swap code of live functions. Changed nested functions whose parent is also
    swapped need no live instance."""
    targets = {}
    for q, oc, nc in swaps:
        targets[(q, oc.co_firstlineno)] = nc
    hit = set()
    for (qualname, firstline), new_code in targets.items():
        for reference in function_index.get((path, qualname, firstline), ()):
            obj = reference()
            if obj is None:
                continue
            code = obj.__code__
            if (code.co_filename, code.co_qualname, code.co_firstlineno) != (path, qualname, firstline):
                continue
            obj.__code__ = new_code
            hit.add((qualname, firstline))
    swapped_q = {q for (q, _l) in hit}
    for q, oc, nc in swaps:
        if (q, oc.co_firstlineno) in hit:
            continue
        parent = q.rsplit(".<locals>.", 1)[0] if ".<locals>." in q else None
        if parent is None or parent not in {s[0] for s in swaps}:
            return False
    return True


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad = False
        self.failfast = True

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.bad = True
        super().addSubTest(test, subtest, err)


def _verification_job(job):
    """Accept legacy four-field jobs and optional independent CPU budgets."""
    key, files, order, timeout = job[:4]
    cpu_budget = job[4] if len(job) > 4 else min(5.0, timeout)
    return key, files, order, timeout, cpu_budget


def _arm_cpu_budget(seconds):
    # Default termination cannot be swallowed by unittest's exception handlers.
    # A terminated child never contributes a successful verification result.
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_PROF, max(0.001, seconds))


def _children_cpu():
    import resource
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def run(jobs, ids, src_root, deadline, log):
    """jobs: (key, {rel: new_src}, [tidx], wall_timeout_s[, cpu_budget_s]).
    Returns {key: (failed_tidx_list, unrun_tidx_list)} for jobs that completed."""
    jobs = [_verification_job(job) for job in jobs]
    # Compilation belongs in the shared parent: child-local caches disappear at
    # exit and would recompile the same large baseline module for every patch.
    planned = {}
    for key, files, _, _, _ in jobs:
        try:
            plans = []
            for rel, new_src in files.items():
                path = os.path.join(src_root, rel)
                with open(path, encoding="utf-8") as fh:
                    swaps = _plan_swaps(path, fh.read(), new_src)
                if swaps is None:
                    break
                plans.append((path, swaps))
            else:
                planned[key] = plans
        except (OSError, SyntaxError, ValueError):
            pass
    jobs = [job for job in jobs if job[0] in planned]
    if not jobs:
        return {}
    _setup(src_root)
    import importlib

    mods = {}
    need = sorted({ids[t].split("::")[0] for _, _, ts, _, _ in jobs for t in ts})
    for f in need:
        name = f[:-3].replace("/", ".")
        try:
            mods[f] = importlib.import_module(name)
        except BaseException as e:
            log("verify: import failed", name, repr(e)[:200])
    for _, files, _, _, _ in jobs:
        for rel in files:
            importlib.import_module(rel[:-3].replace("/", "."))
    gc.collect()
    function_index = _function_index({os.path.join(src_root, rel) for _, files, _, _, _ in jobs for rel in files})
    import resource
    ru = resource.getrusage(resource.RUSAGE_SELF)
    log("verify: parent setup cpu %.3f" % (ru.ru_utime + ru.ru_stime))
    results = {}
    tmpdir = "/tmp"
    previous_cpu = _children_cpu()
    for key, files, order, tmo, cpu_budget in jobs:
        tmo = min(tmo, deadline - time.time())
        if tmo < 3.0:
            log("verify: deadline reached")
            break
        out = os.path.join(tmpdir, f"v_{os.getpid()}_{key}.json")
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                _arm_cpu_budget(cpu_budget)
                ok = True
                for path, swaps in planned[key]:
                    if not _apply(path, swaps, function_index):
                        ok = False
                        break
                failed, unrun = [], []
                if ok:
                    failed, unrun = _run_tests(order, ids, mods)
                    with open(out, "w") as fh:
                        json.dump({"failed": failed, "unrun": unrun}, fh)
                    code = 0
            except BaseException:
                code = 1
            finally:
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                except BaseException:
                    pass
                os._exit(code)
        status = _wait(pid, tmo)
        if status is None:
            log("verify: timeout", key)
        cpu_now = _children_cpu()
        log("verify: job %s cpu %.3f" % (key, cpu_now - previous_cpu))
        previous_cpu = cpu_now
        if status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 and os.path.exists(out):
            with open(out) as fh:
                d = json.load(fh)
            results[key] = (d["failed"], d["unrun"])
        else:
            log("verify: not verified", key)
        try:
            os.remove(out)
        except OSError:
            pass
    return results


def _install_finder(src_root, files):
    import importlib.abc
    import importlib.util

    mods = {rel[:-3].replace("/", ".").removesuffix(".__init__"): (rel, src) for rel, src in files.items()}

    class Loader(importlib.abc.SourceLoader):
        def __init__(self, name):
            self.name = name

        def get_filename(self, fullname):
            return os.path.join(src_root, mods[fullname][0])

        def get_data(self, path):
            for rel, src in mods.values():
                if path == os.path.join(src_root, rel):
                    return src.encode("utf-8")
            with open(path, "rb") as fh:
                return fh.read()

        def path_stats(self, path):
            raise OSError  # never use a cached .pyc for the patched module

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname in mods:
                rel = mods[fullname][0]
                fn = os.path.join(src_root, rel)
                is_pkg = rel.endswith("__init__.py")
                return importlib.util.spec_from_file_location(
                    fullname, fn, loader=Loader(fullname),
                    submodule_search_locations=[os.path.dirname(fn)] if is_pkg else None)
            return None

    sys.meta_path.insert(0, Finder())
    return mods


def _import_patched(mods):
    """Import sqlglot and each patched module; build dialect classes they belong to."""
    import importlib
    import sqlglot  # noqa: F401
    for name in mods:
        importlib.import_module(name)
        if name.startswith(("sqlglot.dialects.", "sqlglot.parsers.", "sqlglot.generators.", "sqlglot.typing.")):
            d = name.rsplit(".", 1)[1]
            from sqlglot.dialects.dialect import Dialect
            try:
                dia = Dialect.get_or_raise(d)
            except ValueError:
                dia = None
            if dia is not None:
                dia.tokenizer_class, dia.parser_class, dia.generator_class


def _wait(pid, timeout):
    deadline = time.time() + timeout
    while True:
        r_pid, st = os.waitpid(pid, os.WNOHANG)
        if r_pid:
            return st
        if time.time() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            return None
        time.sleep(0.002)


def _run_tests(order, ids, mods_by_file):
    failed, unrun = [], []
    setup_done = set()
    for i, t in enumerate(order):
        f, c, m = ids[t].split("::")
        mod = mods_by_file.get(f)
        if mod is None:
            return failed, order[i:]
        cls = getattr(mod, c)
        class_key = (f, c)
        if class_key not in setup_done:
            setup_done.add(class_key)
            try:
                cls.setUpClass()
            except BaseException:
                return failed, order[i:]
        r = _Result()
        cls(m).run(r)
        if r.skipped or r.testsRun != 1:
            # A skipped method did not validate the patched behavior.
            return failed, order[i:]
        if r.bad or r.failures or r.errors or r.unexpectedSuccesses:
            failed.append(t)
            return failed, order[i + 1:]
    return failed, unrun


def _fresh_result(files, order, ids, src_root):
    """Run the same complete patched import and original test methods in either worker."""
    import importlib
    _setup(src_root)
    mods = _install_finder(src_root, files)
    try:
        _import_patched(mods)
    except BaseException as error:
        sys.stderr.write("verify-fresh: import broken %r\n" % (error,))
        return {"broken": True}
    by_file = {}
    for file in sorted({ids[t].split("::")[0] for t in order}):
        try:
            by_file[file] = importlib.import_module(file[:-3].replace("/", "."))
        except BaseException:
            pass
    failed, unrun = _run_tests(order, ids, by_file)
    return {"failed": failed, "unrun": unrun}


def _fresh_worker(request_path, output_path):
    # Even direct callers receive no test output on their answer channel.
    os.dup2(2, 1)
    try:
        with open(request_path, encoding="utf-8") as source:
            request = json.load(source)
        result = _fresh_result(request["files"], request["order"], request["ids"], request["src_root"])
        with open(output_path, "w", encoding="utf-8") as output:
            json.dump(result, output)
        return 0
    except BaseException:
        return 1


def run_fresh(jobs, ids, src_root, deadline, log):
    """Verification for edits outside function bodies: each job runs in a child that
    imports the patched modules from the patch text (class bodies and metaclasses run
    again), then the candidate tests. Returns {key: ("broken", None) | (failed, unrun)}."""
    jobs = [_verification_job(job) for job in jobs]
    clean_exec = os.environ.get("BR_FRESH_EXEC", "1") != "0"
    results = {}
    # third-party libraries the test modules need do not depend on sqlglot: import
    # them once here so every forked child shares them.
    need = {ids[t].split("::")[0] for _, _, order, _, _ in jobs for t in order}
    if not clean_exec and need & {"tests/test_optimizer.py", "tests/test_executor.py", "tests/test_schema.py"}:
        try:
            import duckdb  # noqa: F401
            import numpy  # noqa: F401
            import pandas  # noqa: F401
            import pandas.testing  # noqa: F401
        except BaseException:
            pass
    import unittest.mock  # noqa: F401
    previous_cpu = _children_cpu()
    for key, files, order, tmo, cpu_budget in jobs:
        tmo = min(tmo, deadline - time.time())
        if tmo < 3.0:
            log("verify-fresh: deadline reached")
            break
        out = os.path.join("/tmp", f"vf_{os.getpid()}_{key}.json")
        request_path = None
        if clean_exec:
            fd, request_path = tempfile.mkstemp(prefix="vf_request_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as request:
                json.dump({"files": files, "order": order, "ids": ids, "src_root": src_root}, request)
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                _arm_cpu_budget(cpu_budget)
                if clean_exec:
                    # exec discards the selector's large coverage/operand heap.
                    # Its CPU timer survives exec, covering startup and imports.
                    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__),
                                             "--fresh-worker", request_path, out])
                result = _fresh_result(files, order, ids, src_root)
                with open(out, "w") as fh:
                    json.dump(result, fh)
                code = 0
            except BaseException:
                code = 1
            finally:
                os._exit(code)
        st = _wait(pid, tmo)
        cpu_now = _children_cpu()
        log("verify-fresh: job %s cpu %.3f" % (key, cpu_now - previous_cpu))
        previous_cpu = cpu_now
        if st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0 and os.path.exists(out):
            with open(out) as fh:
                d = json.load(fh)
            results[key] = ("broken", None) if d.get("broken") else (d["failed"], d["unrun"])
        else:
            log("verify-fresh: not verified", key)
        try:
            os.remove(out)
        except OSError:
            pass
        if request_path is not None:
            try:
                os.remove(request_path)
            except OSError:
                pass
    return results


def import_check(src_root, files, log, timeout=30.0):
    """Import the patched modules (and sqlglot) in a throwaway child. False if that raises."""
    pid = os.fork()
    if pid == 0:
        code = 3
        try:
            os.environ["SKIP_INTEGRATION"] = "0"
            sys.path.insert(0, src_root)
            _import_patched(_install_finder(src_root, files))
            code = 0
        except BaseException as e:
            try:
                sys.stderr.write("importcheck: %r\n" % (e,))
            except BaseException:
                pass
            code = 4
        finally:
            os._exit(code)
    st = _wait(pid, timeout)
    ok = st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0
    if not ok:
        log("importcheck failed", sorted(files))
    return ok


def tpcds_probe(src_root, pairs, changed, deadline, log):
    """Run the edited TPC-DS queries exactly as test_execute_tpcds does (parse, execute
    with sqlglot's executor, compare with DuckDB through subtestHelper). True only if
    every edited query passes; the other 98 queries are unchanged."""
    tmo = deadline - time.time()
    if tmo < 5:
        return False
    out = os.path.join("/tmp", f"tp_{os.getpid()}.json")
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            _setup(src_root)
            from sqlglot import parse_one
            import tests.test_executor as te

            def rt(x):
                return pickle.loads(pickle.dumps(x))

            te.TestExecutor.setUpClass()
            te.TestExecutor.tpcds_sqls = [tuple(p) for p in pairs]

            class Probe(te.TestExecutor):
                def runTest(self):
                    exprs = [parse_one(self.tpcds_sqls[i][1]) for i in changed]
                    te.initializer(te.TPCDS_SCHEMA, self.tpcds_tables)
                    for i, e in zip(changed, exprs):
                        args = self.tpcds_sqls[i][0]
                        table = rt(te.mp_execute(rt(e), rt(args)))
                        if table is not None:
                            self.subtestHelper(i, table, tpch=False)

            r = _Result()
            Probe().run(r)
            ok = not (r.bad or r.failures or r.errors)
            with open(out, "w") as fh:
                json.dump({"ok": ok}, fh)
            code = 0
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    st = _wait(pid, tmo)
    ok = False
    if st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0 and os.path.exists(out):
        with open(out) as fh:
            ok = bool(json.load(fh).get("ok"))
    try:
        os.remove(out)
    except OSError:
        pass
    log("tpcds-probe", changed, "pass" if ok else "fail/unknown")
    return ok


def tpcds_code_probe(src_root, files, qidx, deadline, log):
    """Run test_execute_tpcds's per-query work (parse, execute with sqlglot's executor, compare with
    DuckDB) for the queries that reach the edited lines, on the patched sources.  Stops at the first
    failing query.  True only if every listed query passes; the remaining queries never execute a
    changed line (per-query coverage from a cold state), so they behave as on the pinned commit."""
    tmo = deadline - time.time()
    if tmo < 5:
        return False
    if not qidx:
        return True
    out = os.path.join("/tmp", f"tpc_{os.getpid()}.json")
    rows = _tpcds_rows(src_root, log)
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            # the parent may already hold the unpatched modules: drop them so the patched
            # sources are what gets imported here
            for name in list(sys.modules):
                if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                    del sys.modules[name]
            import resource as _r
            _c = lambda: _r.getrusage(_r.RUSAGE_SELF).ru_utime + _r.getrusage(_r.RUSAGE_SELF).ru_stime
            tt = [_c()]
            _setup(src_root)
            mods = _install_finder(src_root, files)
            _import_patched(mods)
            from sqlglot import parse_one
            import tests.test_executor as te
            tt.append(_c())
            for rel in files:  # refuse to report a pass unless the patched source is the one loaded
                m = sys.modules.get(rel[:-3].replace("/", ".").removesuffix(".__init__"))
                if m is None or getattr(m, "__spec__", None) is None or type(m.__spec__.loader).__name__ != "Loader":
                    raise RuntimeError("patched module not loaded: " + rel)

            def rt(x):
                return pickle.loads(pickle.dumps(x))

            if rows is not None:
                _tpcds_fast_setup(te, rows)
                te.initializer(te.TPCDS_SCHEMA, te.TestExecutor.tpcds_tables)  # tables built on first use
            else:
                te.TestExecutor.setUpClass()
                te.initializer(*rt((te.TPCDS_SCHEMA, te.TestExecutor.tpcds_tables)))
            tt.append(_c())
            res = _Result()
            import cmpprune
            base_ms = (cmpprune._load("tpcds_lines.json.gz") or {}).get("ms") or []

            class _Slow(BaseException):
                pass

            def _on_prof(signum, frame):
                raise _Slow()

            signal.signal(signal.SIGPROF, _on_prof)
            state = {"slow": False}

            class Probe(te.TestExecutor):
                def runTest(self):
                    for i in qidx:
                        args, sql, _ = self.tpcds_sqls[i]
                        # a query far slower than on the pinned commit would time out in the
                        # real run: count it as a failure
                        lim = 3.0 * (base_ms[i] if i < len(base_ms) else 700) / 1000.0 + 1.5
                        signal.setitimer(signal.ITIMER_PROF, lim)
                        try:
                            table = rt(te.mp_execute(rt(parse_one(sql)), rt(args)))
                            if table is not None:
                                self.subtestHelper(i, table, tpch=False)
                        except _Slow:
                            state["slow"] = True
                            return
                        finally:
                            signal.setitimer(signal.ITIMER_PROF, 0)
                        if res.bad:
                            return

            Probe().run(res)
            ok = not (state["slow"] or res.bad or res.failures or res.errors or res.skipped)
            tt.append(_c())
            with open(out, "w") as fh:
                json.dump({"ok": ok, "t": [round(b - a, 3) for a, b in zip(tt, tt[1:])]}, fh)
            code = 0
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    st = _wait(pid, tmo)
    ok = False
    steps = None
    if st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0 and os.path.exists(out):
        with open(out) as fh:
            d = json.load(fh)
            ok, steps = bool(d.get("ok")), d.get("t")
    try:
        os.remove(out)
    except OSError:
        pass
    log("tpcds-code-probe", len(qidx), "pass" if ok else "fail/unknown", "cpu import/setup/queries", steps)
    return ok


def _fresh_patched(src_root, files):
    """In a forked child: drop any sqlglot/tests modules the parent holds, import the patched sources."""
    for name in list(sys.modules):
        if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
            del sys.modules[name]
    _setup(src_root)
    mods = _install_finder(src_root, files)
    _import_patched(mods)
    for rel in files:
        m = sys.modules.get(rel[:-3].replace("/", ".").removesuffix(".__init__"))
        if m is None or type(getattr(m, "__spec__", None).loader).__name__ != "Loader":
            raise RuntimeError("patched module not loaded: " + rel)


def _child_result(fn, tmo, tag):
    out = os.path.join("/tmp", f"{tag}_{os.getpid()}.json")
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            res = fn()
            with open(out, "w") as fh:
                json.dump(res, fh)
            code = 0
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    st = _wait(pid, tmo)
    res = None
    if st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0 and os.path.exists(out):
        with open(out) as fh:
            res = json.load(fh)
    try:
        os.remove(out)
    except OSError:
        pass
    return res


def maxnodes_check(src_root, files, mn_tidx, others, ids, deadline, log, cap_s=2.5):
    """test_max_nodes on a code edit.  Step 1: run it for at most cap_s CPU seconds; a failure in
    that window means keep it.  Step 2: run every other remaining candidate on the patched sources.
    Returns (drop_max_nodes, passed_others): drop only when step 1 did not fail and step 2 passed
    every test (on mutants of the pinned commit test_max_nodes never failed while all the other
    tests that execute the edited line passed)."""
    tmo = deadline - time.time()
    if tmo < 10:
        return False, []

    def step1():
        _fresh_patched(src_root, files)
        import importlib
        f, c, m = ids[mn_tidx].split("::")
        mod = importlib.import_module(f[:-3].replace("/", "."))
        state = {"cut": False}

        def on_prof(signum, frame):
            state["cut"] = True
            raise KeyboardInterrupt()

        signal.signal(signal.SIGPROF, on_prof)
        r = _Result()
        signal.setitimer(signal.ITIMER_PROF, cap_s)
        try:
            getattr(mod, c)(m).run(r)
        except KeyboardInterrupt:
            pass
        finally:
            signal.setitimer(signal.ITIMER_PROF, 0)
        failed = bool(r.bad or r.failures or r.errors or r.unexpectedSuccesses)
        return {"cut": state["cut"], "failed": failed and not state["cut"]}

    a = _child_result(step1, min(cap_s * 4 + 15, tmo), "mn1")
    if a is None or a["failed"] or not a["cut"]:
        # failed fast (keep), crashed (keep), or finished: a finished passing run is itself proof
        if a is not None and not a["failed"] and not a["cut"]:
            log("maxnodes: completed and passed")
            return True, []
        log("maxnodes: step1", a)
        return False, []
    if not others:
        log("maxnodes: no other candidates")
        return False, []

    def step2():
        import importlib
        _fresh_patched(src_root, files)
        by_file = {}
        for f in sorted({ids[t].split("::")[0] for t in others}):
            by_file[f] = importlib.import_module(f[:-3].replace("/", "."))
        failed, unrun = _run_tests(others, ids, by_file)
        return {"failed": failed, "unrun": unrun}

    b = _child_result(step2, max(5.0, deadline - time.time()), "mn2")
    if b is None or b["failed"] or b["unrun"]:
        log("maxnodes: others", None if b is None else (len(b["failed"]), len(b["unrun"])))
        return False, []
    log("maxnodes: all", len(others), "others passed -> drop")
    return True, list(others)


def _fp(val, depth, seen):
    """Stable text fingerprint of an import-time value (no addresses)."""
    import enum
    t = type(val)
    if val is None or t in (bool, int, float, complex, str, bytes):
        return repr(val)
    if depth <= 0:
        return "<" + t.__qualname__ + ">"
    vid = id(val)
    if vid in seen:
        return "<cyc:" + t.__qualname__ + ">"
    seen.add(vid)
    try:
        if isinstance(val, enum.Enum):
            return repr(val)
        if t in (list, tuple):
            return "[" + ",".join(_fp(v, depth - 1, seen) for v in val) + "]"
        if t in (set, frozenset):
            return "{" + ",".join(sorted(_fp(v, depth - 1, seen) for v in val)) + "}"
        if isinstance(val, dict):
            items = sorted((_fp(k, depth - 1, seen), _fp(v, depth - 1, seen)) for k, v in val.items())
            return "{" + ",".join(k + ":" + v for k, v in items) + "}"
        if isinstance(val, type):
            return "cls:" + getattr(val, "__module__", "") + "." + val.__qualname__
        if isinstance(val, (types.FunctionType, types.MethodType)):
            f = getattr(val, "__func__", val)
            cells = []
            for c in f.__closure__ or ():
                try:
                    cells.append(_fp(c.cell_contents, depth - 1, seen))
                except ValueError:
                    cells.append("<empty>")
            return ("fn:" + f.__module__ + "." + f.__qualname__ + _fp(f.__defaults__, depth - 1, seen)
                    + _fp(f.__kwdefaults__, depth - 1, seen) + "[" + ",".join(cells) + "]")
        if isinstance(val, types.ModuleType):
            return "mod:" + val.__name__
        if isinstance(val, (staticmethod, classmethod)):
            return "sm:" + _fp(val.__func__, depth - 1, seen)
        if isinstance(val, property):
            return "prop:" + _fp(val.fget, depth - 1, seen)
        mod = getattr(t, "__module__", "") or ""
        if mod.startswith("sqlglot") and hasattr(val, "sql") and hasattr(val, "args"):
            return "expr:" + repr(val)
        d = getattr(val, "__dict__", None)
        if isinstance(d, dict):
            return "obj:" + t.__qualname__ + _fp(d, depth - 1, seen)
        return "<" + t.__qualname__ + ">"
    except BaseException:
        return "<err:" + t.__qualname__ + ">"
    finally:
        seen.discard(vid)


def import_fingerprint(src_root, files, deadline, log):
    """Import every sqlglot module and every test module (patched sources when given) in a forked
    child and return {"module.name": digest} over module globals and class attributes."""
    import hashlib

    def job():
        import importlib
        if files:
            _fresh_patched(src_root, files)
        else:
            for name in list(sys.modules):
                if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                    del sys.modules[name]
            _setup(src_root)
        names = []
        for root, _dirs, fns in os.walk(os.path.join(src_root, "sqlglot")):
            for fn in sorted(fns):
                if fn.endswith(".py") and fn != "__main__.py":
                    rel = os.path.relpath(os.path.join(root, fn), src_root)
                    names.append(rel[:-3].replace(os.sep, ".").removesuffix(".__init__"))
        for root, _dirs, fns in os.walk(os.path.join(src_root, "tests")):
            for fn in sorted(fns):
                if fn.startswith("test_") and fn.endswith(".py") and fn != "test_sqlglotc.py":
                    rel = os.path.relpath(os.path.join(root, fn), src_root)
                    names.append(rel[:-3].replace(os.sep, "."))
        devnull = open(os.devnull, "w")
        old = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = devnull
        try:
            for n in sorted(names):
                try:
                    importlib.import_module(n)
                except BaseException:
                    pass
        finally:
            sys.stdout, sys.stderr = old
        out = {}
        for mname, mod in sorted(sys.modules.items()):
            if mod is None or not (mname in ("sqlglot", "tests") or mname.startswith(("sqlglot.", "tests."))):
                continue
            for k, v in sorted(vars(mod).items()):
                if k.startswith("__") and k.endswith("__"):
                    continue
                if isinstance(v, type) and getattr(v, "__module__", None) == mname:
                    for ck, cv in sorted(vars(v).items()):
                        if ck in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__"):
                            continue
                        out[mname + "." + k + "." + ck] = hashlib.sha1(_fp(cv, 7, set()).encode()).hexdigest()[:16]
                else:
                    out[mname + "." + k] = hashlib.sha1(_fp(v, 7, set()).encode()).hexdigest()[:16]
        return out

    tmo = deadline - time.time()
    if tmo < 15:
        return None
    return _child_result(job, min(60, tmo), "fp")


def maxnodes_proxy(src_root, files, deadline, log, cap_s=4.0):
    """Cheap stand-in for TestParser.test_max_nodes on patched sources: every step of the test, with
    the huge statement shrunk from 10000 to 600 columns (about 60k counted nodes, past both limits),
    under a CPU cap so that a slowdown counts as a failure.  True when every step behaves as the test
    expects."""
    tmo = deadline - time.time()
    if tmo < 8:
        return False

    def job():
        _fresh_patched(src_root, files)
        from sqlglot import exp, parse_one
        from sqlglot.errors import ParseError

        state = {"slow": False}

        def on_prof(signum, frame):
            state["slow"] = True
            raise KeyboardInterrupt()

        signal.signal(signal.SIGPROF, on_prof)
        signal.setitimer(signal.ITIMER_PROF, cap_s)
        ok = False
        try:
            assert isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
            cols = [f"col{i}" for i in range(500)]
            assert isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select)
            expr = "x" + "=x" * 100
            sql = f"SELECT {','.join(expr for _ in range(600))}"
            for limit, needle in ((20_000, "Maximum number of AST nodes"), (50_000, "(50000)")):
                try:
                    parse_one(sql, max_nodes=limit)
                    raise AssertionError("no ParseError")
                except ParseError as e:
                    assert needle in str(e)
            try:
                parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
                raise AssertionError("no ParseError")
            except ParseError:
                pass
            assert isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)
            ok = True
        except KeyboardInterrupt:
            ok = False
        except BaseException:
            ok = False
        finally:
            signal.setitimer(signal.ITIMER_PROF, 0)
        return {"ok": ok and not state["slow"], "slow": state["slow"]}

    r = _child_result(job, min(cap_s * 3 + 10, tmo), "mnp")
    log("maxnodes-proxy", r)
    return bool(r and r["ok"])


_TPCDS_ROWS = {}


def _tpcds_rows(src_root, log):
    """TPC-DS table rows parsed from the CSV fixtures exactly as TestExecutor.setUpClass parses them
    (no sqlglot involved), loaded once in this process and shared with forked probes."""
    if "rows" in _TPCDS_ROWS:
        return _TPCDS_ROWS["rows"]
    try:
        import ast as _ast
        import csv
        import gzip as _gz
        if src_root not in sys.path:
            sys.path.insert(0, src_root)
        from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA
        d = FIXTURES_DIR + "/optimizer/tpc-ds/"
        out = {}
        for table in TPCDS_SCHEMA:
            fn = f"{d}{table}.csv.gz"
            with open(fn, "rb") as f:
                gz = f.read(2) == b"\x1f\x8b"
            fh = _gz.open(fn, "rt", newline="") if gz else open(fn, encoding="utf-8", newline="")
            with fh:
                reader = csv.reader(fh, delimiter="|")
                next(reader)
                rows, ctypes = [], []
                for row in reader:
                    if not ctypes:
                        for v in row:
                            try:
                                ctypes.append(type(_ast.literal_eval(v)))
                            except (ValueError, SyntaxError):
                                ctypes.append(str)
                    rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
            out[table] = rows
        _TPCDS_ROWS["rows"] = out
        try:
            with _gz.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tpcds_expected.pkl.gz"), "rb") as g:
                _TPCDS_ROWS["expected"] = pickle.load(g)
        except BaseException as e:
            log("tpcds expected load failed", repr(e))
    except BaseException as e:
        log("tpcds rows preload failed", repr(e))
        _TPCDS_ROWS["rows"] = None
    return _TPCDS_ROWS["rows"]


def _tpcds_fast_setup(te, rows):
    """TestExecutor.setUpClass for the TPC-DS half only, reusing pre-parsed rows: DuckDB views over
    the same CSV files and sqlglot executor Tables built (with the imported sqlglot) from the rows."""
    import duckdb
    from sqlglot.executor.table import Table
    cls = te.TestExecutor
    cls.tpch_conn = duckdb.connect()
    cls.tpcds_conn = duckdb.connect()
    cls.tpch_tables = {}
    schema = te.TPCDS_SCHEMA

    class _LazyTables(dict):
        def __missing__(self, name):
            if name not in schema:
                raise KeyError(name)
            t = Table(columns=schema[name], rows=list(rows[name]))
            self[name] = t
            return t

    cls.tpcds_tables = _LazyTables()
    state = {"views": False}

    def _conn():
        if not state["views"]:
            for table, columns in schema.items():
                file_name = f"{te.DIR_TPCDS}{table}.csv.gz"
                cls.tpcds_conn.execute(
                    f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
                )
            state["views"] = True
        return cls.tpcds_conn

    expected = _TPCDS_ROWS.get("expected") or {}
    from sqlglot import transpile

    def cached_execute(self, sql, tpch=True):
        # same as TestExecutor.cached_execute; DuckDB's answer for an unchanged transpiled
        # statement is reused from the pinned commit instead of re-running the query
        if sql not in self.cache:
            s = transpile(sql, write="duckdb")[0]
            hit = expected.get(sql)
            if not tpch and hit is not None and hit[0] == s:
                self.cache[sql] = hit[1].copy()
            else:
                self.cache[sql] = _conn().execute(s).fetchdf()
        return self.cache[sql]

    cls.cached_execute = cached_execute
    cls.cache = {}
    cls.tpch_sqls = []
    cls.tpcds_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--fresh-worker":
        os._exit(_fresh_worker(sys.argv[2], sys.argv[3]))
    raise SystemExit("verify.py is a selector support module")
