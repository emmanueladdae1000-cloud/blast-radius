"""Independent regressions for runtime reasoning about comparison literal edits."""
import ast
import difflib
import operator
import os
import sys
import tempfile

sys.path.insert(0, '/audit')
import cmpprune as c


OPS = {ast.Eq: ('==', operator.eq), ast.NotEq: ('!=', operator.ne),
       ast.Lt: ('<', operator.lt), ast.LtE: ('<=', operator.le),
       ast.Gt: ('>', operator.gt), ast.GtE: ('>=', operator.ge),
       ast.Is: ('is', operator.is_), ast.IsNot: ('is not', operator.is_not)}
REL = 'sqlglot/example.py'


def encode(value):
    """Construct baseline facts independently of the production literal encoder."""
    if value is None: return ['none']
    if type(value) is bool: return ['bool', value]
    if type(value) is int: return ['int', str(value)]
    if type(value) is float: return ['float', value.hex()]
    if type(value) is str: return ['str', value]
    if type(value) is bytes: return ['bytes', value.hex()]
    raise AssertionError(type(value))


def make_trace(source, observations, test_ids):
    sites = {}
    nodes = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Compare)]
    assert len(nodes) == len(observations)
    for node, per_test in zip(nodes, observations):
        op, fn = OPS[type(node.ops[0])]
        facts, tests = [], {}
        for test, pairs in per_test.items():
            indices = []
            for left, right in pairs:
                indices.append(len(facts))
                facts.append([encode(left), encode(right), left is right, fn(left, right)])
            tests[str(test_ids.index(test))] = indices
        sites[f'{REL}|{node.lineno}|{node.col_offset}|{op}'] = {'facts': facts, 'tests': tests}
    return {'format': 'raw-baseline-comparisons-v1', 'test_ids': test_ids, 'sites': sites}


def diff(old, new):
    return f'diff --git a/{REL} b/{REL}\n' + ''.join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True), fromfile='a/' + REL, tofile='b/' + REL))


def safe(old, new, observations, test_ids, imports=(), mutate_trace=None):
    trace = make_trace(old, observations, test_ids)
    if mutate_trace:
        mutate_trace(trace)
    c._cache['operand_trace.json.gz'] = trace
    c._cache['import_lines.json.gz'] = {REL: list(imports)}
    with tempfile.TemporaryDirectory() as root:
        os.mkdir(os.path.join(root, 'sqlglot'))
        with open(os.path.join(root, REL), 'w', encoding='utf8') as f:
            f.write(old)
        return c.safe_tests(diff(old, new), root)


def actual_unchanged(old, new, calls):
    before, after = {}, {}
    exec(compile(old, '<old>', 'exec'), before)
    exec(compile(new, '<new>', 'exec'), after)
    return {test for test, values in calls.items()
            if values and all(before['decide'](value) is after['decide'](value)
                              for value in values)}


checked = 0


def check_scalar_case(old, new, old_literal, calls, expected, literal_left=False):
    global checked
    rows = {test: [(old_literal, value) if literal_left else (value, old_literal)
                   for value in values] for test, values in calls.items()}
    assert actual_unchanged(old, new, calls) == expected
    test_ids = list(calls) + ['unobserved']
    result = safe(old, new, [rows], test_ids)
    assert result == expected, (old, new, result, expected)
    checked += 1


old = 'def decide(value):\n    return value < 10\n'
calls = {'low': [0, 9], 'crossed': [10, 11], 'high': [12, 30], 'mixed': [0, 11], 'empty': []}
check_scalar_case(old, old.replace('< 10', '< 12'), 10, calls, {'low', 'high'})

old = 'def decide(value):\n    return 10 <= value\n'
check_scalar_case(old, old.replace('10 <=', '12 <='), 10,
                  {'low': [0, 9], 'crossed': [10, 11], 'high': [12, 20]},
                  {'low', 'high'}, literal_left=True)

old = 'def decide(value):\n    return value < 10\n'
check_scalar_case(old, old.replace('< 10', '<= 9'), 10,
                  {'integers': [0, 9, 10, 11], 'fraction': [9.5]}, {'integers'})

for before, after, literal, calls, expected in [
        ('-2', '-1', -2, {'low': [-3], 'crossed': [-2], 'high': [0]}, {'low', 'high'}),
        ('1.5', '2.5', 1.5, {'low': [1.0], 'crossed': [2.0], 'high': [3.0]}, {'low', 'high'}),
        ("'m'", "'n'", 'm', {'low': ['a'], 'crossed': ['m'], 'high': ['z']}, {'low', 'high'}),
        ("b'm'", "b'n'", b'm', {'low': [b'a'], 'crossed': [b'm'], 'high': [b'z']}, {'low', 'high'}),
]:
    old = f'def decide(value):\n    return value < {before}\n'
    check_scalar_case(old, old.replace('< ' + before, '< ' + after), literal, calls, expected)

