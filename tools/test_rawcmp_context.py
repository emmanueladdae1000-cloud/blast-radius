"""Coverage-context regressions using only synthetic original operand facts."""
import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SITE='sqlglot/example.py|3|4|=='
PAIR=[['int','1'],['int','1'],True,True]
OTHER=[['int','2'],['int','2'],True,True]
TESTS=[{'number':0,'id':'tests/test_optimizer.py::TestOptimizer::test_a'},
       {'number':1,'id':'tests/test_optimizer.py::TestOptimizer::test_b'},
       {'number':2,'id':'tests/test_other.py::TestOther::test_c'}]


class ContextMergeTests(unittest.TestCase):
    def merge(self, mutate=None):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            (folder/'tests.json').write_text(json.dumps({'tests':list(reversed(TESTS))}))
            for seed in ('317','391'):
                d={'format':'raw-baseline-comparisons-v1','context_version':2,'seed':seed,
                   'observations':{SITE:{
                       '@scope:tests/test_optimizer.py::TestOptimizer':{'complete':True,'pairs':[PAIR]},
                       '2':{'complete':True,'pairs':[PAIR]},
                       '@import':{'complete':True,'pairs':[OTHER]}}},
                   'failed_tests':[],'passed_tests':['0','1','2'],'incomplete_tests':[],
                   'collection_failures':[]}
                if mutate:mutate(d)
                with gzip.open(folder/f'rawcmp_{seed}_main_1_session.json.gz','wt') as f:json.dump(d,f)
            result=subprocess.run([sys.executable,str(ROOT/'tools/merge_rawcmp.py'),str(folder),
                                   str(folder/'merged.json.gz'),str(folder/'tests.json')],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            with gzip.open(folder/'merged.json.gz','rt') as f:return json.load(f)

    def test_fixture_scope_and_global_facts_expand_under_attested_v2(self):
        data=self.merge()
        self.assertEqual(data['context_version'],2)
        self.assertEqual(data['test_ids'],[t['id'] for t in TESTS])
        self.assertEqual(set(data['sites'][SITE]['tests']),{'0','1','2'})
        for indices in data['sites'][SITE]['tests'].values():
            self.assertEqual(len(indices),2)

    def test_one_unattested_file_keeps_historical_class_pool_exclusion(self):
        def mutate(d):
            if d['seed']=='391':d.pop('context_version')
        data=self.merge(mutate)
        self.assertEqual(data['context_version'],1)
        self.assertEqual(set(data['sites'][SITE]['tests']),{'2'})

    def test_incomplete_global_observation_disables_site(self):
        data=self.merge(lambda d:d['observations'][SITE]['@import'].update(complete=False))
        self.assertEqual(data['sites'],{})

    def test_incomplete_fixture_scope_blocks_all_members(self):
        data=self.merge(lambda d:d['incomplete_tests'].append('@scope:tests/test_optimizer.py::TestOptimizer'))
        self.assertEqual(set(data['sites'][SITE]['tests']),{'2'})

    def test_incomplete_global_context_blocks_all_tests(self):
        data=self.merge(lambda d:d['incomplete_tests'].append('@import'))
        self.assertEqual(data['sites'],{})


if __name__=='__main__':unittest.main(verbosity=2)
