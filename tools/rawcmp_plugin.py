"""Observe original baseline comparison operands; never execute an alternate operator."""
import ast, builtins, concurrent.futures as cf, functools, gzip, importlib.abc, importlib.machinery
import inspect, json, operator, os, pickle, sys
import pytest
ROOT = os.path.realpath(os.environ.get('BR_PROFILE_SRC', '/app/src'))
OUT = os.environ.get('BR_PROFILE_OUT', '/out')
HELPER = 'br_rawcmp_202611_'
ACTIVE = [os.environ.get('BR_PROFILE_ACTIVE')]
NODES, OBS = {}, {}
FAILED, PASSED, INCOMPLETE = set(), set(), set()
COLLECTION_FAILED = []
TASK_N = 0
WORKER_FINALIZER = None
OPS = {ast.Eq:'==', ast.NotEq:'!=', ast.Lt:'<', ast.LtE:'<=', ast.Gt:'>', ast.GtE:'>=', ast.Is:'is', ast.IsNot:'is not'}
ORIGINAL = {'==':operator.eq, '!=':operator.ne, '<':operator.lt, '<=':operator.le, '>':operator.gt, '>=':operator.ge, 'is':operator.is_, 'is not':operator.is_not}
RICH_NAMES=frozenset(('__eq__','__ne__','__lt__','__le__','__gt__','__ge__'))
with open(os.environ.get('BR_TESTS_JSON', '/data/tests.json')) as f:
    ID2NUM = {t['id']:t['number'] for t in json.load(f)['tests']}

def test_key(nodeid):
    number=ID2NUM.get(nodeid.split('[',1)[0])
    return str(number) if number is not None else '@import'

def static_method(t,name):
    # Read class dictionaries directly without descriptors or custom metaclass lookup.
    for base in type.__getattribute__(t,'__mro__'):
        namespace=type.__getattribute__(base,'__dict__')
        if name in namespace: return namespace[name]
    return None

def identity_type(t):
    if static_method(t,'__eq__') is object.__eq__ and static_method(t,'__ne__') is object.__ne__:
        return ('identity_object',type.__getattribute__(t,'__module__'),type.__getattribute__(t,'__qualname__'))
    return None

def encode(v):
    t=type(v)
    if v is None: return ('none',)
    if t is bool: return ('bool',v)
    if t is int: return ('int',str(v)) if v.bit_length()<=256 else None
    if t is float: return ('float',v.hex())
    if t is str: return ('str',v) if len(v)<=256 else None
    if t is bytes: return ('bytes',v.hex()) if len(v)<=128 else None
    mro=type.__getattribute__(t,'__mro__')
    # A subtype is supported only when EVERY rich comparison slot is unchanged.
    for base in (str,int,float,bytes):
        if not any(cls is base for cls in mro): continue
        # The first definitions of the six slots must be the builtin's own.
        # Check the MRO prefix once, inspecting every override on every call;
        # this also detects classes whose comparison methods change at runtime.
        base_namespace=type.__getattribute__(base,'__dict__')
        for cls in mro:
            if cls is base: break
            namespace=type.__getattribute__(cls,'__dict__')
            if any(namespace[n] is not base_namespace[n] for n in namespace.keys() & RICH_NAMES): return None
        if base is str:
            value=str.__str__(v)
            if len(value)>256: return None
        elif base is int:
            value=int.__int__(v)
            if value.bit_length()>256: return None
            value=str(value)
        elif base is float: value=float.__float__(v).hex()
        else:
            if bytes.__len__(v)>128: return None
            value=bytes.hex(v)
        return (base.__name__+'_subclass',type.__getattribute__(t,'__module__'),type.__getattribute__(t,'__qualname__'),value)
    return identity_type(t)
