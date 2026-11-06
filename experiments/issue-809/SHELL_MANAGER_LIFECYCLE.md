# Git #809 — sourced per-shell worker-manager lifecycle

Status: **experimental, Windows-native**. The original shell-owned startup/cleanup milestone is independently accepted; the later IPC dispatch and three-OS benchmark candidate described below requires separate exact-SHA native acceptance.

## Usage in Git for Windows SDK Bash

The source-built Git executable used here must contain the ephemeral `GIT809_WORKER_PROBE` wmain rendezvous hook built by `windows_real_git_worker_build.py`; ordinary stock `git.exe` has no such hook. Set `GIT809_WORKER_GIT` to its **native Windows executable path**, and `GIT809_MANAGER_PYTHON` to a native Windows Python 3 executable path. Source `experiments/issue-809/git-manager.sh` with a worker count (default 4).

Example commands (conceptual — adjust actual paths to the test machine):

```bash
export GIT809_WORKER_GIT='C:\path\to\instrumented\git.exe'
export GIT809_MANAGER_PYTHON='C:\path\to\python.exe'
source experiments/issue-809/git-manager.sh 4
source experiments/issue-809/git-manager.sh 4  # no duplicate daemon
```

On first source, the script starts one background native Python manager, waits for its ready-file handshake and records `_GIT809_MANAGER_BASH_PID`, `_GIT809_MANAGER_WORKERS`, `_GIT809_MANAGER_READY`, and `_GIT809_MANAGER_LOG` **in the current shell only**. The manager prelaunches the requested number of actual Git processes waiting at wmain; it connects each worker's internal rendezvous pipe and retains the handles.

The Bash source obtains the owning shell's native Windows PID from `/proc/$BASHPID/winpid` and passes it explicitly to the manager. This is necessary: the first Windows test established that native Python `os.getppid()` returned an intermediate MSYS2 launcher PID rather than the shell PID. The manager retains a native process handle for the supplied PID.

On shell termination, the manager's retained native **owner process handle** becomes signaled; the manager uses `WaitForSingleObject(handle, 10000)` in a loop, then closes worker rendezvous handles, terminates workers and exits. This handles ordinary exit **and forced shell process termination**, independent of an `EXIT` trap. An `EXIT` trap is intentionally not installed to avoid replacing an existing handler. The manager never operates on a PID alone after opening the owner handle.

No manager control pipe, manager session-ID-named pipe, shell alias, `git_ipc` command router, request forwarding, resize behavior, or shared-manager discovery is implemented in this stage. The worker rendezvous names contain a per-worker random token required by the existing source hook, **not a per-shell manager session ID**. Running multiple shell-owned managers does not share their worker pipe names, but a future fixed-name public request endpoint cannot safely be shared across simultaneous shells until routing/ownership is specified.

## Idempotence and changes to count

Repeated sourcing with the same count preserves the existing manager and workers. Re-sourcing with a different count **preserves the existing count and reports that resizing is not implemented**; no duplicate manager is launched. To select another pool size in this minimal prototype, terminate that shell and create a new session.


## Experimental command-dispatch and three-OS benchmark candidate (not yet accepted)

The later benchmark milestone adds `windows_git_ipc_pool.py`, `git_ipc()` and `alias git='git_ipc'` to the sourced shell. The prior startup-only manager remains in `windows_shell_owned_pool.py` for its accepted lifecycle regression. The new manager assigns single-use, genuine wmain-preinitialized Git processes to requests delivered through the existing four separate named pipes, then replenishes consumed workers. Worker pool size is set on first sourcing; repeated sourcing does not yet resize it.

The proof-of-concept IPC client constructs a counted little-endian UTF-8 argv frame using **Bash builtins** and returns the worker's exit status. It reconstructs newline-oriented stdout/stderr with Bash `read` and `printf`: **binary stdout/stderr containing NUL, unterminated lines, arbitrary CRLF boundaries, or interactive streaming are not accepted**. The manager captures output to a bounded 1 MiB-per-stream buffer and sends it after the child exits, not incrementally. The Git worker inherits the manager's initial cwd/environment, not the caller's later cwd/environment. Shell signals, stdin, multiple simultaneous `git_ipc` invocations, secure multi-shell isolation and complete general-command parity remain future implementation work. Do not treat the alias as a production-ready general-purpose Git replacement.

`.github/workflows/issue-809-three-os-pool.yml` builds the same checkout's Git source with `NO_RUST=YesPlease NO_CURL=YesPlease NO_GETTEXT=YesPlease` and `make -j4` on Windows, Linux and macOS. Windows additionally compiles the temporary `GIT809_WORKER_PROBE` instrumentation into `git.exe`; the actual source patch is ephemeral to the CI checkout. Each OS measures 12 direct source-built `git --version` invocations inside one persistent Bash process with Bash `EPOCHREALTIME`. Only Windows also measures 12 alternating paired `git_ipc --version` invocations through a **single persistent Python manager with three preinitialized workers and replenishment**. The worker pool is ready before samples begin. Worker startup and background replenishment costs are not fully attributable to individual samples; cross-OS hosts and toolchains differ and cannot be treated as controlled identical hardware.

