"""Reason about bool defaults using raw bindings from the pinned baseline.

An invocation bound to the opposite exact bool necessarily supplied its own
argument. Changing a default cannot affect that invocation. Missing/incomplete
observations, import effects and default/code introspection retain the test.
"""
import ast
from functools import lru_cache
import gzip
import json
from pathlib import Path

PINNED_COMMIT = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
FORMAT = 'raw-baseline-default-bindings-v1'
TRACE_PATH = Path(__file__).with_name('default_bindings.json.gz')
_INSPECTION = {'__defaults__', '__kwdefaults__', '__code__', 'co_code', 'co_consts',
               '__signature__', 'signature', 'getfullargspec', 'getargspec', 'getcallargs',
               'getargvalues', 'getgeneratorlocals', 'getcoroutinelocals', '_getframe',
               'currentframe', 'getmembers', 'getmembers_static'}


@lru_cache(maxsize=8)
def _tree(source):
    return ast.parse(source.encode('utf8'))


def _opaque_inspection(tree):
    # The full pinned-tree audit found only two external signature readers:
    # executor/env.py enumerates parameter names; optimizer.py reads .args.
    # Neither consumes defaults. The target module itself remains conservative.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _INSPECTION:
            return True
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in _INSPECTION:
            return True
        if isinstance(node, ast.Constant) and type(node.value) is str and node.value in _INSPECTION:
            return True
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == 'inspect' and node.attr == 'stack'):
            return True
        # The only accepted decorators below must resolve to the builtins.
        names = {'staticmethod', 'classmethod'}
        if ((isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in names)
                or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names)
                or (isinstance(node, ast.arg) and node.arg in names)
                or (isinstance(node, ast.alias) and (node.asname or node.name.split('.')[0]) in names)):
            return True
    return False


def _transparent_decorators(node):
    return all(isinstance(d, ast.Name) and d.id in ('staticmethod', 'classmethod')
               for d in node.decorator_list)


def _generator(node):
    def visit(child):
        if child is not node and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                   ast.ClassDef, ast.Lambda)):
            return False
        return isinstance(child, (ast.Yield, ast.YieldFrom)) or any(
            visit(part) for part in ast.iter_child_nodes(child))
    return visit(node)


def _slots(node):
    args = node.args.posonlyargs + node.args.args
    positional = list(zip(args[len(args) - len(node.args.defaults):], node.args.defaults))
    return [(arg.arg, value) for arg, value in positional] + [
        (arg.arg, value) for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults)]


def default_changes(rel, old_source, new_source):
    """[(site, old bool, new bool)] for exclusive bool-default edits, else None."""
    if not rel.startswith('sqlglot/') or not rel.endswith('.py') or rel.startswith('sqlglot/typing/'):
        return None
    try:
        old_tree, new_tree = _tree(old_source), ast.parse(new_source.encode('utf8'))
    except (SyntaxError, ValueError, TypeError):
        return None
    if _opaque_inspection(old_tree):
        return None
    replacements = set()
    changes = []

    def match(old, new, ancestors=()):
        if type(old) is not type(new):
            return False
        if (id(old), id(new)) in replacements:
            return True
        if isinstance(old, ast.FunctionDef):
            for (name, before), (new_name, after) in zip(_slots(old), _slots(new)):
                if (name == new_name and isinstance(before, ast.Constant)
                        and isinstance(after, ast.Constant) and type(before.value) is bool
                        and type(after.value) is bool and before.value is not after.value):
                    scopes = (*ancestors, old)
                    if (any(isinstance(scope, ast.FunctionDef) and _generator(scope) for scope in scopes)
                            or any(isinstance(scope, ast.AsyncFunctionDef) for scope in scopes)
                            or any(isinstance(scope, (ast.FunctionDef, ast.ClassDef))
                                   and not _transparent_decorators(scope) for scope in scopes)
                            or any(isinstance(scope, ast.ClassDef) and scope.keywords for scope in scopes)):
                        return False
                    replacements.add((id(before), id(after)))
                    changes.append((f'{rel}|{old.lineno}|{old.col_offset}|{name}',
                                    before.value, after.value))
        if isinstance(old, ast.AST):
            scopes = (*ancestors, old) if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                         ast.ClassDef, ast.Lambda)) else ancestors
            return all(match(a, b, scopes) for (_, a), (_, b) in
                       zip(ast.iter_fields(old), ast.iter_fields(new)))
        if isinstance(old, list):
            return len(old) == len(new) and all(match(a, b, ancestors) for a, b in zip(old, new))
        return old == new

    return changes if match(old_tree, new_tree) and changes else None


@lru_cache(maxsize=1)
def _load_trace():
    try:
        with gzip.open(TRACE_PATH, 'rt', encoding='utf8') as stream:
            return json.load(stream)
    except (OSError, EOFError, ValueError, TypeError):
        return None


def _opposite_row(row, opposite, observed):
    if not isinstance(row, dict) or row.get('complete') is not True:
        return False
    values = row.get('values')
    return (isinstance(values, list) and (bool(values) or not observed)
            and all(type(value) is bool and value is opposite for value in values))


def safe_tests(rel, old_source, new_source, trace=None):
    """Return safe test IDs, keeping all unsupported or unobserved candidates.

    The caller must require a whole patch affecting only this one source file.
    Complete import/class/module/session fixture observations must be attributed
    to every affected test by the baseline merger; per-method rows alone do not
    prove a default unused during shared setup.
    """
    changes = default_changes(rel, old_source, new_source)
    if not changes:
        return set()
    trace = _load_trace() if trace is None else trace
    if (not isinstance(trace, dict) or trace.get('format') != FORMAT
            or trace.get('source_commit') != PINNED_COMMIT):
        return set()
    ids, seeds, sites = trace.get('test_ids'), trace.get('hash_seeds'), trace.get('sites')
    if (not isinstance(ids, list) or any(type(tid) is not str for tid in ids)
            or len(ids) != len(set(ids)) or not isinstance(seeds, list)
            or any(type(seed) not in (str, int) for seed in seeds)
            or len(set(seeds)) < 2 or not isinstance(sites, dict)):
        return set()
    safe = None
    for site, before, after in changes:
        facts = sites.get(site)
        if (not isinstance(facts, dict) or facts.get('default') is not before
                or not _opposite_row(facts.get('imports'), after, observed=False)
                or not isinstance(facts.get('tests'), dict)):
            return set()
        compatible = set()
        for tid, row in facts['tests'].items():
            if not _opposite_row(row, after, observed=True):
                continue
            try:
                index = int(tid)
            except (ValueError, TypeError, OverflowError):
                continue
            if type(tid) is bool or str(index) != str(tid):
                continue
            if 0 <= index < len(ids):
                compatible.add(ids[index])
        safe = compatible if safe is None else safe & compatible
        if not safe:
            return set()
    return safe or set()
