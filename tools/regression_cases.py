"""Dev-only focused counterexamples; no generated data is included in the image."""
import difflib,json,os,pathlib,shutil,subprocess,sys,tempfile,xml.etree.ElementTree as ET
base=pathlib.Path('/baseline/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa')
out=pathlib.Path('/audit/reports/regression'); (out/'commits').mkdir(parents=True,exist_ok=True)
shutil.copy('/audit/sample/tests.json',out/'tests.json')
parser=(base/'sqlglot/parser.py').read_text()
needle='if self._node_count > self.max_nodes:'
assert parser.count(needle)==1
threshold=parser.replace(needle,'if self._node_count > self.max_nodes and self.max_nodes < 100_000:')
fixture_rel='tests/fixtures/optimizer/tpc-ds/tpc-ds.sql'
fixture=(base/fixture_rel).read_text()
assert '-- TPC-DS 1\n' in fixture
mysql=(base/'sqlglot/dialects/mysql.py').read_text()
assert mysql.count('NORMALIZATION_STRATEGY = NormalizationStrategy.CASE_SENSITIVE')==1
expected='AND "date_dim"."d_year" = 2001'
assert expected in fixture
cases={
 'threshold':({'sqlglot/parser.py':threshold},'tests/test_parser.py::TestParser::test_max_nodes'),
 'mixed_threshold':({'sqlglot/parser.py':threshold,fixture_rel:fixture.replace('-- TPC-DS 1\n','-- TPC-DS 1 (validation comment)\n',1)},'tests/test_parser.py::TestParser::test_max_nodes'),
 'class_constant':({'sqlglot/dialects/mysql.py':mysql.replace('NORMALIZATION_STRATEGY = NormalizationStrategy.CASE_SENSITIVE','NORMALIZATION_STRATEGY = NormalizationStrategy.LOWERCASE')},'tests/dialects/test_dialect.py::TestDialect::test_get_or_raise'),
 'expected_fixture':({fixture_rel:fixture.replace(expected,'AND "date_dim"."d_year" = 2002',1)},'tests/test_optimizer.py::TestOptimizer::test_tpcds'),
}
required={}
for name,(files,nodeid) in cases.items():
    diff=''
    for rel,updated in files.items():
        original=(base/rel).read_text()
        diff+='diff --git a/'+rel+' b/'+rel+'\n'
        diff+=''.join(difflib.unified_diff(original.splitlines(True),updated.splitlines(True),fromfile='a/'+rel,tofile='b/'+rel))
    patch_path=out/'commits'/f'{name}.patch'
    log_path=out/f'oracle-{name}.log'
    cached=patch_path.exists() and patch_path.read_text()==diff and log_path.exists() and ('FAILED '+nodeid) in log_path.read_text()
    patch_path.write_text(diff)
    if cached:
        required[name]=[nodeid]
        print(name,'reused unchanged focused oracle failure',flush=True)
        continue
    with tempfile.TemporaryDirectory(prefix='br-oracle-') as tmp:
        work=pathlib.Path(tmp)/'source'
        shutil.copytree(base,work)
        for rel,updated in files.items():(work/rel).write_text(updated)
        env=dict(os.environ,PYTHONPATH=str(work),SKIP_INTEGRATION='0',PYTHONDONTWRITEBYTECODE='1')
        env.pop('BR_RAWCMP_ENABLE',None)
        proc=subprocess.run([sys.executable,'-m','pytest','-q','-p','no:cacheprovider','--tb=short','--timeout=180','--junitxml='+tmp+'/result.xml',nodeid],cwd=work,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=240)
        (out/f'oracle-{name}.log').write_bytes(proc.stdout)
        tree=ET.parse(tmp+'/result.xml')
        failures=tree.findall('.//failure')+tree.findall('.//error')
        if not failures or proc.returncode!=1:
            raise AssertionError((name,proc.returncode,proc.stdout[-2000:]))
        required[name]=[nodeid]
        print(name,'focused oracle confirms failure:',nodeid,flush=True)
(out/'minimum_required_tests.json').write_text(json.dumps(required,indent=2))
print('Focused oracles establish minimum required tests, not complete failure sets.')