old = 'def decide(value):\n    return value == True\n'
check_scalar_case(old, old.replace('== True', '== False'), True,
                  {'false': [False], 'true': [True], 'neither': [2, 3]}, {'neither'})

# Replacement None needs new aliasing facts even though identity operators are forbidden.
old = 'def decide(value):\n    return value == 1\n'
check_scalar_case(old, old.replace('== 1', '== None'), 1,
                  {'none': [None], 'one': [1], 'other': [2]}, {'other'})
old = 'def decide(value):\n    return value != None\n'
check_scalar_case(old, old.replace('!= None', '!= 1'), None,
                  {'none': [None], 'one': [1], 'other': [2]}, {'other'})

# Both operands and the operator can change together, if every observed result agrees.
old = 'def decide(value):\n    return 10 < 20\n'
assert safe(old, old.replace('10 < 20', '11 <= 19'), [{'same': [(10, 20)]}], ['same']) == {'same'}
assert safe(old, old.replace('10 < 20', '21 < 19'), [{'crossed': [(10, 20)]}], ['crossed']) == set()
assert c.compare_only_changes(old, old.replace('10 < 20', '11 <= 19')) is None
assert len(c.compare_only_changes(old, old.replace('<', '<='))) == 1
checked += 3

# Every changed comparison must independently preserve every observation for the test.
old = 'def decide(value):\n    lower = value > 0\n    return lower and value < 10\n'
new = old.replace('> 0', '> -1').replace('< 10', '< 12')
calls = {'middle': [5], 'lower_crossed': [0], 'upper_crossed': [11],
         'outside': [-2, 20], 'mixed': [5, 0]}
rows = [{test: [(value, bound) for value in values] for test, values in calls.items()}
        for bound in (0, 10)]
assert actual_unchanged(old, new, calls) == {'middle', 'outside'}
assert safe(old, new, rows, list(calls)) == {'middle', 'outside'}
checked += 1

# Reject identity-sensitive replacements, unsupported literals, and all other AST edits.
for before, after in [('is None', 'is False'), ('is not None', 'is not False'),
                      ('is None', '== False'), ('== None', 'is False')]:
    old = f'def decide(value):\n    return value {before}\n'
    assert c.compare_operand_changes(old, old.replace(before, after)) is None
    checked += 1

old = 'def decide(value):\n    return value < 10\n'
for expression in ['[12]', '(12,)', '{12}', "{'a': 12}", '12j', '...',
                   'threshold', 'int(12)', '6 + 6', "f'{12}'", str(2 ** 300)]:
    assert c.compare_operand_changes(old, old.replace('< 10', '< ' + expression)) is None, expression
    checked += 1
for unsupported_old, unsupported_new in [('1j', '2j'), ('(1,)', '(2,)'), ('[1]', '[2]')]:
    before = old.replace('< 10', '== ' + unsupported_old)
    assert c.compare_operand_changes(before, before.replace(unsupported_old, unsupported_new)) is None
    checked += 1

old = 'def decide(value):\n    global x\n    return value < 10\n'
new = old.replace('global x', 'global y').replace('< 10', '< 12')
assert c.compare_operand_changes(old, new) is None
old = 'def decide(value):\n    a, b = {}, {}\n    d = {**a, "k": b}\n    return value < 10\n'
new = old.replace('{**a, "k": b}', '{"k": a, **b}').replace('< 10', '< 12')
assert c.compare_operand_changes(old, new) is None
old = 'import math\ndef decide(value):\n    return value < 10\n'
assert c.compare_operand_changes(old, old.replace('import math', 'import os').replace('< 10', '< 12')) is None
assert c.compare_operand_changes(old, old.replace('value < 10', 'value < 10 < 12')) is None
checked += 4

old = 'def decide(value):\n    return value < 10\n'
new = old.replace('< 10', '< 12')
observations = [{'unchanged': [(0, 10)]}]
assert safe(old, new, observations, ['unchanged'], imports=[2]) == set()


def wrong_old_literal(trace):
    next(iter(trace['sites'].values()))['facts'][0][1] = ['int', '11']


def unsupported_fact(trace):
    next(iter(trace['sites'].values()))['facts'][0][0] = ['unknown']


def invalid_fact_index(trace):
    next(iter(trace['sites'].values()))['tests']['0'] = [999]


for mutation in (wrong_old_literal, unsupported_fact, invalid_fact_index):
    assert safe(old, new, observations, ['unchanged'], mutate_trace=mutation) == set()
checked += 4

print('Passed', checked, 'independent literal-edit cases: boundaries, both sides, scalar types, '
      'multiple comparisons, identity, compound AST changes, imports and incomplete facts.')
