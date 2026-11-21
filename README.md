# RWX Blast Radius entry

Select tests for independent patches against SQLGlot commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, then pack them across one to eight workers.

## Run on Windows

Docker Desktop must be running with Linux containers. Put the official starter sample/ directory in this folder first; extract the starter into a separate folder so it does not overwrite the entry. No host Python or make is needed:

```powershell
cd C:\Users\emman\OneDrive\Desktop\codex\rwx-under120
.\test_local.ps1
```

This builds a uniquely named image, runs the official sample three times with the
competition sandbox limits, and checks each output with the original scorer.
Reports include simulated test seconds, all selector/child CPU seconds, and total.
Sample data is mounted separately; sample answers are never mounted into the selector.
The public sample result does not establish the hidden practice or final result.

## Submission

Use a public GitHub repository and the exact commit hash, with `Dockerfile` at its
root. The committed entry must remain under 20 MB. Submit the hash explicitly rather
than relying on whichever commit is currently on the default branch.

## Selection

* Baseline maps record per-test line coverage, imports, class/attribute usage and
  fixture reads from the pinned source. Unknown or unsupported changes fall back
  conservatively.
* Pure comparison-operator edits can be refined using `operand_trace.json.gz`.
  This contains **raw baseline operands, type/descriptor facts, aliasing and the
  original comparison result**. It contains no alternate-operator results,
  precomputed patch answers, or operator-edit-to-failing-test tables. At runtime,
  the actual patch is inspected and its operator is evaluated over these facts.
* Raw tracing supports exact immutable scalars, scalar subtypes whose six rich
  comparison descriptors are unchanged, and identity-equality objects. Unsupported,
  incomplete, oversized, failed or asynchronous observations retain the test.
  Observations are combined across two baseline hash seeds.
* Eligible small candidate sets are verified against patched code in isolated
  children. Passing candidates may be removed; failed and unrun candidates stay.
  Partial verification retains every expensive test that was not actually run.
* TPC fixture changes are analyzed by their input/expected-output dependencies.
  Code probes use the complete affected query set, with conservative fallback when
  the set cannot be checked economically or a check fails or times out.
* The original node-limit property test stays selected when affected. A shrunken
  proxy is not used to justify removing it.
* Worker packing tries every count from 1 through 8 using longest-first balancing,
  minimizing the largest recorded worker load plus two seconds per worker.

Dynamic baseline maps reflect observed executions. Nondeterminism or execution
paths not represented in those observations can still affect unseen cases. Only
an official submission can establish an official score.

## Reproduce the raw observations

`tools/rawcmp_plugin.py` instruments only the **unmodified** pinned SQLGlot checkout.
It executes each original comparison once and records its inputs and original
result. Real process pools remain process pools; spawned workers load the recorder
before SQLGlot, and worker observations are merged. Tests using untracked threads
or subprocesses are excluded from deductions. The generator never evaluates
alternate comparisons and never applies mutations.

`Dockerfile.profile` installs the pinned labeling dependencies and retrieves the
full official GitHub archive at the exact pinned commit. `rebuild_observations.ps1`
runs the two baseline seeds and merges complete observations from passing tests.
Challenge sample inputs/answers and profiling logs are excluded from the production image. Pinned upstream SQLGlot test fixtures remain in src.tar.gz.

## Prior code and dependencies

This builds on this team's existing v5.3 selector and baseline coverage-map tools.
SQLGlot and its test/fixture sources in `src.tar.gz` are the pinned MIT-licensed
upstream sources. Runtime libraries are DuckDB, NumPy, pandas, python-dateutil,
pytz and tzdata at the versions pinned in `Dockerfile`/`requirements.lock`.
`THIRD_PARTY_NOTICES.md` preserves SQLGlot's license. This project is MIT licensed.