"""Narrow value-flow reasoning for literal tokenizer numeric-escape tables.

Only baseline line coverage is used. No alternate execution or patch outcomes are
stored. Dictionary keys, order, type and truthiness stay fixed, so the first
possible behavioral difference is a TokenizerCore numeric_escapes.get call.
"""
import ast
from functools import lru_cache
import importlib.metadata
import json
import os
from pathlib import Path

PINNED_COMMIT = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
AUDIT_FORMAT = 'baseline-numeric-value-flow-v1'
AUDIT_PATH = Path(__file__).with_name('numeric_flow.json')


def _table(node):
    if not isinstance(node, ast.Dict) or not node.keys:
        return None
    keys, values = [], []
    for key, value in zip(node.keys, node.values):
        if not isinstance(key, ast.Constant) or type(key.value) is not str:
            return None
        if not isinstance(value, ast.Tuple) or len(value.elts) != 4:
            return None
        try:
            numbers = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            return None
        if any(type(number) is not int or number.bit_length() > 256 for number in numbers):
            return None
        keys.append(key.value)
        values.append(numbers)
    if len(set(keys)) != len(keys):
        return None
    return keys, values


def _mapping_only(old_source, new_source):
    try:
        before, after = (ast.parse(source.encode('utf8')) for source in (old_source, new_source))
    except (SyntaxError, ValueError):
        return False
    changes = 0

    def target(node):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            name = node.target
        else:
            return False
        return isinstance(name, ast.Name) and name.id == 'NUMERIC_ESCAPES'

    def match(old, new, class_body=False):
        nonlocal changes
        if type(old) is not type(new):
            return False
        ignore_value = False
        if class_body and target(old) and target(new) and (old.value is None or new.value is None):
            if old.value is not new.value:
                return False
        elif class_body and target(old) and target(new) and ast.dump(old.value) != ast.dump(new.value):
            a, b = _table(old.value), _table(new.value)
            if a is None or b is None or a[0] != b[0]:
                return False
            changes += 1
            ignore_value = True
        if isinstance(old, ast.AST):
            return all((ignore_value and name == 'value') or
                       match(a, b, isinstance(old, ast.ClassDef) and name == 'body')
                       for (name, a), (_, b) in zip(ast.iter_fields(old), ast.iter_fields(new)))
        if isinstance(old, list):
            return len(old) == len(new) and all(match(a, b, class_body) for a, b in zip(old, new))
        return old == new

    return match(before, after) and changes == 1


def _shape_use(node, parents):
    """A membership/truthiness observation cannot see changed dictionary values."""
    parent = parents.get(node)
    if (isinstance(parent, ast.Compare) and len(parent.ops) == 1
            and type(parent.ops[0]) in (ast.In, ast.NotIn)
            and parent.comparators == [node]
            and isinstance(parent.left, ast.Constant) and type(parent.left.value) is str):
        return True
    while isinstance(parent, ast.BoolOp):
        node, parent = parent, parents.get(parent)
    return ((isinstance(parent, (ast.If, ast.While, ast.IfExp)) and parent.test is node)
            or (isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not))
            or (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                and parent.func.id == 'bool' and parent.args == [node] and not parent.keywords))


def _alias_is_shape_only(statement, parents):
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return False
    alias = statement.targets[0]
    if not isinstance(alias, ast.Name):
        return False
    scope = parents.get(statement)
    while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        scope = parents.get(scope)
    if scope is None:
        return False
    return all(_shape_use(node, parents) for node in ast.walk(scope)
               if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == alias.id)


@lru_cache(maxsize=1)
def _native_distribution_present():
    # sqlglotc deliberately has no importable module; find_spec cannot detect it.
    try:
        importlib.metadata.distribution('sqlglotc')
    except importlib.metadata.PackageNotFoundError:
        return False
    except Exception:
        return True
    return True


