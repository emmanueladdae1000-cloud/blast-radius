"""Independent source/coverage regressions; no patched SQLGlot execution."""
import ast
import dis
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import expression_refinement as e

REL = 'sqlglot/example.py'


class Maps:
    def __init__(self, data):
        self.data = data

    def src(self, rel):
        return self.data


def data(coverage=None, imports=()):
    return {'cov': coverage or {}, 'sets': [[0], [1], [2], [3]], 'imp': list(imports)}


class ExpressionTests(unittest.TestCase):
    def test_multiline_short_circuit_uses_comparison_line(self):
        old = 'def f(flag, x):\n    return (\n        flag\n        and x == 1\n    )\n'
        d = data({2: 0, 3: 1, 4: 2})
        self.assertEqual(e.affected_tests(REL, old, old.replace('==', '!='), Maps(d)), {2})

    def test_reached_and_unreached_comparisons_union(self):
        old = 'def f(a, x):\n    return (a == 1\n            and x == 2)\n'
        new = old.replace('==', '!=')
        self.assertEqual(e.affected_tests(REL, old, new, Maps(data({2: 1, 3: 2}))), {1, 2})
        self.assertEqual(e.affected_tests(REL, old, new, Maps(data({2: 1}))), {1})

    def test_unreached_valid_anchor_has_no_candidates(self):
        old = 'def f(x):\n    return x == 1\n'
        self.assertEqual(e.affected_tests(REL, old, old.replace('1', '2'), Maps(data())), set())

    def test_optimized_none_condition(self):
        old = 'def f(x):\n    if x is None:\n        return True\n'
        new = old.replace('is None', 'is not None')
        self.assertEqual(e.affected_tests(REL, old, new, Maps(data({2: 3}))), {3})

    def test_folded_dead_comparison_has_no_anchor(self):
        old = 'def f(x):\n    return False and x == 1\n'
        self.assertIsNone(e.affected_tests(REL, old, old.replace('==', '!='), Maps(data({2: 0}))))

    def test_import_anchor_falls_back(self):
        old = 'def f(x):\n    return x == 1\n'
        self.assertIsNone(e.affected_tests(REL, old, old.replace('==', '!='), Maps(data({2: 0}, [2]))))

    def test_enclosing_import_line_does_not_pollute_nested_anchor(self):
        old = 'def f(flag, x):\n    return (flag\n            and x == 1)\n'
        self.assertEqual(e.affected_tests(REL, old, old.replace('==', '!='), Maps(data({2: 0, 3: 2}, [2]))), {2})

    def test_definition_time_expressions_fall_back(self):
        sources = [
            'value = x == 1\n',
            'class C:\n    value = x == 1\n',
            'def f(value=x == 1):\n    return value\n',
            'def outer(x):\n    def inner(value=x == 1):\n        return value\n    return inner\n',
            '@decorator(x == 1)\ndef f():\n    return 0\n',
            'def outer(x):\n    @decorator(x == 1)\n    def inner():\n        return 0\n    return inner\n',
            'def f(x: (value == 1)):\n    return x\n',
        ]
        for old in sources:
            with self.subTest(source=old):
                self.assertIsNone(e.affected_tests(REL, old, old.replace('==', '!='), Maps(data({1: 0, 2: 1, 3: 2}))))

    def test_method_body_is_runtime(self):
        old = 'class C:\n    def f(self, x):\n        return x == 1\n'
        self.assertEqual(e.affected_tests(REL, old, old.replace('==', '!='), Maps(data({3: 1}))), {1})

    def test_unsupported_changes_and_missing_maps(self):
        old = 'def f(a, x):\n    return a and x == 1\n'
        for new in (old.replace('and', 'or'), old.replace('x ==', 'a =='), old):
            self.assertIsNone(e.affected_tests(REL, old, new, Maps(data({2: 0}))))
        for d in (None, {}, {'cov': {}, 'sets': []}):
            self.assertIsNone(e.affected_tests(REL, old, old.replace('==', '!='), Maps(d)))

    def test_code_object_readers_in_typing_modules_require_fallback(self):
        old = ('def parent(x):\n    return x == 2\n'
               'def child(x):\n    return x == 1\n')
        new = old.replace('x == 1', 'x == 2')
        before, after = {}, {}
        exec(old, before)
        exec(new, after)

        def same_code(namespace):
            a, b = namespace['parent'].__code__, namespace['child'].__code__
            return (a.co_code, a.co_names, a.co_varnames, a.co_consts) == (
                b.co_code, b.co_names, b.co_varnames, b.co_consts)

        # The pinned TestTyping._same_code can observe this change even when
        # neither annotator runs and the comparison has no line coverage.
        self.assertFalse(same_code(before))
        self.assertTrue(same_code(after))
        self.assertIsNone(e.affected_tests('sqlglot/typing/example.py', old, new, Maps(data())))

    def test_unavailable_coverage_file_falls_back(self):
        old = 'def f(x):\n    return x == 1\n'
        with patch.object(Maps, 'src', side_effect=FileNotFoundError('missing map')):
            self.assertIsNone(e.affected_tests(REL, old, old.replace('==', '!='), Maps(data())))

    def test_bytecode_location_unknown_falls_back(self):
        old = 'def f(x):\n    return x == 1\n'
        with patch.object(e, '_baseline_anchors', return_value=({(2, 11): (2, 2, 11, 17)}, {})):
            self.assertIsNone(e.affected_tests(REL, old, old.replace('==', '!='), Maps(data({2: 0}))))

    def test_disassembly_without_exact_positions_falls_back(self):
        old = 'def f(x):\n    return x == 11\n'
        missing = types.SimpleNamespace(opname='COMPARE_OP', offset=0, positions=
            types.SimpleNamespace(lineno=None, end_lineno=None, col_offset=None, end_col_offset=None))
        e._baseline_anchors.cache_clear()
        try:
            with patch.object(e.dis, 'get_instructions', return_value=[missing]):
                self.assertIsNone(e.affected_tests(REL, old, old.replace('==', '!='), Maps(data({2: 0}))))
        finally:
            e._baseline_anchors.cache_clear()

    def test_corrupt_coverage_falls_back(self):
        old = 'def f(x):\n    return x == 1\n'
        for sid in (-1, 99, True, '0'):
            self.assertIsNone(e.affected_tests(REL, old, old.replace('==', '!='), Maps(data({2: sid}))))

    def test_compiled_comparison_position_matches_ast(self):
        old = 'def f(flag, x):\n    return (flag\n            and x == 1)\n'
        node = next(n for n in ast.walk(ast.parse(old)) if isinstance(n, ast.Compare))
        code = next(c for c in compile(old, REL, 'exec').co_consts if isinstance(c, types.CodeType))
        instruction = next(i for i in dis.get_instructions(code) if i.opname == 'COMPARE_OP')
        self.assertEqual(tuple(instruction.positions), (node.lineno, node.end_lineno, node.col_offset, node.end_col_offset))
        self.assertEqual(e._baseline_anchors(old, REL)[1][e._position(node)], frozenset({3}))


class PinnedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        roots = [Path('/baseline/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa'), Path('/app/src')]
        cls.root = next((p for p in roots if (p/'sqlglot/parser.py').exists()), None)
        if cls.root is None:
            raise unittest.SkipTest('Pinned SQLGlot source unavailable')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import selector
        selector.MAP = str(Path(__file__).resolve().parents[1]/'map')
        cls.maps = selector.Maps()

    def check_case(self, rel, line, before, after, expected_count):
        old = (self.root/rel).read_text()
        lines = old.splitlines(True)
        self.assertIn(before, lines[line-1])
        lines[line-1] = lines[line-1].replace(before, after, 1)
        result = e.affected_tests(rel, old, ''.join(lines), self.maps)
        mapped = self.maps.src(rel)
        independently_expected = set(mapped['sets'][mapped['cov'][line]])
        self.assertEqual(result, independently_expected)
        self.assertEqual(len(result), expected_count)
        self.assertNotIn(1490, result)
        return result

    def test_parser_table_short_circuit(self):
        self.check_case('sqlglot/parser.py', 9400, '==', '!=', 2)

    def test_tokenizer_scientific_short_circuit(self):
        self.check_case('sqlglot/tokenizer_core.py', 1025, '<', '<=', 5)

    def test_parser_function_literal(self):
        self.check_case('sqlglot/parser.py', 7575, '"FN"', '"OTHER"', 17)


if __name__ == '__main__':
    unittest.main(verbosity=2)
