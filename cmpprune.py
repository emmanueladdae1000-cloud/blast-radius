"""Runtime patch analysis over edit-independent facts from the pinned baseline."""
from functools import lru_cache
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
            out.append((name, tuple(('<ast>',) if isinstance(v, ast.AST) else (type(v).__name__, repr(v)) for v in val)))
            continue
        out.append((name, type(val).__name__, repr(val)))
    return tuple(out)


@lru_cache(maxsize=24)
def _parse_source(source):
    return ast.parse(source)


def compare_only_changes(old_src, new_src):
    """[(line, col, old_op, new_op)] if the sources differ only in single-operator comparisons
    ([] when the trees are identical), else None."""
    try:
        a = _parse_source(old_src)
        b = _parse_source(new_src)
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


def _literal_operand(node):
    """Encode an exact scalar literal, without evaluating source expressions."""
    if isinstance(node, ast.Constant):
        value = node.value
    elif (isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub)
          and isinstance(node.operand, ast.Constant)
          and type(node.operand.value) in (int, float)):
        value = node.operand.value
        if isinstance(node.op, ast.USub):
            value = -value
    else:
        return None
    if value is None: return ['none']
    if type(value) is bool: return ['bool', value]
    if type(value) is int:
        return ['int', str(value)] if value.bit_length() <= 256 else None
    if type(value) is float: return ['float', value.hex()]
    if type(value) is str:
        return ['str', value] if len(value) <= 256 else None
    if type(value) is bytes:
        return ['bytes', value.hex()] if len(value) <= 128 else None
    return None


def compare_operand_changes(old_src, new_src):
    """Recognize only operator/scalar-literal edits to single comparisons.

    Each result is (old line, old column, old operator, new operator,
    left replacement, right replacement). A replacement is (old encoding, new
    encoding), or None for an unchanged operand. All other AST fields, including
    their child positions, must match; nested comparisons are checked separately.
    The operator-only compare_only_changes API deliberately remains unchanged.
    """
    try:
        old_tree, new_tree = _parse_source(old_src), _parse_source(new_src)
    except SyntaxError:
        return None
    changes = []

    def same_literal(old, new):
        before, after = _literal_operand(old), _literal_operand(new)
        if before is None or after is None:
            return None
        if ast.dump(old) == ast.dump(new):
            return ()
        return (before, after)

    def match(old, new):
        if type(old) is not type(new):
            return False
        if isinstance(old, ast.Compare) and len(old.ops) == len(new.ops) == 1:
            before, after = type(old.ops[0]), type(new.ops[0])
            if before in _CMP_OPS and after in _CMP_OPS:
                replacements = []
                for a, b in ((old.left, new.left),
                             (old.comparators[0], new.comparators[0])):
                    literal = same_literal(a, b)
                    if literal is None:
                        if not match(a, b):
                            return False
                        replacements.append(None)
                    else:
                        replacements.append(literal or None)
                if before is not after or any(replacements):
                    if any(replacements) and (before in (ast.Is, ast.IsNot)
                                              or after in (ast.Is, ast.IsNot)):
                        return False
                    changes.append((old.lineno, old.col_offset,
                                    _CMP_OPS[before], _CMP_OPS[after],
                                    *replacements))
                return True
        if isinstance(old, ast.AST):
            return all(match(a, b) for (_, a), (_, b) in
                       zip(ast.iter_fields(old), ast.iter_fields(new)))
        if isinstance(old, list):
            return len(old) == len(new) and all(match(a, b) for a, b in zip(old, new))
        return old == new

    return changes if match(old_tree, new_tree) else None


def _primitive_value(encoded):
    kind = encoded[0]
    if kind.endswith('_subclass'):
        encoded = [kind.removesuffix('_subclass'), encoded[3]]
        kind = encoded[0]
    if kind == 'none': return None
    if kind == 'bool': return encoded[1]
    if kind == 'int': return int(encoded[1])
    if kind == 'float': return float.fromhex(encoded[1])
    if kind == 'str': return encoded[1]
    if kind == 'bytes': return bytes.fromhex(encoded[1])
    raise ValueError('Unsupported operand')


