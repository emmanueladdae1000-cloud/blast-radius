"""Generate development-only default changes and label them with full pytest.

Inputs are raw baseline bindings, never practice/hidden patch results. The data
directory and every oracle answer are excluded from the production image.
"""
import argparse
import ast
import difflib
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import default_refinement
import selector
import build_validation

BASE = build_validation.BASE
OUT = ROOT / 'reports' / 'default-validation'


def generate():
    with gzip.open(ROOT / 'default_bindings.json.gz', 'rt') as stream:
        trace = json.load(stream)
    tests = json.loads((ROOT / 'sample/tests.json').read_text())['tests']
    ms_of = {t['number']: round(t['seconds'] * 1000) for t in tests}
    num_of = {t['id']: t['number'] for t in tests}
    maps = selector.Maps()
    selector.SRC = str(BASE)
    engine = selector.Selector(maps)
    ranked = []
    for site, facts in trace['sites'].items():
        opposite = not facts['default']
        if not default_refinement._opposite_row(facts['imports'], opposite, False):
            continue
        safe = {trace['test_ids'][int(i)] for i, row in facts['tests'].items()
                if default_refinement._opposite_row(row, opposite, True)}
        if len(safe) < 2:
            continue
        ranked.append((1490 in {num_of[t] for t in safe}, len(safe), site))
    rows = []
    for _, _, site in sorted(ranked, reverse=True)[:80]:
        rel, line, col, parameter = site.split('|')
        original = (BASE / rel).read_text()
        tree = ast.parse(original)
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                        and n.lineno == int(line) and n.col_offset == int(col))
        value = next(value for name, value in default_refinement._slots(function)
                     if name == parameter)
        lines = original.splitlines(True)
        row = lines[value.lineno - 1]
        lines[value.lineno - 1] = row[:value.col_offset] + str(not value.value) + row[value.end_col_offset:]
        changed = ''.join(lines)
        safe = default_refinement.safe_tests(rel, original, changed, trace)
        if not safe:
            continue
        patch = 'diff --git a/{0} b/{0}\n'.format(rel) + ''.join(difflib.unified_diff(
            original.splitlines(True), changed.splitlines(True), fromfile='a/' + rel, tofile='b/' + rel))
        with mock.patch.object(default_refinement, 'safe_tests', return_value=set()):
            selected, _ = engine.select(patch)
        before = {num_of[maps.ids[i]] for i in selected}
        after = {n for n in before if tests[n]['id'] not in safe}
        saving = selector.pack_cost(before, ms_of) - selector.pack_cost(after, ms_of)
        rows.append((saving, site, rel, changed, patch, len(before), len(after)))
    rows.sort(reverse=True)
    chosen, seen = [], set()
    for row in rows:
        if row[2] not in seen:
            chosen.append(row)
            seen.add(row[2])
        if len(chosen) == 4:
            break
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'commits').mkdir(exist_ok=True)
    (OUT / 'oracle-logs').mkdir(exist_ok=True)
    shutil.copyfile(ROOT / 'sample/tests.json', OUT / 'tests.json')
    manifest, summary = {}, []
    for i, (saving, site, rel, changed, patch, before, after) in enumerate(chosen):
        name = f'default_{i:02d}'
        (OUT / 'commits' / (name + '.patch')).write_text(patch)
        manifest[name] = {'changes': {rel: changed}, 'sha256': hashlib.sha256(patch.encode()).hexdigest()}
        summary.append({'name': name, 'site': site, 'before': before, 'after': after,
                        'saved_recorded_ms': saving})
    (OUT / 'manifest.json').write_text(json.dumps(manifest))
    (OUT / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--generate', action='store_true')
    parser.add_argument('--oracle', action='store_true')
    args = parser.parse_args()
    if args.generate:
        generate()
    if args.oracle:
        build_validation.OUT = OUT
        build_validation.oracle(list(json.loads((OUT / 'manifest.json').read_text())))
