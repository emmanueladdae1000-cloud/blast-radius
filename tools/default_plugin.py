"""Record only original exact-Boolean default-parameter bindings on the baseline."""
import ast
import builtins
import concurrent.futures as cf
import gzip
import importlib.abc
import importlib.machinery
import json
import os
import pickle
import subprocess
import sys
import pytest

ROOT = os.path.realpath(os.environ.get('BR_PROFILE_SRC', '/app/src'))
OUT = os.environ.get('BR_PROFILE_OUT', '/out')
HELPER = 'br_defaults_202611_'
# A spawned interpreter can import/unpickle before its wrapped task starts.
# Those effects may initialize a reused pool, so attribute them globally.
ACTIVE = [None]
DEFS, DEFAULTS, OBS = {}, {}, {}
FAILED, PASSED, INCOMPLETE = set(), set(), set()
COLLECTION_FAILED = []
TASK_N = 0
WORKER_FINALIZER = None
with open(os.environ.get('BR_TESTS_JSON', '/data/tests.json')) as f:
    TESTS = json.load(f)['tests']
ID2NUM = {t['id']: str(t['number']) for t in TESTS}


def test_key(nodeid):
    # Unlisted/skipped tests may initialize shared state. Their bindings must
    # constrain every later deduction instead of silently disappearing.
    return ID2NUM.get(nodeid.split('[', 1)[0], '@import')


def bind(function, *values):
    context = ACTIVE[0] or '@import'
    rows = OBS.get(context)
    if rows is None:
        rows = {}
        OBS[context] = rows
    for site, value in zip(DEFS[function], values):
        row = rows.get(site)
        if row is None:
            row = [0, True]
            rows[site] = row
        if not row[1]:
            continue
        if type(value) is bool:
            row[0] |= 2 if value else 1
        else:
            row[1] = False


class Instrument(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        positional = node.args.posonlyargs + node.args.args
        defaults = list(zip(positional[-len(node.args.defaults):], node.args.defaults)) if node.args.defaults else []
        defaults += list(zip(node.args.kwonlyargs, node.args.kw_defaults))
        selected = [(arg.arg, value.value) for arg, value in defaults
                    if isinstance(value, ast.Constant) and type(value.value) is bool]
        if not selected:
            return node
        key = f'{self.rel}|{node.lineno}|{node.col_offset}'
        sites = []
        for name, default in selected:
            site = key + '|' + name
            DEFAULTS[site] = default
            sites.append(site)
        DEFS[key] = sites
        call = ast.Expr(value=ast.Call(func=ast.Name(id=HELPER, ctx=ast.Load()),
            args=[ast.Constant(key)] + [ast.Name(id=name, ctx=ast.Load()) for name, _ in selected], keywords=[]))
        ast.copy_location(call, node.body[0])
        at = int(isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
                 and type(node.body[0].value.value) is str)
        node.body.insert(at, call)
        return node


class Loader(importlib.abc.SourceLoader):
    def __init__(self, path): self.path = path
    def get_filename(self, fullname): return self.path
    def get_data(self, path):
        with open(path, 'rb') as f: return f.read()
    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), self.path)
        rel = os.path.relpath(self.path, ROOT).replace(os.sep, '/')
        tree = Instrument(rel).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, self.path, 'exec', dont_inherit=True)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != 'sqlglot' and not fullname.startswith('sqlglot.'): return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith('.py'): return spec
        origin = os.path.realpath(spec.origin)
        if not origin.startswith(os.path.join(ROOT, 'sqlglot') + os.sep):
            raise RuntimeError('Unexpected source ' + origin)
        spec.loader = Loader(origin)
        return spec


