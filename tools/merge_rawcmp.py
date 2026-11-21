"""Merge raw, edit-independent observations from two baseline hash-seed runs."""
import glob,gzip,json,sys
from collections import defaultdict
root,out,tests_path=sys.argv[1:]
with open(tests_path) as f: tests=json.load(f)['tests']
ids=[t['id'] for t in tests]
records=defaultdict(lambda:defaultdict(dict))
passed=defaultdict(set)
blocked=defaultdict(set)
seeds=set()
for path in sorted(glob.glob(root+'/rawcmp_*.json.gz')):
    with gzip.open(path,'rt') as f: data=json.load(f)
    seed=data['seed']; seeds.add(seed)
    if data['collection_failures']: raise RuntimeError('Baseline collection failed: '+repr(data['collection_failures']))
    if path.endswith('_session.json.gz'): passed[seed].update(data['passed_tests'])
    blocked[seed].update(data['failed_tests']); blocked[seed].update(data['incomplete_tests'])
    for site,ts in data['observations'].items():
        for tid,row in ts.items():
            r=records[site][tid].setdefault(seed,{'complete':True,'pairs':set()})
            r['complete'] &= row['complete']
            r['pairs'].update(json.dumps(p,separators=(',',':'),ensure_ascii=True) for p in row['pairs'])
            if len(r['pairs'])>128:r['complete']=False
if len(seeds)<2: raise RuntimeError('Need at least two hash-seed runs')
common=set.intersection(*(passed[s]-blocked[s] for s in seeds))
result={}
for site,ts in records.items():
    facts=[]; factidx={}; usable={}
    for tid,byseed in ts.items():
        if tid not in common or set(byseed)!=seeds or not all(r['complete'] and r['pairs'] for r in byseed.values()):continue
        pairs=set.union(*(r['pairs'] for r in byseed.values()))
        if len(pairs)>128:continue
        indices=[]
        for p in sorted(pairs):
            if p not in factidx:
                factidx[p]=len(facts);facts.append(json.loads(p))
            indices.append(factidx[p])
        usable[tid]=indices
    if usable:result[site]={'facts':facts,'tests':usable}
data={'format':'raw-baseline-comparisons-v1','source_commit':'eb11aec4be8cb2122afe713a4816662c3f0fb2fa','hash_seeds':sorted(seeds),'test_ids':ids,'sites':result}
with gzip.open(out,'wt',encoding='utf8') as f:json.dump(data,f,separators=(',',':'),ensure_ascii=True,allow_nan=False)
print('Raw baseline sites:',len(result),'site/test pairs:',sum(len(r['tests']) for r in result.values()),'passing tests both seeds:',len(common))
print('No alternate comparison results or mutation outcomes were generated.')