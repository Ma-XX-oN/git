# Windows serial IPC performance journal

This journal records the change, evidence and limits of each performance result.
The raw measurements in [performance/evidence](performance/evidence/) are committed
with the journal so they survive temporary-directory and benchmark-folder cleanup.
The objective is **4–5 ms median on the native Windows GitHub runner**, with three
workers and complete cwd/environment transfer. Application stdin and binary output
retain the limitations in [the transport contract](TRANSPORT-CONTRACT.md).
The per-call environment-transfer requirement was superseded by startup
inheritance, as described below; earlier measurements retain their original
protocol and workload definitions.

## Startup-environment contract

### Production diagnostic cleanup

The final cleanup removes ad hoc exit-file writes, per-phase success tracing,
Bash timing/ablation switches and the unused timeout minidump helper. It retains
the blocking two-connector rendezvous, serial persistent allocation,
reply-before-refill scheduling, grouped request writes and shell-pattern pipe
validation. The adopted 100 ms text reader is the single Windows reader.
Operational errors and marker diagnostics continue through Git tracing.
Local cleanup checks passed both native builds and 6,000 serial commands;
that traced stress result is not a performance comparison. The cleanup CI
matrix passed on all three platforms in
[run 42884766360](https://github.com/Ma-XX-oN/git/actions/runs/42884766360).

The cleanup SHA `5300cc41e2` measured **5.0000 ms baseline** versus
**4.1305 ms migrated** on the same laptop: **82.61% of baseline**, **-17.39%**,
or **-0.8695 ms**. The untraced ABBA rounds measured 4.9215/5.0675 ms for
baseline and 4.1340/4.1300 ms for candidate, with 240 samples per variant.
Raw samples, matching compiler flags, binary identities and machine metadata
are in [migration-5300cc-windows-laptop-matched.json](performance/evidence/migration-5300cc-windows-laptop-matched.json).
This compares the final implementation with the original tag; differences
between this and earlier separately timed checkpoints do not isolate cleanup's
cost or establish a gain caused by diagnostic removal alone.

The final same-job CI comparisons at `5300cc41e2` were:

| Platform | Baseline ms | Migrated ms | Baseline % | Change % |
| --- | ---: | ---: | ---: | ---: |
| Windows | 6.0075 | 4.1750 | 69.50% | -30.50% |
| Linux | 1.8050 | 1.0500 | 58.17% | -41.83% |
| macOS | 4.0470 | 2.5290 | 62.49% | -37.51% |
| Laptop | 5.0000 | 4.1305 | 82.61% | -17.39% |

Windows meets the original 4–5 ms command-latency target. Each percentage uses
its own matched baseline; absolute values from different hosts or CI runs are
not pooled. All comparisons use three workers, identical flags within each
pair and tracing disabled. Raw CI samples are in
[Windows](performance/evidence/migration-5300cc-windows-matched.json),
[Linux](performance/evidence/migration-5300cc-linux-matched.json) and
[macOS](performance/evidence/migration-5300cc-macos-matched.json).
The [19-checkpoint history](performance/migration-checkpoints/report.md)
preserves earlier gains, regressions, workload policies and machine metadata.

### Final timings against unmodified Git

The [final graphs](performance/final-comparison/report.md) compare the
migrated build, preloaded, with unmodified upstream Git `750f5de7df` launched
directly. That is the upstream commit this branch starts from, built in the
same job with identical compiler flags. Each round interleaves 120 direct and
120 preloaded `git --version` calls; ABBA rounds alternate the directly timed
binary between upstream and migrated Git. CI ran in
[run 42893906943](https://github.com/Ma-XX-oN/git/actions/runs/42893906943);
the laptop ran locally. Superscript and subscript values are the distances to
the upper and lower bounds of the 95% bootstrap confidence interval of the
median.

| Machine | Unmodified Git ms (95% CI) | Migrated preloaded ms (95% CI) | % of unmodified (95% CI) | Change % |
| --- | ---: | ---: | ---: | ---: |
| Windows CI, EPYC 9V74 / 4 | 16.94<sup>+0.29</sup><sub>-0.41</sub> | 2.893<sup>+0.014</sup><sub>-0.018</sub> | 17.08%<sup>+0.44</sup><sub>-0.30</sub> | -82.92% |
| Linux CI, EPYC 7763 / 4 | 1.421<sup>+0.017</sup><sub>-0.028</sub> | 1.0790<sup>+0.0050</sup><sub>-0.0070</sub> | 75.9%<sup>+1.6</sup><sub>-1.0</sub> | -24.1% |
| macOS CI, M1 Virtual / 3 | 7.13<sup>+0.21</sup><sub>-0.37</sub> | 2.381<sup>+0.093</sup><sub>-0.064</sub> | 33.4%<sup>+2.1</sup><sub>-1.2</sub> | -66.6% |
| Laptop, Ryzen 7 5700U / 16 | 55.20<sup>+1.03</sup><sub>-0.36</sub> | 4.181<sup>+0.049</sup><sub>-0.053</sub> | 7.575%<sup>+0.097</sup><sub>-0.175</sub> | -92.425% |

The migrated build launched directly measured -4.2% (Linux), -2.9% (Windows),
-11.1% (macOS) and +2.4% (laptop) against unmodified Git. These point estimates
alone do not establish statistical equivalence or rule out ordinary-path
overhead. Absolute times differ
from earlier runs on other runner instances; only same-job values are compared.
Raw samples are in `performance/evidence/upstream-750f5d-*.json`.

### Environment policy

The Bash client inherits the environment captured by native manager startup.
Arguments and cwd remain per command. Re-sourcing replaces the manager and its
workers after waiting for cleanup, refreshing exported values and removals.
This removes per-command export serialization, path-cache checks and shadow
selection. All callers use the manager's startup environment. Per-request
environment encodings, their decoders and the worker-only changed-entry updater
are removed. The historical checkpoints below retain their own
environment policies; they are not measurements of this revised contract.

Local Windows validation passed 6,000 serial client commands, manager restart,
environment refresh/removal, resizing, subshell isolation, native transport,
owner death and startup/shutdown ownership checks. The traced stress median was
7.028 ms; tracing and the absence of a matched baseline prevent a performance
claim from that number. A new untraced ABBA comparison is required, with an
unchanged environment and the two revisions' different capture policies stated
explicitly in its metadata.

The same-laptop ABBA comparison for `ba16959609` measured **4.6580 ms baseline**
versus **4.0860 ms migrated**: **87.72% of baseline**, **-12.28%**, or
**-0.5720 ms**. Baseline round medians were 4.6820/4.6455 ms; candidate rounds
were 4.0515/4.1175 ms. Each variant has 240 IPC samples, three workers, matching
compiler flags and tracing disabled, on DESKTOP-P0W3R (AMD Ryzen 7 5700U).
The workload leaves the environment unchanged after manager startup. Baseline
serializes its snapshot per call; the candidate inherits startup values. This
measures the revised contract and combined implementation, not an isolated
shadow-removal gain. Do not subtract the earlier shadow checkpoint's times:
its matched baseline was different. Raw samples and machine/runtime identities
are in [migration-ba1695-windows-laptop-matched.json](performance/evidence/migration-ba1695-windows-laptop-matched.json).
The [checkpoint graphs](performance/migration-checkpoints/report.md) show both
absolute milliseconds and within-comparison baseline-normalized percentages.

The same-job Linux comparison at `ba16959609` measured **1.7755 ms baseline**
versus **1.0550 ms migrated**: **59.42% of baseline**, **-40.58%**, or
**-0.7205 ms**. This AMD EPYC 7763 runner used the same unchanged-environment
workload and explicit per-variant capture policies as the laptop comparison.
Baseline rounds were 1.7720/1.7795 ms; candidate rounds were 1.0550/1.0555 ms.
Both full Git suites, Make/Meson builds and the no-pthread build check passed in
[job 129688162673](https://github.com/Ma-XX-oN/git/actions/runs/42774012137/job/129688162673).
The overall job failed the separate unset-editor fixture described in the
validation record; this performance result does not establish native acceptance.
Raw timing and hardware/runtime metadata are preserved in
[migration-ba1695-linux-matched.json](performance/evidence/migration-ba1695-linux-matched.json).

### Three-platform startup-environment checkpoint

Run [42775783963](https://github.com/Ma-XX-oN/git/actions/runs/42775783963) at
`3b0bdbf56b` retained independent same-job ABBA comparisons:

| Platform | Baseline ms | Migrated ms | Change ms | Baseline % | Change % |
| --- | ---: | ---: | ---: | ---: | ---: |
| Windows | 4.3145 | 3.2590 | -1.0555 | 75.54% | -24.46% |
| Linux | 1.4295 | 0.8555 | -0.5740 | 59.85% | -40.15% |
| macOS | 3.9835 | 2.4150 | -1.5685 | 60.63% | -39.37% |

Each row uses three workers, 240 IPC samples per variant, matching build flags,
disabled tracing and an environment unchanged after manager startup. Baseline
serializes the snapshot per call; the candidate inherits startup values. These
measure the complete implementation and accepted contract together. They do not
isolate one optimization or establish a laptop-to-CI conversion ratio.
The Windows command median is below the original 4–5 ms CI target. Final cleanup
requires its own measurements and acceptance.

Native acceptance and 6,000 commands passed on all three platforms. Linux and
macOS also passed both full Git suites. Windows passed the Makefile suite;
Meson reported 955 passed and one environment failure, `winpwd: command not found`
in t5580. This is separate from the measured command workload and still requires
correction before investigative diagnostic removal. Raw samples, revisions and
hardware/runtime metadata are in
[Windows](performance/evidence/migration-3b0bdb-windows-matched.json),
[Linux](performance/evidence/migration-3b0bdb-linux-matched.json) and
[macOS](performance/evidence/migration-3b0bdb-macos-matched.json).

### Environment-code removal checkpoint

The user required removal of the unused native environment-update API as well
as the Bash sender. Commit `2ac970f4cb` removes both decoders, the worker-only
Windows replacement helpers/snapshot mode and their obsolete comparator.
The worker context implementation is reduced from 558 to 43 lines; ordinary
child-process environment merging remains live. Re-sourcing refreshes inherited
values for all callers. The local Makefile and Meson builds, cwd/inheritance,
worker lifecycle, supported output, manager restart, ordinary run-command and
6,000-command stress checks pass. Full native CI remains required.

The same-laptop ABBA comparison measured **6.5345 ms baseline** versus
**5.6155 ms migrated**: **85.94% of baseline**, **-14.06%**, or **-0.9190 ms**.
Baseline rounds were 6.5470/6.5185 ms; candidate rounds were 5.6980/5.5665 ms.
Each variant has 240 IPC samples, three workers, matching flags and tracing
disabled. Both absolute medians are higher than in the earlier laptop run;
this comparison does not isolate the removal's effect or establish its cause.
The unchanged-environment workload and per-variant capture policies are retained
in [migration-2ac970-windows-laptop-matched.json](performance/evidence/migration-2ac970-windows-laptop-matched.json).

## Abandoned path-shadow checkpoint

**What it was meant to do.** At this checkpoint each command sent the caller's
complete `export -p` environment to the worker, so that a per-command change
such as `VAR=3 git ...` reached Git. On Windows the standard path variables
(`PATH`, `HOME`, `TMP` and others) arrive in MSYS form, such as a
colon-separated POSIX `PATH`, which native Git cannot use: a small shell alias
failed because native shell lookup found nothing. The shell would convert those
variables with its own `cygpath -w` and export the native values as
`__git_env_NAME` copies ("shadows") for the worker to use.

**Why it was a mistake.** The shadows were redundant. The workers already
inherit the manager's startup environment, with its native values, and the
shadows were created from that same environment; in the normal case of an
environment unchanged since startup they carried exactly what the worker
already had. They did nothing useful, yet the implementation added per-command
work: cache comparisons in the client on every call and shadow selection and
removal in the worker. That work contributed to the checkpoint's measured
+11.33% (+0.6245 ms) regression on the laptop; isolation runs attributed
0.4165 ms to the per-call
cache checks alone. Honouring genuinely changed path variables per command was
not practical either: drive prefixes are not the only MSYS mapping (`/usr/bin`,
`/home`, `/tmp`, mount points and path lists also differ, and Git Bash and MSYS2
place `/home` under different roots), so a cheap drive-prefix translation in
the worker would be wrong. Per-command environment changes were therefore
dropped. The next commit, `ba16959609`, makes workers use the manager's startup
environment; re-sourcing `git-manager.sh` replaces the manager and its workers
to pick up changes. All shadow code was removed; none remains in the final
source. The original record follows as a negative result.

The Windows shell caches converted values for the runtime's eight standard
path variables through its own `/usr/bin/cygpath`. Exported `__git_env_NAME`
shadows accompany the unchanged `export -p` snapshot; the worker selects their
native values and removes the metadata. Source-value caches remain unexported.
Startup performs the conversions; an unchanged serial call checks the caches
with Bash builtins. A changed value invokes one conversion process.

The cache test verifies eight initial conversions, zero on an unchanged refresh
and one after a single value changes. The command path gains eight comparisons,
the shadow declarations and worker-side selection. The correction
allows a small shell alias to complete with status 7 where the raw POSIX PATH
previously prevented native shell lookup. The larger dual-stream alias remains
an unresolved acceptance failure, recorded in the migration validation notes.

The same-laptop ABBA comparison at `20a957982a` measured 5.5095 ms for
`working-version-two` and 6.1340 ms for the candidate: +0.6245 ms, +11.33%, or
111.33% of baseline latency. Each variant used 240 serial IPC samples, three
workers, matching build flags, full context and tracing disabled. The host was
DESKTOP-P0W3R, an AMD Ryzen 7 5700U. This is a regression for the complete
migration checkpoint; it does not isolate shadow handling from earlier changes.
Baseline round medians also varied from 5.7415 to 5.1300 ms, so retain that drift
when interpreting the aggregate. Raw timings and runtime metadata are in
[migration-20a957-windows-laptop-matched.json](performance/evidence/migration-20a957-windows-laptop-matched.json).

Two diagnostic ABBA comparisons then held the binary and exported shadows
constant. Suppressing only unchanged refresh checks after startup measured
6.1865 ms versus 5.7700 ms, isolating 0.4165 ms of refresh overhead in that run.
Replacing the individual checks with one quoted snapshot comparison measured
6.0035 ms versus 5.6820 ms: a reduction of 0.3215 ms (5.36%) in that separate run.
The combined key includes each variable's set/unset flag and quoted value, so
empty, missing and delimiter-containing values remain distinguishable. Changed
snapshots still refresh the individual entries; unchanged calls return early.
The conversion-count and readonly/empty/unset cache tests continue to pass.
These isolation results are not comparisons with `working-version-two` and
must not be pooled with its measurements. Raw samples and exact benchmark shell
overrides are retained in
[refresh isolation](performance/evidence/migration-20a957-refresh-isolation.json)
and [combined-check isolation](performance/evidence/migration-20a957-combined-refresh-isolation.json).

## Comparison rules

- Compare laptop with the same laptop experiment, or CI variants within the same
  CI job. Different GitHub jobs can have different machines and load.
- Hold worker count, workload, build flags, DLL/runtime selection, protocol and
  tracing constant within a comparison. Warm `git --version` is the workload
  below; startup and initial pool preparation are excluded.
- Use ABBA order for changes under test; preserve each round and its samples.
  Three-variant isolation uses ABCCBA within one host/job, with matching native
  binary paths and the same client except for the two validation lines.
  Local ABBA entries below have 80 paired direct/IPC calls per round and 160 IPC
  samples per variant. Direct calls alternate order with IPC calls.
- Keep diagnostic traces and phase timers off for end-to-end performance.
  Report stress/phase timings separately. Phase medians do not add up to the
  median total and are not an exact decomposition of an end-to-end difference.
- A disabled-feature experiment only bounds a cost. It is not a valid optimized
  implementation. Correctness failures reject a candidate even when it is faster.
- Preserve negative results. Do not add gains from different experiments or use
  a laptop/CI speed ratio to claim a measured gain on the other machine.

## Matched laptop results

| Experiment | Before median | After median | Observed reduction | Disposition |
|---|---:|---:|---:|---|
| Peer-managed connection rendezvous | 7.8940 ms | 7.5095 ms | 0.3845 ms | Adopted in `b0412d606f` |
| Reply-before-refill plus grouped Bash request writes | 7.3615 ms | 6.5445 ms | 0.8170 ms | Adopted in `82080aa670` |
| Shell patterns instead of regex pipe validation | 6.7650 ms | 6.5045 ms | 0.2605 ms | Adopted in `f62fbc2d7b`; same-job CI below |
| One-call environment replacement | 5.2780 ms | 4.7915 ms | 0.4865 ms | Rejected: three compatibility failures |
| Changed-only environment updates | 5.5045 ms | 4.9770 ms | 0.5275 ms | Temporary prototype; final candidate below supersedes it |
| Final candidate: regex to patterns (A/B) | 5.5645 ms | 5.2880 ms | 0.2765 ms | Same laptop ABCCBA experiment below |
| Final candidate: native environment (B/C) | 5.2880 ms | 5.0450 ms | 0.2430 ms | Same laptop ABCCBA experiment below |
| Final candidate: combined (A/C) | 5.5645 ms | 5.0450 ms | 0.5195 ms | Same laptop ABCCBA experiment below |

These are separate matched experiments. Their baseline variation is visible in
the raw round medians; the reductions must not be added together. An earlier
exploratory pattern-matching comparison suggested roughly 0.5–0.6 ms; the
adoption comparison above measured 0.2605 ms and is the value retained here.

### Peer rendezvous

The two prestarted connector threads traverse the five channel pairs themselves.
The winner cancels its peer, waits for its arrival generation, inspects both
results and publishes completion. This removes main-thread dispatch between pairs.
Client, manager, protocol, cwd and worker count were held constant. Baseline round
medians were 7.5415/11.4130 ms; peer rounds were 7.4645/7.6070 ms. That substantial
variation limits causal precision; this does not explain the entire historical
latency gap. Process-tree CPU was 17.922 versus 17.328 seconds, including direct
calls and replacement startup, not isolated spin-loop CPU.

The separate native primitive experiment measured parked semaphore wakeups at
39.55–44.30 microseconds median versus 0.80–1.90 microseconds for spinning. Tight
reusable handoff throughput was a different experiment. Spinning consumed a core
while waiting; these primitive numbers are not whole-command savings.

Evidence: [paired calls](performance/evidence/peer-handshake-pairs.json),
[hashes and validation](performance/evidence/peer-handshake-summary.json),
[primitive results](performance/evidence/wait-primitive-results.jsonl).

### Allocation-reply publication race and rejected inlining

The old per-request reply pipe did not exist when some callers first opened it.
Every failed open in the diagnostic capture was allocation reply FD 8 and reported
`No such file or directory`. The retry slept 5 ms. Inlining the open helper made
the caller arrive sooner and increased retry frequency: 26/60 calls retried for
the original helper versus 51/60 for the inlined copy. Comparing only retry-free
subsets would be misleading because those populations differ.

Persistent serial request/reply channels remove per-call creation and that race.
No isolated numerical gain for persistence is claimed from these captures.
Concurrency is outside the supported execution model.

Evidence: [initial samples](performance/evidence/reply-race-pairs.json),
[retry descriptors](performance/evidence/reply-race-descriptors.json),
[open errors](performance/evidence/reply-race-open-errors.log).

### Ordered completion, reply scheduling and grouped writes

Commit `82080aa670` flushes and closes stdout, then stderr, then writes, flushes
and closes the exit result, returning through normal CRT shutdown without a
Windows nonlocal jump. A backpressure regression rejects the original status-first
normal-exit worker. This establishes the completion-order defect; it does not prove
that every historical exit-result failure had the same cause.

The 0.8170 ms comparison isolates two additional changes with that ordered-exit
binary held constant: submit the READY worker ID before starting its replacement
thread, and group Bash frame writes under one redirection. It does not measure
the independent gain from replacing the exit mechanism. No helper inlining,
feature omission or worker-count increase is included.

Evidence: [paired calls](performance/evidence/ordered-exit-client-pairs.json),
[source/binary hashes and validation](performance/evidence/ordered-exit-source-hashes.json).

### Shell-pattern validation

The helper validates descriptor, marker and retry count with shell patterns,
avoiding three regular-expression compilations per pipe open. The helper,
descriptor layout, retry policy and marker writes are unchanged. The production
change agrees with its baseline on 560 valid/invalid combinations, including
malformed arithmetic-looking inputs, with the extglob shell option disabled.
The complete local 6,000-command Windows stress workflow passed.

Evidence: [paired calls](performance/evidence/pattern-validation-pairs.json),
[boundary result](performance/evidence/pattern-validation-boundaries.json).

### Environment prototypes

The one-call prototype reuses Git's sorted Unicode environment-block builder and
calls `SetEnvironmentStringsW`. It was faster on ordinary snapshots, but failed
an entirely empty environment and values of 32,768 and 40,000 characters that the
baseline accepted. It is not adopted. The failure summary is preserved from the
recorded test output; the working test file was subsequently regenerated for the
changed-only prototype.

The changed-only prototype also reuses the builder, but merges the existing and
desired blocks and calls `SetEnvironmentVariableW` only for changed/deleted keys.
Changed key spelling is deleted and recreated to preserve case. It preserves
Windows drive-directory entries and last-wins aliases. All 14 differential cases
pass, including Unicode, empty/removed values, aliases, an empty environment,
300 variables, invalid export syntax and long values. Native worker stress and
same-job CI acceptance are required before adopting it; final results follow.

The original 14-case prototype comparison compared case-folded names and values.
The permanent differential test also checks exact name spelling. This exposed
three alias-spelling differences: successive baseline updates retain the first
alias spelling with the last value, while the prototype retained the last spelling.
The production candidate preserves the baseline's first-spelling/last-value rule.
Two additional cases cover renaming an existing variable and an unchanged snapshot.
The prototype timing above remains prototype evidence; the final implementation
requires its own worker tests and matched measurements.

The final candidate passed all 16 exact context comparisons, 26 native worker
lifecycle cases, connection/duplicate-marker/cancellation cases, persistent
allocator tests, Bash framing and the complete local 6,000-command stress run.
Its matched laptop ABCCBA test measured A/B/C medians of 5.5645/5.2880/5.0450 ms,
with 240 calls per variant. Native B/C savings were 0.2430 ms, below the earlier
prototype's 0.5275 ms observation; the final matched result takes precedence for
this candidate. The 4–5 ms objective remains a CI objective and is not established
by this laptop result. The implementation is committed in `4d4fd64486`; its
same-job CI results follow below.

Evidence: [final laptop rounds, samples and identities](performance/evidence/adopted-environment-laptop.json),
[16 exact context comparisons](performance/evidence/adopted-environment-cases.json),
[full local stress log](performance/evidence/adopted-environment-stress.log).

### Reproducing a matched comparison

Build both checkouts with identical flags and native DLLs, then run from the
candidate checkout using native Windows Python and SDK Bash on PATH:

```sh
python experiments/issue-809/performance/compare_windows.py \
  --before-git BASELINE/git.exe --after-git git.exe \
  --baseline-sha 82080aa670fb1665b4827a061a5bc5690066463d --output RESULTS
```

The script isolates A (baseline native plus regex), B (baseline native plus
patterns) and C (changed-only native plus patterns), in ABCCBA order. It records
240 untraced calls per variant, direct-call controls, source/executable hashes,
build flags when available, host, CI run/job and complete round samples. Both
binaries use symmetric temporary paths; all variants use the same manager,
request framing, cwd, environment, workload and three-worker pool. The native
Windows stream workflow builds and runs all variants inside one job.

The context probe makefile in `performance/support` links the identical fixture
against each checkout's real libgit implementation. `performance/compare_context.py`
compares exact resulting environments and return codes for 16 cases, including
long values, drive-directory entries and case aliases. Its output is preserved
alongside the same-job performance artifact.

Evidence: [one-call pairs](performance/evidence/one-call-environment-pairs.json),
[rejection summary](performance/evidence/one-call-environment-semantics.json),
[changed-only pairs](performance/evidence/changed-only-environment-pairs.json),
[differential cases](performance/evidence/changed-only-environment-cases.json),
[prototype patch against `f62fbc2d7b`](performance/evidence/changed-only-environment-prototype.patch).
Apply this zero-context patch with `git apply --unidiff-zero`.

## CI results: historical observations, not a before/after gain

| Source | Three-worker median | Direct Git median | Validation/stress |
|---|---:|---:|---|
| `82080aa670` | 5.8425 ms | 19.6350 ms | Both passed; 6,000 Git stress calls |
| `f62fbc2d7b` | 6.1930 ms | 23.2185 ms | Both passed; 6,000 Git stress calls |

Each row pools 240 untraced calls from its own workflow. These ran in different
GitHub jobs. The second job's direct Git calls were slower too; the table does
not establish a regression or gain from the shell change. A same-job ABBA
comparison is needed for that attribution. Neither row reaches the 4–5 ms target.

Runs and raw data:

- `82080aa670`: [benchmark](https://github.com/Ma-XX-oN/git/actions/runs/42571663814),
  [stress](https://github.com/Ma-XX-oN/git/actions/runs/42571663617),
  [samples](performance/evidence/ci-82080aa670.json).
- `f62fbc2d7b`: [benchmark](https://github.com/Ma-XX-oN/git/actions/runs/42575044315),
  [stress](https://github.com/Ma-XX-oN/git/actions/runs/42575044546),
  [samples](performance/evidence/ci-f62fbc2d7b.json).

## Same-job CI comparison: `4d4fd64486`

[Windows benchmark run 42583383809](https://github.com/Ma-XX-oN/git/actions/runs/42583383809)
passed, including all 16 exact environment comparisons. Baseline `82080aa670`
and candidate `4d4fd64486` were compiled in job `129047248715` with matching
UCRT64 flags and DLL runtime, then measured in ABCCBA order. All variants use
three workers, complete cwd/environment transfer and no diagnostic tracing.

| Variant | Median, 240 calls | Round medians |
|---|---:|---|
| A: baseline native, regex validation | 4.4630 ms | 4.4620 / 4.4660 ms |
| B: baseline native, pattern validation | 4.4160 ms | 4.4080 / 4.4170 ms |
| C: changed-only native, pattern validation | 3.9670 ms | 4.0140 / 3.9415 ms |

The same-job A/B reduction is **0.0470 ms**, B/C is **0.4490 ms**, and A/C is
**0.4960 ms**. This run reaches the 4–5 ms CI objective. Its baseline was faster
than the historical jobs too, so the improvement attributable to these changes
is 0.4960 ms, not the difference from an earlier job's median. The separate
standard 1–5-worker sweep measured three-worker round medians of 3.8460 and
3.8975 ms; those are another cohort, not extra savings to add to the comparison.

Evidence: [matched samples, flags and hashes](performance/evidence/ci-4d4fd64486-matched.json),
[exact context comparisons](performance/evidence/ci-4d4fd64486-context.json),
[standard untraced sweep](performance/evidence/ci-4d4fd64486-pool.json).
The original downloaded workflow artifact remains unchanged; repository text
copies use LF line endings.

[Windows stress run 42583383974](https://github.com/Ma-XX-oN/git/actions/runs/42583383974)
also passed on `4d4fd64486`, attempt 2, job `129048554841`: all 26 lifecycle
cases, marker/cancellation/allocator tests and 6,000 Git commands. Manager handles
were 134 at both the first and final observation (range 134–141). Its traced
stress median of 9.346 ms is not an untraced performance measurement. Attempt 1
exited while sourcing `/etc/profile` before compiler output; only the failed job
was retried, without source changes. Both required native workflows passed.
Evidence: [exact validation-result log excerpt](performance/evidence/ci-4d4fd64486-stress-excerpt.log).

## Production migration audit

This journal and `performance/evidence/` preserve the experimental measurements.
Historical switch names and ticket identifiers in those records describe the
measured revisions. The maintained protocol is documented in
`Documentation/technical/preload.adoc`.

| Adopted optimization | Production implementation audit |
| --- | --- |
| Prestarted blocking connector pair and peer spin rendezvous | Retained in `preload-transport-win32.c`; selector regression coverage includes both connection orders, held probes, duplicate markers and cancellation. |
| Persistent serial allocator channels | Both client request/reply descriptors remain open for the owner session on both platforms. |
| Publish allocation reply before starting replacement | Retained on Windows; POSIX refill moved after successful reply write during review. |
| Group request writes under one Bash redirection | Retained on Windows; applied to POSIX during protocol unification. |
| Shell-pattern validation of pipe names | Retained in the Windows Bash connection helper. |
| Changed-only Windows environment updates | Superseded by startup environment inheritance; the worker-only replacement code is removed. Historical gains remain recorded. |
| Output closure before flushed status, ordinary process exit | Shared completion/status helpers extracted; POSIX nonlocal jumps removed during review. |
| Process-free Bash command context | All callers inherit manager-startup environment and send cwd per command. |

Rejected bulk environment replacement, helper-inlining and incomplete-context
ablations remain historical experiments; they are not production optimizations.

The initial matched laptop migration comparison measured baseline **5.067 ms**
and candidate **5.1705 ms** median (240 IPC samples each): candidate **102.04%**
of baseline, **+2.04%** or **+0.1035 ms**. This is a regression, not a gain.
Raw samples and exact build provenance are in
`t/perf/preload-evidence/windows-before-cleanup.json`. These measurements
precede the review-driven shared protocol and completion changes and do not
measure their effect.

Final comparisons must record each machine separately, with baseline normalized
to 100% within that machine/job. Preserve CPU/architecture, OS, compiler flags,
Python/Bash versions, source revisions and raw samples alongside the absolute
millisecond and normalized percentage graphs. Unknown historical hardware fields
must remain unknown rather than be inferred from another machine.

## Recording the next change

The migration's ongoing acceptance record is
[MIGRATION-VALIDATION.md](performance/MIGRATION-VALIDATION.md). Review changes
share framing, command execution and ordered exit handling across platforms;
they also restore grouped POSIX writes and reply-before-refill scheduling.
The Windows environment decoder and changed-entry updater are split by parsing
and application responsibility, preserving the adopted update strategy.

A reproduced manager shutdown race requires waiting for in-flight worker starts
before releasing pool resources. The synchronization runs around process startup
and shutdown, without holding the pool lock across process creation. Serial
Windows allocator cleanup is expressed as connection identification, GET reading,
readiness waiting and reply publication, using the same event queue. These
changes may affect command latency; no gain is claimed without the next matched
measurement. Keep their raw comparison even if the result is slower.

The ownership audit also reproduced abandonment of a selected worker when its
PID reply had not yet been published. Both pools retain the pending allocation
until full reply completion, so failure cleanup still owns it. This adds a small
ownership update to the serial path; measure its effect with the rest of the
review changes instead of assuming it is free.

Correctness coverage also exposed rejection of readonly and integer declarations
from builtin `export -p`. The shared decoder accepts their already emitted
scalar values and excludes array declarations, matching native Bash child
environments. The ordinary `declare -x` prefix keeps its direct fast path.
Native block encoding remains available for application callers. These changes
are correctness fixes; their combined timing effect belongs in the next matched
comparison rather than an unmeasured gain claim.
Exported Bash functions remain outside the Bash client's snapshot scope; this
existing limitation is documented rather than expanded during this migration.
The POSIX client also retains its request writer for the session, matching the
persistent Windows request/reply pair. Earlier POSIX revisions reopened that
writer for each GET. This removes repeated FIFO opens from the serial path;
its timing contribution remains unmeasured until the next matched run.

### Shared-worker checkpoint on Linux CI

The same-job ABBA comparison for `87a4adb99b` measured **1.362 ms baseline**
against **1.227 ms migrated** median: **90.09% of baseline**, **-9.91%**,
or **-0.135 ms**. This is the combined shared-worker change set, including
grouped POSIX writes, reply-before-refill and ordinary-exit completion; it does
not isolate the contribution of one line or component. Both full Git suites,
both build systems and 6,000-command native stress passed in
[Linux job 129432259436](https://github.com/Ma-XX-oN/git/actions/runs/42697246570/job/129432259436).
Raw samples, flags and the metadata available in that run are preserved in
[migration-87-linux-matched.json](performance/evidence/migration-87-linux-matched.json).
Compare its baseline and candidate within that job; do not compare its absolute
times with a different Linux runner or the laptop.

The completed Windows comparison at that same revision measured 3.9595 ms
baseline versus 3.9540 ms migrated: 99.86% of baseline (-0.14%, -0.0055 ms).
This difference does not establish a meaningful speedup. Windows native stress
passed, while full-suite acceptance remains open because of the send-email
test environment and missing minimal-SDK Meson dependencies.
The macOS job passed both complete suites and measured 4.4125 ms versus
4.0810 ms: 92.49% of its matched baseline (-7.51%, -0.3315 ms).
Raw Windows and macOS samples are preserved alongside the Linux evidence in
`performance/evidence/migration-87-*-matched.json`.
The [checkpoint graphs and table](performance/migration-checkpoints/report.md)
show these same-job comparisons plus the initial laptop extraction. They are
intermediate migration evidence; final post-cleanup results remain required.

### Reviewed-worker checkpoint on the Windows laptop

The same-laptop ABBA comparison for `209625fd56` measured 4.8295 ms baseline
versus 4.8730 ms migrated: 100.90% of baseline (+0.90%, +0.0435 ms).
This is a small regression, not a gain. Both revisions used their matching
clients, three workers, full context, disabled tracing and identical compiler
flags. The combined review changes include explicit manager ownership during
startup and reply publication, parser function splits and export attributes;
this comparison does not isolate their individual costs. The POSIX request-FD
change has no Windows timed-path effect.
Raw samples and the laptop CPU/runtime identities are in
[migration-209625-windows-laptop-matched.json](performance/evidence/migration-209625-windows-laptop-matched.json).

An additional temporary-client test replaced the PID regex with
`[[ $pid == [1-9]*([0-9]) ]]` and the status regex/capture with
`[[ $status == EXIT[[:space:]]+([0-9]) ]]` followed by `${status:5}`.
Both output-drain branches were changed together. Against the same native binary
and laptop in ABBA order, the medians were 5.0395 versus 4.8315 ms (0.2080 ms
reduction, 160 calls per variant). All 782 PID/status recognition and extraction
comparisons agreed with the original regexes with extglob disabled. This remains
a prototype; it is not part of `4d4fd64486` or its same-job CI measurement.
Evidence: [rounds, samples and identities](performance/evidence/status-validation-prototype.json).

For each entry, retain the code commit or prototype patch, baseline identity,
machine/run ID, compiler/runtime flags, worker count, workload, instrumentation,
round order, raw paired samples, correctness checks and rejected alternatives.
State what changed on the timed path and what the comparison can establish.
Use [measured-summary.json](performance/evidence/measured-summary.json) for the
derived values above and [native build flags](performance/evidence/native-build-flags.txt)
for the local native builds. Keep separate-run CI observations separate from
same-job comparisons. Update this journal and its evidence with each accepted
change instead of relying on chat history or temporary files.
