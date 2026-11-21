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

Two run-time checks refine that (both run sqlglot inside the container, which the
rules allow and charge as CPU):

- **Import check** (`verify.import_check`): for any edit to import-time code, the
  patched module is imported in a throwaway child (served from the patch, not the
  disk). If importing it raises, every test is selected.
- **Fresh-import verification** (`verify.run_fresh`): for small candidate sets after
  an edit outside function bodies (class attributes, module constants), a child
  imports the patched modules from the patch text, so class bodies and metaclasses
  run again, then runs the candidate tests and keeps the failing ones. If the import
  itself raises, every test is selected.
- **Candidate verification** (`verify.run`): when every real edit sits inside function
  bodies that never run at import time and the candidate tests are few and cheap, a
  forked child swaps the patched function bodies into the live functions
  (`func.__code__`, so dispatch tables and closures see them), runs the candidate
  tests with unittest, and keeps only the ones that fail. It stops at the first
  failure and keeps the remaining candidates unverified. A crash, a timeout or any
  change that cannot be swapped keeps the full candidate set. Process pools are
  replaced by an in-process executor that pickles arguments and results like a real
  pool, so the patched code is what runs.

- **TPC executor tests** (`test_execute_tpcds` 21 s, `test_execute_tpch`,
  `test_optimized_tpch`): they run end-to-end over the same optimizer and executor
  code as many cheap tests. When they are candidates, the cheapest other candidates
  (at least 5, up to 2.5 s of recorded time) run here first as detectors. If any
  detector fails, every candidate is kept. If all pass, the TPC executor tests are
  dropped and the remaining candidates are kept. On mutants of the pinned commit
  (`tools/calib.py`) and the 30 samples, every break the TPC executor tests caught
  also broke many cheap candidates. Property tests (memory leak, AST node limit) are
  never dropped this way.

Workers are packed longest-first for every worker count 1..8, keeping the cheapest.

## Rebuilding the map

```
git clone https://github.com/tobymao/sqlglot && cd sqlglot && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
pip install -r ../requirements.lock      # the labeling environment
SKIP_INTEGRATION=0 TESTS_JSON=../sample/tests.json python ../tools/forktrace.py ../ft
cd .. && SQLGLOT_SRC=sqlglot python tools/mkmap.py ft map sample/tests.json
tar -czf src.tar.gz -C sqlglot $(cd sqlglot && git ls-files | grep -E '^(sqlglot|tests)/')
```
