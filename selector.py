"""Blast Radius selector.

For each patch, pick the tests that can observe the change, using maps built
from the pinned sqlglot commit (per-test line coverage, class usage, readers of
class/module attributes, fixture reads), then split them over workers.
"""
import gzip
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MAP = os.path.join(HERE, "map")
DATA = os.environ.get("BR_DATA", "/data")
SRC = os.environ.get("BR_SRC", os.path.join(HERE, "src"))  # pinned sqlglot sources (optional)

# Which tests use which part of the TPC query pairs (tests/test_executor.py and
# tests/test_optimizer.py at the pinned commit):
#  test_execute_tpc{h,ds}: pool.submit(mp_execute, parse_one(sql), args) -> meta + sql
#  test_optimized_tpch:    cached_execute(sql) vs transpile(optimized)    -> sql + expected
#  TestOptimizer.test_tpc{h,ds}: check_file(...)                          -> everything
TPC_PAIR_FILES = {
    "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": {
        "any": ["tests/test_optimizer.py::TestOptimizer::test_tpcds"],
        "meta_sql": ["tests/test_executor.py::TestExecutor::test_execute_tpcds"],
        "sql_expected": [],
        "probe": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    },
    "tests/fixtures/optimizer/tpc-h/tpc-h.sql": {
        "any": ["tests/test_optimizer.py::TestOptimizer::test_tpch"],
        "meta_sql": ["tests/test_executor.py::TestExecutor::test_execute_tpch"],
        "sql_expected": ["tests/test_executor.py::TestExecutor::test_optimized_tpch"],
    },
}
TPC_PROBE_MAX = int(os.environ.get("BR_TPC_PROBE_MAX", "6"))
LAZY_LOAD_FILES = {"sqlglot/__init__.py", "sqlglot/dialects/__init__.py", "sqlglot/dialects/dialect.py"}
ROOT_CONFIG = {"pyproject.toml", "setup.cfg", "setup.py", "conftest.py", "tox.ini", "pytest.ini", "Makefile"}


def log(*a):
    print(*a, file=sys.stderr)


class Maps:
    def __init__(self):
        with gzip.open(os.path.join(MAP, "meta.json.gz"), "rt") as fh:
            m = json.load(fh)
        self.ids = m["tests"]
        self.testfile = m["testfile"]
        self.testclass = m["testclass"]
        self.readers = m["readers"]
        self.usage = m["usage"]
        self.fixtures = m["fixtures"]
        self.gsets = m["sets"]
        self.eager = set(m.get("eager", []))
        self.all = set(range(len(self.ids)))
        self._src = {}

    def src(self, rel):
        if rel not in self._src:
            p = os.path.join(MAP, "src", rel.replace("/", "__") + ".json.gz")
            if os.path.exists(p):
                with gzip.open(p, "rt") as fh:
                    d = json.load(fh)
                d["cov"] = {int(k): v for k, v in d["cov"].items()}
                d["imp"] = set(d["imp"])
                self._src[rel] = d
            else:
                self._src[rel] = None
        return self._src[rel]

    def gset(self, i):
        return self.gsets[i]


def parse_patch(text):
    """Return list of file changes: {old, new, hunks: [(old_start, [(tag, text)], counts)]}."""
    files = []
    cur = None
    hunk = None

    def new_file():
        f = {"old": None, "new": None, "hunks": [], "meta": [], "binary": False}
        files.append(f)
        return f

    for line in text.split("\n"):
        # hunk body first: '--- x' / '+++ x' inside a hunk are removed/added lines
        if hunk is not None and (hunk[2][0] > 0 or hunk[2][1] > 0) and line[:1] in ("+", "-", " ", ""):
            tag = line[:1] or " "
            if tag == " " or tag == "":
                if hunk[2][0] <= 0 or hunk[2][1] <= 0:
                    hunk = None
                else:
                    hunk[1].append((" ", line[1:]))
                    hunk[2][0] -= 1
                    hunk[2][1] -= 1
                    continue
            elif tag == "-" and hunk[2][0] > 0:
                hunk[1].append(("-", line[1:]))
                hunk[2][0] -= 1
                continue
            elif tag == "+" and hunk[2][1] > 0:
                hunk[1].append(("+", line[1:]))
                hunk[2][1] -= 1
                continue
            else:
                hunk = None
        if line.startswith("\\"):
            continue
        if line.startswith("diff --git "):
            cur = new_file()
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            hunk = None
            continue
        if line.startswith("--- "):
            if cur is None or cur["hunks"]:
                cur = new_file()
            p = line[4:].split("\t")[0].strip()
            cur["old"] = None if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
            hunk = None
            continue
        if line.startswith("+++ ") and cur is not None:
            p = line[4:].split("\t")[0].strip()
            cur["new"] = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m and cur is not None:
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            hunk = [int(m.group(1)), [], [old_len, new_len]]
            cur["hunks"].append(hunk)
            continue
        if cur is not None and not cur["hunks"]:
            cur["meta"].append(line)
            if line.startswith("new file mode"):
                cur["old"] = None
            elif line.startswith("deleted file mode"):
                cur["new"] = None
            elif line.startswith("rename from "):
                cur["old"] = line[len("rename from "):].strip()
            elif line.startswith("rename to "):
                cur["new"] = line[len("rename to "):].strip()
            elif line.startswith("GIT binary patch") or (line.startswith("Binary files") and line.endswith("differ")):
                cur["binary"] = True
    return [f for f in files if f["old"] or f["new"]]


