"""Blast Radius selector.

For each patch, pick the tests that can observe the change, using maps built
from the pinned sqlglot commit (per-test line coverage, class usage, readers of
class/module attributes, fixture reads), then split them over workers.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAP = os.path.join(HERE, "map")
DATA = os.environ.get("BR_DATA", "/data")
SRC = os.environ.get("BR_SRC", os.path.join(HERE, "src"))  # pinned sqlglot sources (optional)

ROOT_CONFIG = {"pyproject.toml", "setup.cfg", "setup.py", "conftest.py", "tox.ini", "pytest.ini", "Makefile"}


def log(*a):
    print(*a, file=sys.stderr)


class Maps:
    def __init__(self):
        with open(os.path.join(MAP, "meta.json")) as fh:
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
            p = os.path.join(MAP, "src", rel.replace("/", "__") + ".json")
            if os.path.exists(p):
                with open(p) as fh:
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
    """Return list of file changes: {old, new, hunks: [(old_start, [(tag, text)])]}."""
    files = []
    cur = None
    hunk = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "meta": []}
            files.append(cur)
            hunk = None
            continue
        if line.startswith("--- ") and (hunk is None or not hunk[2]):
            if cur is None or cur["hunks"]:
                cur = {"old": None, "new": None, "hunks": [], "meta": []}
                files.append(cur)
            p = line[4:].split("\t")[0].strip()
            cur["old"] = None if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
            hunk = None
            continue
        if line.startswith("+++ ") and cur is not None and hunk is None:
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
        if hunk is not None and line[:1] in ("+", "-", " ") and (hunk[2][0] > 0 or hunk[2][1] > 0):
            tag = line[0]
            hunk[1].append((tag, line[1:]))
            if tag in (" ", "-"):
                hunk[2][0] -= 1
            if tag in (" ", "+"):
                hunk[2][1] -= 1
            continue
        if line.startswith("\\"):
            continue
        if cur is not None and hunk is None:
            cur["meta"].append(line)
            if line.startswith("rename from "):
                cur["old"] = line[len("rename from "):].strip()
            elif line.startswith("rename to "):
                cur["new"] = line[len("rename to "):].strip()
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
        d = self.m.src(rel)
        if d is None:
            if rel.startswith("tests/") and os.path.basename(rel).startswith("test_"):
                return set(self.m.testfile.get(rel, [])), "newtestfile"
            return set(), "unmapped-py"
        removed, ins, added_t, removed_t = changed_lines(ch["hunks"])
        # pure comment/blank edits outside of strings change nothing
        groups = edits(ch["hunks"])
        if all(is_comment_or_blank(t) for t in added_t + removed_t) and not any(
            in_ranges(l, d["strs"]) for l in removed
        ) and not any(insert_in_string(o, d["strs"]) for o in ins):
            # a removed or added line ending a backslash continuation would matter
            if not any(t.rstrip().endswith("\\") for t in added_t + removed_t):
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
        out = set()
        why = []
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
            if L in runtime_only and kind in ("c", "m"):
                tests, _ = self.cov_lines(d, [L])
                out |= tests
                why.append("lambda")
                continue
            in_header = L <= hdr_end
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
                    out |= self.file_users(rel, d)
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
                why.append("import-time:" + ",".join(tg[:3]))
        return out, " ".join(sorted(set(why)))

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
        for ch in parse_patch(text):
            old, new = ch["old"], ch["new"]
            rel = old or new
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


def main():
    maps = Maps()
    with open(os.path.join(DATA, "tests.json")) as fh:
        tj = json.load(fh)
    num_of = {t["id"]: t["number"] for t in tj["tests"]}
    ms_of = {t["number"]: round(t["seconds"] * 1000) for t in tj["tests"]}
    unknown = [t["number"] for t in tj["tests"] if t["id"] not in set(maps.ids)]
    if unknown:
        log("tests not in map:", len(unknown))
    sel = Selector(maps)
    cdir = os.path.join(DATA, "commits")
    out_lines = []
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[:-6]
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            tset, notes = sel.select(text)
            nums = {num_of[maps.ids[t]] for t in tset if maps.ids[t] in num_of}
            if tset == maps.all:
                nums |= set(unknown)
        except Exception as e:  # never crash: run everything for this commit
            log("error", cid, repr(e))
            nums = set(ms_of)
            notes = ["error-all"]
        workers = pack([(n, ms_of[n]) for n in nums])
        log(cid, len(nums), " ".join(notes)[:300])
        out_lines.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out_lines) + ("\n" if out_lines else ""))


if __name__ == "__main__":
    main()
