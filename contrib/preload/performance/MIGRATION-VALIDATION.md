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

### Historical shadow checkpoint

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
changes require re-sourcing. Removing harmless shadow metadata was not required.
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

Investigative diagnostics remain in the source. After all native acceptance
passes, remove the ad hoc exit-file logger, per-phase breadcrumbs/timers,
ablation switches and obsolete dumps in a separate change. Preserve useful
startup, marker, transport, owner-death, status-delivery and cleanup diagnostics
through Git tracing. Rebuild and repeat acceptance and matched comparisons for
that cleanup revision; append its results here before reporting completion.
