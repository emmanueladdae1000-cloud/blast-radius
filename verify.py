"""Run candidate tests for a patch inside this container, against the patched code.

Only used for patches whose every real edit sits inside function bodies that never
run at import time. The patched functions are swapped in place (func.__code__), so
every reference to them (dispatch tables, closures, decorators) sees the new code.
Each patch is checked in its own forked child of a parent that imported sqlglot and
the needed test modules once.
"""
import gc
import json
import os
import pickle
import signal
import sys
import time
import types
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
    consts = tuple(("<code>", c.co_qualname) if isinstance(c, types.CodeType) else (type(c).__name__, repr(c)) for c in code.co_consts)
    return (code.co_code, code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars, consts,
            code.co_flags, code.co_argcount, code.co_kwonlyargcount, code.co_posonlyargcount,
            code.co_exceptiontable, code.co_nlocals)


def _codes(code, out, top=True):
    out.setdefault(code.co_qualname if not top else "<module>", []).append(code)
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            _codes(c, out, False)
    return out


def _plan_swaps(path, old_src, new_src):
    """Return [(qualname, old_code, new_code)] for changed function bodies, or None if
    the change touches module/class-body code (not swappable)."""
    old = _codes(compile(old_src, path, "exec", dont_inherit=True), {})
    new = _codes(compile(new_src, path, "exec", dont_inherit=True), {})
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


def _apply(path, swaps):
    """Swap code of live functions. Changed nested functions whose parent is also
    swapped need no live instance."""
    targets = {}
    for q, oc, nc in swaps:
        targets[(q, oc.co_firstlineno)] = nc
    hit = set()
    for obj in gc.get_objects():
        if type(obj) is types.FunctionType:
            c = obj.__code__
            if c.co_filename == path:
                key = (c.co_qualname, c.co_firstlineno)
                nc = targets.get(key)
                if nc is not None:
                    obj.__code__ = nc
                    hit.add(key)
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

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.bad = True
        super().addSubTest(test, subtest, err)


def run(jobs, ids, src_root, budget_s, log):
    """jobs: list of (key, {rel: new_src}, [tidx in run order], timeout_s).
    Returns {key: (failed_tidx_list, unrun_tidx_list)} for jobs that completed."""
    t_start = time.time()
    _setup(src_root)
    import importlib

    mods = {}
    need = sorted({ids[t].split("::")[0] for _, _, ts, _ in jobs for t in ts})
    for f in need:
        name = f[:-3].replace("/", ".")
        try:
            mods[f] = importlib.import_module(name)
        except BaseException as e:
            log("verify: import failed", name, repr(e)[:200])
    for _, files, _, _ in jobs:
        for rel in files:
            importlib.import_module(rel[:-3].replace("/", "."))
    gc.collect()
    import resource
    ru = resource.getrusage(resource.RUSAGE_SELF)
    log("verify: parent setup cpu %.3f" % (ru.ru_utime + ru.ru_stime))
    results = {}
    tmpdir = "/tmp"
    for key, files, order, tmo in jobs:
        if time.time() - t_start > budget_s:
            log("verify: budget exhausted")
            break
        out = os.path.join(tmpdir, f"v_{os.getpid()}_{key}.json")
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                ok = True
                for rel, new_src in files.items():
                    path = os.path.join(src_root, rel)
                    with open(path, encoding="utf-8") as fh:
                        old_src = fh.read()
                    swaps = _plan_swaps(path, old_src, new_src)
                    if swaps is None or not _apply(path, swaps):
                        ok = False
                        break
                failed, unrun = [], []
                if ok:
                    setup_done = set()
                    for i, t in enumerate(order):
                        f, c, m = ids[t].split("::")
                        mod = mods.get(f)
                        if mod is None:
                            unrun = order[i:]
                            break
                        cls = getattr(mod, c)
                        if c not in setup_done:
                            setup_done.add(c)
                            try:
                                cls.setUpClass()
                            except BaseException:
                                unrun = order[i:]
                                break
                        r = _Result()
                        cls(m).run(r)
                        if r.bad or r.failures or r.errors or r.unexpectedSuccesses:
                            failed.append(t)
                            unrun = order[i + 1:]   # stop: the commit needs a worker anyway
                            break
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
        deadline = time.time() + tmo
        status = None
        while True:
            r_pid, st = os.waitpid(pid, os.WNOHANG)
            if r_pid:
                status = st
                break
            if time.time() > deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                log("verify: timeout", key)
                break
            time.sleep(0.002)
        import resource
        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_now = ru.ru_utime + ru.ru_stime
        log("verify: job %s cpu %.3f" % (key, cpu_now - getattr(run, "_last", 0.0)))
        run._last = cpu_now
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


def import_check(src_root, files, log, timeout=30.0):
    """Import the patched modules (and sqlglot) in a throwaway child. False if that raises."""
    pid = os.fork()
    if pid == 0:
        code = 3
        try:
            os.environ["SKIP_INTEGRATION"] = "0"
            sys.path.insert(0, src_root)
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
            code = 0
        except BaseException as e:
            try:
                sys.stderr.write("importcheck: %r\n" % (e,))
            except BaseException:
                pass
            code = 4
        finally:
            os._exit(code)
    deadline = time.time() + timeout
    while True:
        r_pid, st = os.waitpid(pid, os.WNOHANG)
        if r_pid:
            ok = os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0
            if not ok:
                log("importcheck failed", sorted(files))
            return ok
        if time.time() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            log("importcheck timeout", sorted(files))
            return False
        time.sleep(0.002)