def changed_lines(hunks):
    """Old-file line numbers touched: removed lines, and (o-1, o) around insertions.

    Returns (removed, inserted_at, added_texts, removed_texts)."""
    removed, ins, added_t, removed_t = [], [], [], []
    for start, lines, _ in hunks:
        o = start if start > 0 else 1
        for tag, text in lines:
            if tag == " ":
                o += 1
            elif tag == "-":
                removed.append(o)
                removed_t.append(text)
                o += 1
            else:
                ins.append(o)
                added_t.append(text)
    return removed, ins, added_t, removed_t


def apply_hunks(orig_lines, hunks):
    out = []
    pos = 0
    for start, lines, _ in hunks:
        s = max(start - 1, 0) if any(t != "+" for t, _ in lines) or start > 0 else 0
        if start == 0:
            s = 0
        out.extend(orig_lines[pos:s])
        pos = s
        for tag, text in lines:
            if tag == " ":
                if pos >= len(orig_lines) or orig_lines[pos].rstrip("\r\n") != text:
                    return None
                out.append(orig_lines[pos])
                pos += 1
            elif tag == "-":
                if pos >= len(orig_lines) or orig_lines[pos].rstrip("\r\n") != text:
                    return None
                pos += 1
            else:
                out.append(text + "\n")
    out.extend(orig_lines[pos:])
    return out


def fixture_pairs(text):
    """Same result as tests/helpers.py load_sql_fixture_pairs (pinned commit)."""
    s = "\n".join([line for line in text.splitlines() if line and not line.startswith("--")])
    statements = s.split(";")
    out = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            sql = statements[i].strip()
            lines = sql.split("\n")
            meta = {}
            j = 0
            while lines[j].startswith("#"):
                key, val = lines[j].split(":", maxsplit=1)
                meta[key.lstrip("#").strip()] = val.strip()
                j += 1
            out.append((meta, "\n".join(lines[j:]), statements[i + 1].strip()))
    return out


def is_comment_or_blank(text):
    s = text.strip()
    return s == "" or s.startswith("#")


def in_ranges(line, ranges):
    for a, b in ranges:
        if a <= line <= b:
            return True
    return False


def insert_in_string(o, ranges):
    """An insertion before old line o lands inside a multi-line string."""
    for a, b in ranges:
        if a <= o - 1 and o <= b:
            return True
    return False


def edits(hunks):
    """Yield edit groups: (removed [(line, text)], added [text], insert_at)."""
    out = []
    for start, lines, _ in hunks:
        o = start if start > 0 else 1
        rem, add, at = [], [], None
        for tag, text in lines + [(" ", None)]:
            if tag == " ":
                if rem or add:
                    out.append((rem, add, at if at is not None else o))
                rem, add, at = [], [], None
                o += 1
            elif tag == "-":
                rem.append((o, text))
                o += 1
            else:
                if at is None:
                    at = o
                add.append(text)
    return out


