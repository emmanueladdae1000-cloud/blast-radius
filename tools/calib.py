"""Calibrate: when heavy test H fails on a mutant, does some cheap candidate fail too?"""
import random, re, os, sys, json, subprocess, difflib, gzip
sys.path.insert(0, "/home/claude/work/tools")
from junit import failed_ids
sys.path.insert(0, "/home/claude/work/tools")
from mutcheck import mutate_line
ORIG = "/home/claude/work/sqlglot"; MUT = "/home/claude/work/mut/sqlglot"
T = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in T}; byn = {t["number"]: t for t in T}
meta = json.load(gzip.open("/home/claude/work/repo/map/meta.json.gz", "rt"))
ids = meta["tests"]
HEAVY = {num[i] for i in ["tests/test_executor.py::TestExecutor::test_execute_tpcds", "tests/test_parser.py::TestParser::test_max_nodes",
                          "tests/test_generator.py::TestGenerator::test_generator_memory", "tests/test_executor.py::TestExecutor::test_execute_tpch",
                          "tests/test_executor.py::TestExecutor::test_optimized_tpch"]}
H = ids.index("tests/test_executor.py::TestExecutor::test_execute_tpcds")
def lines_with_H():
    out = []
    for fn in os.listdir("/home/claude/work/repo/map/src"):
        rel = fn[:-8].replace("__", "/")
        if not rel.startswith("sqlglot/"): continue
        d = json.load(gzip.open("/home/claude/work/repo/map/src/" + fn, "rt"))
        for l, sid in d["cov"].items():
            s = d["sets"][sid]
            if H in s and len(s) > 1 and int(l) not in set(d["imp"]):
                out.append((rel, int(l)))
    return out
def main(seed, count):
    rng = random.Random(seed); cands = lines_with_H(); rng.shuffle(cands)
    done = 0
    for rel, ln in cands:
        if done >= count: break
        src = open(os.path.join(ORIG, rel)).read().splitlines(keepends=True)
        line = src[ln - 1]
        if line.strip().startswith("#"): continue
        new = mutate_line(line, rng)
        if not new or new == line: continue
        mutated = src[:ln - 1] + [new] + src[ln:]
        try: compile("".join(mutated), rel, "exec")
        except SyntaxError: continue
        patch = f"diff --git a/{rel} b/{rel}\n" + "".join(difflib.unified_diff(src, mutated, "a/" + rel, "b/" + rel))
        os.makedirs("/tmp/cb/commits", exist_ok=True)
        for f in os.listdir("/tmp/cb/commits"): os.remove("/tmp/cb/commits/" + f)
        open("/tmp/cb/tests.json", "w").write(open("/home/claude/kit/sample/tests.json").read())
        open("/tmp/cb/commits/m.patch", "w").write(patch)
        r = subprocess.run([sys.executable, "/home/claude/work/repo/selector.py"], capture_output=True, text=True,
                           env=dict(os.environ, BR_DATA="/tmp/cb", BR_SRC=ORIG, BR_VERIFY="0"))
        C = {n for w in json.loads(r.stdout.splitlines()[0])["workers"] for n in w}
        if num[ids[H]] not in C: continue
        open(os.path.join(MUT, rel), "w").writelines(mutated)
        try:
            nodes = [byn[n]["id"] for n in sorted(C)]
            open("/tmp/cb/nodes.txt", "w").write("\n".join(nodes))
            p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2", "-rfE", "--junitxml=/tmp/jx_%d.xml" % os.getpid(), "--timeout", "300"] + nodes,
                               cwd=MUT, capture_output=True, text=True, env=dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1"))
        finally:
            open(os.path.join(MUT, rel), "w").writelines(src)
        F = set()
        for l in p.stdout.splitlines():
            if l.startswith(("FAILED", "ERROR", "SUBFAILED")):
                m = re.search(r"(tests/\S+?::\w+::\w+)", l)
                if m and m.group(1) in num: F.add(num[m.group(1)])
        _j = failed_ids("/tmp/jx_%d.xml" % os.getpid())
        if _j is not None:
            F = {num[i] for i in _j if i in num}
        hf = num[ids[H]] in F
        cheap_fail = sorted(F - HEAVY)
        cheapC = sum(byn[n]["seconds"] for n in C - HEAVY)
        print(json.dumps({"rel": rel, "line": ln, "old": line.strip()[:60], "new": new.strip()[:60], "C": len(C), "cheapC_s": round(cheapC, 2),
                          "H_fail": hf, "heavy_fail": sorted(F & HEAVY), "cheap_fail": len(cheap_fail), "Cl": sorted(C), "Fl": sorted(F)}), flush=True)
        done += 1
if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]))
