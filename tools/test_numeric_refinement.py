"""Independent shape, data-flow and coverage checks; no patch-outcome labels."""
import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numeric_refinement as n


OLD = 'class Tokenizer:\n    NUMERIC_ESCAPES = {"u": (16, 4, 4, 65535), "0": (8, 1, 3, 255)}\n'
NEW = OLD.replace('(16, 4, 4, 65535)', '(17, 4, 4, 65535)')
CORE = '''class TokenizerCore:
    __slots__ = ("numeric_escapes",)
    def __init__(self, numeric_escapes):
        self.numeric_escapes = numeric_escapes
    def scan(self, peek):
        return self.numeric_escapes.get(peek)
    def scan_bytes(self, peek):
        return self.numeric_escapes.get(
            peek
        )
    def extract(self, backslash):
        numeric_escapes = self.numeric_escapes
        if backslash and numeric_escapes:
            return self.scan("u")
'''
TOKENS = '''class Tokenizer:
    def _init_core(self):
        return TokenizerCore(numeric_escapes=self.NUMERIC_ESCAPES)
'''
SHAPE = '''class Generator:
    def escape(self):
        flag = bool(self.dialect.tokenizer_class.NUMERIC_ESCAPES or self.other)
        return flag, "x" in self.dialect.tokenizer_class.NUMERIC_ESCAPES
def initialize(klass):
    numeric_escapes = klass.tokenizer_class.NUMERIC_ESCAPES
    if "x" in numeric_escapes:
        return "hex"
    elif "0" in numeric_escapes:
        return "octal"
'''


class Maps:
    def __init__(self, data):
        self.data = data
    def src(self, rel):
        assert rel == 'sqlglot/tokenizer_core.py'
        return self.data


class NumericTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.audit_path = self.root / 'numeric_flow.json'
        audit_path = mock.patch.object(n, 'AUDIT_PATH', self.audit_path)
        audit_path.start()
        self.addCleanup(audit_path.stop)
        package = self.root / 'sqlglot'
        package.mkdir()
        (package / 'tokenizer_core.py').write_text(CORE)
        (package / 'tokens.py').write_text(TOKENS)
        (package / 'generator.py').write_text(SHAPE)
        calls = [node for node in ast.walk(ast.parse(CORE)) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'get']
        self.lines = {line for call in calls for line in range(call.lineno, call.end_lineno + 1)}
        # Constructor/alias readers are deliberately much broader than consumers.
        coverage = {line: 0 for line in range(1, len(CORE.splitlines()) + 1)}
        for line in self.lines:
            coverage[line] = 1 if line == calls[0].lineno else 2
        self.data = {'cov': coverage, 'sets': [list(range(100)), [2, 4], [3, 4, 5]], 'imp': [4, 12]}
        self.maps = Maps(self.data)

    def affected(self, old=OLD, new=NEW):
        return n.affected_tests(old, new, self.maps, str(self.root))

    def test_only_get_consumers_are_selected_including_multiline_calls(self):
        self.assertEqual(self.affected(), {2, 3, 4, 5})
        self.assertEqual(n._consumer_lines(str(self.root)),
                         ('sqlglot/tokenizer_core.py', frozenset(self.lines)))
        self.data['cov'] = {str(k): v for k, v in self.data['cov'].items()}
        self.assertEqual(self.affected(), {2, 3, 4, 5})

    def test_same_keys_keep_initializer_membership_and_truthiness(self):
        old, new = {}, {}
        exec(OLD, old)
        exec(NEW, new)
        a, b = old['Tokenizer'].NUMERIC_ESCAPES, new['Tokenizer'].NUMERIC_ESCAPES
        self.assertEqual(list(a), list(b))
        self.assertEqual(bool(a), bool(b))
        self.assertEqual([key in a for key in ('x', '0', 'u')],
                         [key in b for key in ('x', '0', 'u')])
        self.assertNotEqual(a.get('u'), b.get('u'))
        self.assertEqual(self.affected(new='# comment\n' + NEW), {2, 3, 4, 5})
        signed = NEW.replace('(17, 4, 4, 65535)', '(-17, 4, 4, 65535)')
        self.assertEqual(self.affected(new=signed), {2, 3, 4, 5})

    def test_type_order_key_shape_and_nonliteral_changes_fail_closed(self):
        alternatives = [
            '{"0": (8, 1, 3, 255), "u": (17, 4, 4, 65535)}',
            '{"u": (17, 4, 4, 65535)}',
            '{"u": (17, 4, 4, 65535), "0": (8, 1, 3, 255), "x": (16, 2, 2, 255)}',
            '{"U": (17, 4, 4, 65535), "0": (8, 1, 3, 255)}',
            '{1: (17, 4, 4, 65535), "0": (8, 1, 3, 255)}',
            '{"u": (True, 4, 4, 65535), "0": (8, 1, 3, 255)}',
            '{"u": (17.0, 4, 4, 65535), "0": (8, 1, 3, 255)}',
            '{"u": [17, 4, 4, 65535], "0": (8, 1, 3, 255)}',
            '{"u": (17, 4, 65535), "0": (8, 1, 3, 255)}',
            '{"u": (17, 4, 4, 65535), "u": (16, 4, 4, 65535), "0": (8, 1, 3, 255)}',
            '{"u": (int(17), 4, 4, 65535), "0": (8, 1, 3, 255)}',
            '{"u": (16 + 1, 4, 4, 65535), "0": (8, 1, 3, 255)}',
            '{**other, "u": (17, 4, 4, 65535), "0": (8, 1, 3, 255)}',
            '{}',
        ]
        for table in alternatives:
            with self.subTest(table=table):
                self.assertIsNone(self.affected(new='class Tokenizer:\n    NUMERIC_ESCAPES = ' + table + '\n'))
        self.assertIsNone(self.affected(old=OLD.replace('(16,', '(True,')))

    def test_exactly_one_class_assignment_and_no_compound_edit(self):
        self.assertIsNone(self.affected(new=OLD))
        self.assertIsNone(self.affected(old=OLD + 'x = 1\n', new=NEW + 'x = 2\n'))
        self.assertIsNone(self.affected(old=OLD + OLD.replace('Tokenizer', 'Other'),
                                        new=NEW + NEW.replace('Tokenizer', 'Other')))
        self.assertIsNone(self.affected(old=OLD.split('\n', 1)[1].strip() + '\n',
                                        new=NEW.split('\n', 1)[1].strip() + '\n'))
        self.assertIsNone(self.affected(old='class Tokenizer:\n    NUMERIC_ESCAPES: dict\n'))
        self.assertIsNone(self.affected(new=NEW + 'not valid !!!'))

    def test_import_consumers_or_missing_coverage_map_are_unsupported(self):
        self.data['imp'].append(min(self.lines))
        self.assertIsNone(self.affected())
        self.maps.data = None
        self.assertIsNone(self.affected())

    def test_unknown_value_consumers_reflection_native_and_preprocessing_rejected(self):
        alternatives = [
            ('generator.py', SHAPE + '\ndef opaque(x):\n    return x.NUMERIC_ESCAPES["u"]\n'),
            ('generator.py', SHAPE + '\ndef opaque(x):\n    return getattr(x, "NUMERIC_ESCAPES")\n'),
            ('tokenizer_core.py', CORE + '\ndef opaque(x):\n    return getattr(x, "numeric_escapes")\n'),
            ('tokenizer_core.py', CORE.replace('return self.numeric_escapes.get(peek)',
                                              'return opaque(self.numeric_escapes)')),
            ('tokenizer_core.py', CORE.replace('self.numeric_escapes = numeric_escapes',
                                              'self.numeric_escapes = dict(numeric_escapes)')),
            ('tokenizer_core.py', CORE.replace('if backslash and numeric_escapes:',
                                              'if opaque(numeric_escapes):')),
        ]
        for filename, source in alternatives:
            with self.subTest(filename=filename, source=source):
                path = self.root / 'sqlglot' / filename
                original = path.read_text()
                path.write_text(source)
                n._consumer_lines.cache_clear()
                self.assertIsNone(self.affected())
                path.write_text(original)
        (self.root / 'sqlglot' / 'tokenizer_core.native.so').touch()
        n._consumer_lines.cache_clear()
        self.assertIsNone(self.affected())

    def test_build_cache_uses_direct_audit_and_runtime_avoids_reauditing(self):
        self.audit_path.write_text('{"valid":false}')
        with mock.patch.object(n, '_consumer_lines', side_effect=AssertionError('old runtime cache used')):
            data = n.build_audit(str(self.root))
        self.assertTrue(data['valid'])
        self.assertEqual(data['source_commit'], n.PINNED_COMMIT)
        self.assertEqual(data['source_root'], str(self.root.resolve()))
        self.assertEqual(data['consumer_lines'], sorted(self.lines))
        n._consumer_lines.cache_clear()
        with mock.patch.object(n, '_audit_consumer_lines', side_effect=AssertionError('unneeded live audit')):
            self.assertEqual(self.affected(), {2, 3, 4, 5})

    def test_invalid_cache_fails_closed_without_live_fallback(self):
        valid = n.build_audit(str(self.root))
        alternatives = [None, {}, dict(valid, valid=False), dict(valid, valid=1),
                        dict(valid, format='unknown'), dict(valid, source_commit='other'),
                        dict(valid, source_root='/other'), dict(valid, consumer_file='../escape.py'),
                        dict(valid, consumer_lines=[]), dict(valid, consumer_lines=[True]),
                        dict(valid, consumer_lines=[-1]), dict(valid, consumer_lines=[8, 6]),
                        dict(valid, consumer_lines=[6, 6])]
        for data in alternatives:
            with self.subTest(data=data):
                self.audit_path.write_text(json.dumps(data))
                n._consumer_lines.cache_clear()
                with mock.patch.object(n, '_audit_consumer_lines', side_effect=AssertionError('invalid cache reused')):
                    self.assertIsNone(self.affected())
        self.audit_path.write_text('not JSON')
        n._consumer_lines.cache_clear()
        self.assertIsNone(self.affected())

    def test_installed_native_distribution_disables_even_valid_cache(self):
        n.build_audit(str(self.root))
        n._consumer_lines.cache_clear()
        with mock.patch.object(n, '_native_distribution_present', return_value=True):
            self.assertIsNone(self.affected())
            self.assertFalse(n.build_audit(str(self.root))['valid'])
        n._native_distribution_present.cache_clear()
        try:
            with mock.patch.object(n.importlib.metadata, 'distribution', return_value=object()):
                self.assertTrue(n._native_distribution_present())
            n._native_distribution_present.cache_clear()
            with mock.patch.object(n.importlib.metadata, 'distribution',
                                   side_effect=n.importlib.metadata.PackageNotFoundError):
                self.assertFalse(n._native_distribution_present())
        finally:
            n._native_distribution_present.cache_clear()

    def test_build_cli_writes_beside_module_and_ignores_prior_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory, 'numeric_refinement.py')
            shutil.copyfile(n.__file__, module)
            artifact = module.with_name('numeric_flow.json')
            artifact.write_text('{"valid":false}')
            result = subprocess.run([sys.executable, str(module), '--build-audit', str(self.root)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(artifact.read_text())
            self.assertTrue(data['valid'])
            self.assertEqual(data['consumer_lines'], sorted(self.lines))

    def test_actual_pinned_sources_and_existing_coverage(self):
        import selector
        root = '/app/src'
        if not Path(root, 'sqlglot/tokenizer_core.py').is_file():
            self.skipTest('full pinned profiler checkout is unavailable')
        actual = n.affected_tests(OLD, NEW, selector.Maps(), root)
        self.assertIsNotNone(actual)
        # Derive the expectation independently from direct AST call spans.
        tree = ast.parse(Path(root, 'sqlglot/tokenizer_core.py').read_bytes())
        lines = {line for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'get'
                 and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == 'numeric_escapes'
                 for line in range(node.lineno, node.end_lineno + 1)}
        data = selector.Maps().src('sqlglot/tokenizer_core.py')
        expected = set().union(*(set(data['sets'][data['cov'][line]])
                                 for line in lines if line in data['cov']))
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 23)


if __name__ == '__main__':
    unittest.main(verbosity=2)
