# Issue #1 — Windows `git --preinit` acceptance ledger

Scope source: https://github.com/Ma-XX-oN/git/issues/1.
The Git #809 benchmarks establish experimental feasibility only; they do not
certify this public-switch implementation. All acceptance must use the exact
source SHA being evaluated. Developer branch: `issue-1-preinit-windows`.
Upstream submission must be reconstructed on a clean Git `main` base.

| ID | Requirement | State | Evidence / next check |
| --- | --- | --- | --- |
| A1 | Native `git --preinit` READY/PID and four pipes | PROCESSING | Native Windows public-switch build/120-command benchmark PASS at `5cb8dd6def31885783da8b71bcfac1c35b8aa751`, run 42331080238; repeat exact final source |
| A2 | Direct Bash data path, manager allocates PID and immediately replenishes | PROCESSING | Initial benchmark PASS at same historical SHA; final source recheck |
| A3 | Exact stdout/stderr/exit status: positive, nonzero, UTF-8, binary NUL | PROCESSING | Native protocol runner added; older run 42331668338 passed version/help and Unicode bytes but failed while printing Unicode via CP1252; corrected UTF-8 runner not yet accepted |
| A4 | Slow reader and simultaneous streams, explicit EOF and lifetime | TO_PROCESS | 512 KiB binary slow-reader case registered; final results and parallel stderr pressure not accepted |
| A5 | Bounded abandoned client/disconnect, owner exit, cleanup | PROCESSING | Manager allocation timeout added at `48014c36`; native timeout/disconnect and filesystem cleanup acceptance outstanding |
| A6 | Same-user pipe access control and no name takeover | PROCESSING | Worker ACL and first-instance protection `3f6c0fae`, manager ACL `cc99fe4a`; full negative security checks outstanding |
| A7 | Inherited cwd/env, stdin restrictions, signals, concurrent command boundaries documented | PROCESSING | Technical protocol document added; verify enforceable contract and tests |
| A8 | Non-`--preinit` Git unchanged, Linux/macOS direct Git | PROCESSING | Windows-only compile guard in Git code; exact final-SHA regression outstanding |
| A9 | Native >=100 paired samples, median/P95/raw artifacts | PROCESSING | Initial Windows 120/120 x two modes PASS at run 42331080238, SHA `5cb8dd6`; final-SHA performance pending |
| A10 | Clean upstream patch series and reviewable PR | TO_PROCESS | Dev branch intentionally includes #809 history; create clean branch from fork `main` only after implementation/tests accepted |

**Current classification:** NOT DONE. Do not reuse earlier PASS for later changed SHAs.
