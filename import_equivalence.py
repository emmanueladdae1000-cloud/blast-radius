"""Prove narrow import-time container identity wrappers preserve their graph.

This normalizes syntax, never evaluates a patched expression. Supported values
contain only exact builtin literal containers/scalars, with no external aliases.
Dictionary wrappers preserve ordered keys and value references in a fresh root;
builtin tuple returns its exact tuple argument unchanged.
"""
import ast
from contextlib import contextmanager
from functools import lru_cache
import importlib
import inspect
import json
import os
from pathlib import Path
import sys

PINNED_COMMIT = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
FACTS_FORMAT = 'baseline-import-identity-facts-v2'
FACTS_PATH = Path(__file__).with_name('import_facts.json')

_SCALARS = (type(None), bool, int, float, complex, str, bytes, type(Ellipsis))
_NUMBERS = (int, float, complex)
_OPAQUE = {'eval', 'exec', 'compile', 'globals', 'locals', 'vars', 'dir', 'getattr',
           'setattr', 'delattr', 'id', 'getrefcount', 'getsizeof', 'getsource',
           'getsourcelines', 'getfile', 'getmembers', 'getmembers_static',
           'currentframe', '_getframe', 'settrace', 'setprofile', 'get_objects',
           'get_referrers', 'get_referents', 'get_count', 'get_stats', 'get_traced_memory',
           '__builtins__', '__build_class__', '__dict__', '__code__', 'co_code', 'co_consts', '__defaults__',
           '__kwdefaults__', '__signature__', '__getattribute__', '__getattr__', '__setattr__'}


def _shadowed(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split('.')[0])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
    return names


def _opaque(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _OPAQUE:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _OPAQUE:
            return True
        if isinstance(node, ast.Constant) and type(node.value) is str and node.value in _OPAQUE:
            return True
        if isinstance(node, ast.ImportFrom) and any(alias.name == '*' for alias in node.names):
            return True
        if isinstance(node, ast.Import) and any(alias.name in ('builtins', '__builtin__') for alias in node.names):
            return True
    return False


def _hashable_literal(node):
    return ((isinstance(node, ast.Constant) and type(node.value) in _SCALARS)
            or (isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub)
                and isinstance(node.operand, ast.Constant) and type(node.operand.value) in _NUMBERS)
            or (isinstance(node, ast.Tuple) and all(_hashable_literal(item) for item in node.elts)))


def _literal(node, shadowed):
    """(canonical AST, wrapper count), or None for any nonliteral/unknown call."""
    if isinstance(node, ast.Constant) and type(node.value) in _SCALARS:
        return node, 0
    if (isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub)
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) in _NUMBERS):
        return node, 0
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        parts = [_literal(item, shadowed) for item in node.elts]
        if any(part is None for part in parts):
            return None
        items = [part[0] for part in parts]
        if isinstance(node, ast.Set):
            if not all(_hashable_literal(item) for item in items):
                return None
            value = ast.Set(elts=items)
        else:
            value = type(node)(elts=items, ctx=node.ctx)
        return value, sum(part[1] for part in parts)
    if isinstance(node, ast.Dict):
        if len(node.keys) == 1 and node.keys[0] is None:
            inner = _literal(node.values[0], shadowed)
            if inner is not None and isinstance(inner[0], ast.Dict):
                return inner[0], inner[1] + 1
            return None
        if any(key is None for key in node.keys):
            return None
        keys = [_literal(key, shadowed) for key in node.keys]
        values = [_literal(value, shadowed) for value in node.values]
        if any(part is None for part in keys + values):
            return None
        if not all(_hashable_literal(part[0]) for part in keys):
            return None
        return (ast.Dict(keys=[part[0] for part in keys], values=[part[0] for part in values]),
                sum(part[1] for part in keys + values))
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ('dict', 'tuple') and node.func.id not in shadowed
            and len(node.args) == 1 and not node.keywords):
        argument = _literal(node.args[0], shadowed)
        expected = ast.Dict if node.func.id == 'dict' else ast.Tuple
        if argument is not None and isinstance(argument[0], expected):
            return argument[0], argument[1] + 1
    return None


class _Normalize(ast.NodeTransformer):
    def __init__(self, shadowed, module, namespaces):
        self.shadowed = shadowed
        self.module = module
        self.namespaces = namespaces
        self.classes = []
        self.wrappers = 0
        self.forbidden = False

    def visit_FunctionDef(self, node):
        return node  # Function defaults/decorators/bodies are outside this proof.

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_ClassDef(self, node):
        previous = self.forbidden
        self.classes.append(node.name)
        key = '.'.join([self.module, *self.classes])
        namespace = self.namespaces.get(key)
        known_standard = (isinstance(namespace, dict) and namespace.get('namespace_is_exact_dict') is True
                          and type(namespace.get('body_calls')) is int and namespace['body_calls'] > 0
                          and namespace.get('class_module') == self.module
                          and namespace.get('class_qualname') == '.'.join(self.classes))
        self.forbidden = (previous or bool(node.decorator_list) or bool(node.keywords)
                          or (bool(node.bases) and not known_standard))
        node = self.generic_visit(node)
        self.forbidden = previous
        self.classes.pop()
        return node

    def _assignment(self, node, targets):
        if self.forbidden or not all(isinstance(target, ast.Name) for target in targets):
            return node
        literal = _literal(node.value, self.shadowed)
        if literal is not None:
            node.value = literal[0]
            self.wrappers += literal[1]
        return node

    def visit_Assign(self, node):
        return self._assignment(node, node.targets)

    def visit_AnnAssign(self, node):
        return self._assignment(node, [node.target]) if node.value is not None else node


