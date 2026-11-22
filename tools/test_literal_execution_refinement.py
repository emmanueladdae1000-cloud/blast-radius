"""Independent scalar execution-span and actual baseline line-event regressions."""
import ast
import dis
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import literal_execution_refinement as l

REL = 'sqlglot/example.py'


class Maps:
    def __init__(self, coverage=None, imports=(), sets=None):
        self.data = {'cov': coverage or {}, 'imp': set(imports),
                     'sets': [[0], [1], [2], [3]] if sets is None else sets}
    def src(self, rel):
        return self.data


class LiteralExecutionTests(unittest.TestCase):
    def affected(self, old, new, maps=None, rel=REL):
        return l.affected_tests(rel, old, new, maps or Maps())

    def test_keyword_literal_uses_its_vm_line_in_short_circuit(self):
        old = ('def produce(value, *, copy):\n    return value if copy else -value\n'
               'def f(flag):\n    return (flag and produce(\n        5,\n        copy=True\n    ))\n')
        new = old.replace('copy=True', 'copy=False')
        before, after = {}, {}
        exec(compile(old, REL, 'exec'), before)
        exec(compile(new, REL, 'exec'), after)
        observed = []
        if not hasattr(sys, 'monitoring'):
            self.skipTest('requires the profiler Python monitoring API')
        tool = 5
        sys.monitoring.use_tool_id(tool, 'literal execution independent regression')
        try:
            for flag in (False, True):
                lines = set()
                def line_event(code, line):
                    if code.co_filename == REL:
                        lines.add(line)
                sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, line_event)
                sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
                before['f'](flag)
                sys.monitoring.set_events(tool, 0)
                observed.append(lines)
        finally:
            sys.monitoring.set_events(tool, 0)
            sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, None)
            sys.monitoring.free_tool_id(tool)
        self.assertNotIn(6, observed[0])
        self.assertIn(6, observed[1])
        sets, coverage = [], {}
        for line in set.union(*observed):
            coverage[line] = len(sets)
            sets.append([i for i, lines in enumerate(observed) if line in lines])
        self.assertEqual(self.affected(old, new, Maps(coverage, sets=sets)), {1})
        self.assertEqual(before['f'](False), after['f'](False))
        self.assertNotEqual(before['f'](True), after['f'](True))

    def test_return_const_and_arithmetic_operand(self):
        old = 'def f():\n    return 42\n'
        self.assertEqual(self.affected(old, old.replace('42', '43'), Maps({2: 3})), {3})
        code = next(c for c in compile(old, REL, 'exec').co_consts if isinstance(c, types.CodeType))
        node = next(n for n in ast.walk(ast.parse(old)) if isinstance(n, ast.Constant))
        materialize = next(i for i in dis.get_instructions(code) if i.opname in ('LOAD_CONST', 'RETURN_CONST'))
        self.assertEqual(tuple(materialize.positions), l._position(node))
        old = 'def f(x):\n    return x + 42\n'
        self.assertEqual(self.affected(old, old.replace('42', '43'), Maps({2: 1})), {1})

    def test_exact_scalar_types_and_typed_values(self):
        for before, after in [('True', 'False'), ('True', '1'), ('None', 'False'),
                              ('1', '2'), ('1.25', '2.5'), ("'a'", "'b'"),
                              ("b'a'", "b'b'"), ('1e999', '2.5')]:
            old = f'def f():\n    return call(value={before})\n'
            with self.subTest(before=before, after=after):
                self.assertEqual(self.affected(old, old.replace('value='+before, 'value='+after), Maps({2: 2})), {2})
        old = 'def f():\n    return call(value=1)\n'
        for unsupported in ('[2]', '(2,)', '{2}', '2j', '...', 'other', 'int(2)'):
            self.assertIsNone(self.affected(old, old.replace('value=1', 'value='+unsupported), Maps({2: 0})))

    def test_multiple_literal_sites_union_and_unreached_anchor(self):
        old = 'def f(flag):\n    if flag:\n        return 1\n    return 2\n'
        new = old.replace('return 1', 'return 3').replace('return 2', 'return 4')
        self.assertEqual(self.affected(old, new, Maps({3: 0, 4: 2})), {0, 2})
        self.assertEqual(self.affected(old, new, Maps()), set())

    def test_folded_or_eliminated_literal_falls_back(self):
        for old, before, after in [
                ('def f():\n    return 1 + 2\n', '2', '3'),
                ('def f(x):\n    return False and call(copy=True)\n', 'copy=True', 'copy=False'),
                ('def f():\n    return -1\n', '-1', '-2'),
                ('def f():\n    return f"prefix{value}suffix"\n', 'prefix', 'other')]:
            with self.subTest(source=old):
                self.assertIsNone(self.affected(old, old.replace(before, after), Maps({2: 0})))

    def test_definition_class_import_and_async_contexts_fall_back(self):
        sources = [
            'value = True\n',
            'class C:\n    value = True\n',
            'def f(value=True):\n    return value\n',
            'def outer():\n    def inner(value=True):\n        return value\n    return inner\n',
            '@decorator(value=True)\ndef f():\n    return 0\n',
            'def f(x: "True"):\n    return x\n',
            'value = lambda flag=True: flag\n',
            'async def f():\n    return True\n',
        ]
        for old in sources:
            with self.subTest(source=old):
                self.assertIsNone(self.affected(old, old.replace('True', 'False'), Maps({1: 0, 2: 1, 3: 2})))

    def test_runtime_method_lambda_and_utf8_positions(self):
        for old in ['class C:\n    def f(self):\n        return True\n',
                    'f = lambda x: call(x, flag=True)\n',
                    'def f(é):\n    return call(é, flag="é")\n']:
            before, after = ('"é"', '"ø"') if 'flag="é"' in old else ('True', 'False')
            line = 3 if 'class C' in old else 1 if 'lambda' in old else 2
            self.assertEqual(self.affected(old, old.replace(before, after), Maps({line: 1})), {1})

    def test_source_ast_must_otherwise_match(self):
        old = 'def f(x):\n    return call(x, flag=True)\n'
        for new in (old, old.replace('call(x', 'call(other').replace('True', 'False'),
                    old.replace('return call', 'return other').replace('True', 'False'),
                    old.replace('def f(x)', 'def f(x=1)').replace('True', 'False')):
            self.assertIsNone(self.affected(old, new, Maps({2: 0})))

    def test_import_and_code_observer_fallback(self):
        old = 'def f():\n    return call(flag=True)\n'
        new = old.replace('True', 'False')
        self.assertIsNone(self.affected(old, new, Maps({2: 0}, imports=[2])))
        self.assertIsNone(self.affected(old, new, Maps({2: 0}), rel='sqlglot/typing/example.py'))
        for observer in ('f.__code__', 'getattr(f, "co_consts")', 'inspect.getsource(f)'):
            original = old + '\nreader = ' + observer + '\n'
            self.assertIsNone(self.affected(original, original.replace('True', 'False'), Maps({2: 0})))

    def test_missing_vm_positions_or_metadata_and_bad_indexes_fall_back(self):
        old = 'def f():\n    return call(flag=True)\n'
        new = old.replace('True', 'False')
        with patch.object(l, '_baseline_anchors', return_value={}):
            self.assertIsNone(self.affected(old, new, Maps({2: 0})))
        with patch.object(Maps, 'src', side_effect=FileNotFoundError('missing')):
            self.assertIsNone(self.affected(old, new))
        for coverage in ({2: -1}, {2: 99}, {2: True}, {'2': 0}):
            self.assertIsNone(self.affected(old, new, Maps(coverage)))
        for tests in ([True], [-1], ['0']):
            self.assertIsNone(self.affected(old, new, Maps({2: 0}, sets=[tests])))


if __name__ == '__main__':
    unittest.main(verbosity=2)