Mac OS runners' system Bash 3.2 lacks `EPOCHREALTIME`. The workflow installs Homebrew Bash and the Python harness checks for timer support before collecting samples. Native acceptance requires workflow success and review of per-OS JSON artifacts at the exact source SHA; first failed workflows or older ready-worker timings are not acceptance.

## Future implementation (explicitly deferred)

- Define `git_ipc` Bash function and `alias git='git_ipc'`; `\git` bypasses the alias. Preserve Git's exact argument bytes (including Unicode/empty arguments), cwd, environment, stdin, signals, stdout/stderr streaming and native exit status. Do not activate an alias until real command routing works.
- Add a dedicated manager control message endpoint for `SET_WORKERS`, acknowledgement, status and orderly shutdown. Re-sourcing with a different count should resize the existing pool, not launch another manager; active workers finish unaffected.
- Define stable per-shell manager identity/address (the shell PID was discussed) and validate it against native Windows PID semantics and ownership for multi-terminal concurrency. For this stage there is no public session-addressed pipe.
- Implement preinitialized worker checkout/assignment, concurrent invocation isolation, single-use consumption, automatic asynchronous replenishment and bounded outstanding requests. Validate throughput including creation/replenishment cost, rather than only ready-worker latency.
- Implement parent-death cleanup for all process/pipe race cases, normal shell exit, SIGTERM, hard termination and system shutdown, and preserve existing shell traps when adding any optional proactive cleanup.
- Secure local IPC and constrain access rights; validate untrusted request frames and resource limits. Test stale manager processes and PID reuse before production use.
- Verify on native Windows the shell-source lifecycle plus controlled persistent-shell Git workload latency, pool startup, idle memory, replenishment latency, sustained throughput and simultaneous shells. Native Linux/macOS behavior must be designed separately if portability is required.

## Evidence

Previous actual Git worker command-equivalence acceptance: native Windows Actions run 42167287146 at SHA `178952bb1ed57fe46cd6ec521ecaea6920f5e198`. Ready-worker performance, independent two-attempt Windows confirmation: Actions run 42167964759 at SHA `c04c427b605e1762f8885d1d9b818a067eae875b`. Those runs do **not** validate shell-owned lifecycle. The lifecycle verification is registered in `.github/workflows/issue-809-shell-manager.yml`; consult its exact SHA and results before claiming acceptance.

## Direct PID-addressed IPC prototype — pending native acceptance

The current experimental branch has a newer direct-worker prototype than the historical relay design above. **Do not treat the previous relay benchmark as performance acceptance for this code.** The experimental Git worker is built with a transient patch of `compat/mingw.c` and `git.c` using `windows_real_git_worker_build.py`; the ordinary Git executable and the Linux/macOS build are not changed.

- **Bash entrypoint:** `git-ipc` (hyphen), with `alias git='git-ipc'`. `\\git` bypasses the alias. It requests a worker PID using the single allocation channel `\\\\.\\pipe\\git809-allocate-<owner-winpid>` and directly opens `\\\\.\\pipe\\<worker-pid>-stdin`, `-stdout`, `-stderr`, and `-exit-code`.
- **Manager:** `windows_git_ipc_pool.py` only maintains the pool and grants PIDs. A replacement process is spawned in an independent thread at allocation time, before replying with the allocated PID. The manager does **not** read Git output or forward the Git frame. If all ready workers are consumed it waits for ready replacement for a bounded 5 seconds rather than waiting on an allocated worker.
- **Worker:** `--git809-direct-worker` is a CI-only temporary hook; it creates four PID-named pipes, signals READY, accepts a counted UTF-8 command frame and executes the actual Git command. A scoped, transient `git.c` exit-hook bridges Git builtin-dispatch `exit(ret)` so the worker can flush stdout/stderr and deliver the status. This is **not** the future public `--preinit` implementation.
- **Drainage:** stdout and stderr are consumed in two independent Bash background reader loops. The worker calls Win32 `FlushFileBuffers()` on output and exit-code pipes before disconnecting and exiting. The requirement is that it remain alive until the client has consumed all buffered bytes. Native slow-reader and failure-case acceptance remains necessary.
- **Limitations:** Bash `read` and `printf` reconstruct text lines, not exact arbitrary binary streams. Per-command cwd/env/stdin forwarding, cancellation, signal parity, end-user security and true general Git CLI equivalence are **not accepted**. The one-shot worker does not yet catch every process exit path outside the central Git builtin dispatcher.