def record(site,a,b,result,valid=True,identical=False):
    if ACTIVE[0] is None: return
    rows=OBS.get(site)
    if rows is None:
        rows={}
        OBS[site]=rows
    row=rows.get(ACTIVE[0])
    if row is None:
        row={'pairs':set(),'complete':True}
        rows[ACTIVE[0]]=row
    if not row['complete']: return
    if not valid:
        row['complete']=False
        return
    aa,bb=a,b
    if aa is None or bb is None or type(result) is not bool:
        row['complete']=False
        return
    pair=(aa,bb,identical,result)
    if pair in row['pairs']: return
    if len(row['pairs'])>=64: row['complete']=False
    else: row['pairs'].add(pair)

def compare(site,a,b):
    # Calls outside a running test cannot contribute an observation. A row which
    # cannot be certified stays incomplete, so further operand encoding only
    # adds overhead (especially in parser size and memory-leak properties).
    tid=ACTIVE[0]
    if tid is None:
        return ORIGINAL[NODES[site][3]](a,b)
    row=OBS.get(site,{}).get(tid)
    if row is not None and not row['complete']:
        return ORIGINAL[NODES[site][3]](a,b)
    try:
        aa,bb=encode(a),encode(b)
        identical=a is b
    except Exception:
        aa=bb=None
        identical=False
    try:
        result=ORIGINAL[NODES[site][3]](a,b)
    except BaseException:
        try: record(site,None,None,None,False)
        except Exception: INCOMPLETE.add(ACTIVE[0])
        raise
    try:
        record(site,aa,bb,result,True,identical)
    except Exception:
        INCOMPLETE.add(ACTIVE[0])
    return result

class Instrument(ast.NodeTransformer):
    def __init__(self,rel): self.rel=rel
    def visit_Compare(self,node):
        self.generic_visit(node)
        if len(node.ops)!=1 or type(node.ops[0]) not in OPS: return node
        op=OPS[type(node.ops[0])]
        site=f'{self.rel}|{node.lineno}|{node.col_offset}|{op}'
        NODES[site]=[self.rel,node.lineno,node.col_offset,op,node.end_lineno,node.end_col_offset]
        call=ast.Call(func=ast.Name(id=HELPER,ctx=ast.Load()),args=[ast.Constant(site),node.left,node.comparators[0]],keywords=[])
        return ast.copy_location(call,node)

class Loader(importlib.abc.SourceLoader):
    def __init__(self,path): self.path=path
    def get_filename(self,fullname): return self.path
    def get_data(self,path):
        with open(path,'rb') as f: return f.read()
    def get_code(self,fullname):
        tree=ast.parse(self.get_data(self.path),self.path)
        rel=os.path.relpath(self.path,ROOT).replace(os.sep,'/')
        tree=Instrument(rel).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree,self.path,'exec',dont_inherit=True)

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path,target=None):
        if fullname!='sqlglot' and not fullname.startswith('sqlglot.'): return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec is None or not spec.origin or not spec.origin.endswith('.py'): return spec
        origin=os.path.realpath(spec.origin)
        if not origin.startswith(os.path.join(ROOT,'sqlglot')+os.sep): raise RuntimeError('Unexpected source '+origin)
        spec.loader=Loader(origin)
        return spec

if any(n=='sqlglot' or n.startswith('sqlglot.') for n in sys.modules): raise RuntimeError('Plugin loaded after SQLGlot')
setattr(builtins,HELPER,compare)
sys.meta_path.insert(0,Finder())

def dump(tag):
    rows={site:{tid:{'complete':r['complete'],'pairs':sorted(r['pairs'],key=repr)} for tid,r in ts.items()} for site,ts in OBS.items()}
    data={'format':'raw-baseline-comparisons-v1','context_version':2,'seed':os.environ.get('SEEDTAG','s'),'nodes':{s:NODES[s] for s in rows},'observations':rows,'failed_tests':sorted(FAILED),'passed_tests':sorted(PASSED),'incomplete_tests':sorted(x for x in INCOMPLETE if x is not None),'collection_failures':COLLECTION_FAILED}
    os.makedirs(OUT,exist_ok=True)
    name='rawcmp_%s_%s_%s_%s.json.gz'%(data['seed'],os.environ.get('PYTEST_XDIST_WORKER','main'),os.getpid(),tag)
    with gzip.open(os.path.join(OUT,name),'wt',encoding='utf8') as f: json.dump(data,f,separators=(',',':'),allow_nan=False)

