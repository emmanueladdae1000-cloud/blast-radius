# RWX Blast Radius entry

Select tests for independent patches against SQLGlot commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, then pack them across one to eight workers.

## Run on Windows

Docker Desktop must be running with Linux containers. Put the official starter sample/ directory in this folder first; extract the starter into a separate folder so it does not overwrite the entry. No host Python or make is needed:

```powershell
cd C:\Users\emman\OneDrive\Desktop\codex\rwx-under110
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
* Comparison-operator and exact scalar-operand edits can be refined using `operand_trace.json.gz`.
  This contains **raw baseline operands, type/descriptor facts, aliasing and the
  original comparison result**. It contains no alternate-operator results,
  precomputed patch answers, or operator-edit-to-failing-test tables. At runtime,
  the actual patch is inspected and its operator is evaluated over these facts.
* Raw tracing supports exact immutable scalars, scalar subtypes whose six rich
  comparison descriptors are unchanged, and identity-equality objects. Unsupported,
  incomplete, oversized, failed or asynchronous observations retain the test.
  Observations are combined across two baseline hash seeds.
* Economical candidate subsets are verified against patched code in isolated
  children. Passing candidates may be removed; failed and unrun candidates stay.
  Partial verification retains every failed, skipped and unrun method. Choices use baseline per-test CPU and class-setup costs, remapped by test ID, against actual packed output savings.
* TPC fixture changes are analyzed by their input/expected-output dependencies.
  Code probes use the complete affected query set, with conservative fallback when
  the set cannot be checked economically or a check fails or times out.
* Complete baseline observations include the real node-limit and generator-memory property tests. Unknown/overflow observations stay conservative. A shrunken proxy is not used to justify removing any property test.
* Worker packing tries every count from 1 through 8 using longest-first balancing,
  minimizing the largest recorded worker load plus two seconds per worker.
* For a source file changing only comparisons, exact compiled
  comparison positions identify the baseline execution lines that can observe
  the change. This avoids inheriting coverage from an entire multiline condition
  when short-circuit evaluation never reaches its changed comparison. Missing
  bytecode anchors, import execution and code-object observers fall back.
* Verification children have independent CPU budgets. A terminated job retains
  every original candidate, including methods that passed before termination.
* Exact scalar-literal changes can use the executed LOAD_CONST/RETURN_CONST
  location, with the same import and introspection safeguards.
* Boolean-default edits use raw baseline parameter bindings. A complete observed
  binding to the opposite exact Boolean proves that invocation supplied its own
  argument. Shared fixture observations constrain every affected test.
* Fresh verification starts a clean Python worker, avoiding the selector's large
  inherited heap. It still runs the complete original test methods.

`default_bindings.json.gz` contains original bindings from two complete pinned
pytest runs, including class/module fixture scope and reused worker attribution.
It contains no alternate defaults or mutation outcomes. The comparison artifact
also comes from two complete runs with shared fixture scope, opaque process-pool
argument transport and worker-final serialization correctly attributed.

Dependency proofs refine each file's contribution before unioning source, test,
fixture and configuration candidates. If a test is outside that union, no file
can introduce its first observable difference on the original execution. Actual
runtime verification always applies every changed Python source, including a
file whose own dependency contribution is empty. Fixture/configuration changes
continue to disable source-only verification.

Narrow import-time container identity wrappers can be proved unchanged using
strict AST canonicalization. Class bodies require build-time observations that
their actual namespace is an exact builtin dict. The facts builder observes
class-body frames without invoking namespace methods or metaclasses. This
separate import-identity proof currently requires a single-file patch.

Dynamic baseline maps reflect observed executions. Nondeterminism or execution
paths not represented in those observations can still affect unseen cases. Only
an official submission can establish an official score.

## Additional baseline source reasoning

Semantically unchanged root configurations are recognized from the exact pinned
root files included in the source bundle. Python no-op checks preserve executable
control flow and exact constant types, and source encoding changes fall back to
all tests. TOML comparisons preserve types and key order.

A single literal NUMERIC_ESCAPES value edit can use a narrowly audited dictionary
value-flow dependency: unchanged ordered keys/nonemptiness preserve constructor,
metaclass and generator shape uses. Values first influence behavior at two
TokenizerCore .get spans, whose existing baseline coverage supplies candidates.
Four-integer tuple values and every other AST field are checked; unknown, native,
reflective or compound flows fall back. The source-flow audit runs during image
build, and its read-only artifact contains only source dependency metadata.

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

`tools/default_plugin.py` records and merges Boolean parameter bindings, with
fixture scope and process-pool attribution regressions. `tools/validate_defaults.py`
creates development-only changes and obtains labels from the complete pinned
pytest suite; its generated data and oracle answers stay outside the image.

## Prior code and dependencies

This builds on this team's existing v5.3 selector and baseline coverage-map tools.
SQLGlot and its test/fixture sources in `src.tar.gz` are the pinned MIT-licensed
upstream sources. Baseline test CPU facts are stored in test_costs.json; no patch identities or failure outcomes are stored there. Runtime libraries are DuckDB, NumPy, pandas, python-dateutil,
pytz and tzdata at the versions pinned in `Dockerfile`/`requirements.lock`.
`THIRD_PARTY_NOTICES.md` preserves SQLGlot's license. This project is MIT licensed.