[Git issue #1](https://github.com/Ma-XX-oN/git/issues/1) separately tracks the requested public `git --preinit` switch and production worker contract.

The latest three-OS experimental benchmark collects 120 samples per configuration, verifies direct-versus-IPC version output and Git help output, and checks distinct worker allocations. All outputs must be verified against an exact source SHA and native CI run before reporting a new performance result. The old 12-sample Windows Python-relay timings remain **historical and superseded** for the direct implementation.


## Windows named pipe versus loopback TCP — targeted transport measurements (2026-11-06)

**Verified experiment SHA:** `09f9699225d007444652ff6c6bface58a53a2e65`.
**Native Windows CI:** [run 42287848349](https://github.com/Ma-XX-oN/git/actions/runs/42287848349), original attempt and independent same-SHA job rerun (both SUCCESS). Windows-only, no Git recompilation, no Linux/macOS work. Source: `windows_tcp_pipe_benchmark.py`, CI: `issue-809-tcp-pipe.yml`. Both use **exact Git for Windows SDK Bash**; the earlier test's native Python PATH had accidentally resolved `bash` to the WSL launcher, which is not a Git Bash test.

The server is native Windows Python, with a synchronous Win32 **duplex named pipe** or a `127.0.0.1` TCP listener. Each client is a single persistent Git Bash process. There are 150 sequential short ASCII echo requests/replies on one established connection; Bash `EPOCHREALTIME` brackets only the request/reply, excluding client startup and connection setup. Native server connection/message counters verify acceptance, with actual per-sample microseconds logged and recorded in `git809-bash-pipe-results/windows-tcp-versus-pipe.json` workflow artifacts.

| Native Windows run | Two actual pipe instances (median / P95 µs) | TCP loopback (median / P95 µs) | One actual pipe instance |
| --- | --- | --- | --- |
| Original | 63 / 106 | 196 / 229 | `Device or resource busy`, 0 requests |
| Same-SHA rerun | 47 / 71 | 144 / 201 | `Device or resource busy`, 0 requests |

**Verified:** For both attempts, the single-instance Win32 duplex pipe accepted one server-side connection that carried zero requests; Bash failed to open it for data. With **two actual instances**, server counters reported **two connections, 150 successful requests**. TCP reported **one connection, 150 successful requests**. Neither the read/write direction nor buffer capacity requires separate named-pipe instances. The single-instance Bash behavior needs a source-level MSYS2 investigation before adopting duplicated pipe instances as a production workaround.

**Measured result:** Named-pipe roundtrips were approximately 3.1x faster than TCP in both native Windows attempts, but neither transport was tested as a complete native Git command. No claim of end-to-end worker speedup or full binary stdout/stderr parity follows from these echo timings.

**TCP integration costs:** A single TCP stream is duplex but would require framing/multiplexing stdin, stdout, stderr and exit status, or separate connections and listener/port coordination. Worker PID alone is not an address: the manager would need to return actual port(s), and an application-level session token is required to prevent unauthorized same-host client connections. Windows named pipes can instead use filesystem-like names and Win32 access control. TCP `send` only proves kernel buffering, not application consumption; explicit client acknowledgment is required to meet the worker's "do not exit until fully read" criterion. Loopback listener must bind `127.0.0.1` and handle cleanup, port lifetime, cancellation and Nagle/delayed-ACK behavior. This prototype did not test authentication, four-channel multiplexing, large/binary data or disconnection handling.

**Decision:** Keep named pipes as the performance baseline; do not switch #809's worker implementation to TCP based on this test. Investigate and repair the MSYS2/Bash single-instance connection path while preserving exactly four logical communication channels.

## Two-instance Git Bash pipe handling — verified 2026-11-06

- **Accepted code SHA:** `a458c02c73e4a824330269a4a9b9d5bbe094574e`; native Windows acceptance: [run 42293544096](https://github.com/Ma-XX-oN/git/actions/runs/42293544096), Windows job SUCCESS. The previous runs at `a2ac8dead07c608dd7c169856e7168f8aa022b32` and `94e1e2c9305e30187ee7b375854bd7c231b07bbe` were NOT accepted: the first failed to open the single-instance allocation pipe, and the second completed the Bash command workload but failed on a benchmark parser's literal-backslash-n marker bug.
- **Architecture:** Each of the four independent PID-addressed, one-way worker channels (stdin, stdout, stderr, exit status) has **two native Windows named-pipe instances** under the same name. The separate manager allocation endpoint also has two instances, because the Bash `<>` open otherwise fails. Thus one worker has eight worker-channel instances plus the manager's two control instances. These are *not* eight logical channels and they do *not* multiplex data. Bash uses `>>` for the worker stdin channel, `<` for the three worker outputs, and `<>` only for the allocation request/reply control endpoint.
- **Independent unidirectional baseline:** [run 42292019217](https://github.com/Ma-XX-oN/git/actions/runs/42292019217), commit `3d09fd4834a7f4104f7184e5ff1739bbb2ad6fc6`, native Windows four-channel success and failure cases both passed with exactly **two instances per channel**, after reducing the prior six-instance probe.
- **End-to-end result:** The Windows pool benchmark at the accepted SHA completed 120 alternating direct-vs-IPC pairs and verified worker allocation counts, Git alias, output identity for `git --version`, and large `git help -a` output. Direct Git median **12.2945 ms**; IPC median **12.644 ms** (IPC **0.3495 ms slower** on this runner). These measurements are not an improvement claim, and are distinct from the earlier raw named-pipe-versus-TCP latency measurements.
- **Important limitation:** Observing two connections does not establish that MSYS2 internally uses bidirectional opens for these one-way redirections. The exact implementation reason remains **unverified**. The two-instance sequence is a tested experimental workaround, not a documented Windows API requirement or a security/production readiness guarantee.