def _audit_consumer_lines(src_root):
    """Audit named value flows in the unmodified pinned Python source tree.

    The full pinned source/tests audit additionally found no reflective traversal
    of tokenizer/core state: generic reflection handles modules, generators and
    expressions. Native overlays are unsupported. Unknown named consumers fail
    closed, rather than silently discarding their baseline readers.
    """
    root = Path(src_root)
    package = root / 'sqlglot'
    core_rel = 'sqlglot/tokenizer_core.py'
    if _native_distribution_present() or not (root / core_rel).is_file():
        return None
    if any(path.suffix.lower() in ('.so', '.pyd', '.dll') for path in package.rglob('*')):
        return None
    lines = set()
    forwarded = stored = False
    try:
        for path in root.rglob('*.py'):
            source = path.read_bytes()
            if b'NUMERIC_ESCAPES' not in source and b'numeric_escapes' not in source:
                continue
            tree = ast.parse(source)
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    if node.id == 'NUMERIC_ESCAPES':
                        return None
                    if node.id == 'numeric_escapes' and not _shape_use(node, parents):
                        statement = parents.get(node)
                        if (path != root / core_rel or not isinstance(statement, ast.Assign)
                                or statement.value is not node or len(statement.targets) != 1
                                or not isinstance(statement.targets[0], ast.Attribute)
                                or statement.targets[0].attr != 'numeric_escapes'
                                or not isinstance(statement.targets[0].value, ast.Name)
                                or statement.targets[0].value.id != 'self'):
                            return None
                if (isinstance(node, ast.Constant) and type(node.value) is str
                        and node.value == 'NUMERIC_ESCAPES'):
                    return None  # e.g. getattr(tokenizer, 'NUMERIC_ESCAPES')
                if (isinstance(node, ast.Constant) and type(node.value) is str
                        and node.value == 'numeric_escapes'):
                    slots = parents.get(node)
                    statement = parents.get(slots)
                    if (path != root / core_rel or not isinstance(slots, ast.Tuple)
                            or not isinstance(statement, ast.Assign)
                            or not any(isinstance(t, ast.Name) and t.id == '__slots__'
                                       for t in statement.targets)):
                        return None
                if not isinstance(node, ast.Attribute):
                    continue
                parent = parents.get(node)
                if node.attr == 'NUMERIC_ESCAPES':
                    call = parents.get(parent)
                    if (path == package / 'tokens.py' and isinstance(parent, ast.keyword)
                            and parent.arg == 'numeric_escapes' and isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name) and call.func.id == 'TokenizerCore'):
                        forwarded = True
                    elif _shape_use(node, parents) or _alias_is_shape_only(parent, parents):
                        pass
                    else:
                        return None
                elif node.attr == 'numeric_escapes':
                    if path != root / core_rel or not isinstance(node.value, ast.Name) or node.value.id != 'self':
                        return None
                    if isinstance(node.ctx, ast.Store):
                        if (not isinstance(parent, ast.Assign) or parent.targets != [node]
                                or not isinstance(parent.value, ast.Name) or parent.value.id != 'numeric_escapes'):
                            return None
                        stored = True
                    elif (isinstance(parent, ast.Attribute) and parent.attr == 'get'
                          and isinstance(parents.get(parent), ast.Call)
                          and parents[parent].func is parent):
                        call = parents[parent]
                        lines.update(range(call.lineno, call.end_lineno + 1))
                    elif not (_shape_use(node, parents) or _alias_is_shape_only(parent, parents)):
                        return None
    except (OSError, SyntaxError, ValueError):
        return None
    return (core_rel, frozenset(lines)) if lines and forwarded and stored else None


def build_audit(src_root, output_path=None):
    """Analyze baseline source directly, never using an earlier audit artifact."""
    root = os.path.realpath(os.path.abspath(src_root))
    result = _audit_consumer_lines(root)
    data = {'format': AUDIT_FORMAT, 'source_commit': PINNED_COMMIT,
            'source_root': root, 'valid': result is not None,
            'consumer_file': result[0] if result is not None else None,
            'consumer_lines': sorted(result[1]) if result is not None else []}
    path = Path(output_path) if output_path is not None else AUDIT_PATH
    path.write_text(json.dumps(data, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf8')
    return data


@lru_cache(maxsize=8)
def _consumer_lines(src_root):
    root = os.path.realpath(os.path.abspath(src_root))
    if _native_distribution_present():
        return None
    try:
        with AUDIT_PATH.open(encoding='utf8') as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return _audit_consumer_lines(root)
    except (OSError, ValueError, TypeError):
        return None
    if (not isinstance(data, dict) or data.get('format') != AUDIT_FORMAT
            or data.get('source_commit') != PINNED_COMMIT
            or data.get('source_root') != root or data.get('valid') is not True
            or data.get('consumer_file') != 'sqlglot/tokenizer_core.py'):
        return None
    lines = data.get('consumer_lines')
    if (not isinstance(lines, list) or not lines
            or any(type(line) is not int or line < 1 for line in lines)
            or lines != sorted(set(lines))
            or not (Path(root) / data['consumer_file']).is_file()):
        return None
    return data['consumer_file'], frozenset(lines)


def affected_tests(old_source, new_source, maps, src_root=None):
    """Return affected baseline map indexes, or None for unsupported edits/flows.

    Call only for a whole patch whose sole semantic edit is this one assignment;
    edits to other files must not invalidate the audited forwarding code.
    """
    if not _mapping_only(old_source, new_source):
        return None
    root = os.path.abspath(src_root or os.environ.get('BR_SRC', os.path.join(os.path.dirname(__file__), 'src')))
    consumers = _consumer_lines(root)
    if consumers is None:
        return None
    rel, lines = consumers
    data = maps.src(rel)
    if not data or any(line in data['imp'] or str(line) in data['imp'] for line in lines):
        return None
    selected = set()
    for line in lines:
        index = data['cov'].get(line)
        if index is None:
            index = data['cov'].get(str(line))
        if index is not None:
            selected.update(data['sets'][index])
    return selected


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-audit', metavar='PINNED_SOURCE_ROOT', required=True)
    arguments = parser.parse_args()
    audit = build_audit(arguments.build_audit)
    print('Numeric value-flow baseline audit:', 'valid' if audit['valid'] else 'unsupported',
          audit['consumer_file'], audit['consumer_lines'])
