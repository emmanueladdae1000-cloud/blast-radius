from __future__ import annotations

import ast
import gzip
import json
import math
import re
import sys
from pathlib import Path

DATA = Path("/data")
SQLGLOT = Path("/sqlglot")
MAP_PATH = Path("/app/coverage_map.json.gz")

WORKER_COST_MS = 2000
MAX_WORKERS = 8


def duration_ms(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def pack_workers(test_nums: set[int], dur_by_num: dict[int, int]) -> list[list[int]]:
    """Find worker partition (1..8 workers) minimizing max(load) + 2000 * k."""
    if not test_nums:
        return []
    items = sorted(test_nums, key=lambda n: dur_by_num[n], reverse=True)
    durs = [dur_by_num[n] for n in items]
    total_ms = sum(durs)
    max_item = durs[0]

    # Single worker is always optimal if total load <= max(max_item, 2000) + 2000
    best_cost = total_ms + WORKER_COST_MS
    best_workers: list[list[int]] = [items]

    max_k = min(MAX_WORKERS, len(items))
    for k in range(2, max_k + 1):
        # Lower bound for k workers
        lb = max(max_item, (total_ms + k - 1) // k) + WORKER_COST_MS * k
        if lb >= best_cost:
            continue

        # LPT heuristic
        workers: list[list[int]] = [[] for _ in range(k)]
        loads = [0] * k
        for n, d in zip(items, durs):
            idx = loads.index(min(loads))
            workers[idx].append(n)
            loads[idx] += d

        # Local improvement: move single items from busiest worker if it reduces makespan
        improved = True
        while improved:
            improved = False
            max_load = max(loads)
            hi = loads.index(max_load)
            for pos, n in enumerate(workers[hi]):
                d = dur_by_num[n]
                lo = loads.index(min(loads))
                if lo != hi and loads[lo] + d < max_load:
                    workers[hi].pop(pos)
                    loads[hi] -= d
                    workers[lo].append(n)
                    loads[lo] += d
                    improved = True
                    break

        occupied = [w for w in workers if w]
        cost = max(sum(dur_by_num[n] for n in w) for w in occupied) + WORKER_COST_MS * len(occupied)
        if cost < best_cost:
            best_cost = cost
            best_workers = occupied

    return best_workers


def parse_unified_diff(patch_text: str) -> dict[str, dict]:
    """Parse a git unified diff into per-file hunks and touched old lines."""
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
                elif hl.startswith("+"):
                    hunk_lines.append(hl)
                    info["old_lines"].add(max(1, old_ln - 1))
                    info["old_lines"].add(old_ln)
                elif hl.startswith(" "):
                    hunk_lines.append(hl)
                    old_ln += 1
                else:
                    # Context or empty line
                    hunk_lines.append(" " + hl)
                    old_ln += 1
                i += 1
            info["hunks"].append((old_start, old_count, new_start, new_count, hunk_lines))
        else:
            i += 1

    return files


def apply_hunks(old_text: str, hunks: list) -> str | None:
    """Apply parsed unified diff hunks to old_text. Returns None on mismatch."""
    old_lines = old_text.splitlines(keepends=True)
    out: list[str] = []
    pos = 0  # 0-based index into old_lines

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


def is_harmless_py_change(old_text: str, new_text: str, touched_lines: set[int]) -> tuple[bool, bool]:
    """Return (is_ast_identical_harmless, has_syntax_error)."""
    # Guard lines 1-2 for encoding cookies (# coding: ...)
    old_head = old_text.splitlines()[:2]
    new_head = new_text.splitlines()[:2]
    coding_re = re.compile(r"coding[:=]\s*[-\w.]+")
    if any(coding_re.search(l) for l in old_head) != any(coding_re.search(l) for l in new_head):
        return False, False
    if any(coding_re.search(l) for l in new_head) and old_head != new_head:
        return False, False

    try:
        old_tree = ast.parse(old_text)
    except SyntaxError:
        return False, False

    try:
        new_tree = ast.parse(new_text)
    except SyntaxError:
        return False, True

    # Strict AST equivalence ignoring line numbers/column offsets
    if ast.dump(old_tree, include_attributes=False) == ast.dump(new_tree, include_attributes=False):
        return True, False

    return False, False


class ASTScopeCache:
    def __init__(self) -> None:
        self._cache: dict[str, list[tuple[int, int, str]] | None] = {}

    def get_funcs(self, rel_path: str) -> list[tuple[int, int, str]] | None:
        if rel_path in self._cache:
            return self._cache[rel_path]
        p = SQLGLOT / rel_path
        if not p.exists() or not rel_path.endswith(".py"):
            self._cache[rel_path] = None
            return None
        try:
            tree = ast.parse(p.read_text("utf-8", errors="ignore"))
        except SyntaxError:
            self._cache[rel_path] = None
            return None
        funcs = [
            (n.lineno, n.end_lineno or n.lineno, n.name)
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self._cache[rel_path] = funcs
        return funcs


def main() -> None:
    tests_json = json.loads((DATA / "tests.json").read_text("utf-8"))
    tests = tests_json["tests"]
    all_test_nums = {t["number"] for t in tests}
    dur_by_num = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    tests_by_file: dict[str, set[int]] = {}
    for t in tests:
        tests_by_file.setdefault(t["file"], set()).add(t["number"])

    with gzip.open(MAP_PATH, "rt", encoding="utf-8") as gz:
        cov = json.load(gz)

    line_map: dict[str, dict[str, list[int]]] = cov["line_to_tests"]
    func_map: dict[str, dict[str, list[int]]] = cov["func_to_tests"]
    open_map: dict[str, list[int]] = cov["open_to_tests"]
    dial_map: dict[str, list[int]] = cov["dialect_to_tests"]

    # Precompute file -> all tests that executed any line in that file
    file_all_tests: dict[str, set[int]] = {}
    for f, lmap in line_map.items():
        s: set[int] = set()
        for ts in lmap.values():
            s.update(ts)
        file_all_tests[f] = s
    for f, fmap in func_map.items():
        s = file_all_tests.setdefault(f, set())
        for ts in fmap.values():
            s.update(ts)
    for f, ts in open_map.items():
        file_all_tests.setdefault(f, set()).update(ts)

    ast_cache = ASTScopeCache()

    # Core modules whose syntax/import error would break most or all tests
    core_files = {
        "sqlglot/__init__.py",
        "sqlglot/errors.py",
        "sqlglot/helper.py",
        "sqlglot/tokens.py",
        "sqlglot/parser.py",
        "sqlglot/generator.py",
        "sqlglot/expressions/__init__.py",
        "sqlglot/expressions/core.py",
        "sqlglot/dialects/__init__.py",
        "sqlglot/dialects/dialect.py",
    }

    for patch_path in sorted((DATA / "commits").glob("*.patch")):
        commit_id = patch_path.stem
        patch_text = patch_path.read_text("utf-8", errors="replace")
        touched = parse_unified_diff(patch_text)

        selected: set[int] = set()
        fallback_all = False

        for rel_path, info in touched.items():
            src_path = SQLGLOT / rel_path
            old_lines: set[int] = info["old_lines"]

            # Direct edits to a test file
            if rel_path.startswith("tests/") and rel_path.endswith(".py"):
                if rel_path in tests_by_file:
                    selected.update(tests_by_file[rel_path])
                else:
                    # Shared test helper like tests/helpers.py or tests/dialects/validator.py
                    f_tests = file_all_tests.get(rel_path)
                    if f_tests:
                        selected.update(f_tests)
                    else:
                        fallback_all = True
                continue

            # Non-Python files (fixtures, configs, etc.)
            if not rel_path.endswith(".py"):
                if rel_path in open_map:
                    selected.update(open_map[rel_path])
                elif rel_path.startswith("tests/fixtures/"):
                    # Fallback by fixture directory name
                    for op, ts in open_map.items():
                        if Path(op).parent == Path(rel_path).parent:
                            selected.update(ts)
                    if not selected:
                        fallback_all = True
                elif rel_path in ("pyproject.toml", "setup.py", "setup.cfg"):
                    selected.update(tests_by_file.get("tests/test_sqlglotc.py", set()))
                else:
                    # Unknown non-python file: conservative
                    pass
                continue

            # Python file: check if it's a deleted/new/binary file
            if info["is_deleted"] or info["is_new"] or info["is_binary"]:
                if rel_path in file_all_tests:
                    selected.update(file_all_tests[rel_path])
                else:
                    fallback_all = True
                continue

            # Try applying hunks to check for AST-identical no-op or syntax error
            if src_path.exists() and info["hunks"]:
                old_text = src_path.read_text("utf-8", errors="replace")
                new_text = apply_hunks(old_text, info["hunks"])
                if new_text is not None:
                    harmless, has_syntax_err = is_harmless_py_change(old_text, new_text, old_lines)
                    if has_syntax_err:
                        # Syntax error breaks every test that imports this file
                        if rel_path in core_files:
                            fallback_all = True
                            break
                        selected.update(file_all_tests.get(rel_path, set()))
                        parts = Path(rel_path).parts
                        stem = Path(rel_path).stem.lower()
                        if len(parts) >= 3 and parts[0] == "sqlglot" and parts[1] in ("dialects", "parsers", "generators", "typing"):
                            selected.update(dial_map.get(stem, ()))
                        if rel_path in open_map:
                            selected.update(open_map[rel_path])
                        if not selected:
                            fallback_all = True
                        continue
                    if harmless:
                        # Strictly identical Python AST (e.g., pure comment or whitespace edit).
                        # Still check if any test opens this .py file as raw text (like test_typing.py, which only does ast.parse, so AST-identical is safe too, unless something else opens it).
                        continue

            # Real Python semantic change
            # 1. Any test that opens this file
            if rel_path in open_map:
                selected.update(open_map[rel_path])

            # 2. Exact line coverage
            f_lines = line_map.get(rel_path, {})
            for ln in old_lines:
                selected.update(f_lines.get(str(ln), ()))

            # 3. Enclosing function coverage
            funcs = ast_cache.get_funcs(rel_path)
            f_funcs = func_map.get(rel_path, {})
            outside_func = False
            if funcs is not None:
                for ln in old_lines:
                    enclosing = [start for start, end, _ in funcs if start <= ln <= end]
                    if enclosing:
                        for start in enclosing:
                            selected.update(f_funcs.get(str(start), ()))
                    else:
                        outside_func = True
            else:
                outside_func = True

            # 4. Module/class-body edit (import-time attributes, registries, constants)
            if outside_func:
                parts = Path(rel_path).parts
                stem = Path(rel_path).stem.lower()
                if len(parts) >= 3 and parts[0] == "sqlglot" and parts[1] in ("dialects", "parsers", "generators", "typing"):
                    if stem in dial_map:
                        selected.update(dial_map[stem])
                    else:
                        selected.update(file_all_tests.get(rel_path, set()))
                elif rel_path in file_all_tests:
                    selected.update(file_all_tests[rel_path])
                elif rel_path.startswith("sqlglotc/"):
                    selected.update(tests_by_file.get("tests/test_sqlglotc.py", set()))
                else:
                    fallback_all = True

            # 5. If file is a sqlglot/ file that never appeared in coverage map (e.g. sqlglotc/setup.py)
            if rel_path.startswith("sqlglotc/"):
                selected.update(tests_by_file.get("tests/test_sqlglotc.py", set()))

        if fallback_all:
            selected = set(all_test_nums)

        workers = pack_workers(selected, dur_by_num)
        print(json.dumps({"commit": commit_id, "workers": workers}))


if __name__ == "__main__":
    main()
