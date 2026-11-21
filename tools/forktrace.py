"""Per-test isolated tracing of the pinned sqlglot suite.

Parent imports everything once (recording import-time lines), runs each class's
setUpClass (recording class-level lines), then forks one child per test that
records exactly the sqlglot/tests lines and sqlglot classes the test touches.
"""
import sys, os, json, time, importlib, pkgutil, unittest, traceback
ROOT = os.path.abspath(os.getcwd())
sys.path.insert(0, ROOT)
os.environ.setdefault("SKIP_INTEGRATION", "0")
OUT = sys.argv[1]
ONLY = sys.argv[2] if len(sys.argv) > 2 else None
JOBS = int(os.environ.get("JOBS", "2"))
os.makedirs(OUT, exist_ok=True)

import concurrent.futures as _cf
import pickle as _pk
def _rt(x):
    # mimic the pickling a real process pool does
    return _pk.loads(_pk.dumps(x))
class _SyncExecutor(_cf.Executor):
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer: initializer(*_rt(initargs))
    def submit(self, fn, /, *args, **kwargs):
        f = _cf.Future()
        try: f.set_result(_rt(fn(*_rt(args), **_rt(kwargs))))
        except BaseException as e: f.set_exception(e)
        return f
    def shutdown(self, wait=True, *, cancel_futures=False): pass
_cf.ProcessPoolExecutor = _SyncExecutor


import builtins, io as _io, pathlib as _pl
READS = set()
_oopen = builtins.open
def _rec(fp):
    try: ap = os.path.abspath(os.fspath(fp))
    except Exception: return
    if ap.startswith(ROOT + "/tests/"): READS.add(ap[len(ROOT) + 1:])
def _open(file, *a, **k):
    if isinstance(file, (str, bytes, os.PathLike)): _rec(file)
    return _oopen(file, *a, **k)
builtins.open = _open; _io.open = _open
_opo = _pl.Path.open
def _popen(self, *a, **k): _rec(self); return _opo(self, *a, **k)
_pl.Path.open = _popen
import gzip as _gz
_ogz = _gz.open
def _gzopen(f, *a, **k):
    if isinstance(f, (str, bytes, os.PathLike)): _rec(f)
    return _ogz(f, *a, **k)
_gz.open = _gzopen
def take_reads():
    s = sorted(READS); READS.clear(); return s

M = sys.monitoring
TOOL = 3
PREFIXES = (ROOT + "/sqlglot/", ROOT + "/tests/")
LINES = set()
LAMS = set()
def _line(code, line):
    f = code.co_filename
    if f.startswith(PREFIXES):
        LINES.add((f[len(ROOT) + 1:], line))
        if code.co_name == "<lambda>":
            LAMS.add((f[len(ROOT) + 1:], line))
    return M.DISABLE
def _pystart(code, offset):
    # one-line functions/lambdas never raise a LINE event for their only line
    f = code.co_filename
    if f.startswith(PREFIXES):
        rel = f[len(ROOT) + 1:]
        ls = {code.co_firstlineno}
        for _, _, ln in code.co_lines():
            if ln is not None:
                ls.add(ln)
                break
        for ln in ls:
            LINES.add((rel, ln))
            if code.co_name == "<lambda>":
                LAMS.add((rel, ln))
    return M.DISABLE
def start():
    LINES.clear()
    M.use_tool_id(TOOL, "forktrace")
    M.register_callback(TOOL, M.events.LINE, _line)
    M.register_callback(TOOL, M.events.PY_START, _pystart)
    M.set_events(TOOL, M.events.LINE | M.events.PY_START)
def stop():
    M.set_events(TOOL, 0)
    M.register_callback(TOOL, M.events.LINE, None)
    M.register_callback(TOOL, M.events.PY_START, None)
    M.free_tool_id(TOOL)
    M.restart_events()
    s = sorted(LINES); LINES.clear(); return s

def dump(name, obj):
    with open(os.path.join(OUT, name), "w") as fh:
        json.dump(obj, fh)

# ---- phase A: import all of sqlglot
start()
import sqlglot
failed = []
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        importlib.import_module(m.name)
    except BaseException as e:
        failed.append([m.name, repr(e)[:200]])
