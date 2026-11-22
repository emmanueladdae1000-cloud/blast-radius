"""Independent container/alias checks; no outcome lookup data is used."""
import ast
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import import_equivalence as i

REL = 'sqlglot/example.py'


class ImportEquivalenceTests(unittest.TestCase):
    def same(self, old, new):
        return i.equivalent(REL, old, new)

    def test_dictionary_unpack_copy_and_tuple_identity_wrappers(self):
        for original, replacement in [
                ('{"a": (1, 2), "b": [3, {"c": None}]}', '{**{"a": (1, 2), "b": [3, {"c": None}]}}'),
                ('{"a": (1, 2)}', 'dict({"a": (1, 2)})'),
                ('(1, 2, {"a": []})', 'tuple((1, 2, {"a": []}))'),
                ('{}', 'dict({})'), ('()', 'tuple(())'),
                ('{"a": (1, 2)}', '{**dict({"a": tuple((1, 2))})}')]:
            old, new = 'TABLE = ' + original + '\n', 'TABLE = ' + replacement + '\n'
            with self.subTest(original=original, replacement=replacement):
                self.assertTrue(self.same(old, new))
                self.assertTrue(self.same(new, old))
                before, after = {}, {}
                exec(old, before)
                exec(new, after)
                self.assertEqual(before['TABLE'], after['TABLE'])
                self.assertIs(type(before['TABLE']), type(after['TABLE']))

    def test_tuple_wrapper_returns_the_exact_existing_tuple(self):
        old = 'BASE = (1, 2)\nTABLE = {"a": (1, 2), "b": (1, 2)}\n'
        new = old.replace('"a": (1, 2)', '"a": tuple((1, 2))')
        self.assertTrue(self.same(old, new))
        for source in (old, new):
            namespace = {}
            exec(compile(source, '<identity>', 'exec'), namespace)
            table = namespace['TABLE']
            self.assertIs(table['a'], table['b'])
            self.assertIs(table['a'], namespace['BASE'])

    def test_nested_mutable_containers_remain_distinct_and_chained_aliases_shared(self):
        old = 'A = B = {"a": [], "b": [], "c": ({"x": []},)}\n'
        new = old.replace('{"a": [], "b": [], "c": ({"x": []},)}',
                          'dict({"a": [], "b": [], "c": tuple(({"x": []},))})')
        self.assertTrue(self.same(old, new))
        for source in (old, new):
            namespace = {}
            exec(source, namespace)
            self.assertIs(namespace['A'], namespace['B'])
            self.assertIsNot(namespace['A']['a'], namespace['A']['b'])
            namespace['A']['a'].append(1)
            self.assertEqual(namespace['B']['a'], [1])
            self.assertEqual(namespace['B']['b'], [])

    def test_module_class_and_annotated_assignment(self):
        old = 'class Outer:\n    class Inner:\n        TABLE: dict = {"x": (1, 2)}\n'
        new = old.replace('{"x": (1, 2)}', '{**{"x": tuple((1, 2))}}')
        self.assertTrue(self.same(old, '# comment\n' + new))

    def test_typed_values_key_order_and_other_ast_edits_are_not_equal(self):
        old = 'TABLE = {"a": True, "b": 2}\n'
        for new in ['TABLE = dict({"a": 1, "b": 2})\n',
                    'TABLE = dict({"b": 2, "a": True})\n',
                    'TABLE = dict({"a": True, "b": 3})\n',
                    'OTHER = dict({"a": True, "b": 2})\n',
                    'TABLE = dict({"a": True, "b": 2})\nx = 1\n']:
            self.assertFalse(self.same(old, new))

    def test_external_aliases_calls_unpack_variants_and_loose_conversions_rejected(self):
        old = 'TABLE = {"a": (1, 2)}\n'
        for replacement in ['dict(other)', '{**other}', '{**{}, "a": (1, 2)}',
                            'dict({"a": [1, 2]})', 'dict(a=(1, 2))',
                            'dict({"a": tuple([1, 2])})', 'custom({"a": (1, 2)})',
                            '{**{"a": call()}}', '{**{"a": external}}']:
            self.assertFalse(self.same(old, 'TABLE = ' + replacement + '\n'))
        self.assertFalse(self.same('TABLE = (1, 2)\n', 'TABLE = tuple([1, 2])\n'))

    def test_builtin_shadowing_in_all_scopes_and_wildcard_imports_rejected(self):
        assignments = ['dict = custom\n', 'from external import dict\n', 'from external import dict as dict\n',
                       'def dict(value):\n    return value\n', 'class C:\n    dict = custom\n',
                       'def unrelated(dict):\n    return dict\n', 'from external import *\n']
        for prefix in assignments:
            old = prefix + 'TABLE = {"a": 1}\n'
            self.assertFalse(self.same(old, prefix + 'TABLE = dict({"a": 1})\n'))
        # The unpack opcode itself does not resolve the builtin name dict.
        self.assertTrue(self.same('dict = custom\nTABLE = {"a": 1}\n',
                                  'dict = custom\nTABLE = {**{"a": 1}}\n'))
        self.assertFalse(self.same('tuple = custom\nTABLE = (1, 2)\n',
                                   'tuple = custom\nTABLE = tuple((1, 2))\n'))

    def test_exception_and_match_string_bindings_shadow_builtins(self):
        prefixes = ['try:\n    pass\nexcept Exception as dict:\n    pass\n',
                    'match value:\n    case dict:\n        pass\n',
                    'match value:\n    case [*dict]:\n        pass\n',
                    'match value:\n    case {"key": item, **dict}:\n        pass\n']
        for prefix in prefixes:
            old = prefix + 'TABLE = {"a": 1}\n'
            self.assertFalse(self.same(old, prefix + 'TABLE = dict({"a": 1})\n'))
        old = 'try:\n    pass\nexcept Exception as tuple:\n    pass\nTABLE = (1, 2)\n'
        self.assertFalse(self.same(old, old.replace('TABLE = (1, 2)', 'TABLE = tuple((1, 2))')))

    def test_inherited_custom_prepare_can_poison_builtin_lookup(self):
        prefix = '''class Namespace(dict):
    def __missing__(self, key):
        if key == "dict":
            return lambda value: "poison"
        raise KeyError(key)
class Meta(type):
    @classmethod
    def __prepare__(meta, name, bases):
        return Namespace()
class Base(metaclass=Meta):
    pass
'''
        old = prefix + 'class C(Base):\n    TABLE = {"a": 1}\n'
        new = old.replace('TABLE = {"a": 1}', 'TABLE = dict({"a": 1})')
        before, after = {}, {}
        exec(old, before)
        exec(new, after)
        self.assertEqual(before['C'].TABLE, {'a': 1})
        self.assertEqual(after['C'].TABLE, 'poison')
        self.assertFalse(self.same(old, new))

    def test_class_with_bases_requires_matching_standard_namespace_fact(self):
        old = 'class C(Base):\n    TABLE = {"a": 1}\n'
        new = old.replace('{"a": 1}', 'dict({"a": 1})')
        facts = {'format': i.FACTS_FORMAT, 'source_commit': i.PINNED_COMMIT,
                 'source_root': '/unit', 'class_namespaces': {'sqlglot.example.C': {
                     'class_module': 'sqlglot.example', 'class_qualname': 'C',
                     'namespace_is_exact_dict': True, 'body_calls': 1}}}
        self.assertFalse(i.equivalent(REL, old, new, facts={}, src_root='/unit'))
        self.assertTrue(i.equivalent(REL, old, new, facts=facts, src_root='/unit'))
        self.assertFalse(i.equivalent(REL, old, new, facts=facts, src_root='/other'))
        facts['class_namespaces']['sqlglot.example.C']['namespace_is_exact_dict'] = False
        self.assertFalse(i.equivalent(REL, old, new, facts=facts, src_root='/unit'))

    def test_actual_namespace_observation_survives_replacement_metaclass_new(self):
        source = '''class Namespace(dict):
    def __missing__(self, key):
        if key == "dict":
            return lambda value: "poison"
        raise KeyError(key)
class Meta(type):
    @classmethod
    def __prepare__(meta, name, bases):
        return Namespace()
    def __new__(meta, name, bases, namespace):
        if name == "C":
            return type(name, (), dict(namespace))
        return super().__new__(meta, name, bases, namespace)
class Base(metaclass=Meta):
    pass
class C(Base):
    TABLE = {"a": 1}
'''
        filename = '/unit/sqlglot/example.py'
        node = next(node for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.ClassDef) and node.name == 'C')
        expected = {(filename, 'C', node.lineno): ('sqlglot.example', 'C')}
        with i._observe_namespaces(expected) as namespaces:
            namespace = {'__name__': 'sqlglot.example'}
            exec(compile(source, filename, 'exec'), namespace)
        self.assertIs(type(namespace['C']), type)  # Final type hides the original custom prepare.
        row = namespaces['sqlglot.example.C']
        self.assertFalse(row['namespace_is_exact_dict'])
        facts = {'format': i.FACTS_FORMAT, 'source_commit': i.PINNED_COMMIT,
                 'source_root': '/unit', 'class_namespaces': namespaces}
        self.assertFalse(i.equivalent(REL, source, source.replace('{"a": 1}', 'dict({"a": 1})'),
                                     facts=facts, src_root='/unit'))

    def test_function_definition_and_opaque_class_contexts_rejected(self):
        sources = ['def f():\n    TABLE = {"a": 1}\n    return TABLE\n',
                   'def f(value={"a": 1}):\n    return value\n',
                   '@decorate({"a": 1})\ndef f():\n    return 0\n',
                   '@decorate\nclass C:\n    TABLE = {"a": 1}\n',
                   'class C(metaclass=Meta):\n    TABLE = {"a": 1}\n']
        for old in sources:
            self.assertFalse(self.same(old, old.replace('{"a": 1}', 'dict({"a": 1})')))

    def test_opaque_observers_and_namespace_mutations_rejected(self):
        for observer in ('id(TABLE)', 'sys.getrefcount(TABLE)', 'globals()', 'vars(TABLE)',
                         'getattr(TABLE, name)', 'inspect.getsource(module)',
                         'function.__code__', 'getattr(function, "co_consts")',
                         'gc.get_objects()', 'sys.setprofile(callback)'):
            old = 'TABLE = {"a": 1}\nresult = ' + observer + '\n'
            self.assertFalse(self.same(old, old.replace('{"a": 1}', 'dict({"a": 1})')))
        self.assertFalse(self.same('import builtins\nTABLE = {"a": 1}\n',
                                   'import builtins\nTABLE = dict({"a": 1})\n'))
        self.assertFalse(self.same('__builtins__ = custom\nTABLE = {"a": 1}\n',
                                   '__builtins__ = custom\nTABLE = dict({"a": 1})\n'))
        self.assertFalse(i.equivalent('sqlglot/typing/example.py', 'TABLE = {}\n', 'TABLE = dict({})\n'))

    def test_literal_key_hashability_and_unsupported_targets(self):
        for old in ['TABLE = {[]: 1}\n', 'TABLE = {{1}: 1}\n', 'TABLE = {[1]}\n',
                    'self.TABLE = {"a": 1}\n', 'TABLE[0] = {"a": 1}\n']:
            self.assertFalse(self.same(old, old.replace('{"a": 1}', 'dict({"a": 1})')
                                       if '{"a": 1}' in old else old.replace(' = ', ' = dict(').rstrip() + ')\n'))

    def test_existing_independent_full_oracle_container_cases(self):
        base = Path('/baseline/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa')
        manifest = Path('/audit/reports/validation-new/manifest.json')
        if not base.is_dir() or not manifest.is_file():
            self.skipTest('development oracle corpus unavailable')
        cases = json.loads(manifest.read_text())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, 'facts.json')
            subprocess.run([sys.executable, '-c',
                            "import sys,import_equivalence as i;i.build_facts('/app/src',sys.argv[1])",
                            str(target)], check=True, capture_output=True,
                           env=dict(os.environ, PYTHONPATH=str(Path(i.__file__).parent)))
            facts = json.loads(target.read_text())
            for key in ('sqlglot.dialects.snowflake.Snowflake',
                        'sqlglot.dialects.snowflake.Snowflake.Tokenizer'):
                self.assertTrue(facts['class_namespaces'][key]['namespace_is_exact_dict'])
            for name in ('v01_dict_rebuild', 'v04_tuple_rebuild'):
                for rel, new in cases[name]['changes'].items():
                    self.assertTrue(i.equivalent(rel, (base/rel).read_text(), new,
                                                 facts=facts, src_root='/app/src'), name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
