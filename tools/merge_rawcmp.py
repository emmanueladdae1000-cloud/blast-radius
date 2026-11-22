"""Merge raw, edit-independent observations from two baseline hash-seed runs."""
import glob,gzip,json,sys
from collections import defaultdict
from functools import lru_cache
root,out,tests_path=sys.argv[1:]
with open(tests_path) as f: tests=json.load(f)['tests']
if {t['number'] for t in tests}!=set(range(len(tests))):raise RuntimeError('Expected contiguous zero-based test numbers')
ids=[t['id'] for t in sorted(tests,key=lambda t:t['number'])]
known_tests={str(t['number']) for t in tests}
# The original recorder left worker input unpickling attributed to its previous
# task. TestOptimizer owns a class-scoped process pool across test methods
# (tests/test_optimizer.py setUpClass); discard that class's deductions from
# these historical profiles. Executor pools live within one outer test.
pool_lifetime_unknown={str(t['number']) for t in tests
                       if t['id'].startswith('tests/test_optimizer.py::TestOptimizer::')}
records=defaultdict(lambda:defaultdict(dict))
passed=defaultdict(set)
blocked=defaultdict(set)
seeds=set()
all_context2=True
profile_count=0
@lru_cache(maxsize=None)
def expand(context):
    if not context.startswith('@scope:'):return (context,)
    prefix=context[len('@scope:'):]
    matched=tuple(str(t['number']) for t in tests if not prefix or t['id']==prefix or t['id'].startswith(prefix+'::') or t['id'].startswith(prefix+'/'))
    return matched or ('@import',)
for path in sorted(glob.glob(root+'/rawcmp_*.json.gz')):
    with gzip.open(path,'rt') as f: data=json.load(f)
    profile_count+=1
    all_context2 &= data.get('context_version')==2
    seed=data['seed']; seeds.add(seed)
    if data['collection_failures']: raise RuntimeError('Baseline collection failed: '+repr(data['collection_failures']))
    if path.endswith('_session.json.gz'): passed[seed].update(data['passed_tests'])
    blocked[seed].update(data['failed_tests'])
    for context in data['incomplete_tests']:blocked[seed].update(expand(context))
    for site,ts in data['observations'].items():
        for context,row in ts.items():
            for tid in expand(context):
                r=records[site][tid].setdefault(seed,{'complete':True,'pairs':set()})
                r['complete'] &= row['complete']
                r['pairs'].update(json.dumps(p,separators=(',',':'),ensure_ascii=True) for p in row['pairs'])
                if len(r['pairs'])>128:r['complete']=False
if len(seeds)<2: raise RuntimeError('Need at least two hash-seed runs')
common=known_tests.intersection(*(passed[s]-blocked[s] for s in seeds))
if not all_context2:common-=pool_lifetime_unknown
if any('@import' in blocked[s] for s in seeds):common=set()
result={}
for site,ts in records.items():
    facts=[]; factidx={}; usable={}
    shared=ts.get('@import',{})
    if shared and (set(shared)!=seeds or not all(r['complete'] for r in shared.values())):continue
    for tid,byseed in ts.items():
        if tid not in common or set(byseed)!=seeds or not all(r['complete'] and r['pairs'] for r in byseed.values()):continue
        pairs=set.union(*(r['pairs'] for r in byseed.values()))
        if shared:pairs.update(set.union(*(r['pairs'] for r in shared.values())))
        if len(pairs)>128:continue
        indices=[]
        for p in sorted(pairs):
            if p not in factidx:
                factidx[p]=len(facts);facts.append(json.loads(p))
            indices.append(factidx[p])
        usable[tid]=indices
    if usable:result[site]={'facts':facts,'tests':usable}
data={'format':'raw-baseline-comparisons-v1','context_version':2 if all_context2 else 1,'source_commit':'eb11aec4be8cb2122afe713a4816662c3f0fb2fa','hash_seeds':sorted(seeds),'test_ids':ids,'sites':result}
with gzip.open(out,'wt',encoding='utf8') as f:json.dump(data,f,separators=(',',':'),ensure_ascii=True,allow_nan=False)
print('Raw baseline sites:',len(result),'site/test pairs:',sum(len(r['tests']) for r in result.values()),'passing tests both seeds:',len(common))
print('No alternate comparison results or mutation outcomes were generated.')
print('Context version:',2 if all_context2 else 1,'profile files:',profile_count)
print('Historical class-pool attribution fallback excludes',0 if all_context2 else len(pool_lifetime_unknown),'TestOptimizer tests.')