class Selector:
    def __init__(self, maps):
        self.m = maps
        self.importcheck = {}

    # ---------- helpers
    def cov_lines(self, d, lines):
        out = set()
        imp = False
        for l in lines:
            sid = d["cov"].get(l)
            if sid is not None:
                out.update(d["sets"][sid])
            if l in d["imp"]:
                imp = True
        return out, imp

    def cov_range(self, d, a, b):
        return self.cov_lines(d, range(a, b + 1))

    def file_runtime(self, d):
        out = set()
        for sid in set(d["cov"].values()):
            out.update(d["sets"][sid])
        return out

    def module_of(self, rel):
        return rel[:-3].replace("/", ".")

    def class_users(self, rel, qual):
        key = self.module_of(rel) + ":" + qual
        sid = self.m.usage.get(key)
        return (set(self.m.gset(sid)) if sid is not None else None)

    def classes_in(self, d):
        return [s[6] for s in d["stmts"] if s[3] == "k"]

    def reader_tests(self, name, rel=None):
        """Tests that read `name` at run time, and whether it is also read at import
        time in a way that could see a class defined in module `rel` (lazily loaded
        modules are created after `import sqlglot`, so only later reads count)."""
        r = self.m.readers.get(name)
        if r is None:
            return None, True
        if rel is not None and len(r) > 2 and self.module_of(rel).removesuffix(".__init__") not in self.m.eager:
            return set(self.m.gset(r[0])), bool(r[2])
        return set(self.m.gset(r[0])), bool(r[1])

    def file_users(self, rel, d):
        out = self.file_runtime(d)
        for q in self.classes_in(d):
            u = self.class_users(rel, q)
            if u:
                out |= u
        return out

    def importers_tests(self, rel):
        """Tests that (transitively) import a module: conservative = all."""
        return set(self.m.all)

    # ---------- per file kinds
    def py_change(self, rel, ch):
        self.vok = False
        self.vsrc = None
        d = self.m.src(rel)
        if d is None:
            if rel.startswith("tests/") and os.path.basename(rel).startswith("test_"):
                return set(self.m.testfile.get(rel, [])), "newtestfile"
            return set(), "unmapped-py"
        removed, ins, added_t, removed_t = changed_lines(ch["hunks"])
        # pure comment/blank edits outside of strings change nothing
        groups = edits(ch["hunks"])
        cookie = any(re.match(r"^[ \t\f]*#.*?coding[:=]", t) for t in added_t + removed_t) and (
            min(removed + ins + [99]) <= 3)
        if not cookie and all(is_comment_or_blank(t) for t in added_t + removed_t) and not any(
            in_ranges(l, d["strs"]) for l in removed
        ) and not any(insert_in_string(o, d["strs"]) for o in ins):
            # a removed or added line ending a backslash continuation would matter
            if not any(t.rstrip().endswith("\\") for t in added_t + removed_t):
                self.vok = True
                return set(), "comment"
        # syntax check of the patched file
        orig_path = os.path.join(SRC, rel)
        if os.path.exists(orig_path):
            with open(orig_path, encoding="utf-8") as fh:
                orig = fh.readlines()
            new = apply_hunks(orig, ch["hunks"])
            if new is None:
                return set(self.m.all), "apply-failed"
            try:
                compile("".join(new), rel, "exec", dont_inherit=True)
            except SyntaxError:
                return self.syntax_broken(rel, d), "syntax"
            self.vsrc = "".join(new)
        out = set()
        why = []
        vok = self.vsrc is not None and rel.startswith("sqlglot/")
        stmts = d["stmts"]
        # lines that carry real (non-comment) edits
        targets = set()
        runtime_only = set()   # lines whose edit sits wholly inside a lambda body
        for rem, add, at in groups:
            real_rem = [(l, t) for l, t in rem if not is_comment_or_blank(t) or in_ranges(l, d["strs"])]
            real_add = [t for t in add if not is_comment_or_blank(t)] or (add if insert_in_string(at, d["strs"]) else [])
            if real_rem:
                for l, t in real_rem:
                    targets.add(l)
                if len(rem) == len(add):
                    for (l, t_old), t_new in zip(rem, add):
                        if self.inside_lambda(d, l, t_old, t_new):
                            runtime_only.add(l)
            elif real_add:
                # pure insertion before old line `at`
                targets.add(at)
                if at > 1:
                    targets.add(at - 1)
        for L in sorted(targets):
            # innermost statement containing L
            best = None
            for s in stmts:
                if s[0] <= L <= s[1] and (best is None or (s[1] - s[0]) < (best[1] - best[0])):
                    best = s
            if best is None:
                continue  # outside any statement (blank/comment at module level)
            start, end, hdr_end, kind, sc_a, sc_b, owner, tg = best
            if tg == ["<doc>"]:
                why.append("docstring")
                continue  # no collected test reads docstrings (pytest runs no doctests here)
            if L in runtime_only and kind in ("c", "m") and L not in d.get("implam", ()):
                tests, _ = self.cov_lines(d, [L])
                out |= tests
                why.append("lambda")
                continue
            in_header = L <= hdr_end
            if not (kind == "f" and in_header):
                vok = False
            if not in_header:
                if kind in ("d", "k"):
                    # comment / blank line inside a def/class body, between statements
                    if L in ins and L - 1 in targets:
                        pass
                    continue
                # inside compound body but not inside a child statement (else:, blank...)
                tests, imp = self.cov_range(d, start, end)
                out |= tests
                if imp:
                    out |= self.file_users(rel, d)
                why.append("compound")
                continue
            if kind == "f":
                tests, imp = self.cov_range(d, start, hdr_end)
                out |= tests
                if imp:
                    vok = False
                    out |= self.file_users(rel, d)
                    out |= self.subprocess_tests(rel)
                    if self.vsrc is not None:
                        self.importcheck[rel] = self.vsrc
                    why.append("f-imp")
                else:
                    why.append("f")
                if rel.startswith("sqlglot/") and self.is_docstring(d, best):
                    out |= set(self.m.testfile.get("tests/test_docs.py", []))
            elif kind == "d":
                tests, imp = self.cov_range(d, hdr_end + 1, sc_b)
                out |= tests
                if rel.startswith("tests/"):
                    out |= self.test_by_def(rel, owner, tg[0] if tg else None)
                why.append("def")
            elif kind == "k":
                why.append("class-hdr")
                if rel.startswith("tests/"):
                    out |= self.tests_module_wide(rel, d)
                    continue
                u = self.class_users(rel, owner)
                out |= u or set()
                t2, _ = self.cov_range(d, start, end)
                out |= t2
                rt, _ = self.reader_tests(owner.split(".")[-1])
                out |= rt or set()
            elif kind in ("c", "m"):
                if rel.startswith("tests/"):
                    out |= self.tests_module_wide(rel, d)
                    why.append("test-import-time")
                    continue
                out |= self.import_time_change(rel, d, best)
                out |= self.subprocess_tests(rel)
                if self.vsrc is not None:
                    self.importcheck[rel] = self.vsrc
                why.append("import-time:" + ",".join(tg[:3]))
        self.vok = vok
        return out, " ".join(sorted(set(why)))

    def subprocess_tests(self, rel=None):
        if rel not in LAZY_LOAD_FILES:
            return set()
        if not hasattr(self, "_spt"):
            idx = {i: n for n, i in enumerate(self.m.ids)}
            self._spt = {idx[i] for i in SUBPROCESS_IMPORT_TESTS if i in idx}
        return set(self._spt)

    def inside_lambda(self, d, line, old, new):
        if old == new:
            return False
        p = 0
        while p < min(len(old), len(new)) and old[p] == new[p]:
            p += 1
        q = 0
        while q < min(len(old), len(new)) - p and old[-1 - q] == new[-1 - q]:
            q += 1
        a, b = p, len(old) - q   # changed region in old coordinates [a, b)
        for l1, c1, l2, c2 in d.get("lams", []):
            lo = c1 if l1 == line else 0
            hi = c2 if l2 == line else 10 ** 9
            if l1 <= line <= l2 and lo <= a and b <= hi and (b > a or lo < a < hi):
                return True
        return False

    def is_docstring(self, d, s):
        return False

    def test_by_def(self, rel, owner, name):
        out = set()
        if owner and name:
            key = f"{rel}::{owner.split('.')[0]}"
            for t in self.m.testclass.get(key, []):
                if self.m.ids[t].endswith("::" + name):
                    out.add(t)
        return out

    def tests_module_wide(self, rel, d):
        base = os.path.basename(rel)
        if base.startswith("test_"):
            return set(self.m.testfile.get(rel, []))
        # helper module under tests/: every test that runs any of its code + importers
        out = self.file_runtime(d)
        mod = self.module_of(rel)
        short = mod.split(".")[-1]
        for f, ts in self.m.testfile.items():
            p = os.path.join(SRC, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    txt = fh.read()
            except OSError:
                out |= set(ts)
                continue
            if mod in txt or ("import " + short) in txt or (short + " import") in txt:
                out |= set(ts)
        return out

    def import_time_change(self, rel, d, s):
        start, end, hdr_end, kind, sc_a, sc_b, owner, tg = s
        if kind == "c":
            users = self.class_users(rel, owner)
            meth, _ = self.cov_range(d, sc_a, sc_b)
            base = (users or set()) | meth
            if tg:
                rd = set()
                safe = True
                for n in tg:
                    r, unsafe = self.reader_tests(n, rel)
                    if r is None or unsafe:
                        safe = False
                        break
                    rd |= r
                if safe:
                    if users is not None:
                        return base & rd
                    return rd
            if users is None:
                return base | self.file_users(rel, d)
            return base
        # module level
        if tg:
            rd = set()
            safe = True
            for n in tg:
                r, unsafe = self.reader_tests(n)
                if r is None or unsafe:
                    safe = False
                    break
                rd |= r
            if safe:
                return rd
        return self.file_users(rel, d)

    def syntax_broken(self, rel, d):
        return set(self.m.all)

    def fixture_change(self, rel, ch=None):
        if ch is not None and rel in TPC_PAIR_FILES and ch.get("hunks") and not ch.get("binary"):
            r = self.tpc_pairs_change(rel, ch)
            if r is not None:
                return r
        t = self.m.fixtures.get(rel)
        if t is not None:
            return set(t), "fixture"
        # unknown fixture: tests in files mentioning its name
        stem = os.path.basename(rel).split(".")[0]
        out = set()
        for f, ts in self.m.testfile.items():
            try:
                with open(os.path.join(SRC, f), encoding="utf-8") as fh:
                    if stem in fh.read():
                        out |= set(ts)
            except OSError:
                return set(self.m.all), "fixture-unknown-all"
        return out, "fixture-unknown"

    def tpc_pairs_change(self, rel, ch):
        """TPC query files are loaded in TestExecutor.setUpClass, so every executor test
        "reads" them; only three use them. Parse the (meta, sql, expected) pairs before
        and after the patch exactly as tests/helpers.load_sql_fixture_pairs does, and
        select the tests that use the parts that changed."""
        path = os.path.join(SRC, rel)
        try:
            with open(path, encoding="utf-8") as fh:
                orig = fh.readlines()
        except OSError:
            return None
        new = apply_hunks(orig, ch["hunks"])
        if new is None:
            return None
        try:
            a = fixture_pairs("".join(orig))
            b = fixture_pairs("".join(new))
        except Exception:
            return None   # parse failure would break setUpClass: keep every reader
        if len(a) != len(b):
            return None
        meta_sql = expected = False
        for (m1, s1, e1), (m2, s2, e2) in zip(a, b):
            if m1 != m2 or s1 != s2:
                meta_sql = True
            if s1 != s2 or e1 != e2:
                expected = True
        if not (meta_sql or expected):
            return set(), "tpc-noop"
        spec = TPC_PAIR_FILES[rel]
        changed_sql = [i for i, (x, y) in enumerate(zip(a, b)) if x[0] != y[0] or x[1] != y[1]]
        if spec.get("probe") and changed_sql:
            self.tpc_probe_candidate = (spec["probe"], [list(p) for p in b], changed_sql)
        idx = {i: n for n, i in enumerate(self.m.ids)}
        want = list(spec["any"])
        if meta_sql:
            want += spec["meta_sql"]
        if expected:
            want += spec["sql_expected"]
        return {idx[i] for i in want if i in idx}, "tpc-pairs" + (":sql" if meta_sql else "") + (":exp" if expected else "")

    def select(self, text):
        out = set()
        notes = []
        self.verify = {"ok": True, "src": {}, "fresh_ok": True, "allsrc": {}}
        self.importcheck = {}
        self.tpc_probe_candidate = None
        self.tpc_probe = None
        n_files = 0
        for ch in parse_patch(text):
            n_files += 1
            old, new = ch["old"], ch["new"]
            rel = old or new
            if ch.get("binary") and rel.endswith(".py"):
                out |= set(self.m.all)
                notes.append("binary-py-all")
                continue
            if not ch["hunks"] and old == new and not ch.get("binary"):
                notes.append("mode-only:" + rel)
                continue
            if old is None or new is None or old != new:
                # added / deleted / renamed files
                if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
                    if old is None and new and new.startswith("sqlglot/"):
                        notes.append("new-src")
                        continue
                    if old is None and new and new.startswith("tests/") and not os.path.basename(new).startswith("test_"):
                        notes.append("new-test-helper")
                        continue
                    out |= set(self.m.all)
                    notes.append("add/del/rename-all")
                    continue
                if rel.startswith("tests/fixtures/"):
                    t, why = self.fixture_change(old or new)
                    out |= t
                    notes.append(why)
                    continue
            if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
                t, why = self.py_change(rel, ch)
                out |= t
                notes.append(f"{rel}:{why}")
                if not self.vok:
                    self.verify["ok"] = False
                elif self.vsrc is not None and t:
                    self.verify["src"][rel] = self.vsrc
                if why != "comment":
                    if self.vsrc is None or not rel.startswith("sqlglot/") or why in ("syntax", "apply-failed"):
                        self.verify["fresh_ok"] = False
                    else:
                        self.verify["allsrc"][rel] = self.vsrc
                continue
            elif rel.startswith("tests/fixtures/"):
                t, why = self.fixture_change(rel, ch)
                out |= t
                notes.append(why)
            elif os.path.basename(rel) in ROOT_CONFIG and "/" not in rel:
                out |= set(self.m.all)
                notes.append("rootcfg-all")
            elif rel.startswith("tests/"):
                # non-python data under tests/: tests reading it, else files mentioning it
                t, why = self.fixture_change(rel)
                out |= t
                notes.append("tests-data:" + why)
            else:
                notes.append("ignored:" + rel)
        if n_files == 1 and self.tpc_probe_candidate and len(self.tpc_probe_candidate[2]) <= TPC_PROBE_MAX:
            self.tpc_probe = self.tpc_probe_candidate
        for n in notes:
            head = n.split(":")[0]
            if not (head.endswith(".py") or n.startswith("mode-only") or n.startswith("ignored:")):
                self.verify["ok"] = False
                self.verify["fresh_ok"] = False
        return out, notes


def pack(items):
    """items: list of (number, ms). Return workers minimizing max load + 2 s per worker."""
    if not items:
        return []
    items = sorted(items, key=lambda x: (-x[1], x[0]))
    best = None
    for k in range(1, 9):
        if k > len(items):
            break
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n, ms in items:
            i = loads.index(min(loads))
            loads[i] += ms
            ws[i].append(n)
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best[1]


def commit_category(notes):
    """Coarse kind of a commit, from the selector's own notes."""
    text = " ".join(notes)
    py = [n for n in notes if n.split(":")[0].endswith(".py") and not n.endswith(":comment")]
    has_fix = any(("fixture" in n or "tpc-" in n) and not n.split(":")[0].endswith(".py") for n in notes)
    if any(n.split(":")[0].startswith("tests/") and n.split(":")[0].endswith(".py") for n in py):
        return "testfile"
    if not py and has_fix:
        return "fixture"
    if not py and not has_fix:
        return "comment"
    if py and has_fix:
        return "mixed"
    if "import-time" in text or "class-hdr" in text or "f-imp" in text:
        return "import"
    return "code"


VERIFY = os.environ.get("BR_VERIFY", "1") == "1"
HEAVY_CLASSES = {"tests/test_executor.py::TestExecutor"}
# tests that start a fresh interpreter (they would see the unpatched files)
NO_VERIFY = {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}
SUBPROCESS_IMPORT_TESTS = NO_VERIFY
V_MAX_MS = int(os.environ.get("BR_V_MAX_MS", "2000"))      # total recorded ms of a verified set
V_MAX_TEST_MS = int(os.environ.get("BR_V_MAX_TEST_MS", "1500"))
V_MAX_TESTS = int(os.environ.get("BR_V_MAX_TESTS", "120"))
V_BUDGET_S = float(os.environ.get("BR_V_BUDGET_S", "90"))
V_FRESH_MAX_MS = int(os.environ.get("BR_V_FRESH_MAX_MS", "1500"))
# Verification stops at this many seconds after start, whatever the commit count
# (practice allows 180 s for the batch, the final 270 s).
V_DEADLINE_S = float(os.environ.get("BR_V_DEADLINE_S", "110"))
HEAVY_SKIP = os.environ.get("BR_HEAVY_SKIP", "1") == "1"
# End-to-end executor runs over TPC queries: they exercise the same optimizer and
# executor code as many cheap tests, so a break they would catch also breaks cheap
# candidates (measured on mutants of the pinned commit). Property tests such as the
# memory-leak and AST-node-limit tests are never skipped.
GAMBLE_TESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
}
HS_MAX_MS = int(os.environ.get("BR_HS_MAX_MS", "2500"))
HS_MAX_TEST_MS = int(os.environ.get("BR_HS_MAX_TEST_MS", "2600"))
HS_MIN_RUN = int(os.environ.get("BR_HS_MIN_RUN", "5"))