def dump(tag):
    observations = {context: {site: {'complete': row[1], 'values':
        ([False] if row[0] & 1 else []) + ([True] if row[0] & 2 else [])}
        for site, row in rows.items()} for context, rows in OBS.items()}
    used = {site for rows in OBS.values() for site in rows}
    data = {'format': 'raw-baseline-default-bindings-v1', 'seed': os.environ.get('SEEDTAG', 's'),
            'defaults': {site: DEFAULTS[site] for site in used}, 'observations': observations,
            'passed_tests': sorted(PASSED), 'failed_tests': sorted(FAILED),
            'incomplete_contexts': sorted(INCOMPLETE), 'collection_failures': COLLECTION_FAILED}
    os.makedirs(OUT, exist_ok=True)
    name = 'defaults_%s_%s_%s_%s.json.gz' % (data['seed'], os.environ.get('PYTEST_XDIST_WORKER', 'main'), os.getpid(), tag)
    with gzip.open(os.path.join(OUT, name), 'wt', encoding='utf8') as f:
        json.dump(data, f, separators=(',', ':'), allow_nan=False)


def profile_task(context, payload):
    global OBS, TASK_N, WORKER_FINALIZER
    if WORKER_FINALIZER is None:
        from multiprocessing.util import Finalize
        WORKER_FINALIZER = Finalize(None, dump, args=('workerfinal',), exitpriority=0)
    dump('initializer' + str(TASK_N + 1))
    OBS = {}
    ACTIVE[0] = context
    os.environ['BR_DEFAULT_ACTIVE'] = context
    TASK_N += 1
    try:
        # The outer process-pool message contains only opaque bytes. Restore
        # SQLGlot arguments only after their owning test context is installed.
        fn, args, kwargs = pickle.loads(payload)
        return fn(*args, **kwargs)
    finally: dump('task' + str(TASK_N))


def _sqlglot_stack():
    frame = sys._getframe(1)
    while frame:
        if frame.f_code.co_filename.startswith(os.path.join(ROOT, 'sqlglot') + os.sep): return True
        frame = frame.f_back
    return False


_process_submit = cf.ProcessPoolExecutor.submit
def process_submit(self, fn, /, *args, **kwargs):
    if ACTIVE[0] is None and not _sqlglot_stack(): return _process_submit(self, fn, *args, **kwargs)
    from multiprocessing.reduction import ForkingPickler
    try: payload = bytes(ForkingPickler.dumps((fn, args, kwargs)))
    except Exception:
        INCOMPLETE.add(ACTIVE[0] or '@import')
        return _process_submit(self, fn, *args, **kwargs)
    return _process_submit(self, profile_task, ACTIVE[0] or '@import', payload)


_thread_submit = cf.ThreadPoolExecutor.submit
def thread_submit(self, *args, **kwargs):
    if ACTIVE[0] is not None or _sqlglot_stack(): INCOMPLETE.add(ACTIVE[0] or '@import')
    return _thread_submit(self, *args, **kwargs)


_popen_init = subprocess.Popen.__init__
def popen_init(self, *args, **kwargs):
    if ACTIVE[0] is not None or _sqlglot_stack(): INCOMPLETE.add(ACTIVE[0] or '@import')
    _popen_init(self, *args, **kwargs)


def _set_context(context):
    old = ACTIVE[0], os.environ.get('BR_DEFAULT_ACTIVE')
    ACTIVE[0] = context
    if context is None: os.environ.pop('BR_DEFAULT_ACTIVE', None)
    else: os.environ['BR_DEFAULT_ACTIVE'] = context
    return old


def _restore(old):
    ACTIVE[0] = old[0]
    if old[1] is None: os.environ.pop('BR_DEFAULT_ACTIVE', None)
    else: os.environ['BR_DEFAULT_ACTIVE'] = old[1]


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    old = _set_context(test_key(item.nodeid))
    try: yield
    finally: _restore(old)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.scope == 'function':
        yield
        return
    prefix = request.node.nodeid.split('[', 1)[0]
    matched = any(not prefix or tid == prefix or tid.startswith(prefix + '::')
                  or tid.startswith(prefix + '/') for tid in ID2NUM)
    old = _set_context('@scope:' + prefix if matched else '@import')
    try: yield
    finally: _restore(old)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    # Cleanup may affect later tests; global attribution is conservative.
    old = _set_context('@import')
    try: yield
    finally: _restore(old)


