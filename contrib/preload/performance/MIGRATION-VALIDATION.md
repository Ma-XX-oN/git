# Preload migration validation record

This record tracks acceptance and matched measurements for the production
worker migration. The protocol and operator reference is
[preload.adoc](../../../Documentation/technical/preload.adoc).
The [performance journal](../PERFORMANCE-JOURNAL.md) and `evidence/` preserve
the historical measurements in this branch. The private baseline and migration
acceptance requirements are tracked in [ticket #5](https://github.com/Ma-XX-oN/git/issues/5).

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

| Environment | Baseline median | Candidate median | Candidate minus baseline | Baseline % | Change % |
|---|---:|---:|---:|---:|---:|
| Laptop, native Windows Makefile build | 5.0670 ms | 5.1705 ms | +0.1035 ms | 102.04% | +2.04% |
| Linux CI, candidate `2b45a20a71` | 1.7930 ms | 1.7595 ms | -0.0335 ms | 98.13% | -1.87% |
| Linux CI, candidate `87a4adb99b` | 1.3620 ms | 1.2270 ms | -0.1350 ms | 90.09% | -9.91% |
| Windows CI, candidate `87a4adb99b` | 3.9595 ms | 3.9540 ms | -0.0055 ms | 99.86% | -0.14% |
| macOS CI, candidate `87a4adb99b` | 4.4125 ms | 4.0810 ms | -0.3315 ms | 92.49% | -7.51% |
| Laptop, candidate `209625fd56` | 4.8295 ms | 4.8730 ms | +0.0435 ms | 100.90% | +0.90% |

The laptop difference is about 2%; it is not recorded as a gain. The raw laptop
comparison is in [windows-before-cleanup.json](../../../t/perf/preload-evidence/windows-before-cleanup.json).
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

## Shared worker and review fixes

Commit `87a4adb99b5390b8b61b6da7ca4fdeda668f05a5` shares the command lifecycle,
explicit request termination, session-directory startup and native owner PID
contract. Both platforms retain native environment blocks and Bash export text.
POSIX ordinary-exit completion replaces nonlocal jumps. Partial stdio setup is
tracked so error cleanup delivers output EOF before status.

Native preload acceptance, including 6,000-command stress, passed on all three
platforms in [run 42697246570](https://github.com/Ma-XX-oN/git/actions/runs/42697246570).
The Linux job also passed both full Git suites and the matched comparison.
Its raw matched timings are in
[migration-87-linux-matched.json](evidence/migration-87-linux-matched.json).
The macOS job passed both full Git suites and its matched comparison.
The Windows job passed native preload acceptance and the matched comparison
(3.9595 ms baseline, 3.9540 ms candidate), but failed the Makefile send-email
suite and the Meson build. Meson's probes ran against the minimal SDK, which
omits `shellapi.h` and the `winspool` import library. The validation workflow
uses the full SDK for those probes. A local verbose reproduction of the
send-email failure selected native MinGW Perl; the test's shell sendmail
fixture requires MSYS Perl. The workflow selects it for the test suites and
captures verbose logs. Windows acceptance remains open until the corrected
workflow passes.

The local send-email suite passes all 217 cases (including its expected
breakage) with MSYS Perl first on PATH. The full SDK's profile was also
reproduced exiting during normal profile.d tests under Bash errexit; sourcing
it in a conditional context completes initialization, preserving errexit for
subsequent build/test commands. Run 42733857727 validated the persistent POSIX
client through native stress on Linux and macOS; its Windows job stopped in
profile initialization before compilation and provides no worker acceptance.

Local review validation expanded to 31 Windows worker cases and 28 exact
environment comparisons, including native API environment frames, omitted
environment inheritance, invalid UTF-8 arguments, numeric escapes and malformed
Unicode escapes. Nine t5790 cases include empty and short export snapshots,
readonly/integer attributes and exclusion of arrays from scalar snapshots.
The startup/shutdown regression first demonstrated that a pool could finish
closing before an in-flight process start returned; both Python pool backends
then passed after cleanup was coordinated with those starts. No per-command
measurement is attributed to these changes until a matched comparison runs.

Local t5790 wrapper mode requires a POSIX `GIT_BUILD_DIR` path. Without that
explicit path the local harness selected installed Git or could not find
`test-tool`. All eight cases pass using the rebuilt tree after correcting that
test environment; the tests themselves are not skipped or weakened.

Generate absolute and normalized figures from matched raw reports with
`t/perf/plot_preload_comparison.py --input <matched.json> --label <machine-stage>
--output <directory>`. Repeat `--input` and `--label` for additional stages or
machines. Each row uses its own host/job baseline at 100%; results are not pooled
across hardware. The JSON companion preserves machine/runtime metadata, raw
samples, compiler flags, source revisions and source-report digests.

## Final cleanup gate

Investigative diagnostics remain in the source. After all native acceptance
passes, remove the ad hoc exit-file logger, per-phase breadcrumbs/timers,
ablation switches and obsolete dumps in a separate change. Preserve useful
startup, marker, transport, owner-death, status-delivery and cleanup diagnostics
through Git tracing. Rebuild and repeat acceptance and matched comparisons for
that cleanup revision; append its results here before reporting completion.
