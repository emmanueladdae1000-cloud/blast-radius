"""Independent development corpus and full-suite oracle, never used by the entry."""
import argparse
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

BASE = Path('/baseline/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa')
PROJECT = Path('/audit')
OUT = PROJECT / 'reports' / 'validation-new'


def source(rel):
    return (BASE / rel).read_text()


def replace_once(rel, before, after):
    text = source(rel)
    if before not in text:
        raise ValueError((rel, before))
    return text.replace(before, after, 1)


def assigned_value(rel, name, replacement):
    text = source(rel)
    matches = [n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
    if len(matches) != 1:
        raise ValueError((name, len(matches)))
    value = matches[0].value
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    start = offsets[value.lineno - 1] + value.col_offset
    end = offsets[value.end_lineno - 1] + value.end_col_offset
    return text[:start] + replacement(text[start:end]) + text[end:]


def rebuild_first_tuple(text):
    expression = ast.parse(text, mode='eval').body
    if not isinstance(expression, ast.Dict) or not isinstance(expression.values[0], ast.Tuple):
        raise ValueError('Expected a literal dictionary with a tuple value')
    expression.values[0] = ast.Call(func=ast.Name(id='tuple', ctx=ast.Load()),
                                    args=[expression.values[0]], keywords=[])
    return ast.unparse(expression)


def generate():
    snow = 'sqlglot/dialects/snowflake.py'
    mysql = 'sqlglot/dialects/mysql.py'
    tsql = 'sqlglot/dialects/tsql.py'
    click = 'sqlglot/generators/clickhouse.py'
    simpl = 'sqlglot/optimizer/simplify.py'
    fixture = 'tests/fixtures/optimizer/tpc-ds/tpc-ds.sql'
    title = replace_once(fixture, '-- TPC-DS 1\n', '-- TPC-DS 1 (independent validation)\n')
    changed_month = replace_once(simpl, 'd.month <= 3', 'd.month <= 4')
    cases = {
        'v00_config_comment': {'pyproject.toml': source('pyproject.toml') + '\n# Independent validation comment\n'},
        'v01_dict_rebuild': {snow: assigned_value(snow, 'NUMERIC_ESCAPES', lambda text: '{**(' + text + ')}')},
        'v02_enum_rebuild': {mysql: assigned_value(mysql, 'NORMALIZATION_STRATEGY', lambda _: 'NormalizationStrategy("CASE_SENSITIVE")')},
        'v03_bool_fold': {mysql: assigned_value(mysql, 'DPIPE_IS_STRING_CONCAT', lambda _: '(False or False)')},
        'v04_tuple_rebuild': {snow: assigned_value(snow, 'NUMERIC_ESCAPES', rebuild_first_tuple)},
        'v05_literal_length': {click: replace_once(click, 'len(expression.expressions) > 1', 'len(expression.expressions) > 2')},
        'v06_literal_month': {simpl: changed_month},
        'v07_normalization': {mysql: assigned_value(mysql, 'NORMALIZATION_STRATEGY', lambda _: 'NormalizationStrategy.LOWERCASE')},
        'v08_week_offset': {tsql: assigned_value(tsql, 'WEEK_OFFSET', lambda _: '0')},
        'v09_fixture_comment': {fixture: title},
        'v10_fixture_expected': {fixture: replace_once(fixture, 'AND "date_dim"."d_year" = 2001', 'AND "date_dim"."d_year" = 2003')},
        'v11_mixed_literal': {simpl: changed_month, fixture: title},
    }
    (OUT / 'commits').mkdir(parents=True, exist_ok=True)
    (OUT / 'oracle-logs').mkdir(exist_ok=True)
    shutil.copyfile(PROJECT / 'sample/tests.json', OUT / 'tests.json')
    manifest = {}
    for name, changes in cases.items():
        patch = ''
        for rel, after in changes.items():
            if rel.endswith('.py'):
                compile(after, rel, 'exec')
            patch += f'diff --git a/{rel} b/{rel}\n'
            patch += ''.join(difflib.unified_diff(source(rel).splitlines(True), after.splitlines(True),
                                                 fromfile='a/' + rel, tofile='b/' + rel))
        (OUT / 'commits' / (name + '.patch')).write_text(patch)
        manifest[name] = {'changes': changes, 'sha256': hashlib.sha256(patch.encode()).hexdigest()}
    (OUT / 'manifest.json').write_text(json.dumps(manifest))
    print('Generated', len(manifest), 'independent validation cases.', flush=True)
    return manifest


def oracle(names):
    manifest = json.loads((OUT / 'manifest.json').read_text())
    tests = json.loads((OUT / 'tests.json').read_text())['tests']
    num_of = {t['id']: t['number'] for t in tests}
    answers_path = OUT / 'answers.json'
    answers = json.loads(answers_path.read_text()) if answers_path.exists() else {'commits': {}}
    metadata_path = OUT / 'oracle-metadata.json'
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    for name in names:
        entry = manifest[name]
        if metadata.get(name, {}).get('sha256') == entry['sha256'] and name in answers['commits']:
            print('Cached full oracle:', name, flush=True)
            continue
        print('Starting full pinned pytest oracle:', name, flush=True)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='br-full-oracle-') as temp:
            checkout = Path(temp) / 'source'
            shutil.copytree(BASE, checkout)
            for rel, text in entry['changes'].items():
                (checkout / rel).write_text(text)
            report = Path(temp) / 'junit.xml'
            env = dict(os.environ, PYTHONPATH=str(checkout), SKIP_INTEGRATION='0',
                       PYTHONDONTWRITEBYTECODE='1', PYTHON_CPU_COUNT='2')
            env.pop('BR_RAWCMP_ENABLE', None)
            command = [sys.executable, '-m', 'pytest', '-q', '-n', '2', '--dist', 'worksteal',
                       '-p', 'no:cacheprovider', '--tb=short', '--timeout=150',
                       '-o', 'console_output_style=count', '--junitxml=' + str(report), 'tests']
            result = subprocess.run(command, cwd=checkout, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=420)
            (OUT / 'oracle-logs' / (name + '.log')).write_bytes(result.stdout)
            if result.returncode not in (0, 1) or not report.exists():
                raise RuntimeError((name, result.returncode, result.stdout[-3000:]))
            failing, unknown = set(), []
            for case in ET.parse(report).iter('testcase'):
                if case.find('failure') is None and case.find('error') is None:
                    continue
                cls = case.get('classname', '')
                module, _, class_name = cls.rpartition('.')
                method = re.match(r'[A-Za-z_]\w*', case.get('name', ''))
                if not method:
                    unknown.append(case.attrib)
                    continue
                nodeid = module.replace('.', '/') + '.py::' + class_name + '::' + method.group()
                if nodeid not in num_of:
                    unknown.append({'mapped': nodeid, **case.attrib})
                else:
                    failing.add(num_of[nodeid])
            if unknown:
                raise RuntimeError(('Unmapped oracle failures', name, unknown[:8]))
            if result.returncode == 1 and not failing:
                raise RuntimeError(('Failed pytest without mapped failing test', name, result.stdout[-3000:]))
            answers['commits'][name] = {'failing': sorted(failing), 'kind': 'independent-validation'}
            metadata[name] = {'sha256': entry['sha256'], 'seconds': round(time.monotonic() - started, 3),
                              'full_suite': True, 'source_commit': 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'}
            answers_path.write_text(json.dumps(answers))
            metadata_path.write_text(json.dumps(metadata, indent=2))
            print(name, 'full oracle failing tests:', len(failing), 'elapsed:', metadata[name]['seconds'], flush=True)
    if set(answers['commits']) == set(manifest):
        print('All independent cases have complete full-suite oracle labels.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--generate', action='store_true')
    parser.add_argument('--oracle', nargs='*')
    args = parser.parse_args()
    if args.generate:
        generate()
    if args.oracle is not None:
        names = args.oracle or list(json.loads((OUT / 'manifest.json').read_text()))
        oracle(names)