def pytest_runtest_logreport(report):
    if report.failed: FAILED.add(test_key(report.nodeid))
    if report.when == 'call' and report.passed: PASSED.add(test_key(report.nodeid))


def pytest_collectreport(report):
    if report.failed: COLLECTION_FAILED.append(report.nodeid)


def pytest_sessionfinish(session): dump('session')


def merge(folder, output, tests_path):
    import glob
    from collections import defaultdict
    tests = json.load(open(tests_path))['tests']
    if {t['number'] for t in tests} != set(range(len(tests))):
        raise RuntimeError('Baseline test numbers must be contiguous zero-based indices')
    ids = [t['id'] for t in sorted(tests, key=lambda t: t['number'])]
    known = {str(t['number']) for t in tests}
    defaults = {}; records = defaultdict(lambda: defaultdict(dict))
    seeds = set(); passed = defaultdict(set); blocked = defaultdict(set)
    def expand(context):
        if not context.startswith('@scope:'): return {context}
        prefix = context[len('@scope:'):]
        return {str(t['number']) for t in tests if not prefix or t['id'] == prefix
                or t['id'].startswith(prefix + '::') or t['id'].startswith(prefix + '/')}
    for path in sorted(glob.glob(folder + '/defaults_*.json.gz')):
        with gzip.open(path, 'rt') as f: data = json.load(f)
        seed = data['seed']; seeds.add(seed)
        if data['collection_failures']: raise RuntimeError('Collection failed: ' + repr(data['collection_failures']))
        if path.endswith('_session.json.gz'): passed[seed].update(data['passed_tests'])
        blocked[seed].update(data['failed_tests'])
        for context in data['incomplete_contexts']: blocked[seed].update(expand(context))
        for site, default in data['defaults'].items():
            if site in defaults and defaults[site] is not default: raise RuntimeError('Inconsistent default')
            defaults[site] = default
        for context, rows in data['observations'].items():
            for tid in expand(context):
                for site, row in rows.items():
                    merged = records[site][tid].setdefault(seed, {'complete': True, 'values': set()})
                    merged['complete'] &= row['complete']
                    merged['values'].update(row['values'])
    if len(seeds) < 2: raise RuntimeError('Need two complete hash-seed runs')
    common = known.intersection(*(passed[seed] - blocked[seed] for seed in seeds))
    sites = {}
    def combine(byseed, imports=False):
        if not byseed: return {'complete': imports, 'values': []}
        complete = set(byseed) == seeds and all(r['complete'] for r in byseed.values())
        values = set().union(*(r['values'] for r in byseed.values()))
        return {'complete': complete, 'values': sorted(values)}
    for site, contexts in records.items():
        imports = combine(contexts.get('@import', {}), imports=True)
        if any('@import' in blocked[seed] for seed in seeds): imports['complete'] = False
        rows = {tid: combine(byseed) for tid, byseed in contexts.items() if tid in common}
        sites[site] = {'default': defaults[site], 'imports': imports, 'tests': rows}
    result = {'format': 'raw-baseline-default-bindings-v1',
              'source_commit': 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa',
              'hash_seeds': sorted(seeds), 'test_ids': ids, 'sites': sites}
    with gzip.open(output, 'wt', encoding='utf8') as f: json.dump(result, f, separators=(',', ':'))
    print('Default binding sites:', len(sites), 'site/test rows:', sum(len(s['tests']) for s in sites.values()),
          'passing tests both seeds:', len(common))
    print('Only original baseline Boolean bindings were recorded; no alternate defaults executed.')


if __name__ == '__main__' and sys.argv[1:2] == ['--merge']:
    merge(*sys.argv[2:])
else:
    if any(n == 'sqlglot' or n.startswith('sqlglot.') for n in sys.modules):
        raise RuntimeError('Plugin loaded after SQLGlot')
    setattr(builtins, HELPER, bind)
    sys.meta_path.insert(0, Finder())
    cf.ProcessPoolExecutor.submit = process_submit
    cf.ThreadPoolExecutor.submit = thread_submit
    subprocess.Popen.__init__ = popen_init
