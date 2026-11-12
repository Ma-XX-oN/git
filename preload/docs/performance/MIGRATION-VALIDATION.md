# Preload migration validation record

This record tracks acceptance and matched measurements for the production
worker migration. The protocol and operator reference is
[protocol.adoc](../protocol.adoc).
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
comparison is in [windows-before-cleanup.json](legacy-evidence/windows-before-cleanup.json).
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

## READY dispatch with private mailboxes

The Unix manager attempts allocation on its request-dispatch thread when the
mailbox has no outstanding task. A READY worker is replied to through the same
private FIFO before replenishment. An empty pool queues the existing blocking
allocation on the mailbox thread. Queued and executing tasks prevent later
requests on that mailbox from taking the immediate path.

Six focused tests cover immediate reply/refill ordering, nonblocking allocation
with and without a READY worker, shortage isolation, ordering behind PRELOAD
and another GET, and cleanup when delivery fails. They pass locally, as do the
existing manager lifecycle, Windows protocol/concurrency, and shell-environment
checks. Native CI acceptance and performance results are recorded below.

`t/perf/compare_preload_dispatch.py` compares the generic library from
`346517302dfc305cf122f359714c0dc12e4b0884` with the candidate using one identical
worker executable. Repeated shell calls and command substitutions are measured
separately in ABBA rounds, with alternating direct/preloaded calls in each
round. Tracing is disabled; startup is excluded. The report retains raw samples,
source hashes, worker SHA-256, hardware and runtime identities. CI runs three
ABBA repetitions per workload on each platform, alongside both build systems'
worker validation and the existing matched production/upstream comparison.

The local Windows control uses one ABBA repetition, 120 paired calls per round,
on an AMD Ryzen 7 5700U (16 logical CPUs), Windows 11 build 26200, Python 3.13.1
and MSYS Bash 5.2.37. Repeated shell calls measured 3.9735 ms baseline and
3.8880 ms candidate (97.85% of baseline); command substitutions measured
30.0300 ms baseline and 29.5650 ms candidate (98.45% of baseline). The Windows
dispatch implementation is unchanged, so these small differences are control
variation, not evidence of a Unix fast-path benefit or a C-manager benefit.
Raw samples and identities are in
[ready-dispatch-windows-control.json](evidence/ready-dispatch-windows-control.json).

### CI measurements for 64f6b0ac25

