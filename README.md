# Blast Radius entry

Picks, for each sqlglot patch, the tests that can observe the change, then splits
them over workers to minimise `busiest worker + 2 s per worker`.

## How it decides

Everything below is built once from the pinned commit `eb11aec` (allowed: "maps built
from the pinned commit"). No patch, kind of edit, or answer is looked up.

1. **Per-test line coverage** (`tools/forktrace.py`). The parent imports all of sqlglot
   once, runs each test class's `setUpClass`, then forks one child per test, so every
   test's coverage is exact even with caches and lazy imports. Lines run at import
   time, during `setUpClass`, or while importing a test module are kept separately
   and attributed to every test they can affect. Process-pool work runs in process
   (with a pickle round trip, as a real pool does) so it is attributed too.
2. **Class usage**: which sqlglot classes (expressions, dialects, tokenizers, parsers,
   generators) each test instantiates or looks up, expanded over the MRO.
3. **Readers**: for every attribute/constant name, the tests that execute a line that
   reads it, and whether it is also read at import time.
4. **Fixture reads**: which tests open each file under `tests/`.

At run time (`selector.py`), for every changed file in a patch:

- edits that only touch comments or blank lines (outside strings) select nothing;
- the patched file is rebuilt from the pinned source and compiled; a syntax error
  selects every test;
- each edited line is mapped to its enclosing statement (AST): statements inside
  functions use the coverage of that statement; def headers use the function body's
  coverage; edits inside a lambda body use that line's runtime coverage;
- import-time statements (class attributes, module constants) select the users of the
  class intersected with the readers of the assigned name, falling back to all users
  of the class when the name is also consumed at import time;
- fixture edits select the tests that read the fixture;
- added/deleted/renamed modules and root config files select everything.

Workers are packed longest-first for every worker count 1..8, keeping the cheapest.

## Rebuilding the map

```
git clone https://github.com/tobymao/sqlglot && cd sqlglot && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
pip install -r ../requirements.lock      # the labeling environment
SKIP_INTEGRATION=0 TESTS_JSON=../sample/tests.json python ../tools/forktrace.py ../ft
cd .. && SQLGLOT_SRC=sqlglot python tools/mkmap.py ft map sample/tests.json
tar -czf src.tar.gz -C sqlglot $(cd sqlglot && git ls-files '*.py' | grep -E '^(sqlglot|tests)/')
```
