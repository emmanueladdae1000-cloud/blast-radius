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

    def reader_tests(self, name):
        r = self.m.readers.get(name)
        if r is None:
            return None, True
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
                    r, unsafe = self.reader_tests(n)
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

    def fixture_change(self, rel):
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

    def select(self, text):
        out = set()
        notes = []
        self.verify = {"ok": True, "src": {}}
        self.importcheck = {}
        for ch in parse_patch(text):
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
                continue
            elif rel.startswith("tests/fixtures/"):
                t, why = self.fixture_change(rel)
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
        for n in notes:
            head = n.split(":")[0]
            if not (head.endswith(".py") or n.startswith("mode-only") or n.startswith("ignored:")):
                self.verify["ok"] = False
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


VERIFY = os.environ.get("BR_VERIFY", "1") == "1"
HEAVY_CLASSES = {"tests/test_executor.py::TestExecutor"}
# tests that start a fresh interpreter (they would see the unpatched files)
NO_VERIFY = {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}
SUBPROCESS_IMPORT_TESTS = NO_VERIFY
V_MAX_MS = int(os.environ.get("BR_V_MAX_MS", "4000"))      # total recorded ms of a verified set
V_MAX_TEST_MS = int(os.environ.get("BR_V_MAX_TEST_MS", "1500"))
V_MAX_TESTS = int(os.environ.get("BR_V_MAX_TESTS", "120"))
V_BUDGET_S = float(os.environ.get("BR_V_BUDGET_S", "90"))


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
    ichecks = []
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
            if sel.importcheck:
                ichecks.append((len(results), dict(sel.importcheck)))
            v = sel.verify
            if VERIFY and v["ok"] and v["src"] and tset and len(tset) <= V_MAX_TESTS:
                tl = sorted(tset, key=lambda t: (ms_of.get(num_of.get(maps.ids[t], -1), 10 ** 6), t))
                tot = sum(ms_of.get(num_of.get(maps.ids[t], -1), 10 ** 6) for t in tl)
                heavy = any("::".join(maps.ids[t].split("::")[:2]) in HEAVY_CLASSES or maps.ids[t] in NO_VERIFY for t in tl)
                big = any(ms_of.get(num_of.get(maps.ids[t], -1), 10 ** 6) > V_MAX_TEST_MS for t in tl)
                if not heavy and not big and tot <= V_MAX_MS:
                    jobs.append((len(results), v["src"], tl, 4.0 + 4.0 * tot / 1000.0))
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
    if jobs:
        try:
            import verify
            done = verify.run(jobs, maps.ids, SRC, V_BUDGET_S, log)
            for key, (failed, unrun) in done.items():
                keep = {num_of[maps.ids[t]] for t in failed + unrun if maps.ids[t] in num_of}
                results[key][2] = results[key][2] + [f"verified:{len(results[key][1])}->{len(keep)}"]
                results[key][1] = keep
        except Exception as e:
            log("verify error", repr(e))
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
