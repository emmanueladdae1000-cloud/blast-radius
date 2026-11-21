import json, sys
T = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in T}
cls = {t["number"]: "::".join(t["id"].split("::")[:2]) for t in T}
EXE = "tests/test_executor.py::TestExecutor"
def pack(nums):
    items = sorted((ms[n] for n in nums), reverse=True)
    if not items: return 0
    best = None
    for k in range(1, 9):
        loads = [0] * k
        for d in items:
            i = loads.index(min(loads)); loads[i] += d
        c = max(loads) + 2000 * k
        best = c if best is None or c < best else best
    return best
def policy(C, F, B, monster_ms=3000, cpu_factor=1.5, allow_exe=True, exe_setup=1500, per_test=20):
    C = set(C); F = set(F)
    mons = {n for n in C if ms[n] >= monster_ms}
    if not mons: return pack(C), "nomonster", 0
    pool = sorted((n for n in C - mons if (allow_exe or cls[n] != EXE) and ms[n] <= 2600), key=lambda n: (ms[n], n))
    L1 = []; tot = 0
    for n in pool:
        if tot + ms[n] > B: break
        L1.append(n); tot += ms[n]
    if len(L1) < 3: return pack(C), "fewL1", 0
    cpu = 0; setup = set()
    for n in L1:
        if cls[n] == EXE and EXE not in setup: cpu += exe_setup; setup.add(EXE)
        cpu += cpu_factor * ms[n] + per_test
        if n in F:
            return cpu + pack(C), "killed", 0
    rest = C - set(L1) - mons
    miss = 1 if (mons & F) else 0
    return cpu + pack(rest) + 300000 * miss, "skip", miss
rows = []
for path in sys.argv[1:]:
    for l in open(path):
        try: d = json.loads(l)
        except Exception: continue
        if "Cl" in d: rows.append((d["Cl"], d["Fl"]))
print("rows", len(rows))
for B in [0, 500, 1000, 2000, 4000, 8000, 12000, 20000]:
    tot = 0; base = 0; misses = 0; kinds = {}
    for C, F in rows:
        c, k, m = policy(C, F, B) if B else (pack(C), "base", 0)
        tot += c; base += pack(C); misses += m; kinds[k] = kinds.get(k, 0) + 1
    print(f"B={B:6d} total={tot/1000:8.1f} base={base/1000:8.1f} saved={(base-tot)/1000:7.1f} misses={misses} {kinds}")
