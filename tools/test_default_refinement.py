"""Independent bool-default/binding regressions without alternate outcome maps."""
import ast
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import default_refinement as d

REL = 'sqlglot/example.py'
OLD = 'def decide(value, enabled=True):\n    return value if enabled else -value\n'
NEW = OLD.replace('enabled=True', 'enabled=False')
IDS = ['explicit_opposite', 'uses_default', 'mixed', 'missing', 'incomplete', 'wrong_type', 'empty']


def row(values, complete=True):
    return {'complete': complete, 'values': values}


def trace(old=OLD):
    sites = {}
    for node in ast.walk(ast.parse(old)):
        if not isinstance(node, ast.FunctionDef):
            continue
        args = node.args.posonlyargs + node.args.args
        pairs = list(zip(args[len(args) - len(node.args.defaults):], node.args.defaults))
        pairs += list(zip(node.args.kwonlyargs, node.args.kw_defaults))
        for parameter, value in pairs:
            if not isinstance(value, ast.Constant) or type(value.value) is not bool:
                continue
            before, opposite = value.value, not value.value
            sites[f'{REL}|{node.lineno}|{node.col_offset}|{parameter.arg}'] = {
                'default': before, 'imports': row([]), 'tests': {
                    '0': row([opposite]), '1': row([before]), '2': row([before, opposite]),
                    '4': row([opposite], complete=False), '5': row([int(opposite)]), '6': row([])}}
    return {'format': d.FORMAT, 'source_commit': d.PINNED_COMMIT,
            'hash_seeds': ['317', '391'], 'test_ids': IDS, 'sites': sites}


