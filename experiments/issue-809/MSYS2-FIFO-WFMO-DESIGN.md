# RWF 809 / MSYS2: FIFO event-driven reader experiment

Status: DESIGN RECORDED, NOT IMPLEMENTED OR NATIVE-VERIFIED (2026-11-10, America/Toronto)
Scope: experimental MSYS2 runtime FIFO read path only. Do not alter production Git IPC, change FIFO writer semantics, or merge upstream until Windows native verification.

## Source and workbench
- Target: `msys2/msys2-runtime`, branch `msys2-3.6.12`, commit `50c44b7c03c8a3298dc838b8768973e2020b9ed6`.
- `winsup/cygwin/fhandler/fifo.cc` blob `15117db6eb359eabd7b49913cb2cebb6af85c275`: `raw_read()` three-pass per-writer scan; `cygwait(select_sem, 1, cw_cancel | cw_sig_eintr)` polling; `fifo_reader_thread_func()` already event-driven.
- `winsup/cygwin/fhandler/pipe.cc` blob `11ef78c73be92a1fb1834d5ff40891d9ec8c64c5`: `raw_write()` handles partial, nonblocking, EPIPE/SIGPIPE, and semaphore signaling.
- Historical native runtime build recipe: `.github/workflows/issue-809-upstream-handlefree.yml` on previous experiment branch. Isolate patched DLL; never replace production runtime.
- Source/engineering guides: upstream `README`, `winsup/README`, `winsup/testsuite/README`; production path fixes must remain general and upstreamable.

## Approved v1 architecture
1. Preserve the per-writer native pipe instance and reader-owner model. One writer corresponds to one `fifo_client_handler`.
2. For connected instances, investigate arming *one pending zero-byte asynchronous read* with a stable completion event per writer. This is a readiness notification, not prefetch: data must remain in the pipe until the ordinary reader consumes it. Do not assume this works on the MSYS2 `NtCreateNamedPipeFile` handle until experimentally verified.
3. Call `WaitForMultipleObjects(..., FALSE, bounded_timeout)` on pending completion events and explicit controls. This blocks efficiently; incoming data must wake without waiting for timeout. Use current signal-aware handling at bounded timeout for v1; signal-handle integration and an infinite wait are deferred upgrades.
4. When selecting among simultaneously completed events, keep the *last successful writer first*. After a successful read, move its event/identity to index 0 and shift intervening entries one place right. This preserves current `raw_read()` preference (the earlier suggestion to rotate it to the end was incorrect). This is not a total chronological ordering guarantee.
5. `MAXIMUM_WAIT_OBJECTS == 64` is a *total wait-array* limit, including control events. Set the actual writer limit to `64 - reserved_control_count`, derived from real wait-array membership. Document the exact effective limit in implementation and tests. Do NOT silently overflow. Preserve pre-existing FIFO behavior beyond that limit via an explicitly tested fallback; imposing a new writer limit as a production regression is not acceptable. IOCP is a deferred, separate scalability improvement.
6. Preserve the existing POSIX `read()`, `select()/poll()`, writer, signal, cancellation, EOF, ownership-transfer, `dup/fork/exec`, and `close()` semantics. Do not create an unconditional raw Win32 infinite wait bypassing signal/cancel handling.
7. Treat every pending operation and event as owned by a clearly defined reader-handler lifetime. Before closing, reordering, transferring, or duplicating native handles, cancel and *settle* any pending operation; a close while `WaitForMultipleObjects()` is waiting is undefined behavior. Never hold `fifo_client_lock` while waiting for notifications needed by the connection-accept thread. Concurrent handler-list mutation requires an explicit stable snapshot/ownership invariant.
8. **Readiness must not consume bytes.** Pending multi-client data reads would require persistent private buffers and ownership-transfer handoff; avoid that complexity if zero-byte reads work. Re-arm only after retrieving completion status and verifying whether the pipe is readable, disconnected, or still empty (including zero-byte write edge cases). Do not spin on immediately-completed empty reads.

## Windows-first verification ledger (all TO_PROCESS)
| ID | Acceptance requirement | Method |
|----|------------------------|--------|
| A1 | Zero-byte overlapped read on *actual MSYS2 native pipe instances* sleeps until nonempty data; no byte consumption; handles empty writes, disconnect and cancellation | Isolated patched-DLL Windows CI harness, ETW/Detours if necessary |
| A2 | One writer and N writers (1, 2, 3, 16, >=effective cap), with last-reader preference, no starvation changes beyond current semantics | Native focused tests; log selected writer and wake event |
| A3 | Multiple-reader ownership transfer with pending reads loses no bytes; no stalled or use-after-close events | Native stress; instrument owner handoff |
| A4 | Blocking/nonblocking read, EOF, EINTR/cancellation, select/poll, and SIGPIPE/EPIPE pass | Existing upstream `winsup/testsuite` registered tests and targeted cases |
| A5 | Repeated create/open/close/unlink and fork/exec/dup leave no events/handles/threads | Windows native stress; count handles |
| A6 | Per-command latency and CPU improvement measured against unchanged source/config on same runner | Same-runner control and treatment |
| A7 | Existing path-metadata double-connection problem independently fixed/verified | Dedicated prior Detours one-instance probe; do not conflate with read polling |
| A8 | Native acceptance on Windows, then Linux/macOS as applicable | Native CI at exact SHA; report all evidence and failures |

## Deferred improvements (explicitly recorded)
- IOCP instead of bounded WaitForMultipleObjects array to scale beyond its 64 total-handle limit.
- Integrate all POSIX signals/cancellation/ownership events into a truly signal-aware wait that can block indefinitely. Until verified, use bounded timeout for the existing signal mechanism.
- Further optimize initial FIFO pathname metadata lookup separately from data-read waiting.

## References
- Microsoft: https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitformultipleobjects
- Microsoft: https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-server-using-overlapped-i-o
- Microsoft: https://learn.microsoft.com/en-us/windows/win32/ipc/synchronous-and-overlapped-input-and-output
- LLDB Windows zero-byte pipe readiness example: https://codebrowser.dev/llvm/lldb/source/Host/windows/MainLoopWindows.cpp.html

WARNING: This document is a design and verification contract, NOT evidence of a working patch.