@lru_cache(maxsize=1)
def _load_facts():
    try:
        return json.loads(FACTS_PATH.read_text(encoding='utf8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        return False


def _namespaces(facts, root):
    if (not isinstance(facts, dict) or facts.get('format') != FACTS_FORMAT
            or facts.get('source_commit') != PINNED_COMMIT or facts.get('source_root') != root
            or not isinstance(facts.get('class_namespaces'), dict)):
        return {}
    return facts['class_namespaces']


def _module_name(rel):
    name = rel[:-3].replace('/', '.')
    return name[:-9] if name.endswith('.__init__') else name


@contextmanager
def _observe_namespaces(expected):
    """Observe actual class-body namespaces, without invoking any mapping API."""
    if sys.getprofile() is not None:
        raise ValueError('Build facts require a fresh unprofiled baseline process')
    namespaces = {}

    def profile(frame, event, arg):
        if event != 'call' or frame.f_code.co_flags & inspect.CO_NEWLOCALS:
            return
        code = frame.f_code
        target = expected.get((code.co_filename, code.co_qualname, code.co_firstlineno))
        if target is None:
            return
        module, qualified = target
        key = module + '.' + qualified
        exact = type(frame.f_locals) is dict
        row = namespaces.setdefault(key, {'class_module': module, 'class_qualname': qualified,
                                          'namespace_is_exact_dict': True, 'body_calls': 0})
        row['namespace_is_exact_dict'] &= exact
        row['body_calls'] += 1

    sys.setprofile(profile)
    try:
        yield namespaces
    finally:
        sys.setprofile(None)


def build_facts(src_root, output_path=None):
    """Observe only original pinned class definitions in a fresh build process."""
    root = Path(src_root).resolve()
    if not (root / 'sqlglot' / '__init__.py').is_file():
        raise ValueError('Missing pinned source package')
    if any(name == 'sqlglot' or name.startswith('sqlglot.') for name in sys.modules):
        raise ValueError('Build facts require SQLGlot not yet imported')
    sys.path.insert(0, str(root))

    def class_paths(nodes, prefix=()):
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                path = (*prefix, node.name)
                yield path, node.lineno
                yield from class_paths(node.body, path)
            elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for field in ('body', 'orelse', 'finalbody'):
                    children = getattr(node, field, None)
                    if isinstance(children, list):
                        yield from class_paths(children, prefix)

    expected = {}
    modules = []
    for path in sorted((root / 'sqlglot').rglob('*.py')):
        rel = path.relative_to(root).as_posix()
        if path.name == '__main__.py':
            continue
        paths = list(class_paths(ast.parse(path.read_bytes()).body))
        if not paths:
            continue
        module_name = _module_name(rel)
        modules.append((module_name, path))
        for names, line in paths:
            qualified = '.'.join(names)
            expected[(str(path.resolve()), qualified, line)] = (module_name, qualified)
    with _observe_namespaces(expected) as namespaces:
        for module_name, path in modules:
            module = importlib.import_module(module_name)
            origin = Path(module.__file__).resolve() if getattr(module, '__file__', None) else None
            if origin != path.resolve():
                raise ValueError('Unexpected baseline module origin: ' + module_name)
    facts = {'format': FACTS_FORMAT, 'source_commit': PINNED_COMMIT,
             'source_root': str(root), 'class_namespaces': namespaces}
    target = Path(output_path) if output_path is not None else FACTS_PATH
    target.write_text(json.dumps(facts, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf8')
    return facts


def equivalent(rel, old_source, new_source, facts=None, src_root=None):
    """True only for proved container wrappers; callers gate the patch to one file.

    Builtin name bindings are checked across the entire target module, including
    class and function scopes. Opaque introspection and the pinned code-observer
    typing subtree are unsupported. All remaining AST fields must match exactly.
    """
    if (not rel.startswith('sqlglot/') or not rel.endswith('.py')
            or rel.startswith('sqlglot/typing/')):
        return False
    try:
        before, after = (ast.parse(source.encode('utf8')) for source in (old_source, new_source))
        if _opaque(before) or _opaque(after):
            return False
        shadowed = _shadowed(before) | _shadowed(after)
        root = os.path.realpath(src_root or os.environ.get('BR_SRC', str(Path(__file__).with_name('src'))))
        namespace_facts = _namespaces(_load_facts() if facts is None else facts, root)
        module = _module_name(rel)
        old_normalizer = _Normalize(shadowed, module, namespace_facts)
        new_normalizer = _Normalize(shadowed, module, namespace_facts)
        old_tree, new_tree = old_normalizer.visit(before), new_normalizer.visit(after)
        return (old_normalizer.wrappers + new_normalizer.wrappers > 0
                and ast.dump(old_tree) == ast.dump(new_tree))
    except (SyntaxError, ValueError, TypeError, AttributeError, RecursionError):
        return False


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-facts', metavar='PINNED_SOURCE_ROOT', required=True)
    args = parser.parse_args()
    facts = build_facts(args.build_facts)
    print('Baseline import identity class namespaces:', len(facts['class_namespaces']))