class DefaultTests(unittest.TestCase):
    def safe(self, old=OLD, new=NEW, facts=None, rel=REL):
        return d.safe_tests(rel, old, new, trace(old) if facts is None else facts)

    def test_explicit_opposite_is_safe_but_default_mixed_missing_or_incomplete_stay(self):
        self.assertEqual(self.safe(), {'explicit_opposite'})
        before, after = {}, {}
        exec(OLD, before)
        exec(NEW, after)
        self.assertEqual(before['decide'](5, enabled=False), after['decide'](5, enabled=False))
        self.assertNotEqual(before['decide'](5), after['decide'](5))

    def test_false_to_true_uses_exact_opposite_boolean(self):
        self.assertEqual(self.safe(NEW, OLD), {'explicit_opposite'})
        facts = trace(NEW)
        first = next(iter(facts['sites'].values()))
        first['tests']['0'] = row([1])
        self.assertEqual(self.safe(NEW, OLD, facts), set())

    def test_import_bindings_are_required_and_must_not_use_old_default(self):
        for imports in (row([True]), row([], False), row([0]), None, {'values': []}):
            facts = trace()
            next(iter(facts['sites'].values()))['imports'] = imports
            self.assertEqual(self.safe(facts=facts), set())
        facts = trace()
        next(iter(facts['sites'].values()))['imports'] = row([False])
        self.assertEqual(self.safe(facts=facts), {'explicit_opposite'})

    def test_shared_fixture_old_default_binding_prevents_method_pruning(self):
        facts = trace()
        # The merger expands shared class/module/session bindings into the method
        # row. A later explicit argument does not erase a setup default use.
        next(iter(facts['sites'].values()))['tests']['0'] = row([False, True])
        self.assertEqual(self.safe(facts=facts), set())

    def test_positional_only_and_keyword_only_defaults_intersect(self):
        old = 'def decide(enabled=True, /, *, optional=False):\n    return enabled or optional\n'
        new = old.replace('enabled=True', 'enabled=False').replace('optional=False', 'optional=True')
        facts = trace(old)
        sites = list(facts['sites'].values())
        sites[0]['tests']['1'] = row([False])
        sites[1]['tests']['2'] = row([True])
        self.assertEqual(self.safe(old, new, facts), {'explicit_opposite'})

    def test_multiple_functions_intersect_without_assuming_unobserved_calls_safe(self):
        old = 'def one(flag=True):\n    return flag\ndef two(option=False):\n    return option\n'
        new = old.replace('flag=True', 'flag=False').replace('option=False', 'option=True')
        facts = trace(old)
        del list(facts['sites'].values())[1]['tests']['0']
        self.assertEqual(self.safe(old, new, facts), set())

    def test_builtin_method_decorators_and_ordinary_sync_closures(self):
        for decorator in ('classmethod', 'staticmethod'):
            old = f'class C:\n    @{decorator}\n    def decide(value, enabled=True):\n        return enabled\n'
            self.assertEqual(self.safe(old, old.replace('enabled=True', 'enabled=False')), {'explicit_opposite'})
        old = 'def outer():\n    def inner(enabled=True):\n        return lambda: enabled\n    return inner\n'
        self.assertEqual(self.safe(old, old.replace('enabled=True', 'enabled=False')), {'explicit_opposite'})

    def test_async_generator_lambda_and_opaque_decorators_fall_back(self):
        sources = [
            'async def decide(enabled=True):\n    return enabled\n',
            'def decide(enabled=True):\n    yield enabled\n',
            'def outer():\n    yield 1\n    def decide(enabled=True):\n        return enabled\n',
            'decide = lambda enabled=True: enabled\n',
            '@decorator\ndef decide(enabled=True):\n    return enabled\n',
            '@decorator\nclass C:\n    def decide(self, enabled=True):\n        return enabled\n',
            'class C(metaclass=Meta):\n    def decide(self, enabled=True):\n        return enabled\n',
            'from external import staticmethod\nclass C:\n    @staticmethod\n    def decide(enabled=True):\n        return enabled\n',
        ]
        for old in sources:
            with self.subTest(source=old):
                self.assertIsNone(d.default_changes(REL, old, old.replace('enabled=True', 'enabled=False')))

    def test_type_shape_annotation_and_compound_edits_fall_back(self):
        for new in (OLD, OLD.replace('enabled=True', 'enabled=1'),
                    OLD.replace('enabled=True', 'enabled=bool(0)'),
                    NEW.replace('return value', 'return value + 1'),
                    NEW.replace('value, enabled', 'value: int, enabled'),
                    NEW.replace('enabled=False', 'enabled=False, other=False'),
                    NEW.replace('def decide', 'def renamed')):
            with self.subTest(source=new):
                self.assertIsNone(d.default_changes(REL, OLD, new))
        self.assertEqual(self.safe(rel='tests/test_example.py'), set())

    def test_default_and_code_inspection_fall_back(self):
        for expression in ('decide.__defaults__', 'decide.__kwdefaults__', 'decide.__code__',
                           'inspect.signature(decide)', 'inspect.getfullargspec(decide)',
                           'inspect.currentframe()', 'inspect.stack()',
                           'getattr(decide, "__defaults__")'):
            old = OLD + '\nresult = ' + expression + '\n'
            self.assertIsNone(d.default_changes(REL, old, old.replace('enabled=True', 'enabled=False')))
        self.assertIsNone(d.default_changes('sqlglot/typing/example.py', OLD, NEW))
        # An ordinary data structure named stack is unrelated to frame/default inspection.
        old = OLD + '\nstack = []\nstack.append(1)\n'
        self.assertIsNotNone(d.default_changes(REL, old, old.replace('enabled=True', 'enabled=False')))

    def test_wrong_metadata_unknown_indexes_and_missing_sites_are_not_safe(self):
        for changes in ({'format': 'unknown'}, {'source_commit': 'other'}, {'hash_seeds': ['317']},
                        {'hash_seeds': ['317', '317']}, {'hash_seeds': [True, False]},
                        {'test_ids': ['duplicate', 'duplicate']}, {'sites': {}}):
            facts = trace()
            facts.update(changes)
            self.assertEqual(self.safe(facts=facts), set())
        for default in (False, 1, None):
            facts = trace()
            next(iter(facts['sites'].values()))['default'] = default
            self.assertEqual(self.safe(facts=facts), set())
        facts = trace()
        tests = next(iter(facts['sites'].values()))['tests']
        tests.clear()
        tests.update({'999': row([False]), '-1': row([False]), 'bad': row([False]),
                      '0.0': row([False]), '01': row([False])})
        self.assertEqual(self.safe(facts=facts), set())

    def test_actual_pinned_bool_defaults_parse_without_special_case_tables(self):
        root = Path('/app/src')
        if not (root / 'sqlglot/parser.py').is_file():
            self.skipTest('pinned profiler source unavailable')
        old = (root / 'sqlglot/parser.py').read_text()
        new = old.replace('self, token_type: TokenType, advance: bool = True, expression:',
                          'self, token_type: TokenType, advance: bool = False, expression:', 1)
        changes = d.default_changes('sqlglot/parser.py', old, new)
        self.assertEqual(changes, [('sqlglot/parser.py|2109|4|advance', True, False)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
