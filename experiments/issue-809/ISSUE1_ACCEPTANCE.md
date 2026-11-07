# Issue #1 — Windows `git --preinit` acceptance ledger

Scope source: https://github.com/Ma-XX-oN/git/issues/1.
The Git #809 benchmarks establish experimental feasibility only; they do not
certify this public-switch implementation. All acceptance must use the exact
source SHA being evaluated. Developer branch: `issue-1-preinit-windows`.
Upstream submission must be reconstructed on a clean Git `main` base.

| ID | Requirement | State | Evidence / next check |
| --- | --- | --- | --- |
| A1 | Native `git --preinit` READY/PID and four pipes | PROCESSING | Windows normal-build benchmark PASS at historical SHA `5cb8dd6` (run 42331080238); final SHA `748e7a255f6b36c9e8eda059f1760f6eb6f9f388` queued, not accepted |
| A2 | Direct Bash data path, manager allocates PID and immediately replenishes | PROCESSING | Initial benchmark PASS at same historical SHA; final source recheck |
| A3 | Exact stdout/stderr/exit status: positive, nonzero, UTF-8, binary NUL | PROCESSING | Windows run 42331978319 verified version/help, Unicode, and config-missing rc=1; fatal rev-parse exited without status (FAILED). `usage.c` exit capture added at SHA `c833d2b0`, final SHA native rerun pending; binary NUL case not yet reached |
| A4 | Slow reader and simultaneous streams, explicit EOF and lifetime | TO_PROCESS | 512 KiB binary slow-reader case registered; final results and parallel stderr pressure not accepted |
| A5 | Bounded abandoned client/disconnect, owner exit, cleanup | PROCESSING | Manager bounds allocated worker lifetime via `GIT_PREINIT_TIMEOUT_SECONDS` (default 600), added at `48014c36`; native timeout/disconnect and temporary-directory cleanup still unverified |
| A6 | Same-user pipe access control and no name takeover | PROCESSING | Worker SID ACL and first-instance protection `3f6c0fae`, manager SID ACL and remote-client rejection `cc99fe4a`; owner-client access has executed, cross-user negative tests missing |
| A7 | Inherited cwd/env, stdin restrictions, signals, concurrent command boundaries documented | PROCESSING | Technical protocol document added; verify enforceable contract and tests |
| A8 | Non-`--preinit` Git unchanged, Linux/macOS direct Git | PROCESSING | Windows-only compile guard in Git code; exact final-SHA regression outstanding |
| A9 | Native >=100 paired samples, median/P95/raw artifacts | PROCESSING | Initial Windows 120/120 x two modes PASS at run 42331080238, SHA `5cb8dd6`; final-SHA performance pending |
| A10 | Clean upstream patch series and reviewable PR | PROCESSING | Clean staging branch `feature/git-preinit-windows-upstream` created from fork `main`, 5 changed Git/docs files and no #809 history; native acceptance and upstream-test integration outstanding; PR deliberately not submitted |

**Current classification:** NOT DONE. Do not reuse earlier PASS for later changed SHAs.

## Protocol-race diagnostic

Microsoft ConnectNamedPipe distinguishes ERROR_PIPE_CONNECTED (client still
connected) and ERROR_NO_DATA (client connected, then disconnected before the
server call). Probe-only handling corrected in the worker (commit 448fffa8)
and Python manager allocation server (commit dbc1f94a). Data-bearing pipe
connections still reject ERROR_NO_DATA. Native verification of combined
changes is pending.

## Latest development source

Commit: `748e7a255f6b36c9e8eda059f1760f6eb6f9f388` (Windows Git side),
with latest manager behavior from `dbc1f94a`. Changes after historical PASS
must not inherit that PASS.