def profile_task(tid,payload):
    global OBS,TASK_N,WORKER_FINALIZER
    if WORKER_FINALIZER is None:
        from multiprocessing.util import Finalize
        WORKER_FINALIZER=Finalize(None,dump,args=('workerfinal',),exitpriority=0)
    dump('initializer'+str(TASK_N+1))
    OBS={}
    ACTIVE[0]=tid
    os.environ['BR_PROFILE_ACTIVE']=tid
    TASK_N+=1
    try:
        fn,args,kwargs=pickle.loads(payload)
        return fn(*args,**kwargs)
    finally: dump('task'+str(TASK_N))

_process_submit=cf.ProcessPoolExecutor.submit
def process_submit(self,fn,/,*args,**kwargs):
    if ACTIVE[0] is None: return _process_submit(self,fn,*args,**kwargs)
    from multiprocessing.reduction import ForkingPickler
    try: payload=bytes(ForkingPickler.dumps((fn,args,kwargs)))
    except Exception:
        INCOMPLETE.add(ACTIVE[0])
        return _process_submit(self,fn,*args,**kwargs)
    return _process_submit(self,profile_task,ACTIVE[0],payload)
cf.ProcessPoolExecutor.submit=process_submit
_thread_submit=cf.ThreadPoolExecutor.submit
def thread_submit(self,*args,**kwargs):
    INCOMPLETE.add(ACTIVE[0])
    return _thread_submit(self,*args,**kwargs)
cf.ThreadPoolExecutor.submit=thread_submit

import subprocess
_original_popen_init=subprocess.Popen.__init__
def _watch_popen(self,*args,**kwargs):
    if ACTIVE[0] is not None: INCOMPLETE.add(ACTIVE[0])
    _original_popen_init(self,*args,**kwargs)
subprocess.Popen.__init__=_watch_popen
@pytest.hookimpl(hookwrapper=True,tryfirst=True)
def pytest_runtest_protocol(item,nextitem):
    old=_set_context(test_key(item.nodeid))
    try: yield
    finally:_restore_context(old)

def _set_context(context):
    old=ACTIVE[0],os.environ.get('BR_PROFILE_ACTIVE')
    ACTIVE[0]=context
    if context is None:os.environ.pop('BR_PROFILE_ACTIVE',None)
    else:os.environ['BR_PROFILE_ACTIVE']=context
    return old

def _restore_context(old):
    ACTIVE[0]=old[0]
    if old[1] is None:os.environ.pop('BR_PROFILE_ACTIVE',None)
    else:os.environ['BR_PROFILE_ACTIVE']=old[1]

@pytest.hookimpl(hookwrapper=True,tryfirst=True)
def pytest_fixture_setup(fixturedef,request):
    if fixturedef.scope=='function':
        yield
        return
    prefix=request.node.nodeid.split('[',1)[0]
    matched=any(not prefix or tid==prefix or tid.startswith(prefix+'::') or tid.startswith(prefix+'/') for tid in ID2NUM)
    old=_set_context('@scope:'+prefix if matched else '@import')
    try:yield
    finally:_restore_context(old)

@pytest.hookimpl(hookwrapper=True,tryfirst=True)
def pytest_runtest_teardown(item,nextitem):
    old=_set_context('@import')
    try:yield
    finally:_restore_context(old)

def pytest_runtest_logreport(report):
    if report.failed: FAILED.add(test_key(report.nodeid))
    if report.when=='call' and report.passed: PASSED.add(test_key(report.nodeid))

def pytest_collectreport(report):
    if report.failed: COLLECTION_FAILED.append(report.nodeid)

def pytest_sessionfinish(session): dump('session')
