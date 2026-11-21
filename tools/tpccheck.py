import os, sys, json, subprocess, difflib, re
sys.path.insert(0, "/home/claude/work/tools")
from junit import failed_ids
SRC = "/home/claude/work/sqlglot"; MUT = "/home/claude/work/mut2/sqlglot"
T = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]; num = {t["id"]: t["number"] for t in T}
cases = [
 ("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", '"store_returns"."sr_customer_sk" AS "ctr_customer_sk",', '"store_returns"."sr_customer_sk" AS "ctr_customer_skX",', "exp"),
 ("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", "AND d_year = 2001", "AND d_year = 2002", "sql"),
 ("tests/fixtures/optimizer/tpc-h/tpc-h.sql", None, None, "tpch-exp"),
 ("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", "Sum(sr_return_amt) AS ctr_total_return", "Sum(sr_return_amt AS ctr_total_return", "sql-broken"),
 ("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", "ORDER  BY c_customer_id\nLIMIT 100;", "ORDER  BY c_customer_id DESC\nLIMIT 100;", "sql-order"),
]
def run(rel, old, new, tag):
    src = open(os.path.join(SRC, rel)).read().splitlines(keepends=True)
    if old is None:   # tpc-h: change first expected-output line containing '"lineitem"."l_returnflag"'
        for i, l in enumerate(src):
            if '"lineitem"."l_returnflag" AS "l_returnflag"' in l: old = l.strip(); new = old.replace('l_returnflag" AS', 'l_returnflagX" AS'); break
    i = next(k for k, l in enumerate(src) if old in l)
    m = src[:]; m[i] = m[i].replace(old, new, 1)
    patch = f"diff --git a/{rel} b/{rel}\n" + "".join(difflib.unified_diff(src, m, "a/" + rel, "b/" + rel))
    os.makedirs("/tmp/tc/commits", exist_ok=True)
    for f in os.listdir("/tmp/tc/commits"): os.remove("/tmp/tc/commits/" + f)
    open("/tmp/tc/tests.json", "w").write(open("/home/claude/kit/sample/tests.json").read())
    open("/tmp/tc/commits/m.patch", "w").write(patch)
    r = subprocess.run([sys.executable, "/home/claude/work/repo/selector.py"], capture_output=True, text=True, env=dict(os.environ, BR_DATA="/tmp/tc", BR_SRC=SRC))
    C = {n for w in json.loads(r.stdout.splitlines()[0])["workers"] for n in w}
    note = [l for l in r.stderr.splitlines() if l.startswith("m ")]
    open(os.path.join(MUT, rel), "w").writelines(m)
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE", "--junitxml=/tmp/jx_%d.xml" % os.getpid(), "tests/test_executor.py",
                            "tests/test_optimizer.py::TestOptimizer::test_tpcds", "tests/test_optimizer.py::TestOptimizer::test_tpch"],
                           cwd=MUT, capture_output=True, text=True, env=dict(os.environ, SKIP_INTEGRATION="0"), timeout=900)
    finally:
        open(os.path.join(MUT, rel), "w").writelines(src)
    F = set()
    for l in p.stdout.splitlines():
        if l.startswith(("FAILED", "ERROR", "SUBFAILED")):
            mm = re.search(r"(tests/\S+?::\w+::\w+)", l)
            if mm and mm.group(1) in num: F.add(num[mm.group(1)])
    _j = failed_ids("/tmp/jx_%d.xml" % os.getpid())
    if _j is not None:
        F = {num[i] for i in _j if i in num}
    print(tag, "C", sorted(C), "F", sorted(F), "MISS" if F - C else "ok", note, p.stdout.strip().splitlines()[-1][-60:], flush=True)
for c in cases[int(os.environ.get("ONLY", "0")):int(os.environ.get("UPTO", "99"))]: run(*c)
