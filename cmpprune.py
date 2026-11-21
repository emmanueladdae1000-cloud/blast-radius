"""Comparison-operator swaps.

cmp_profile.json.gz maps "file|line|col|op|alt" -> tests (by id) that executed that comparison
under two hash seeds and for which the alternative operator never gave a different result.
When a patch only swaps comparison operators (comment-only files aside), such tests run exactly
as before and cannot fail.  Built from the pinned commit by running its own test suite.
"""
import ast
import gzip
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
_CMP_OPS = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
            ast.Is: "is", ast.IsNot: "is not"}
_cache = {}


def _load(name):
    if name not in _cache:
        try:
            with gzip.open(os.path.join(HERE, name), "rt", encoding="utf-8") as g:
                _cache[name] = json.load(g)
        except Exception:
            _cache[name] = None
    return _cache[name]


def parse_unified_diff(patch_text: str) -> dict[str, dict]:
    files: dict[str, dict] = {}
    lines = patch_text.splitlines(keepends=True)
    i = 0
    n = len(lines)
    cur_file = None

    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+?)\r?\n?$", line)
            if m:
                old_path, new_path = m.group(1), m.group(2)
                cur_file = new_path if new_path != "/dev/null" else old_path
                files[cur_file] = {
                    "old_path": old_path,
                    "new_path": new_path,
                    "hunks": [],
                    "old_lines": set(),
                    "is_new": False,
                    "is_deleted": False,
                    "is_binary": False,
                }
            i += 1
            continue

        if cur_file is None:
            i += 1
            continue

        info = files[cur_file]
        if line.startswith("new file mode"):
            info["is_new"] = True
            i += 1
        elif line.startswith("deleted file mode"):
            info["is_deleted"] = True
            i += 1
        elif line.startswith("Binary files "):
            info["is_binary"] = True
            i += 1
        elif line.startswith("--- "):
            if line.strip() == "--- /dev/null":
                info["is_new"] = True
            i += 1
        elif line.startswith("+++ "):
            if line.strip() == "+++ /dev/null":
                info["is_deleted"] = True
            i += 1
        elif line.startswith("@@ "):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                i += 1
                continue
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            i += 1
            hunk_lines = []
            old_ln = old_start
            saw_minus_in_block = False
            while i < n and not lines[i].startswith(("diff --git ", "@@ ")):
                hl = lines[i]
                if hl.startswith("\\ No newline"):
                    hunk_lines.append(hl)
                    i += 1
                    continue
                if hl.startswith("-"):
                    hunk_lines.append(hl)
                    info["old_lines"].add(old_ln)
                    old_ln += 1
                    saw_minus_in_block = True
                elif hl.startswith("+"):
                    hunk_lines.append(hl)
                    if not saw_minus_in_block:
                        info["old_lines"].add(max(1, old_ln - 1))
                        info["old_lines"].add(old_ln)
                elif hl.startswith(" "):
                    hunk_lines.append(hl)
                    old_ln += 1
                    saw_minus_in_block = False
                else:
                    hunk_lines.append(" " + hl)
                    old_ln += 1
                    saw_minus_in_block = False
                i += 1
            info["hunks"].append((old_start, old_count, new_start, new_count, hunk_lines))
        else:
            i += 1

    return files


def apply_hunks(old_text: str, hunks: list) -> str | None:
    old_lines = old_text.splitlines(keepends=True)
    out: list[str] = []
    pos = 0

    for old_start, old_count, _new_start, _new_count, hunk_lines in hunks:
        target = max(0, old_start - 1) if old_count > 0 else old_start
        if target < pos or target > len(old_lines):
            return None
        out.extend(old_lines[pos:target])
        pos = target

        for hl in hunk_lines:
            if hl.startswith("\\ No newline"):
                continue
            tag = hl[0]
            body = hl[1:]
            if tag == " ":
                if pos >= len(old_lines):
                    return None
                out.append(old_lines[pos])
                pos += 1
            elif tag == "-":
                if pos >= len(old_lines):
                    return None
                pos += 1
            elif tag == "+":
                out.append(body)

    out.extend(old_lines[pos:])
    return "".join(out)




def _prim_fields(node):
    out = []
    for name, val in ast.iter_fields(node):
        if isinstance(val, ast.AST):
            continue
        if isinstance(val, list):
            out.append((name, len(val), tuple(type(v).__name__ for v in val if not isinstance(v, ast.AST))))
            continue
        out.append((name, type(val).__name__, repr(val)))
    return tuple(out)


