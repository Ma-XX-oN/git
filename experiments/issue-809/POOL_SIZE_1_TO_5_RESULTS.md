# RWF #809 — worker-pool size 1 through 5, sequential commands

Status: **Windows and Linux new runs PASS; macOS 2/4/5 partial, workflow FAIL**.
This is an experimental benchmark, not upstream Git acceptance.

## Method

- Same Git #809 preinitialized single-use worker and independent shell-owned manager.
- Persistent Bash; `git --version`; Git commands strictly **serial**, never concurrently launched.
- Each completed round contains 120 pairs of direct Git and `git-ipc`, alternating order.
- 100 ms pipe reader mode on Windows; original Unix shell read mode retained.
- Windows 2/4/5 + Linux 2/4/5 benchmarked in order 2,4,5,5,4,2.
- No redundant 1/3 benchmarks on this branch. Those points reuse earlier native runs.
- Timings exclude manager initialization but include any synchronous waiting for worker allocation and replacement readiness.
- Plotted statistic: arithmetic mean of **round medians** (not median over pooled raw samples).
- Neither workload nor manager implementation was changed to generate the 2/4/5 points.

## Median IPC times, milliseconds

| Workers | Windows | Linux | macOS |
| --- | ---: | ---: | ---: |
| 1 | 5.49700 | 1.07025 | 2.81025 |
| 2 | 2.71450 | 0.87125 | 1.84200 |
| 3 | 1.63025 | 0.86925 | 1.08150 |
| 4 | 2.69700 | 0.87200 | 1.65250 |
| 5 | 2.69125 | 0.86750 | 1.78850 |

**Do not interpret Windows' 3-worker dip or macOS 3-worker dip as a
same-runner advantage.** Their 1/3 points came from different runner
instances than 2/4/5; Windows direct Git median was ~15 ms in the
earlier run versus ~21 ms in the new one. macOS historical direct
medians ~4 ms versus ~5.4–6.1 ms in the new run.

### Origin and count per data point

- Windows 1/3: run 42334705364, source
  `500d9c183ca53fbfe41a1c20b2f7d595cb581574`, two 120-sample
  successful rounds each.
- Linux 1/3 and macOS 1/3: run 42335421538, source
  `f7cd69f2c108cfbf394199a68df7836817aaab32`, two 120-sample
  successful rounds each.
- Windows 2/4/5 and Linux 2/4/5: run 42336399222, source
  `bec48e9752fae1e8ef7ef5a8a9b896d88892a54c`, two 120-sample
  successful rounds each, all jobs PASS.
- macOS 2/4/5: same new run, one completed 120-sample round per
  pool size; the fourth round (5 workers) FAILED at allocation reply
  after 39 completed pairs. These data are reported as partial.
- Exact source-build Git flags retained from the original worker
  builder; CI artifacts contain paired raw measurements and build metadata.

## macOS allocation reply failure

Observed `GIT-IPC ERROR: allocation response failed`; manager remained
alive and logged 41 allocations in the failing 5-worker round (one
initial verification + 39 successful paired IPC commands + a
further allocated worker). The pending response did not reach the
Bash client successfully. This is an observed manager-to-client
reply-delivery failure, not proof of a particular FIFO race mechanism.

## Conclusion

Two workers reached essentially the same median latency as four/five
on the **same** Windows and Linux runner, and were strictly sequential
for every command. A one-worker pool remained slower in historical
native comparisons because replacement readiness can enter the
serial command's critical path.

The macOS full 2/4/5 matrix did not pass acceptance; its single-round
points cannot independently establish an optimum pool size, nor
establish that the manager is unnecessary. Fix and test the
allocation reply protocol separately before accepting that claim.
