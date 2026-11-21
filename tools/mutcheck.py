"""Generate random single-line mutations, compare selector output vs real pytest failures."""
import random, re, os, sys, json, subprocess, difflib, ast
sys.path.insert(0, "/home/claude/work/repo")
os.environ["BR_SRC"] = "/home/claude/work/sqlglot"
import selector
ORIG = "/home/claude/work/sqlglot"
MUT = "/home/claude/work/mut/sqlglot"
T = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in T}
ms = {t["number"]: round(t["seconds"] * 1000) for t in T}
maps = selector.Maps(); sel = selector.Selector(maps)
RULES = [(r"\band\b", "or"), (r"\bor\b", "and"), (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "),
         (r" > ", " >= "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\bis not\b", "is"),
         (r"\bnot ", ""), (r"\b(\d+)\b", None), (r'"([A-Z_]{3,})"', None), (r"\+ 1\b", "- 1")]
def mutate_line(line, rng):
    opts = [(i, m) for i, (pat, rep) in enumerate(RULES) for m in re.finditer(pat, line)]
    if not opts: return None
    i, m = rng.choice(opts); pat, rep = RULES[i]
    if rep is None:
        if pat.startswith(r"\b(\d"):
            rep = str(int(m.group(1)) + 1)
        else:
            rep = '"' + m.group(1) + 'X"'
    return line[:m.start()] + rep + line[m.end():]
def run_suite():
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2", "-rfE",
                        "--timeout", "120", "tests"], cwd=MUT, capture_output=True, text=True, env=env)
    fails = set()
    for line in r.stdout.splitlines():
        if line.startswith(("FAILED", "ERROR", "SUBFAILED")):
            m = re.search(r"(tests/\S+?::\w+::\w+)", line)
            if m and m.group(1) in num: fails.add(num[m.group(1)])
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]
    return fails, tail
def main(seed, count, filt):
    rng = random.Random(seed)
    files = [os.path.join(dp, f)[len(ORIG) + 1:] for dp, _, fs in os.walk(ORIG + "/sqlglot") for f in fs if f.endswith(".py")]
    files = [f for f in files if re.search(filt, f)]
    done = 0
    while done < count:
        rel = rng.choice(files)
        src = open(os.path.join(ORIG, rel)).read().splitlines(keepends=True)
        i = rng.randrange(len(src)); line = src[i]
        if line.strip().startswith("#") or not line.strip(): continue
        new = mutate_line(line, rng)
        if not new or new == line: continue
        mutated = src[:i] + [new] + src[i + 1:]
        try: compile("".join(mutated), rel, "exec")
        except SyntaxError: continue
        patch = "".join(difflib.unified_diff(src, mutated, "a/" + rel, "b/" + rel))
        patch = f"diff --git a/{rel} b/{rel}\n" + patch
        os.makedirs("/tmp/mc/commits", exist_ok=True)
        for f in os.listdir("/tmp/mc/commits"): os.remove("/tmp/mc/commits/" + f)
        open("/tmp/mc/tests.json", "w").write(open("/home/claude/kit/sample/tests.json").read())
        open("/tmp/mc/commits/m.patch", "w").write(patch)
        r = subprocess.run([sys.executable, "/home/claude/work/repo/selector.py"], capture_output=True, text=True,
                           env=dict(os.environ, BR_DATA="/tmp/mc", BR_SRC=ORIG))
        o = json.loads(r.stdout.strip().splitlines()[0])
        Cn = {n for w in o["workers"] for n in w}
        notes = [l for l in r.stderr.splitlines() if l.startswith("m ")]
        open(os.path.join(MUT, rel), "w").writelines(mutated)
        try:
            F, tail = run_suite()
        finally:
            open(os.path.join(MUT, rel), "w").writelines(src)
        miss = F - Cn
        print(json.dumps({"file": rel, "line": i + 1, "old": line.strip()[:80], "new": new.strip()[:80],
                          "F": len(F), "C": len(Cn), "miss": sorted(miss), "notes": notes, "tail": tail[-80:]}), flush=True)
        done += 1
if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else ".")
