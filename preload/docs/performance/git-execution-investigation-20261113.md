# Git execution and full RepoWorkflow native runtime: investigation

Investigation Git branch: investigation/git-execution-bottlenecks-20261113.
Git diagnostic source base: 308cb45620be316750ba532a6b4cd684b65c9753.
RepoWorkflow catalogue evidence source: c5e258db7918696fe87810d2546368227e29eb31.
Status: diagnostic investigation; no change to RepoWorkflow tests or release policy.

## Process startup: exact three-OS native run

Git Actions run 43495959068, SHA 6b99933e380f2fc4a27519ba47f2332985c75404.
120 samples per case, warmup and alternating-order controls, Python 3.13,
same Git binary and minimal native C launch control within each runner.
Source: preload/perf/process_breakdown.py. Raw process-breakdown.json in job artifacts.
Median process invocation in ms (control discard / git --version discard):

- Windows: 5.343 / 10.908 (additional Git-specific ~5.565 ms).
- Linux: 1.3105 / 1.3119 (additional ~0.0014 ms).
- macOS: 6.532 / 8.312 (additional ~1.780 ms).

Caution: per-case medians need not be additive. Different OS runners have
different CPUs, environments and loader configurations. These numbers are
not an isolated causal measurement of OS implementation.

## Total test runtime and registered test multiplicity

RepoWorkflow complete native 114-group catalogue, exact SHA c5e258db…:

| Run | Windows s | macOS s | Linux s |
| --- | ---: | ---: | ---: |
| 42374812399 | 5602 | 2153 | 1041 |
| 42375875863 | 6263 | 2627 | 1520 |

Group issue-583-authoritative-validate maps to scripts/validate.py,
which calls Python compileall and then unittest discover -s tests -v.
That nested execution reports 2254 tests on every OS. Recorded group seconds:

| Run | Windows s | macOS s | Linux s |
| --- | ---: | ---: | ---: |
| 42374812399 | 2935 | 1107 | 540 |
| 42375875863 | 3271 | 1300 | 776 |

The complete catalogue simultaneously runs many overlapping registered modules.
Group issue-545-all-acceptance has a load_tests() that runs 23
other issue-545 catalogue modules again. Group issue-572-assembled-contracts
loads eight other modules again. The 2254 nested tests include 595 repeated
test identifiers among 2252 log rows after removing a tests. import prefix
(1657 unique identifiers); two test executions did not match that log parser.
Individual tests can run five times.

On both runs, issue-583 alone accounts for about 53% of the Windows-minus-Linux
runtime gap; issue-583 together with issue-545-all-acceptance accounts for
about 62–64% of the gap. These are workload attribution figures, NOT an
assertion that all elapsed time is Git execution.

## Diagnostic next stage

Use an opt-in Python sitecustomize.py to count actually launched subprocesses,
their inclusive time, and audited filesystem operations in the existing
registered issue-545-all-acceptance group. No changes to RepoWorkflow test
files or test-selection rules. A Windows-first CI gate runs against the same
historical RWF SHA, then Linux and macOS. Interpretation: child Python
process duration includes its descendants, so cumulative category timings
overlap. Do not sum nested durations to claim whole-suite elapsed time.

Run references:
https://github.com/Ma-XX-oN/git/actions/runs/43495959068
https://github.com/Ma-XX-oN/RepoWorkflow/actions/runs/42374812399
https://github.com/Ma-XX-oN/RepoWorkflow/actions/runs/42375875863