def compare_only_changes(old_src, new_src):
    """[(line, col, old_op, new_op)] if the sources differ only in single-operator comparisons
    ([] when the trees are identical), else None."""
    try:
        a = ast.parse(old_src)
        b = ast.parse(new_src)
    except SyntaxError:
        return None
    wa, wb = list(ast.walk(a)), list(ast.walk(b))
    if len(wa) != len(wb):
        return None
    changes = []
    for x, y in zip(wa, wb):
        if type(x) is not type(y):
            if isinstance(x, ast.cmpop) and isinstance(y, ast.cmpop):
                continue
            return None
        if isinstance(x, ast.Compare):
            if len(x.ops) != len(y.ops):
                return None
            if [type(o) for o in x.ops] != [type(o) for o in y.ops]:
                if len(x.ops) != 1 or type(x.ops[0]) not in _CMP_OPS or type(y.ops[0]) not in _CMP_OPS:
                    return None
                changes.append((x.lineno, x.col_offset, _CMP_OPS[type(x.ops[0])], _CMP_OPS[type(y.ops[0])]))
            continue
        if isinstance(x, ast.cmpop):
            continue
        if _prim_fields(x) != _prim_fields(y):
            return None
    return changes


def safe_tests(patch_text, src_root):
    """Test ids that provably cannot fail on this patch (empty set when nothing is provable)."""
    prof = _load("cmp_profile.json.gz")
    imp = _load("import_lines.json.gz")
    if not prof or imp is None:
        return set()
    files = parse_unified_diff(patch_text)
    if not files:
        return set()
    safe = None
    for rel, info in files.items():
        if not rel.endswith(".py") or not rel.startswith("sqlglot/"):
            return set()
        if info["is_new"] or info["is_deleted"] or info["is_binary"] or not info["hunks"]:
            return set()
        if info["old_path"] != info["new_path"]:
            return set()
        path = os.path.join(src_root, rel)
        if not os.path.exists(path):
            return set()
        with open(path, encoding="utf-8") as fh:
            old_src = fh.read()
        new_src = apply_hunks(old_src, info["hunks"])
        if new_src is None:
            return set()
        chg = compare_only_changes(old_src, new_src)
        if chg is None:
            return set()
        imp_lines = set(imp.get(rel, ()))
        for line, col, op_old, op_new in chg:
            if line in imp_lines:
                return set()
            s = set(prof["map"].get(f"{rel}|{line}|{col}|{op_old}|{op_new}", ()))
            safe = s if safe is None else safe & s
            if not safe:
                return set()
    if not safe:
        return set()
    ids = prof["ids"]
    return {ids[i] for i in safe}


def tpcds_queries(patch_text, src_root):
    """For a patch that only edits sqlglot/*.py: (patched sources {rel: text}, sorted query indices of
    test_execute_tpcds that execute a changed line).  None when the patch touches anything else, a
    changed line runs during the test's setup/imports, or the patch does not apply."""
    tl = _load("tpcds_lines.json.gz")
    if not tl:
        return None
    files = parse_unified_diff(patch_text)
    if not files:
        return None
    out_files = {}
    q = set()
    for rel, info in files.items():
        if rel.startswith("tests/fixtures/") and not rel.endswith(".py"):
            if rel.startswith("tests/fixtures/optimizer/tpc-ds/"):
                return None  # the TPC-DS test reads these: the fixture logic handles them
            continue  # test_execute_tpcds reads no other fixture
        if not rel.endswith(".py") or not rel.startswith("sqlglot/"):
            return None
        if info["is_new"] or info["is_deleted"] or info["is_binary"] or not info["hunks"]:
            return None
        if info["old_path"] != info["new_path"]:
            return None
        path = os.path.join(src_root, rel)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            old_src = fh.read()
        new_src = apply_hunks(old_src, info["hunks"])
        if new_src is None:
            return None
        try:
            if ast.dump(ast.parse(old_src)) == ast.dump(ast.parse(new_src)):
                continue  # comment-only file
        except SyntaxError:
            return None
        out_files[rel] = new_src
        pre = set(tl["prelude"].get(rel, ()))
        qm = tl["q"].get(rel, {})
        for ln in info["old_lines"]:
            if ln in pre:
                return None
            q.update(qm.get(str(ln), ()))
    if not out_files:
        return None
    return out_files, sorted(q)


def code_patch_files(patch_text, src_root, allow_fixtures=False):
    """{rel: patched source} for a patch that only edits existing sqlglot/*.py files with at least one
    non-comment change (plus, with allow_fixtures, non-Python files under tests/fixtures/, which are
    left out of the result); None otherwise."""
    files = parse_unified_diff(patch_text)
    if not files:
        return None
    out = {}
    for rel, info in files.items():
        if allow_fixtures and rel.startswith("tests/fixtures/") and not rel.endswith(".py"):
            continue
        if not rel.endswith(".py") or not rel.startswith("sqlglot/"):
            return None
        if info["is_new"] or info["is_deleted"] or info["is_binary"] or not info["hunks"]:
            return None
        if info["old_path"] != info["new_path"]:
            return None
        path = os.path.join(src_root, rel)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            old_src = fh.read()
        new_src = apply_hunks(old_src, info["hunks"])
        if new_src is None:
            return None
        try:
            if ast.dump(ast.parse(old_src)) == ast.dump(ast.parse(new_src)):
                continue
        except SyntaxError:
            return None
        out[rel] = new_src
    return out or None
