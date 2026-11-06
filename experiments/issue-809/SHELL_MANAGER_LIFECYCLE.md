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
