# Preload migration validation record

This record tracks acceptance and matched measurements for the production
worker migration. The protocol and operator reference is [preload.adoc](preload.adoc).
Historical experimental results are preserved by tag `working-version-two`.

## Controls

The baseline is `working-version-two` (`a3843f0c5e0dd2fda2449328680f03b742bd883f`).
Each revision uses its own matching client and manager, three workers, full cwd
and environment transfer, tracing disabled and identical compiler flags. A
comparison runs baseline/candidate/candidate/baseline, 120 paired direct/IPC
samples per round. Raw binary hashes, compiler flags and all samples are retained.
Laptop results are compared with laptop results; each CI comparison stays in
one native runner job. These measurements assess preservation of performance,
not a new optimization.

## Before investigative-diagnostic removal

Candidate `241634f378` contains the extracted worker, context and blocking
transport modules. Windows and POSIX implementations are selected at build
time. Startup uses `--preload-worker`; active launchers use feature names.

| Environment | Baseline median | Candidate median | Candidate minus baseline |
|---|---:|---:|---:|
| Laptop, native Windows Makefile build | 5.0670 ms | 5.1705 ms | +0.1035 ms |
| Linux CI, candidate `2b45a20a71` | 1.7930 ms | 1.7595 ms | -0.0335 ms |

The laptop difference is about 2%; it is not recorded as a gain. The raw laptop
comparison is in [windows-before-cleanup.json](../../t/perf/preload-evidence/windows-before-cleanup.json).
The Linux job is [129323027789](https://github.com/Ma-XX-oN/git/actions/runs/42664103499/job/129323027789).

Local Windows validation passed the Makefile and Meson builds, 26 native worker
lifecycle cases, linked production transport, owner death, six t5790 cases and
the persistent allocator's 6,000-request/shortage/held-probe/shutdown cases.
Sixteen complete environment comparisons matched the baseline, including case
aliases, Unicode, empty snapshots, removed variables and values up to 40,000
characters. A full-context 6,000-command worker stress run passed; its 8.2685 ms
median uses a different stress workload and must not be compared with the paired
benchmark above. Local full-suite runs have unresolved harness failures; they
are not acceptance evidence. The local Meson test harness could not launch its
Git binary with its generated environment, although the built binary runs with
the native runtime PATH. CI remains the full-suite acceptance gate.

For `241634f378`, native preload acceptance and 6,000-command stress passed on
Windows, Linux and macOS in [run 42665714433](https://github.com/Ma-XX-oN/git/actions/runs/42665714433).
The earlier Linux candidate passed both complete Git suites and matched
comparison in [run 42664103499](https://github.com/Ma-XX-oN/git/actions/runs/42664103499).
Remaining platform suites, build-system tests and performance comparisons must
finish before diagnostic removal. The boundary follow-up also validates unsigned
POSIX argument counts with the high bit set.

## Final cleanup gate

Investigative diagnostics remain in the source. After all native acceptance
passes, remove the ad hoc exit-file logger, per-phase breadcrumbs/timers,
ablation switches and obsolete dumps in a separate change. Preserve useful
startup, marker, transport, owner-death, status-delivery and cleanup diagnostics
through Git tracing. Rebuild and repeat acceptance and matched comparisons for
that cleanup revision; append its results here before reporting completion.
