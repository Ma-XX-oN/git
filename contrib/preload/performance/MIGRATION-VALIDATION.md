# Preload migration validation record

This record tracks acceptance and matched measurements for the production
worker migration. The protocol and operator reference is
[preload.adoc](../../../Documentation/technical/preload.adoc).
The [performance journal](../PERFORMANCE-JOURNAL.md) and `evidence/` preserve
the historical measurements in this branch. The private baseline and migration
acceptance requirements are tracked in [ticket #5](https://github.com/Ma-XX-oN/git/issues/5).

## Validation order

The user's final ordering correction supersedes the earlier full-suite gates
recorded below. Development validates the actual worker fast path, supported
existing regression cases, lifecycle/stdio/status boundaries, cleanup, 6,000
serial commands and matched performance with Makefile and Meson builds.
Investigative diagnostics are removed after that gate and fast-path validation
is repeated. The complete ordinary-Git regression suites are reserved for final
community-PR preparation, after the final fast path is working.

Development pushes therefore run the fast matrix only. The workflow's manual
`community_pr` flag defaults to false; enabling it starts a separate compatibility
job only after the entire fast matrix succeeds. The two earlier compatibility
runs were stopped after their passing native artifacts were saved.
The existing Git regression adapter runs selected Git-file, alias and config
assertions through `git-ipc`. Call logs are checked against manager allocations;
there is no direct-Git fallback. Initial local coverage passed with 45 adapted
calls and 45 allocations. Its Bash subshell protects test-lib's descriptors and
is excluded from latency measurements.

## Fast-only gate and diagnostic cleanup

At `453b850220`, the expanded native fast-path step passed on all three
platforms. Linux took 66 seconds, macOS 111 seconds and Windows 118 seconds, including the selected
existing regression assertions through real workers and 6,000 serial commands.
The preload workflow's complete ordinary-Git suites were disabled for development pushes.
Both local Make and Meson builds passed the adapter's 45 commands, with matching
worker allocations; Linux and macOS CI also passed the Meson worker checks.

The separate cleanup removes the Windows exit-file logger, per-phase success
breadcrumbs, Bash timers/ablation switches, the obsolete 1 ms reader and the
timeout minidump utility. The 100 ms text drain becomes the single Windows
client reader. Git tracing retains readiness, startup/request failures,
connection/marker errors, timeouts, owner death and output/status/cleanup errors.
Local cleanup validation passed both builds' 32 Windows lifecycle cases,
the 10 integrated Meson preload subtests, transport, manager re-source and
supported Bash output cases, 45 adapted regression calls with matching
allocations, and 6,000 serial commands in 72.592 seconds. That stress run enabled
tracing; its median is diagnostic, not a matched performance result.
The complete pre-cleanup CI matrix passed in run 42882122186. Rebuilt fast-path
acceptance and paired benchmarks passed on the cleanup revision in
[run 42884766360](https://github.com/Ma-XX-oN/git/actions/runs/42884766360).

The repository's separate default `CI` workflow also starts on pushes. Its
cleanup-revision run 42884766119 was cancelled when that independent trigger
was found. Commit `c51a44fb1f` excludes migration-branch pushes from the default
matrix; pull-request CI and the explicit final compatibility job remain
available for community-PR preparation. This CI-only commit leaves the worker
source tested by run 42884766360 unchanged.

The final same-laptop untraced ABBA comparison at `5300cc41e2` passed with
5.0000 ms baseline and 4.1305 ms candidate (-0.8695 ms, -17.39%). This is
82.61% of its own matched baseline; the raw comparison retains machine/build
metadata and all 240 samples per variant. It is not an isolated measurement
of the instrumentation removal relative to the preceding migration checkpoint.

### Final acceptance and measurements

The cleanup matrix passed on Windows, Linux and macOS: native lifecycle,
transport, allocation, owner death, manager restart, supported output, selected
existing regressions through `git-ipc`, 6,000 serial commands, Make and Meson
worker checks, paired performance, and POSIX builds without pthread support.
The native fast-test steps took 55 seconds on Linux, 100 seconds on macOS and
100 seconds on Windows. These are test durations, separate from setup, builds
and paired benchmark execution. Full ordinary-Git suites remain reserved for
final community-PR preparation and were not part of this acceptance matrix.

| Platform | Hardware / logical CPUs | Baseline ms | Migrated ms | Baseline % | Change % |
| --- | --- | ---: | ---: | ---: | ---: |
| Windows Server 2025 CI | AMD EPYC 9V74 / 4 | 6.0075 | 4.1750 | 69.50% | -30.50% |
| Linux CI | AMD EPYC 7763 / 4 | 1.8050 | 1.0500 | 58.17% | -41.83% |
| macOS 26.7.2 CI | Apple M1 (Virtual) / 3 | 4.0470 | 2.5290 | 62.49% | -37.51% |
| Windows 11 laptop | AMD Ryzen 7 5700U / 16 | 5.0000 | 4.1305 | 82.61% | -17.39% |

Each row is an untraced ABBA comparison with three workers, 240 samples per
variant and matching compiler flags. The baseline snapshots environment per
call; the final worker inherits manager startup environment. The workload
keeps that environment unchanged. Native OS/Bash/Python versions, full flags,
source revisions, binary digests and raw samples accompany each raw report. The
[checkpoint graphs](migration-checkpoints/report.md) retain all 12 preceding
rows and add the three fast-gate CI pairs and four final comparisons.
Separate jobs establish within-pair improvements; changes between checkpoints
do not isolate the effect of a single cleanup edit.

The [final graphs](final-comparison/report.md) compare the migrated build,
preloaded, with unmodified upstream Git `750f5de7df` launched directly, each
built and timed within one job ([run 42893906943](https://github.com/Ma-XX-oN/git/actions/runs/42893906943)
and a local laptop run). Error bars and the superscript/subscript values give
the distances to the bounds of the 95% bootstrap confidence interval of the
median.

| Platform | Hardware / logical CPUs | Unmodified Git ms (95% CI) | Migrated preloaded ms (95% CI) | % of unmodified (95% CI) | Change % |
| --- | --- | ---: | ---: | ---: | ---: |
| Windows CI | AMD EPYC 9V74 / 4 | 16.94<sup>+0.29</sup><sub>-0.41</sub> | 2.893<sup>+0.014</sup><sub>-0.018</sub> | 17.08%<sup>+0.44</sup><sub>-0.30</sub> | -82.92% |
| Linux CI | AMD EPYC 7763 / 4 | 1.421<sup>+0.017</sup><sub>-0.028</sub> | 1.0790<sup>+0.0050</sup><sub>-0.0070</sub> | 75.9%<sup>+1.6</sup><sub>-1.0</sub> | -24.1% |
| macOS CI | Apple M1 (Virtual) / 3 | 7.13<sup>+0.21</sup><sub>-0.37</sub> | 2.381<sup>+0.093</sup><sub>-0.064</sub> | 33.4%<sup>+2.1</sup><sub>-1.2</sub> | -66.6% |
| Windows 11 laptop | AMD Ryzen 7 5700U / 16 | 55.20<sup>+1.03</sup><sub>-0.36</sub> | 4.181<sup>+0.049</sup><sub>-0.053</sub> | 7.575%<sup>+0.097</sup><sub>-0.175</sub> | -92.425% |

The adopted performance paths remain implemented: two blocking connectors
with peer rendezvous, persistent serial allocator channels, reply-before-refill,
grouped Bash request writes, shell-pattern pipe validation and a single 100 ms
text drain. Per-request environment optimizations are preserved as historical
evidence and superseded by the requested startup-inheritance contract. A source
sweep found no remaining active exit-file logger, timing/ablation controls,
minidump helper, worker ticket symbols or nonlocal exit jumps. Historical
evidence and the baseline adapter retain names needed to describe old revisions.

The documented Bash stdin/binary-output limitations and Windows stderr-last /
large-stderr deadlock limitation remain accepted. No relay or custom Bash
builtin was added. Only evidence and the default-CI branch guard follow the
tested cleanup source SHA; worker and client source remains unchanged.

## Comparison controls

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

Measure against unmodified Git with
`t/perf/compare_preload_upstream.py --upstream <built upstream tree>
--output <directory>` from a built migrated tree; the
`preload-upstream-timing.yml` workflow does this on `workbench/` branches. Plot
the resulting `upstream.json` reports with
`t/perf/plot_preload_upstream.py --input <upstream.json> --label <machine>
--output <directory>`.

## Windows Bash environment conversion

### Startup-environment contract

The Bash client captures its environment through ordinary native manager startup.
It sends arguments and cwd per command, without an environment snapshot, path
shadows or per-command conversion checks. Re-sourcing `git-manager.sh` sends
`STOP` on the existing allocation channel, waits for pool cleanup and starts a
replacement with the current exported environment. Exported changes and removals
become visible after that restart. Native API callers retain the optional `env`
and `env-export` encodings and supply target-native path values themselves.
The native Make rebuild and real Windows re-source test passed, including
changed/unset values, worker-count replacement, retired endpoint cleanup and
subshell isolation. The allocator's 6,000-request, shortage, held-probe and
shutdown tests also passed after adding STOP. Broader migration gates remain
separate from these results.

The rebuilt startup-environment version subsequently passed 31 native Windows
worker tests, 28 baseline context comparisons, all 12 t5790 tests, native
transport, owner death and both locally importable manager ownership/lifecycle
backends. A 6,000-command client run with worker tracing passed in 64.966 seconds
with a 7.028 ms median. This is diagnostic stress timing, not a matched gain.
Its local raw CSV, manager log, worker trace and binary identity are in
`Temp/preload-startup-environment-6000`; exact-revision CI evidence remains
required before diagnostic removal.

Untraced ABBA timing at committed `ba16959609` measured baseline 4.6580 ms
versus candidate 4.0860 ms (-0.5720 ms, -12.28%) on the same Ryzen 7 5700U laptop.
The unchanged-environment workload and per-variant environment policies are
explicit in the [raw report](evidence/migration-ba1695-windows-laptop-matched.json).
The current CI run is [42774012137](https://github.com/Ma-XX-oN/git/actions/runs/42774012137).
Its Linux and macOS native step failed in the new environment restart fixture;
the early artifacts contain first and second READY records with empty stdout
and stderr. Full job logs are not yet available. These are acceptance failures
under investigation, not a passed gate; investigative diagnostics stay enabled.

The diagnostic rerun at `fdcc645f15` identified `phase=unset-reference status=21`
on both POSIX runners: the direct Git reference failed before manager restart.
Git can return no editor after GIT_EDITOR is unset in a noninteractive environment.
A controlled local invocation with TERM=dumb and no editor configuration also
returned 1 with no output. The fixture now supplies an explicit fallback EDITOR;
its native Windows restart/unset/resize/subshell checks pass with TERM=dumb.
POSIX CI must verify this fixture correction; production manager code is unchanged.
The refreshed local Meson build also passed all 31 Windows worker cases and all
12 t5790 cases.

At `0d9235d1f3`, Linux and macOS passed the restart fixture and all 6,000 commands
(9.414 and 40.707 seconds respectively, traced). Their output test completed its
first four cases, then hit the aggregate 30-second deadline in the fifth case.
Linux had forwarded all 1 MiB stderr and 739,235 stdout bytes; macOS had forwarded
all 1 MiB stderr and 368,278 stdout bytes. The five-case POSIX test processes over
6 MiB through Bash builtins; its aggregate deadline is increased to 120 seconds.
Windows keeps its 30-second diagnostic timeout. This changes the test deadline,
not the reader or the accepted Windows limitation; completed POSIX output/status
checks remain required.

The `ba16959609` Linux job completed both full Git suites, Make/Meson builds,
matched timing and the no-pthread check. Meson reported 916 tests, zero failures.
The same-job untraced comparison measured 1.7755 ms baseline versus 1.0550 ms
candidate (-0.7205 ms, -40.58%) with explicit environment-policy metadata.
Raw evidence is [migration-ba1695-linux-matched.json](evidence/migration-ba1695-linux-matched.json).
The job remains an overall failure because of its earlier unset-editor fixture.

At `3b0bdbf56b`, the native acceptance step passed on Windows, Linux and macOS in
[run 42775783963](https://github.com/Ma-XX-oN/git/actions/runs/42775783963), including
the startup/re-source fixture, 6,000 commands and each platform's supported output
cases. The 120-second aggregate POSIX output deadline completed all byte/status
assertions. Traced stress durations were 30.983 seconds on Windows, 8.588 seconds
on Linux and 50.570 seconds on macOS; they are not matched performance results.
The Windows summary reports a large working-tree diff stat; the reason has not
yet been established, so this metadata must remain visible in the raw artifact.
The full Make/Meson regression suites remain running. No diagnostic cleanup has
been applied.

## Completed pre-cleanup matrix and Windows shell correction

Run 42775783963 at `3b0bdbf56b` completed successfully on Linux and macOS,
including both complete Git suites, native acceptance and matched timing.
Windows passed its Makefile suite and native acceptance; Meson reported 955
passed, 108 skipped and one failed test, `t5580-unc-paths`.
The failure occurs before any UNC operation: `winpwd: command not found` at
line 20. Meson selected `C:/Program Files/Git/bin/sh.exe`; a local alias snippet
fails with that installed shell and succeeds with the SDK's `sh.exe`.
The acceptance workflow uses Meson's existing `sane_tool_path` option to select
the SDK tools, matching its Makefile suite. It also runs t5580 immediately after
the Meson build to detect this environment failure before the lengthy full suite.
The UNC test and Git's path-handling implementation are unchanged. The corrected
full Windows run is still required before investigative diagnostic removal.

After reconfiguring and rebuilding the local Meson tree with
`-Dsane_tool_path=C:/msys64/usr/bin`, the real Meson launcher passed all eight
t5580 UNC tests (13.39 seconds, none skipped) and all 12 t5790 preload tests
(7.97 seconds). The selected shell was `C:/msys64/usr/bin/sh.exe`. This verifies
the identified alias failure and its tool-selection correction locally; the
complete suite remains the acceptance gate.

All three same-job comparisons at this revision used matching compiler flags,
three workers, unchanged environments, explicit environment-policy metadata and
disabled tracing. They measured baseline/candidate medians of 4.3145/3.2590 ms
on Windows (-24.46%), 1.4295/0.8555 ms on Linux (-40.15%) and 3.9835/2.4150 ms
on macOS (-39.37%). Each comparison is independent; do not pool machines or
subtract another run's baseline. Raw samples and hardware/runtime identities are
in `evidence/migration-3b0bdb-*-matched.json`.

### Abandoned shadow checkpoint

This checkpoint (`20a957982a`) tried to keep per-command environment transfer
working on Windows by sending native-converted copies ("shadows") of the
standard path variables alongside the MSYS `export -p` snapshot. It was a
mistake: the shadows came from the manager's environment, which the workers
already inherit, so they duplicated values the worker already had while adding
per-command checks that slowed every call (+11.33% on the laptop). Converting
genuinely changed paths per command was not reliable, because MSYS mappings go
beyond drive prefixes. The next commit, `ba16959609`, dropped per-command
environment changes: workers use the manager's startup environment, and
re-sourcing `git-manager.sh` refreshes it. No shadow code remains. See the
[performance journal](../PERFORMANCE-JOURNAL.md#abandoned-path-shadow-checkpoint).
The original record follows.

A new client-level output/status test reproduces a pre-existing failure with a
small shell alias: `export -p` sends a colon-separated MSYS PATH, native shell
lookup returns NULL, and `git_shell_path()` faults before status delivery.
The same alias fails through the preserved `working-version-two` client;
inherited native environment values succeed. This is not a measured migration
regression. The shadow-based correction passes the reduced shell alias, all
31 native worker lifecycle cases and all 28 baseline context comparisons.
The environment cache test verifies eight startup conversions, zero conversions
for unchanged values and one conversion after changing a value. Empty, unset
and readonly originals retain their intended behavior. Both context encodings
select native shadows on Windows and discard the transport metadata.

The expanded dual-output alias remains a failing acceptance case: 10,000 bytes
per stream completes with status 7, while 100,000 bytes per stream stalls in the
Bash drain. A bounded-read probe advanced past stdout but then blocked reading
stderr; adding a readiness probe did not resolve it. These temporary diagnostic
probes did not alter the production reader. Native worker backpressure tests
still pass. This failure is not evidence that full Bash output acceptance has
passed, and investigative diagnostics remain in place.

The descriptor probe tests the actual output descriptors with Bash's
`[[ -p /dev/fd/N ]]` and `[[ -f /dev/fd/N ]]` predicates. Both report regular
files, and the bounded variant reads 4,096 stdout bytes before blocking on the
stderr read. Thus empty forwarded-output files do not establish that the client
received no bytes: the client prints a chunk only after its read returns.
The [Bash manual](https://www.gnu.org/s/bash/manual/html_node/Bash-Builtins.html)
specifies that `read -t` does not time out regular files. The exact MSYS runtime
classification path has not been established; do not describe an upstream
runtime defect as proven. The kernel handles remain blocking named pipes:
insufficient buffer space blocks the writer rather than discarding bytes.

The test writes 1 MiB of zeros to stdout followed by 1 MiB to stderr, then exits
7, and repeats with the stream order reversed. With the unbounded chunk reader,
waiting for stdout EOF while stderr fills explains the observed deadlock. The
two output streams have independent ordering; a reader must drain both without
waiting indefinitely for the other stream's EOF. The standalone output test has
a 30-second deadline and terminates only its owned process tree on timeout.

Local builtin capability checks used
`enable -f /nonexistent/preload.so preload` in both
`C:/Program Files/Git/usr/bin/bash.exe` (5.2.37(1)-release, x86_64-pc-msys) and
`C:/msys64/usr/bin/bash.exe`. Both returned `dynamic loading not available`.
This establishes that these installed binaries cannot load an addon through
`enable -f`; it does not establish a restriction on every possible Bash build.
No installed Bash binary was modified.

An earlier requirement to observe exported changes on each call led to the
shadow checkpoint above. It is superseded by the startup-environment contract:
changes require re-sourcing, and the shadow code was removed in `ba16959609`.
A drive-prefix-only translation is insufficient for runtime mount mappings and
path lists. The client uses each platform's installed Bash; builtin work is
explicitly deferred.

The shell client should invoke its own `cygpath`, as requested, so conversion
uses the caller's installation and mount table. A single path uses `-w`; a path
list uses `-wp`. Preserve the caller's HOME choice rather than deriving it from
USERPROFILE or the worker's build SDK. The runtime's explicit standard table
contains PATH, HOME, LD_LIBRARY_PATH, ORIGINAL_PATH, SHELL, TMPDIR, TMP and TEMP.
Other variables can already contain native Windows paths or participate in
MSYS's additional native-spawn heuristics; enumerating the standard table does
not establish complete equivalence for those variables.

A local probe compared all eight standard variables through both Git Bash and
MSYS2 Bash against a native Python child launched by the same shell. All 16
comparisons matched. The probe deliberately used `/home/preload conversion`
and `/tmp/preload conversion`: Git Bash and MSYS2 produced their own distinct
home/temp mappings, and each matched its corresponding native child. Raw values
are preserved in
[cygpath-standard-environment-check.jsonl](evidence/cygpath-standard-environment-check.jsonl).
This validates the converter choice for those inputs; it does not yet validate
cache invalidation, empty/unset values or arbitrary custom-variable heuristics.

Source inspection found that `CW_CVT_ENV_TO_WINENV` calls `build_env` with
`keep_posix=true`, bypassing the additional custom-variable heuristics. The
installed MSYS DLL does not export `arg_heuristic_with_exclusions`, despite its
header declaration; a temporary probe failed to link against it. Neither API
is accepted as a complete environment-conversion solution. No runtime DLL is
loaded into native Git for this investigation.

Validate conversion against the environment actually received by a native child
of the same shell, including standard paths, custom paths/path lists, exclusions,
empty values, removals and later changes. Keep conversion costs separate from
steady-state command timings and preserve new matched evidence after a fix.

References: [Git Bash standard environment table](https://github.com/git-for-windows/msys2-runtime/blob/main/winsup/cygwin/environ.cc),
[environment wrapper](https://github.com/git-for-windows/msys2-runtime/blob/main/winsup/cygwin/external.cc),
and [MSYS path conversion rules](https://www.msys2.org/docs/filesystem-paths/).

## Documented Windows output limitation

The Windows Bash large-stderr deadlock is an accepted client limitation for this
migration. It is not a 64 KiB cap on total output: the pipe requests that buffer
size, and large stdout-only output can complete. Large stderr-only output can
also deadlock while the client waits for stdout EOF. The limit depends on stream
order and unread stderr; it is not data loss or a portable safe byte threshold.
Use ordinary Git for commands with potentially substantial stderr.

Normal Windows output acceptance checks 4 KiB per stream and 1 MiB stdout-only,
including nonzero exit status and exact bytes. POSIX output acceptance retains
all large-stream cases. The diagnostic cases remain available with
`test_shell_output.py --case stderr-large`, `--case out-first` or
`--case err-first`; they have a 30-second deadline and owned-tree cleanup.
The default Windows test reports their skip explicitly. This does not claim
that the unsupported cases pass.

Local validation of this boundary passed the supported small-mixed and
stdout-large cases with exact bytes and status 7. The separately selected
stderr-large diagnostic timed out at 30 seconds and completed its owned-tree
cleanup, confirming that stderr-only output is affected as documented.

## Final cleanup gate

The user subsequently required removal of the unused per-request environment
API as well as the Bash sender. This supersedes earlier entries retaining native
`env`/`env-export` support. Both platforms accept cwd context only and inherit the
manager's startup environment. The decoder, environment application routines,
worker-only Windows snapshot mode and updater, environment comparison fixture
and its CI invocation are removed. Ordinary Git child-process environment
merging remains in use. Rebuilt acceptance and matched measurements must cover
this reduced implementation before diagnostic removal.

The local Makefile rebuild passed after the removal. All 32 native worker cases,
10 cwd/inheritance regression cases, manager re-source/unset/resize/subshell
checks and supported output cases passed. Unsupported environment tags fail
framing validation. A 6,000-command traced run passed in 75.107 seconds with an
8.471 ms median; this diagnostic timing has no matched baseline and is not a
performance claim. Raw local evidence is in `Temp/preload-no-env-6000`.
The Makefile run-command regression passed all 24 cases (including its platform
skips). After the Meson rebuild, its real SDK-shell launcher passed t5790 (10
subtests), t5580 (eight UNC subtests) and t0061 (20 passed subtests, four skipped).
No compiler errors or warnings were reported by either rebuild.
The follow-up same-laptop untraced ABBA comparison at `2ac970f4cb` measured
6.5345 ms baseline and 5.6155 ms candidate (-0.9190 ms, -14.06%). This is a new
matched cohort, not an isolated before/after comparison of the removed code.
Raw samples are in [migration-2ac970-windows-laptop-matched.json](evidence/migration-2ac970-windows-laptop-matched.json).

Investigative diagnostics remain in the source. After all native acceptance
passes, remove the ad hoc exit-file logger, per-phase breadcrumbs/timers,
ablation switches and obsolete dumps in a separate change. Preserve useful
startup, marker, transport, owner-death, status-delivery and cleanup diagnostics
through Git tracing. Rebuild and repeat acceptance and matched comparisons for
that cleanup revision; append its results here before reporting completion.
