"""Independent semantic checks for raw-operand reasoning and compound AST edits."""
import ast,difflib,enum,operator,os,sys,tempfile
sys.path.insert(0,'/audit')
sys.path.insert(0,'/audit/tools')
os.environ['BR_TESTS_JSON']='/audit/sample/tests.json'
import cmpprune as c
import rawcmp_plugin as p

class Choice(enum.Enum):
    A=1
    B=2
class Plain: pass
class Overloaded:
    def __eq__(self,other): return True
    def __ne__(self,other): return False

same=Plain()
text=''.join(['a sufficiently ','long noninterned string'])
values=[None,False,True,-1000,-5,0,3,257,2**80,-0.0,1.25,float('nan'),float('inf'),'','x',text,(' '+text)[1:],b'',b'x',same,same,Plain(),Choice.A,Choice.B]
class StringChoice(str, enum.Enum):
    A='a'
    B='b'
class IntegerChoice(enum.IntEnum):
    A=1
    B=2
class FloatNumber(float): pass
class ByteString(bytes): pass
class LyingString(str):
    def __str__(self): return 'not the underlying value'
class RichInt(int):
    def __eq__(self, other): return True
values += [StringChoice.A,StringChoice.B,IntegerChoice.A,IntegerChoice.B,FloatNumber(1.25),ByteString(b'x'),LyingString('x')]
assert p.encode(RichInt(1)) is None
ops={'==':operator.eq,'!=':operator.ne,'<':operator.lt,'<=':operator.le,'>':operator.gt,'>=':operator.ge,'is':operator.is_,'is not':operator.is_not}
checked=0
for a in values:
    for b in values:
        aa,bb=p.encode(a),p.encode(b)
        if aa is None or bb is None: continue
        for name,fn in ops.items():
            try: model=c._observed_compare(name,aa,bb,a is b)
            except (ValueError,TypeError,OverflowError): continue
            try: actual=fn(a,b)
            except Exception as e: raise AssertionError((name,a,b,'model accepted real exception')) from e
            assert type(actual) is bool and model is actual,(name,aa,bb,model,actual)
            checked+=1
assert checked>2000,checked
assert p.encode(Overloaded()) is None
assert p.encode(Plain()) is not None
Plain.__eq__=lambda self,other: True
assert p.encode(Plain()) is None,'mutable equality descriptor was cached'

old='def f(v):\n    global x\n    return v is None\n'
new=old.replace('global x','global y').replace('is None','== None')
assert c.compare_only_changes(old,new) is None
assert len(c.compare_only_changes(old,old.replace('is None','== None')))==1
old='def f(a, b, v):\n    d = {**a, "k": b}\n    return v is None\n'
new='def f(a, b, v):\n    d = {"k": a, **b}\n    return v == None\n'
assert c.compare_only_changes(old,new) is None,'Dict unpacking positions were ignored'

src='def f(a,b):\n    return a is b\n'
node=next(n for n in ast.walk(ast.parse(src)) if isinstance(n,ast.Compare))
site=f'sqlglot/example.py|{node.lineno}|{node.col_offset}|is'
c._cache['operand_trace.json.gz']={'format':'raw-baseline-comparisons-v1','test_ids':['observed','missing'],'sites':{site:{'facts':[[['none'],['none'],True,True]],'tests':{'0':[0]}}}}
c._cache['import_lines.json.gz']={}
def patch(new):
    return 'diff --git a/sqlglot/example.py b/sqlglot/example.py\n'+''.join(difflib.unified_diff(src.splitlines(True),new.splitlines(True),fromfile='a/sqlglot/example.py',tofile='b/sqlglot/example.py'))
with tempfile.TemporaryDirectory() as root:
    os.mkdir(root+'/sqlglot')
    with open(root+'/sqlglot/example.py','w') as f:f.write(src)
    assert c.safe_tests(patch(src.replace('is b','== b')),root)=={'observed'}
    assert c.safe_tests(patch(src.replace('is b','is not b')),root)==set()
    c._cache['import_lines.json.gz']={'sqlglot/example.py':[node.lineno]}
    assert c.safe_tests(patch(src.replace('is b','== b')),root)==set()
print('Passed',checked,'independent operand/operator cases; mutable/overloaded types, compound edits, missing observations and import-time exclusions.')