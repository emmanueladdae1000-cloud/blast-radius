"""Patch contributions compose, but verification must execute all source edits."""
import difflib
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import selector
import verify


def patch_file(rel, old, new):
    return f'diff --git a/{rel} b/{rel}\n' + ''.join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True), fromfile='a/' + rel, tofile='b/' + rel))


class CompositionTests(unittest.TestCase):
    def test_fixture_contribution_survives_source_pruning(self):
        maps = types.SimpleNamespace(all={0, 1}, ids=['code', 'fixture'],
                                     fixtures={'tests/fixtures/independent.sql': [1]})
        engine = selector.Selector(maps)
        def source(rel, change):
            engine.vok, engine.vsrc = True, 'def f():\n    return 1\n'
            return set(), 'f operand-trace:1->0'
        text = patch_file('sqlglot/example.py', 'old\n', 'new\n') + patch_file(
            'tests/fixtures/independent.sql', 'SELECT 1;\n', 'SELECT 2;\n')
        with mock.patch.object(engine, 'py_change', side_effect=source):
            selected, notes = engine.select(text)
        self.assertEqual(selected, {1})
        self.assertFalse(engine.verify['ok'])  # fixtures were not executed by code-only worker
        self.assertTrue(engine.allow_expression_refinement)
        self.assertTrue(engine.allow_default_refinement)
        self.assertTrue(engine.allow_numeric_refinement)

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires Linux fork')
    def test_locally_harmless_source_still_applied_during_verification(self):
        old_a = 'def f(x):\n    return x < 5\n'
        new_a = old_a.replace('< 5', '<= 5')  # f(0) remains True in the baseline
        old_b = 'def call():\n    return a.f(0)\n'
        new_b = 'def call():\n    return not a.f(5)\n'
        # B alone still returns True; A+B returns False. Dropping A from the
        # verification worker would incorrectly mark the test as passing.
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory, 'sqlglot')
            package.mkdir()
            path_a, path_b = package/'composition_a.py', package/'composition_b.py'
            path_a.write_text(old_a)
            path_b.write_text(old_b)
            a, b = types.ModuleType('sqlglot.composition_a'), types.ModuleType('sqlglot.composition_b')
            exec(compile(old_a, str(path_a), 'exec'), a.__dict__)
            b.__dict__['a'] = a
            exec(compile(old_b, str(path_b), 'exec'), b.__dict__)
            class Example(unittest.TestCase):
                def test_original(self):
                    self.assertTrue(b.call())
            tests = types.SimpleNamespace(Example=Example)
            ids = ['composition_test.py::Example::test_original']
            engine = selector.Selector(types.SimpleNamespace(all={0}, ids=ids))
            changed = {'sqlglot/composition_a.py': new_a, 'sqlglot/composition_b.py': new_b}
            def source(rel, change):
                engine.vok, engine.vsrc = True, changed[rel]
                return (set() if rel.endswith('composition_a.py') else {0}), 'f'
            text = patch_file('sqlglot/composition_a.py', old_a, new_a) + patch_file(
                'sqlglot/composition_b.py', old_b, new_b)
            with mock.patch.object(engine, 'py_change', side_effect=source):
                selected, _ = engine.select(text)
            self.assertEqual(selected, {0})
            self.assertEqual(engine.verify['src'], changed)
            modules = {'sqlglot.composition_a': a, 'sqlglot.composition_b': b,
                       'composition_test': tests}
            with mock.patch.dict(sys.modules, modules), mock.patch.object(verify, '_setup'):
                result = verify.run([(0, engine.verify['src'], [0], 5, 2)], ids,
                                    directory, time.time() + 10, lambda *args: None)
            self.assertEqual(result, {0: ([0], [])})
            self.assertTrue(b.call())  # the child did not mutate the parent


if __name__ == '__main__':
    unittest.main(verbosity=2)