[Run 43068465402](https://github.com/Ma-XX-oN/git/actions/runs/43068465402)
measures candidate `64f6b0ac25fd5c14db510982dc145502845a8afe` against generic
library baseline `346517302dfc305cf122f359714c0dc12e4b0884`. Each workload has
three ABBA repetitions, six rounds and 720 samples per variant. Each row below
compares variants on one runner using one worker binary; values are pooled
sample medians. These cohorts must not be compared as before/after measurements
against earlier CI runs on different hardware.

| Platform | Workload | Baseline ms | Candidate ms | Candidate / baseline | Reduction ms |
|---|---|---:|---:|---:|---:|
| Linux | Repeated shell calls | 0.9040 | 0.8720 | 96.46% | 0.0320 |
| Linux | Command substitution | 1.6035 | 1.5575 | 97.13% | 0.0460 |
| macOS | Repeated shell calls | 1.9255 | 1.8560 | 96.39% | 0.0695 |
| macOS | Command substitution | 3.0725 | 3.0420 | 99.01% | 0.0305 |
| Windows | Repeated shell calls (control) | 4.1660 | 4.2350 | 101.66% | -0.0690 |
| Windows | Command substitution (control) | 14.2500 | 14.3720 | 100.86% | -0.1220 |

Linux candidate round medians are below every baseline round median for each
workload. macOS round variation is larger: the command-substitution ABBA groups
vary in direction, so its pooled 1% reduction is inconclusive. The shell-call
result is a modest observed reduction, not a general speedup guarantee.
The Windows dispatch path is unchanged; its small increases do not measure
the Unix fast path, and their repeatability is not established.

| Runner | Hardware | OS / architecture | Python | Bash |
|---|---|---|---|---|
| Linux | AMD EPYC 9V74, 4 logical CPUs assigned | Linux 7.0.0-1019-azure, x86_64, glibc 2.43 | 3.13.18 | 5.3.9 |
| macOS | Apple M2 Pro (Virtual), 5 logical CPUs assigned | macOS 26.7.2, arm64 | 3.13.17 | 5.3.20 |
| Windows | Intel Xeon Platinum 8370C, 4 logical CPUs assigned | Windows Server 2025 build 26100, AMD64 | 3.13.17 | 5.3.20 (Cygwin) |

Both Unix jobs pass Makefile and Meson worker validation, the six dispatch
tests, manager protocol/concurrency and lifecycle checks, owner death,
environment/output checks, supported command regressions, 6,000-command stress,
and the no-pthread build check. Their additional same-job comparison against
the pre-migration client measures 0.8210 versus 0.8720 ms on Linux and 1.5410
versus 1.8110 ms on macOS. That is a different baseline from the table above.
The Windows job also passes Makefile/Meson worker validation, the dispatch,
protocol/concurrency, lifecycle, owner-death, environment/output, supported
regression and 6,000-command checks. Its production/upstream comparison measures
4.1160 ms for the pre-migration client, 3.8665 ms for the candidate and 22.0240 ms
for direct upstream Git. All three native jobs succeed; the optional full
ordinary-Git compatibility jobs are skipped, as required for this development
stage.

Separate diagnostic cohorts with stage timers enabled measure request-to-reply
median at 109 us on Linux and 286 us on macOS. Allocation and reply-delivery
stage medians are 15/42 us and 34/111 us respectively. Their sum is not a
measured C-manager saving: these wall-clock stages include Python work, system
calls and scheduling. A C manager remains a reasonable same-protocol experiment
for the remaining overhead; no native-manager performance gain is established.

Raw comparisons and stage samples are preserved under
[Linux evidence](evidence/ready-dispatch-64f6/Linux/preload-dispatch-comparison/matched.json)
and [macOS evidence](evidence/ready-dispatch-64f6/macOS/preload-dispatch-comparison/matched.json),
plus the [Windows control](evidence/ready-dispatch-64f6/Windows/preload-dispatch-comparison/matched.json).
Each platform directory also contains `preload-comparison/matched.json` and
`preload-stage-timing/{paced,burst}/`, including raw client timestamps and
manager logs as well as the stage summaries.

The separate [general Git CI run](https://github.com/Ma-XX-oN/git/actions/runs/43068465407)
has failures outside this change: representative Linux/macOS/Windows builds
reject signedness comparisons in `preload-worker.c`, `preload-worker-posix.c`
and `preload-transport-win32.c` under `-Werror`; dependency setup also encounters
an existing `/usr/bin/dirname` symlink. Those C files and
`ci/install-dependencies.sh` are byte-for-byte unchanged between the baseline
and candidate commits. The preload acceptance results do not establish that
the general Git CI matrix is green.

## CPU, syscall and scheduler profiling for 4086b8177f

[Profiling run 43076253849](https://github.com/Ma-XX-oN/git/actions/runs/43076253849)
uses the unchanged production shell, Unix manager and pool sources from the
READY-dispatch experiment. Only diagnostic launchers and the stage harness are
changed. Each ordinary diagnostic cohort measures 400 `git --version` calls
after 20 warmups, with a direct invocation between calls to allow replenishment.
Shell calls and command substitutions are separate workloads. This profiles
serial READY-pool behavior, not shortages, concurrent allocation or substantial
Git command execution.

The launcher measures nested operations with wall and per-thread CPU clocks,
then measures selected boundaries in separate lighter cohorts. The raw spans
retain parent indices, native thread IDs, monotonic/epoch anchors and absolute
CPU timestamps. Dispatch-to-reply ends at the FIFO write return, excluding
replacement startup and later callback work. The stage-only cohort still has
the original stage timers enabled; none of these results is an uninstrumented
speedup measurement.

### Client stages and manager CPU

Stage-only shell-call medians, in microseconds:

| Stage | Linux | macOS |
|---|---:|---:|
| Whole generated command function | 816.0 | 2466.0 |
| Allocation request sent to reply consumed | 122.0 | 459.5 |
| Worker channels opened | 121.0 | 522.5 |
| Request sent to output complete | 317.0 | 914.0 |
| Output complete to exit status returned | 83.0 | 223.0 |

The largest stage is command/output handling, followed by channel setup on
macOS. Allocation is one contributor; treating the whole command latency as
manager overhead would misidentify the optimization target. The medians above
are not additive decompositions of the median whole call.

Focused shell-call operation medians below show wall / thread CPU time in
microseconds. Each row is a separate cohort; nested operations such as
`Path.exists` and `os.stat` must not be added together.

| Operation | Linux wall / CPU | macOS wall / CPU |
|---|---:|---:|
| Allocation | 17.7 / 16.5 | 46.9 / 46.2 |
| Construct worker Path object | 6.5 / 5.3 | 20.2 / 19.4 |
| Path string conversions, two per call | 16.9 / 14.6 | 44.8 / 43.3 |
| Check mailbox lock file exists | 9.4 / 8.1 | 22.1 / 21.2 |
| Underlying stat boundary | 8.9 / 7.6 | 18.8 / 17.8 |
| Write reply FIFO | 8.5 / 7.2 | 12.2 / 11.2 |
| Dispatch through reply write return | 65.6 / 63.7 | 168.0 / 165.9 |

The existence check is not an exclusive-lock acquisition; it checks whether the
client's claim file remains present. The FIFO write alone is much smaller than
the original allocated-to-delivered timer, which also includes constructing and
rendering the full worker pathname. Path manipulation is a visible CPU cost,
especially on macOS. These boundaries include wrapper/clock overhead and the
unwrapped Python/runtime work within each operation; they are not pure kernel
syscall durations.

The lighter `os.write` cohort's command median is 101.35% of the stage-only
cohort on Linux and 92.72% on macOS. These sequential cohorts are not ABBA
comparisons: macOS drift is larger than a simple instrumentation adjustment.
The full nested profile adds further overhead, so its absolute durations are
used for structure, not normal latency or a C rewrite forecast. Calibration
and every cohort's raw samples are retained.

### Linux kernel evidence

The separate `strace -f -ttt -T -yy` passes complete 40 measured commands per
workload. The analyzer reconstructs unfinished/resumed syscalls and matches
client native TIDs to each command's stage window. Every shell call has:

- one GET write and **42 one-byte reads** to consume its 42-byte pathname reply;
- five worker-channel opens;
- 78 descriptor setup/close syscalls;
- 30 worker-output/status reads, 23 pselect calls, and five worker-request writes;
- no mailbox claim or mailbox FIFO open after the warmup retained its mailbox.

Command substitutions add one mailbox claim, one mailbox open, a liveness
check, and FREE. Their median descriptor setup/close count is 122. These serial
traces contain no claim retries, so mailbox contention does not explain their
additional cost. Traced syscall elapsed times include ptrace overhead and are
not substituted for the stage-only timings.

A separate 100-call capture combines `perf` sched_switch/sched_wakeup events
with the lighter write probe. Matching clocks and thread IDs distinguishes
running, runnable and blocked states. For that capture, mean durations are:

| Span | Wall us | Running us | Runnable us | Blocked us |
|---|---:|---:|---:|---:|
| Whole shell command function | 998.1 | 626.8 | 52.5 | 318.8 |
| Shell waiting for/reading allocation reply | 163.3 | 63.6 | 3.2 | 96.4 |
| Shell worker-channel setup | 166.9 | 120.1 | 15.0 | 31.8 |
| Shell command/output handling | 385.4 | 167.5 | 27.3 | 190.6 |
| Manager dispatch through reply write return | 81.0 | 78.8 | 2.1 | 0.0 |

For the whole shell call, the state proportions are 62.8% running, 5.3% runnable
and 31.9% blocked. The manager's reply span is overwhelmingly running in this
capture; mutex/GIL blocking is not its observed dominant cost. Blocked shell
time includes waiting on the manager and worker channels. These participant
spans overlap, so manager time must not be added to shell wall time. The
scheduler decode reports no lost events.

### Scope, hardware and next experiment

Linux uses an AMD EPYC 9V74 with four logical CPUs assigned, Linux
7.0.0-1019-azure/glibc 2.43, Python 3.13.18 and Bash 5.3.9. macOS uses an Apple
M1 virtual machine with three logical CPUs, macOS 26.7.2, Python 3.13.17 and
Bash 5.3.20. Source and worker SHA-256 identities are in the archived summaries.
The macOS runner differs from the earlier matched experiment's M2 Pro; these
absolute timings cannot be compared as before/after results across those runs.

macOS CPU/wall profiling completed, but two dtruss launch approaches captured
startup rather than a completed benchmark. The final trace attempts explicitly
record status 1 and `workload_completed: false`; no macOS kernel syscall-count
or blocked/runnable claim is made. Its shell-call dispatch/reply mean CPU/wall
ratio is 85.9%, with a 31.4 us mean wall-minus-CPU tail and a 1.7 us median gap.
The exact cause of that tail remains unresolved. Its separate command-
substitution cohort has a 98.0% mean CPU/wall ratio. Linux scheduler findings
are not assumed to transfer to macOS.

The next focused experiment is a PID-only allocation reply, with Bash building
the worker directory from its known session prefix. This would remove generic
Path construction/rendering on the manager's reply path and shorten the
client's byte-at-a-time input. For the captured Linux five-digit PIDs, the
reply would be six bytes rather than 42, reducing reply reads mechanically
from 42 to six. A matched protocol experiment is still required to measure
latency savings; the macOS one-byte-read behavior has not been verified by a
kernel trace. A second small client experiment is using `read -u` directly
where read currently redirects from another descriptor, preserving dynamic
descriptor allocation while avoiding that redirection setup.

A C manager can reduce manager-side CPU, but it leaves the measured Bash
reply-reading and worker I/O setup costs in place. The evidence supports the
short-reply/client-I/O experiments before committing to a language migration.
Production code is unchanged by this profiling work.

The tools use the documented
[thread CPU clock](https://docs.python.org/3/library/time.html#time.thread_time_ns),
[strace call records](https://man7.org/linux/man-pages/man1/strace.1.html) and
[perf scheduler events](https://man7.org/linux/man-pages/man1/perf-sched.1.html).
Seven local accounting/parser tests cover nesting, thread isolation, exception
cleanup, self-time subtraction, syscall reassembly, reply-versus-refill boundaries
and blocked-versus-runnable separation.

Readable results: [Linux analysis](evidence/preload-profile-4086-Linux-analysis.json)
and [macOS analysis](evidence/preload-profile-4086-macOS-analysis.json).
Complete raw captures, including failed macOS trace logs, are archived as
[Linux ZIP](evidence/preload-profile-4086-Linux.zip) and
[macOS ZIP](evidence/preload-profile-4086-macOS.zip). Their SHA-256 digests are
`a76cbdb0f011cddc98b8fceb141869c793c75cade4b6a5bd59b452003e44c43e`
and `14d673a44530ab07c97afb582fab7413521a75a91886b75cd87e42ddb27e73ef`.
After extraction, reproduce the analysis with
`python t/perf/analyze_preload_profile.py DIRECTORY --output analysis.json`.

## Unix FIFO versus TCP and fixed-width PID replies

Run the standalone communication experiment with
`python t/perf/compare_fifo_tcp.py --output results.json`. It requires Unix and
Bash with EPOCHREALTIME. The JSON retains every validated RTT in microseconds,
per-round medians/P95s, source SHA-256, runtime versions and hardware details.
The workflow's `transport=true` input runs it on Linux and macOS without
building or changing Git's production transport.

### Historical comparison scope

The downloaded transcript
`Rwf-809-Git-Os-Performance-Disparity-Discovery__Windows IPC Benchmark Results_2026,11,06;15,38,56-2026,11,07;00,04,09.md.zst`
explicitly calls the earlier experiment Windows-only and says it does not
run Linux/macOS (line 29). Its result table at lines 839-843 reports TCP
144 us median/201 us P95 versus a two-instance duplex named pipe at
47 us/71 us, with 150/150 exchanges each. The independent-run table at
lines 873-877 reports 196 versus 63 us. Those results establish a Windows
comparison; they provide no Unix FIFO-versus-TCP measurement. The one-instance
pipe failed to connect and is not a valid latency result.

### Controls and completed runs

Each round uses a persistent Bash caller and a Python server thread. Both
transports use the same server request parser and blocking `os.read`/`os.write`
operations. FIFO uses two private, precreated endpoints; TCP uses one private
accepted loopback connection. Endpoint creation, opening and connection setup
precede warmup. There are no mailbox claims, locks, Git workers or per-request
helper processes in this experiment.

Every request is one `GET\n` write. The reply is the server PID padded to eight
ASCII hexadecimal digits. `line` sends eight digits plus newline and uses
`read -r`; `fixed8` sends eight digits and uses `read -r -N 8`; `fixed9`
sends exactly the same nine bytes as `line` and uses `read -r -N 9`.
All reads use `-u` and a ten-second failure timeout, with `LC_ALL=C` and Bash
POSIX mode disabled. Every reply is checked for exact content. The timer
includes the request write and reply read, but excludes reply validation and
sample emission. EPOCHREALTIME gives microsecond-resolution wall-clock samples;
negative deltas are rejected.

Each FIFO/TCP cohort uses three ABBA rounds (FIFO, TCP, TCP, FIFO), 30 warmup
exchanges and 1000 measured exchanges per connection. Default TCP and
server-side TCP_NODELAY have separate FIFO baselines; Bash's client socket
retains default options. Each platform records 72,000 measured exchanges per
run, with no failed exchanges. Two protocol tests also passed on each platform,
covering fragmented/coalesced requests, malformed input and premature EOF.

- [Initial run 43082592700](https://github.com/Ma-XX-oN/git/actions/runs/43082592700),
  source `44afa6abb4`: framing cases grouped in sequence;
  [complete raw JSON ZIP](evidence/fifo-tcp-44afa6.zip).
- [Ordering-control run 43082917994](https://github.com/Ma-XX-oN/git/actions/runs/43082917994),
  source `f3580912b3`: framing order rotates each round so each case runs
  early, middle and late; [complete raw JSON ZIP](evidence/fifo-tcp-f35809.zip).

### Matched results

The table pools 6000 samples per transport/framing from the ordering-control
run's default-TCP cohort. P95 uses sorted index `int(.95 * (n - 1))`.
Percentages normalize TCP's median against its matched FIFO median.

| Platform | Reply/read | FIFO median / P95 us | TCP median / P95 us | TCP median change |
|---|---|---:|---:|---:|
| Linux | 9-byte line | 49 / 62 | 62 / 77 | +26.5% |
| Linux | 8-byte fixed | 32 / 36 | 39 / 50 | +21.9% |
| Linux | 9-byte fixed | 33 / 38 | 40 / 52 | +21.2% |
| macOS | 9-byte line | 81 / 177 | 118 / 250 | +45.7% |
| macOS | 8-byte fixed | 64 / 149 | 111 / 194 | +73.4% |
| macOS | 9-byte fixed | 69 / 153 | 118 / 224 | +71.0% |

Server-side TCP_NODELAY did not reverse the transport result. Its independently
matched FIFO/TCP median pairs were Linux 49/60, 32/39 and 34/41 us, and macOS
90/125, 63/106 and 64/111 us, for line/fixed8/fixed9 respectively. TCP remained
20.6-22.4% slower on Linux and 38.9-73.4% slower on macOS in those cohorts.

Fixed8 reduced FIFO median latency versus line reads by 17 us on each runner:
34.7% on Linux and 21.0% on macOS in the default-TCP cohorts. The identical-wire
fixed9 control reduced FIFO latency by 16 us (32.7%) on Linux and 12 us (14.8%)
on macOS. This supports a benefit from fixed-length reading independently of
removing the newline. It does not establish syscall counts on macOS.

The initial run also favored FIFO throughout: default-TCP FIFO/TCP medians were
Linux 58/96, 43/61 and 43/73 us, and macOS 73/116, 61/104 and 62/97 us for
line/fixed8/fixed9. Do not treat differences between runs as optimization
effects: the Linux CPU model changed between jobs and macOS has substantial
latency tails. Only within-job matched results support the percentages above.

The ordering-control Linux runner has four logical CPUs on an AMD EPYC 9V74,
Linux 7.0.0-1019-azure/glibc 2.43, Python 3.13.18 and Bash 5.3.9. The initial
Linux runner used an AMD EPYC 7763 with four logical CPUs and the same runtimes.
Both macOS jobs report an Apple M1 virtual machine with three logical CPUs,
7 GiB memory, macOS 26.7.2, Python 3.13.17 and Bash 5.3.20.

The measured recommendation is to retain FIFO and evaluate the fixed-width PID
reply in the real allocation path. TCP provides connection separation but did
not improve established-connection latency here. These are serial raw
communication results; they exclude allocation, worker I/O, connection churn,
authentication and contention. The 17 us median reduction is not a predicted
full-command speedup, and this experiment makes no C-manager performance claim.

## Fixed-width PID allocation replies

Commit `774f78d500` changes successful Unix GET replies from a full
worker-directory line to exactly eight ASCII hexadecimal PID digits. The shell
reads them with `read -r -N 8` and rebuilds the worker directory from its known
session prefix. A zero PID introduces the existing error-message line, so
allocation failures keep their diagnostics. PRELOAD and STOP keep their line
replies; Windows is unchanged.

The workflow's `fixed-pid` job builds one worker binary and runs both the
baseline client (`26501aaea0`, the last line-reply revision) and the candidate
against it. It records three ABBA blocks of full calls (12 rounds per workload,
three initial workers, startup excluded) for repeated shell calls and `$(...)`
command substitutions. It then records paced stage timings of 300 calls in
baseline, candidate, candidate, baseline order. Fixed-frame shell tests cover
consecutive replies, an error followed by success, malformed frames and EOF
mid-frame; both platforms also pass Makefile/Meson worker validation and the
6000-command stress check.

- [Run 43085205935](https://github.com/Ma-XX-oN/git/actions/runs/43085205935),
  source `d97275f854`: Linux completed; macOS stalled in a baseline
  command-substitution round and the run was cancelled. The stall exposed an
  unbounded cleanup wait in the harness, fixed in `2ced40b1b1`, which also saves
  each round as it completes. The macOS logs showed repeated shell calls at
  2.840 ms baseline versus 2.757 ms candidate before the stall; no macOS raw
  samples were saved, so that figure is log-only.
  [Linux raw JSON ZIP](evidence/fixed-pid-d97275-linux.zip).
- [Run 43087127296](https://github.com/Ma-XX-oN/git/actions/runs/43087127296),
  source `2ced40b1b1`: both platforms completed;
  [complete raw JSON ZIP](evidence/fixed-pid-2ced40.zip).

### Stage results

Paced stage medians in microseconds, one value per stage run:

| Platform / run | Stage | Baseline | Candidate |
|---|---|---:|---:|
| Linux 43085205935 | client sent->replied | 146, 142 | 100, 97 |
| Linux 43085205935 | manager allocated->delivered | 54, 54 | 34, 33 |
| Linux 43085205935 | client total | 1000.5, 1001 | 949, 951 |
| Linux 43087127296 | client sent->replied | 136, 143 | 97, 101.5 |
| Linux 43087127296 | manager allocated->delivered | 57.5, 60 | 35, 36.5 |
| Linux 43087127296 | client total | 998, 1001.5 | 961, 966 |
| macOS 43087127296 | client sent->replied | 501, 517 | 388, 425.5 |
| macOS 43087127296 | manager allocated->delivered | 149.5, 157.5 | 87, 86.5 |
| macOS 43087127296 | client total | 2766, 2925 | 2629.5, 2719 |

Every candidate stage run is below every baseline stage run for these three
stages. The exchange saves 35-49 us on Linux and 75-129 us on macOS; about
half of that on Linux and most of it on macOS is in manager reply construction
and delivery, where pathname construction is gone. In run 43087127296 the reply transit stage also falls
(Linux 32-33 to 18-19 us; macOS 104.5-114.5 to 81-93 us), consistent with the one-byte read
loop being replaced by a fixed-length read.

### Full-call results

| Platform / run | Workload | Baseline ms | Candidate ms | Change |
|---|---|---:|---:|---:|
| Linux 43085205935 | shell | 1.131 | 1.093 | -3.4% |
| Linux 43085205935 | `$(...)` | 2.022 | 1.998 | -1.2% |
| Linux 43087127296 | shell | 1.140 | 1.118 | -1.9% |
| Linux 43087127296 | `$(...)` | 1.991 | 1.987 | -0.2% |
| macOS 43087127296 | shell | 2.333 | 2.563 | +9.9% |
| macOS 43087127296 | `$(...)` | 3.259 | 3.275 | +0.5% |

On Linux, every candidate repeated-shell round median in run 43085205935 is
below every baseline round; in run 43087127296, five of six candidate rounds
are below every baseline round (the exception is round 1, 1.148 ms). The
`$(...)` rounds overlap in both runs, so that workload's change is within
noise.

The macOS full-call comparison is not usable as evidence either way. Its round
medians drifted upward through the job regardless of variant: shell rounds rose
from 1.892 ms (baseline, round 0) to 2.4-3.0 ms, and `$(...)` rounds from
2.46 ms to 4.40 ms, a drift an order of magnitude larger than the 75-130 us
stage effect. The ABBA order does not cancel drift of that size: the +9.9%
shell result is driven by the two lowest rounds of the job (baseline rounds 0
and 3, 1.892 and 2.044 ms), which ran before the drift set in. The stage runs, which are shorter and interleaved, show the
candidate faster on macOS as well.

Run 43085205935 used a four-CPU AMD EPYC 9V74 Linux runner; run 43087127296
used a four-CPU AMD EPYC 7763 Linux runner (Linux 7.0.0-1019-azure, glibc 2.43,
Python 3.13.18, Bash 5.3.9) and a three-CPU Apple M1 virtual macOS 26.7.2
runner with Python 3.13.17 and Bash 5.3.20. Compare results only within a job.

### Conclusion

The fixed-width PID reply removes about 40 us (Linux) and 100 us (macOS) from
the allocation exchange, as the stage timings show on every run. On Linux this
is a 2-3.5% reduction for repeated shell calls; command substitutions, which
add mailbox claiming and more descriptor work, show no resolvable change. The
remaining per-call cost is dominated by worker-channel setup
(`freed->channels`, `channels->request`) and output/status handling, which this
change does not touch. Manager-side forwarding of cwd and arguments over a
preconnected context channel targets that remaining cost and is the next
experiment; measure it in two variants (forwarding with a channel opened at GET
time, then with a channel prepared alongside the worker) to separate moving work
out of Bash from removing connection setup.

### Windows

Windows now uses the same GET frame, read by the shared `_preload_read_pid`
helper; PRELOAD and STOP keep line replies. It was adopted for a single
protocol and reader on both platforms, not for speed: the local measurements
below show no resolvable Windows latency change.

An MSYS `strace` of four preloaded calls shows the mechanism. Baseline Bash read
each 36-byte reply (`\\.\pipe\preload-<owner>\worker-<pid>` plus newline) with
36 one-byte `read()` calls, 144 in total; MSYS opens the request pipe through
`fhandler_disk_file`, but Bash still treats it as unseekable. The candidate
reads each reply with one eight-byte `read()`. strace inflates per-call times,
so the trace gives counts only.

Matched comparison on the laptop (AMD Ryzen 7 5700U, 16 logical CPUs, Windows 11
10.0.26200, Python 3.13.1, Bash 5.2.37 MSYS), one `git.exe` for both variants,
baseline `6cbb403fae`, three ABBA blocks of 12 rounds per workload:

| Workload | Baseline ms | Candidate ms | Change |
|---|---:|---:|---:|
| shell | 4.153 | 4.123 | -0.7% |
| `$(...)` | 31.039 | 31.240 | +0.6% |

Shell round medians overlap (baseline 4.096-4.204 ms, candidate
4.078-4.235 ms); `$(...)` is dominated by MSYS fork cost.

Paced stage medians (300 calls each, run order baseline, candidate, candidate,
baseline), in microseconds:

| Stage | Baseline | Candidate |
|---|---:|---:|
| client sent->replied | 245, 258 | 239, 250 |
| manager allocated->delivered | 55, 56 | 52, 52 |
| client replied->channels | 1173, 1203 | 1239, 1190 |
| client request->output | 1632, 1628 | 1660, 1631 |
| client total | 3826, 3856 | 3908, 3890 |

Unlike Unix, the exchange stages differ by less than run-to-run variation. The
likely reasons, not separately measured: the Windows manager built its reply
with one f-string and had no mailbox lock check, so there was little manager
work to remove, and the one-byte reads are cheap without tracing. Windows per-call time is dominated by opening the five
worker channels (about 1.2 ms) and command/output handling (about 1.6 ms),
which makes manager-side forwarding over a preconnected channel the more
promising Windows experiment.

Windows validation with the change: the shared-reader test, manager protocol
(including Git Bash's throwaway connection) and concurrency checks, owner
death, manager lifecycle, shell environment, existing regression scripts,
shell output, and the 6000-command acceptance stress (median 5.742 ms) pass.
[Raw comparison, stage JSON and strace read tallies](evidence/fixed-pid-windows-laptop.zip).
