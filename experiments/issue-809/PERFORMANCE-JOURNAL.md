# Windows serial IPC performance journal

This journal records the change, evidence and limits of each performance result.
The raw measurements in [performance/evidence](performance/evidence/) are committed
with the journal so they survive temporary-directory and benchmark-folder cleanup.
The objective is **4–5 ms median on the native Windows GitHub runner**, with three
workers and complete cwd/environment transfer. Application stdin and binary output
retain the limitations in [the transport contract](TRANSPORT-CONTRACT.md).

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
| Shell patterns instead of regex pipe validation | 6.7650 ms | 6.5045 ms | 0.2605 ms | Adopted in `f62fbc2d7b`; same-job CI measurement pending |
| One-call environment replacement | 5.2780 ms | 4.7915 ms | 0.4865 ms | Rejected: three compatibility failures |
| Changed-only environment updates | 5.5045 ms | 4.9770 ms | 0.5275 ms | Temporary prototype; broader validation pending |
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
same-job CI acceptance are still required before adopting it.

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
by this laptop result. Same-job CI acceptance is pending.

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

## Recording the next change

For each entry, retain the code commit or prototype patch, baseline identity,
machine/run ID, compiler/runtime flags, worker count, workload, instrumentation,
round order, raw paired samples, correctness checks and rejected alternatives.
State what changed on the timed path and what the comparison can establish.
Use [measured-summary.json](performance/evidence/measured-summary.json) for the
derived values above and [native build flags](performance/evidence/native-build-flags.txt)
for the local native builds. Keep separate-run CI observations separate from
same-job comparisons. Update this journal and its evidence with each accepted
change instead of relying on chat history or temporary files.
