"""Use mandatory bytecode line events for pure runtime scalar-literal edits.

The only possible value change is a constant loaded by an exact source-position
instruction. Tests that never reached its baseline VM line cannot observe it.
Folded literals, definition-time values and code-object observers are unsupported.
"""
import ast
import dis
from functools import lru_cache
import inspect
import struct
import types

_SCALARS = (type(None), bool, int, float, str, bytes)
_OBSERVERS = {'__code__', 'co_code', 'co_consts', '__defaults__', '__kwdefaults__',
              '__signature__', 'getsource', 'getsourcelines', 'getfile', '_getframe',
              'currentframe', 'getgeneratorlocals', 'getcoroutinelocals', 'gi_frame', 'cr_frame'}


def _position(node):
    return node.lineno, node.end_lineno, node.col_offset, node.end_col_offset


def _key(value):
    if type(value) not in _SCALARS:
        return None
    if type(value) is float:
        return float, struct.pack('!d', value)
    return type(value), value


@lru_cache(maxsize=8)
def _tree(source):
    return ast.parse(source.encode('utf8'))


def _runtime_literals(tree):
    allowed = set()

    def visit(node, runtime=False, forbidden=False, parent=None):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for name, value in ast.iter_fields(node):
                values = value if isinstance(value, list) else [value]
                for child in values:
                    if isinstance(child, ast.AST):
                        visit(child, name == 'body', forbidden or name != 'body'
                              or isinstance(node, ast.AsyncFunctionDef), node)
            return
        if isinstance(node, ast.ClassDef):
            for name, value in ast.iter_fields(node):
                values = value if isinstance(value, list) else [value]
                for child in values:
                    if isinstance(child, ast.AST):
                        visit(child, False, forbidden or name != 'body', node)
            return
        if isinstance(node, ast.Lambda):
            visit(node.args, False, True, node)
            visit(node.body, True, forbidden, node)
            return
        if (isinstance(node, ast.Constant) and runtime and not forbidden
                and _key(node.value) is not None and not isinstance(parent, ast.JoinedStr)):
            allowed.add(id(node))
        for child in ast.iter_child_nodes(node):
            visit(child, runtime, forbidden, node)

    visit(tree)
    return allowed


def _observer(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _OBSERVERS:
            return True
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in _OBSERVERS:
            return True
        if isinstance(node, ast.Constant) and type(node.value) is str and node.value in _OBSERVERS:
            return True
    return False


def literal_changes(old_source, new_source):
    try:
        before, after = _tree(old_source), ast.parse(new_source.encode('utf8'))
    except (SyntaxError, ValueError, TypeError):
        return None
    if _observer(before):
        return None
    allowed = _runtime_literals(before)
    changes = []

    def match(old, new):
        if type(old) is not type(new):
            return False
        if isinstance(old, ast.Constant) and _key(old.value) != _key(new.value):
            if id(old) not in allowed or _key(new.value) is None:
                return False
            changes.append((_position(old), _key(old.value)))
            return old.kind == new.kind
        if isinstance(old, ast.AST):
            return all(match(a, b) for (_, a), (_, b) in
                       zip(ast.iter_fields(old), ast.iter_fields(new)))
        if isinstance(old, list):
            return len(old) == len(new) and all(match(a, b) for a, b in zip(old, new))
        return old == new

    return changes if match(before, after) and changes else None


@lru_cache(maxsize=32)
def _baseline_anchors(source, rel):
    tree = _tree(source)
    allowed = _runtime_literals(tree)
    literals = {(_position(node), _key(node.value)) for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and id(node) in allowed}
    anchors = {}

    def scan(code):
        if code.co_flags & inspect.CO_NEWLOCALS:
            lines = tuple(code.co_lines())
            for instruction in dis.get_instructions(code):
                if instruction.opname not in ('LOAD_CONST', 'RETURN_CONST'):
                    continue
                p = instruction.positions
                position = (p.lineno, p.end_lineno, p.col_offset, p.end_col_offset)
                key = (position, _key(instruction.argval))
                if key not in literals:
                    continue
                event_lines = {line for start, end, line in lines
                               if start <= instruction.offset < end}
                if len(event_lines) != 1:
                    continue
                line = next(iter(event_lines))
                if type(line) is int and position[0] <= line <= position[1]:
                    anchors.setdefault(key, set()).add(line)
        for constant in code.co_consts:
            if isinstance(constant, types.CodeType):
                scan(constant)

    scan(compile(source.encode('utf8'), rel, 'exec', dont_inherit=True))
    return {key: frozenset(lines) for key, lines in anchors.items()}


def affected_tests(rel, old_source, new_source, maps):
    """Return map indices or None; callers must gate the whole patch to one file."""
    if (not rel.startswith('sqlglot/') or not rel.endswith('.py')
            or rel.startswith('sqlglot/typing/')):
        return None
    changes = literal_changes(old_source, new_source)
    if not changes:
        return None
    try:
        data = maps.src(rel)
        if (not isinstance(data, dict) or not isinstance(data.get('cov'), dict)
                or not isinstance(data.get('sets'), list) or 'imp' not in data
                or any(type(line) is not int for line in data['cov'])):
            return None
        imported = set(data['imp'])
        if any(type(line) is not int for line in imported):
            return None
        anchors = _baseline_anchors(old_source, rel)
        out = set()
        for change in changes:
            lines = anchors.get(change)
            if not lines or lines & imported:
                return None
            for line in lines:
                index = data['cov'].get(line)
                if index is None:
                    continue
                if type(index) is not int or not 0 <= index < len(data['sets']):
                    return None
                tests = data['sets'][index]
                if (not isinstance(tests, (list, tuple))
                        or any(type(test) is not int or test < 0 for test in tests)):
                    return None
                out.update(tests)
        return out
    except (OSError, SyntaxError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return None