def _observed_compare(op, left, right, identical):
    if op == 'is': return identical
    if op == 'is not': return not identical
    identity_left = left[0] in ('none', 'identity_object')
    identity_right = right[0] in ('none', 'identity_object')
    if identity_left and identity_right and op in ('==', '!='):
        return identical if op == '==' else not identical
    if left[0] == 'identity_object' or right[0] == 'identity_object':
        raise ValueError('Overloaded or unknown comparison')
    import operator
    functions = {'==': operator.eq, '!=': operator.ne, '<': operator.lt,
                 '<=': operator.le, '>': operator.gt, '>=': operator.ge}
    result = functions[op](_primitive_value(left), _primitive_value(right))
    if type(result) is not bool:
        raise ValueError('Non-Boolean comparison')
    return result


def safe_tests(patch_text, src_root, *, _source=None):
    """Derive unchanged comparison behavior at runtime from raw baseline observations.

    The file contains no alternate-operator outcomes or edit-to-answer tables.
    Missing, incomplete, or unsupported baseline observations keep the test.
    """
    trace = _load('operand_trace.json.gz')
    imports = _load('import_lines.json.gz')
    if not trace or trace.get('format') != 'raw-baseline-comparisons-v1' or imports is None:
        return set()
    if _source is None:
        files = parse_unified_diff(patch_text)
    else:
        rel, original, patched = _source
        files = {rel: {'is_new': False, 'is_deleted': False, 'is_binary': False,
                       'old_path': rel, 'new_path': rel, 'hunks': [True]}}
    if not files:
        return set()
    safe = None
    changed = False
    for rel, info in files.items():
        if (not rel.startswith('sqlglot/') or not rel.endswith('.py') or
                info['is_new'] or info['is_deleted'] or info['is_binary'] or
                info['old_path'] != info['new_path'] or not info['hunks']):
            return set()
        try:
            if _source is None:
                with open(os.path.join(src_root, rel), encoding='utf8') as f:
                    original = f.read()
                patched = apply_hunks(original, info['hunks'])
            if patched is None:
                return set()
            changes = compare_operand_changes(original, patched)
        except (OSError, ValueError, SyntaxError):
            return set()
        if changes is None:
            return set()
        for line, col, before, after, replace_left, replace_right in changes:
            changed = True
            if line in imports.get(rel, ()):
                return set()
            site = trace['sites'].get(f'{rel}|{line}|{col}|{before}')
            if not site:
                return set()
            facts = site['facts']
            compatible = set()
            evaluations = {}
            for tid, indices in site['tests'].items():
                if not indices:
                    continue
                unchanged = True
                for index in indices:
                    if index not in evaluations:
                        try:
                            left, right, identical, observed = facts[index]
                            old = _observed_compare(before, left, right, identical)
                            new_left, new_right = left, right
                            if replace_left is not None:
                                if left != replace_left[0]:
                                    raise ValueError('Literal differs from baseline observation')
                                new_left = replace_left[1]
                            if replace_right is not None:
                                if right != replace_right[0]:
                                    raise ValueError('Literal differs from baseline observation')
                                new_right = replace_right[1]
                            if replace_left is not None or replace_right is not None:
                                # Literal edits never use identity operators. Equality still
                                # needs fresh aliasing when a replacement introduces None.
                                identical = new_left[0] == new_right[0] == 'none'
                            new = _observed_compare(after, new_left, new_right, identical)
                            evaluations[index] = old is observed and new is observed
                        except (ValueError, TypeError, KeyError, IndexError, OverflowError):
                            evaluations[index] = False
                    if not evaluations[index]:
                        unchanged = False
                        break
                if unchanged:
                    number = int(tid)
                    if 0 <= number < len(trace['test_ids']):
                        compatible.add(trace['test_ids'][number])
            safe = compatible if safe is None else safe & compatible
            if not safe:
                return set()
    return safe if changed and safe else set()


def safe_source_tests(rel, original, patched, src_root):
    """Comparison equivalence for one original source file and its patched text."""
    if rel.startswith('sqlglot/typing/'):
        # A metadata test can inspect annotator code without invoking it.
        return set()
    return safe_tests('', src_root, _source=(rel, original, patched))

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
            if ast.dump(_parse_source(old_src)) == ast.dump(_parse_source(new_src)):
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
            if ast.dump(_parse_source(old_src)) == ast.dump(_parse_source(new_src)):
                continue
        except SyntaxError:
            return None
        out[rel] = new_src
    return out or None
