# Validation

This is local validation. No official hidden practice/final result has been obtained.

## Official 30-commit public sample

Exact competition sandbox flags: linux/amd64, no network, 2 CPUs, 4 GB memory with
no swap, 256 processes, read-only root, 64 MB /tmp, nobody user, dropped capabilities.
Only patches and tests.json were mounted into the selector. The unchanged official
scorer checked its output separately, with sample answers unavailable to the selector.
Measured CPU includes selector descendants and the small measurement wrapper.

| Run | Simulated seconds | CPU seconds | Total seconds | Misses |
| --- | ---: | ---: | ---: | ---: |
| 1 | 106.125 | 5.546145 | 111.671145 | 0 |
| 2 | 106.125 | 5.567669 | 111.692669 | 0 |
| 3 | 106.125 | 6.389825 | 112.514825 | 0 |

Median total 111.692669 seconds; rounded by the scoring rule: **111.7 seconds**.

## Existing 15-case local validation set (evalA)

One run: simulated 104.408 + CPU 10.439916 = total 114.847916 seconds, zero misses
against that dataset's labels. This is a different local dataset, not the hidden
practice data, and only one run was measured.

## Additional accuracy checks

* 4,476 independent operand/operator cases, including primitive identity, NaN,
  scalar/enum subtypes, overloaded and mutable comparison methods.
* Compound changes involving global declarations and dictionary unpacking are
  rejected by comparison-only reasoning; absent and import-time observations retain tests.
* Four fresh focused regressions were executed against the real pinned pytest tests:
  parser node-count threshold, mixed parser/fixture edit, MySQL normalization class
  constant, and expected-output-only TPC fixture edit. Every independently confirmed
  failing test is retained. These are minimum required sets, not complete failure sets.
* The raw-observation generator ran the unmodified full checkout under two hash
  seeds: 1618 tests passed and 1 skipped in each run; the expensive max-nodes and
  memory-leak tests were excluded from profiling and cannot be removed by its facts.
* All 314 files in src.tar.gz match the pinned upstream checkout byte-for-byte.
* score.py is unchanged from the supplied official starter. Current runtime files
  and maps match the tested Docker image hashes.

## Limits

Hardware and unseen patches can change scores. These measurements establish a
sub-120 public-sample result; an official submission is needed to confirm the
requested threshold on the hidden practice/final datasets.