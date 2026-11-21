"""Build the committed map from fork-trace data + pinned sources."""
import json, os, sys, glob, ast, re
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze
SRC = os.environ.get("SQLGLOT_SRC", "sqlglot")
FT = sys.argv[1]
OUT = sys.argv[2]
TESTS = json.load(open(sys.argv[3]))["tests"]
READS = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else {}
os.makedirs(OUT + "/src", exist_ok=True)
ids = [t["id"] for t in TESTS]
tidx = {i: n for n, i in enumerate(ids)}
testfile = defaultdict(list); testclass = defaultdict(list)
for n, i in enumerate(ids):
    f, c, m = i.split("::")
    testfile[f].append(n); testclass[f + "::" + c].append(n)

cov = defaultdict(lambda: defaultdict(set))   # file -> line -> tidx
users = defaultdict(set)                        # class -> tidx
mro = {}
reads = defaultdict(set)                        # path -> tidx
status = {}
def add(lines, tset):
    for f, l in lines:
        cov[f][l] |= tset
def add_seen(seen, tset, m):
    mro.update(m)
    for c in seen:
        for b in m.get(c, [c]):
            users[b] |= tset
imp = json.load(open(f"{FT}/_import.json"))
implines = defaultdict(set)
for f, l in imp["lines"]:
    implines[f].add(l)
for path in glob.glob(f"{FT}/t*.json"):
    n = int(os.path.basename(path)[1:-5]); d = json.load(open(path))
    add(d["lines"], {n}); add_seen(d["seen"], {n}, d["mro"])
    for r in d.get("reads", []): reads[r].add(n)
    status[n] = d["ok"]
for path in glob.glob(f"{FT}/f_*.json"):
    mod = os.path.basename(path)[2:-5]; f = mod.replace(".", "/") + ".py"
    d = json.load(open(path)); s = set(testfile[f])
    add(d["lines"], s); add_seen(d["seen"], s, d["mro"])
    for r in d.get("reads", []): reads[r] |= s
for path in glob.glob(f"{FT}/c_*.json"):
    q = os.path.basename(path)[2:-5]; mod, cls = q.rsplit(".", 1)
    s = set(testclass[mod.replace(".", "/") + ".py::" + cls])
    d = json.load(open(path))
    add(d["lines"], s); add_seen(d["seen"], s, d["mro"])
    for r in d.get("reads", []): reads[r] |= s
for k, v in READS.items():   # optional pytest-based fixture reads
    if k in tidx:
        for r in v: reads[r].add(tidx[k])
missing = [i for i in range(len(ids)) if i not in status]
print("tests traced", len(status), "missing", len(missing), "not ok", [ids[i] for i, ok in status.items() if not ok][:20])

# readers index: identifier -> lines (Load / attribute / string constant)
names_lines = defaultdict(list)   # name -> [(file, line)]
srcfiles = sorted(glob.glob(f"{SRC}/sqlglot/**/*.py", recursive=True) + glob.glob(f"{SRC}/tests/**/*.py", recursive=True))
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
for p in srcfiles:
    rel = p[len(SRC) + 1:]
    src = open(p, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            names_lines[node.id].append((rel, node.lineno))
        elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Store):
            names_lines[node.attr].append((rel, node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and IDENT.match(node.value) and len(node.value) < 60:
            names_lines[node.value].append((rel, node.lineno))
    # per-file record
    stm = analyze.statements(src)
    strs = analyze.string_ranges(src)
    fc = cov.get(rel, {})
    sets = []; setid = {}; covl = {}
    for l, s in fc.items():
        key = tuple(sorted(s))
        if key not in setid:
            setid[key] = len(sets); sets.append(list(key))
        covl[l] = setid[key]
    rec = {"cov": covl, "sets": sets, "imp": sorted(implines.get(rel, ())), "stmts": stm, "strs": strs, "lams": analyze.lambda_spans(src)}
    out = os.path.join(OUT, "src", rel.replace("/", "__") + ".json")
    json.dump(rec, open(out, "w"), separators=(",", ":"))

# readers for every name that is an assignment target / class name / def name
wanted = set()
for p in srcfiles:
    if "/sqlglot/" not in p and "/tests/" not in p: continue
    try: tree = ast.parse(open(p, encoding="utf-8").read())
    except SyntaxError: continue
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)): wanted.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for n in ast.walk(t):
                    if isinstance(n, ast.Name): wanted.add(n.id)
gsets = []; gid = {}
def sid(s):
    key = tuple(sorted(s))
    if key not in gid: gid[key] = len(gsets); gsets.append(list(key))
    return gid[key]
readers = {}
for name in wanted:
    s = set(); unsafe = False
    for f, l in names_lines.get(name, ()):
        s |= cov.get(f, {}).get(l, set())
        if l in implines.get(f, ()): unsafe = True
    readers[name] = [sid(s), 1 if unsafe else 0]
usage = {c: sid(s) for c, s in users.items()}
fixtures = {p: sorted(s) for p, s in reads.items()}
json.dump({"tests": ids, "testfile": testfile, "testclass": testclass, "readers": readers, "usage": usage,
           "fixtures": fixtures, "sets": gsets}, open(os.path.join(OUT, "meta.json"), "w"), separators=(",", ":"))
tot = sum(os.path.getsize(p) for p in glob.glob(OUT + "/**", recursive=True) if os.path.isfile(p))
print("map bytes", tot, "readers", len(readers), "usage", len(usage), "gsets", len(gsets))