def main():
    t0 = time.time()
    # Only answers may reach stdout: keep a private handle on it and send fd 1
    # (and anything a test or child process prints) to stderr.
    sys.stdout.flush()
    real_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    maps = Maps()
    with open(os.path.join(DATA, "tests.json")) as fh:
        tj = json.load(fh)
    num_of = {t["id"]: t["number"] for t in tj["tests"]}
    ms_of = {t["number"]: round(t["seconds"] * 1000) for t in tj["tests"]}
    known = set(maps.ids)
    unknown = [t["number"] for t in tj["tests"] if t["id"] not in known]
    if unknown:
        log("tests not in map:", len(unknown))
    sel = Selector(maps)
    cdir = os.path.join(DATA, "commits")
    results = []   # [cid, nums, notes]
    jobs = []
    fresh = []
    ichecks = []
    probes = []    # TPC-DS query edits: run the edited queries the way test_execute_tpcds does
    special = {}   # key -> tests to keep when every cheap candidate passed (heavy skip)
    names = sorted(n for n in os.listdir(cdir) if n.endswith(".patch"))
    for name in names:
        cid = name[:-6]
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            tset, notes = sel.select(text)
            nums = {num_of[maps.ids[t]] for t in tset if maps.ids[t] in num_of}
            if tset == maps.all:
                nums |= set(unknown)
            v = sel.verify
            fresh_job = False
            if VERIFY and tset and tset != maps.all and len(tset) <= V_MAX_TESTS:
                tl = sorted(tset, key=lambda t: (ms_of.get(num_of.get(maps.ids[t], -1), 10 ** 6), t))
                tot = sum(ms_of.get(num_of.get(maps.ids[t], -1), 10 ** 6) for t in tl)
                heavy = any("::".join(maps.ids[t].split("::")[:2]) in HEAVY_CLASSES or maps.ids[t] in NO_VERIFY for t in tl)
                big = any(ms_of.get(num_of.get(maps.ids[t], -1), 10 ** 6) > V_MAX_TEST_MS for t in tl)
                swap_ok = v["ok"] and v["src"] and not sel.importcheck
                fresh_ok = v["fresh_ok"] and v["allsrc"]
                msf = lambda t: ms_of.get(num_of.get(maps.ids[t], -1), 10 ** 6)
                monsters = [t for t in tl if maps.ids[t] in GAMBLE_TESTS]
                if not heavy and not big and tot <= V_MAX_MS:
                    if swap_ok:
                        jobs.append((len(results), v["src"], tl, 4.0 + 4.0 * tot / 1000.0))
                    elif fresh_ok and tot <= V_FRESH_MAX_MS:
                        fresh.append((len(results), dict(v["allsrc"]), tl, 6.0 + 4.0 * tot / 1000.0))
                        fresh_job = True
                elif HEAVY_SKIP and monsters and (swap_ok or fresh_ok):
                    # Run every cheap candidate here. Any failure: keep the full set.
                    # All pass: the monster tests are dropped (they cover the same code).
                    # kill detectors: the cheapest candidates, up to HS_MAX_MS of recorded time
                    run, runtot = [], 0
                    exe = "tests/test_executor.py::TestExecutor"
                    # cheapest first, non-executor tests before executor ones (whose class
                    # setup loads the TPC data)
                    for t in sorted(tl, key=lambda t: ("::".join(maps.ids[t].split("::")[:2]) == exe, msf(t), t)):
                        if t in monsters or maps.ids[t] in NO_VERIFY or msf(t) > HS_MAX_TEST_MS:
                            continue
                        if runtot + msf(t) > HS_MAX_MS:
                            break
                        run.append(t)
                        runtot += msf(t)
                    ship = [t for t in tl if t not in monsters and t not in run]
                    if len(run) >= HS_MIN_RUN and runtot <= HS_MAX_MS:
                        keep_ship = {num_of[maps.ids[t]] for t in ship if maps.ids[t] in num_of}
                        special[len(results)] = keep_ship
                        if swap_ok:
                            jobs.append((len(results), v["src"], run, 6.0 + 3.0 * runtot / 1000.0))
                        else:
                            fresh.append((len(results), dict(v["allsrc"]), run, 8.0 + 3.0 * runtot / 1000.0))
                            fresh_job = True
            if VERIFY and sel.tpc_probe:
                probes.append((len(results), sel.tpc_probe))
            if sel.importcheck and not fresh_job:
                ichecks.append((len(results), dict(sel.importcheck)))
        except Exception as e:  # never crash: run everything for this commit
            log("error", cid, repr(e))
            nums = set(ms_of)
            notes = ["error-all"]
        results.append([cid, nums, notes])
    if ichecks:
        try:
            import verify
            for key, files in ichecks:
                ok = verify.import_check(SRC, files, log)
                if not ok:
                    results[key][1] = set(ms_of)
                    results[key][2] = results[key][2] + ["import-broken-all"]
        except Exception as e:
            log("importcheck error", repr(e))
            for key, files in ichecks:
                results[key][1] = set(ms_of)
    if probes:
        try:
            import verify
            for key, (test_id, pairs, changed) in probes:
                ok = verify.tpcds_probe(SRC, pairs, changed, t0 + V_DEADLINE_S * 0.5, log)
                if ok and test_id in num_of:
                    results[key][1] = set(results[key][1]) - {num_of[test_id]}
                    results[key][2] = results[key][2] + ["tpcds-probe-pass"]
                else:
                    results[key][2] = results[key][2] + ["tpcds-probe-kept"]
        except Exception as e:
            log("probe error", repr(e))
    if fresh:
        try:
            import verify
            fresh.sort(key=lambda j: (j[0] not in special, j[0]))
            done = verify.run_fresh(fresh, maps.ids, SRC, t0 + V_DEADLINE_S * 0.4, log)
            for key, files, _, _ in fresh:
                res = done.get(key)
                if res is None:
                    # not verified: still make sure the patched modules import
                    if not verify.import_check(SRC, files, log):
                        results[key][1] = set(ms_of)
                        results[key][2] = results[key][2] + ["import-broken-all"]
                elif res[0] == "broken":
                    results[key][1] = set(ms_of)
                    results[key][2] = results[key][2] + ["import-broken-all"]
                else:
                    failed, unrun = res
                    if key in special:
                        if failed or unrun:
                            results[key][2] = results[key][2] + ["heavy-kept"]
                            continue
                        keep = special[key]
                    else:
                        keep = {num_of[maps.ids[t]] for t in failed + unrun if maps.ids[t] in num_of}
                    results[key][2] = results[key][2] + [f"fresh-verified:{len(results[key][1])}->{len(keep)}"]
                    results[key][1] = keep
        except Exception as e:
            log("verify-fresh error", repr(e))
            for key, _, _, _ in fresh:
                results[key][1] = set(ms_of)
    if jobs:
        try:
            import verify
            jobs.sort(key=lambda j: (j[0] not in special, j[0]))
            done = verify.run(jobs, maps.ids, SRC, t0 + V_DEADLINE_S, log)
            for key, (failed, unrun) in done.items():
                if key in special:
                    if failed or unrun:
                        results[key][2] = results[key][2] + ["heavy-kept"]
                        continue
                    keep = special[key]
                else:
                    keep = {num_of[maps.ids[t]] for t in failed + unrun if maps.ids[t] in num_of}
                results[key][2] = results[key][2] + [f"verified:{len(results[key][1])}->{len(keep)}"]
                results[key][1] = keep
        except Exception as e:
            log("verify error", repr(e))
    # Ablation probe (practice board only): drop selections for one category of commit.
    probe = os.environ.get("BR_ABLATE") or (open(os.path.join(HERE, "ABLATE")).read().strip()
                                              if os.path.exists(os.path.join(HERE, "ABLATE")) else "")
    if probe:
        for r in results:
            if commit_category(r[2]) == probe:
                r[1] = set()
    # Measurement probe (practice board only): drop two slow tests everywhere. The board's
    # simulated seconds and miss count then show how often they are selected and how often they fail.
    drop_ids = {"tests/test_parser.py::TestParser::test_max_nodes",
                "tests/test_executor.py::TestExecutor::test_execute_tpcds"}
    drop_nums = {num_of[i] for i in drop_ids if i in num_of}
    for r in results:
        r[1] = set(r[1]) - drop_nums
    cats = {}
    for r in results:
        c = commit_category(r[2])
        cats[c] = cats.get(c, 0) + 1
    log("categories", cats)
    out_lines = []
    for cid, nums, notes in results:
        workers = pack([(n, ms_of[n]) for n in nums])
        log(cid, len(nums), " ".join(notes)[:300])
        out_lines.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    real_out.write("\n".join(out_lines) + ("\n" if out_lines else ""))
    real_out.flush()
    log("total wall %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
