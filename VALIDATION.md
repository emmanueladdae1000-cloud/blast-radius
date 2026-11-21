# Candidate validation

Official score before this revision:168.9 seconds,158.846 simulated + about10 seconds CPU,
with zero missed commits. The revised official score is pending submission.

## Public sample, exact competition runtime limits

| Run | Simulated | CPU | Total | Misses |
| --- | ---: | ---: | ---: | ---: |
|1|103.357|4.900600|108.257600|0|
|2|103.357|5.194315|108.551315|0|
|3|103.357|5.215891|108.572891|0|

Three-run median108.551315; rounded per scoring rule: **108.6 seconds**.
Measured CPU includes the selector, its children and the small measurement wrapper.
The original official scorer checked outputs separately; answers were never mounted
into the selector. The sandbox used linux/amd64,2 CPUs,4GB/no swap, nobody,
read-only root,64MB /tmp, no network,256 processes and dropped capabilities.

## Independent labels and regressions

* Existing15-case evalA:103.396 simulated +11.144155 CPU =114.540155 total, zero misses.
* Twelve newly generated independent cases were each labeled by a full pinned pytest
  suite run. This includes harmless root-config/dictionary/enum rewrites, literal
  boundary changes, real class-constant faults, fixture and mixed edits.
  Candidate55.222 simulated +2.050413CPU =57.272413total, zero misses.
  Prior entry on the identical12 cases:106.102 simulated +5.038823CPU =111.140823total,
  zero misses. These aggregate totals are not comparable to the practice leaderboard.
*4476 operand/operator semantic checks,40 literal-refinement checks,12 economic
  verifier tests and11 numeric data-flow tests pass. Economic tests also check100
  randomized packing-cost equivalences and real source-encoding changes.
* Whole-patch safety check: one literal numeric-value change narrows273 original
  candidates to10; an added second-file edit disables that refinement and retains273.
* Every runtime file and data map matches the tested image hashes.

## Complete raw baseline observations

Both unmodified pinned source runs completed without scored failures or exclusions:
seed317:1620 passed,1skipped,25440subtests,464.11seconds;
seed391:1620 passed,1skipped,25440subtests,491.15seconds.

The merged artifact has1562 sites and142219 site/test observation pairs. Parser
max_nodes1490 has47 usable sites, Athena max_nodes9 has61, generator-memory1224 has20.
Untracked asynchronous tests214/1565 are excluded from deductions. A preliminary
300-second profiling timeout was not merged. No alternate operators or mutation
outcomes were generated during baseline profiling.

The source archive retains exact pinned SQLGlot/test files and adds exact pinned
root configuration files, allowing semantic root-config checks to run in production.
Numeric data-flow audit runs only on the pinned source during the image build.

## Interpretation

These are local results and regression evidence. Only the official hidden scorer
can confirm the user's requested below110/first-place result. Final data is unseen.