_lams = sorted(LAMS)
dump("_import.json", {"lines": stop(), "lams": _lams, "failed": failed})

# ---- usage hooks
SEEN = set()
from sqlglot.expressions.core import Expr
from sqlglot.dialects.dialect import Dialect, _Dialect
from sqlglot.tokens import Tokenizer
from sqlglot.parser import Parser
from sqlglot.generator import Generator
for base in (Expr, Dialect, Tokenizer, Parser, Generator):
    def make(orig):
        def __init__(self, *a, **k):
            SEEN.add(type(self))
            return orig(self, *a, **k)
        return __init__
    base.__init__ = make(base.__init__)
_og = _Dialect.__dict__["__getitem__"].__func__
def _gi(cls, key):
    r = _og(cls, key); SEEN.add(r); return r
_Dialect.__getitem__ = classmethod(_gi)
_oget = _Dialect.__dict__["get"].__func__
def _get(cls, key, default=None):
    r = _oget(cls, key, default)
    if r is not None: SEEN.add(r)
    return r
_Dialect.get = classmethod(_get)
def cname(c): return f"{c.__module__}:{c.__qualname__}"
MRO = {}
def seen_names():
    out = []
    for c in SEEN:
        n = cname(c); out.append(n)
        if n not in MRO:
            MRO[n] = [cname(b) for b in c.__mro__ if b.__module__.startswith("sqlglot")]
    SEEN.clear(); return sorted(out)

tests = json.load(open(os.environ["TESTS_JSON"]))["tests"]
by_file = {}
for t in tests:
    f, c, m = t["id"].split("::")
    by_file.setdefault(f, {}).setdefault(c, []).append((t["number"], m))

class Result(unittest.TestResult):
    def __init__(self):
        super().__init__(); self.bad = []
    def addSubTest(self, test, subtest, err):
        if err is not None: self.bad.append("sub:" + str(subtest)[:100])
        super().addSubTest(test, subtest, err)

running = {}
def reap(block):
    while running and (block or len(running) >= JOBS):
        pid, status = os.waitpid(-1, 0)
        running.pop(pid, None)
        if not block: return
def run_one(cls, num, meth):
    if ONLY and ONLY not in f"{cls.__name__}::{meth}": return
    pid = os.fork()
    if pid == 0:
        try:
            SEEN.clear(); READS.clear()
            t0 = time.process_time()
            start()
            case = cls(meth); r = Result(); case.run(r)
            lines = stop()
            bad = r.bad + [x[1][-1500:] for x in r.failures + r.errors]
            dump(f"t{num}.json", {"lines": lines, "seen": seen_names(), "mro": MRO,
                                   "ok": not bad, "reads": take_reads(), "bad": bad[:5], "skip": len(r.skipped), "cpu": time.process_time() - t0})
        except BaseException:
            traceback.print_exc()
        finally:
            sys.stdout.flush(); sys.stderr.flush(); os._exit(0)
    running[pid] = num
    reap(False)

t_all = time.time()
for f, classes in by_file.items():
    modname = f[:-3].replace("/", ".")
    SEEN.clear(); READS.clear(); start()
    try:
        mod = importlib.import_module(modname)
    except BaseException as e:
        print("IMPORT FAIL", modname, e, file=sys.stderr); stop(); continue
    dump(f"f_{modname}.json", {"lines": stop(), "seen": seen_names(), "mro": MRO, "reads": take_reads()})
    for cname_, items in classes.items():
        cls = getattr(mod, cname_)
        SEEN.clear(); READS.clear(); start()
        try:
            cls.setUpClass()
        except BaseException as e:
            print("SETUPCLASS FAIL", cname_, e, file=sys.stderr)
        dump(f"c_{modname}.{cname_}.json", {"lines": stop(), "seen": seen_names(), "mro": MRO, "reads": take_reads()})
        for num, meth in items:
            run_one(cls, num, meth)
        reap(True)
        try:
            cls.tearDownClass(); cls.doClassCleanups()
        except BaseException: pass
    print("done", f, round(time.time() - t_all, 1), file=sys.stderr)
