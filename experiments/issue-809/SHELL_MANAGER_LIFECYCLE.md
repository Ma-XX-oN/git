# Git #809 — sourced per-shell worker-manager lifecycle

Status: **experimental, Windows-native; acceptance is tracked by CI results**. This is deliberately limited to starting the shell-owned manager, preinitializing workers, idempotent sourcing, and stopping the manager/workers on owner-shell termination. It does **not** yet replace the `git` command.

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

On shell termination, the manager's retained native **owner process handle** becomes signaled; the manager uses `WaitForSingleObject(handle, 10000)` in a loop, then closes worker rendezvous handles, terminates workers and exits. This handles ordinary exit **and forced shell process termination**, independent of an `EXIT` trap. An `EXIT` trap is intentionally not installed to avoid replacing an existing handler. The manager never operates on a PID alone after opening the owner handle.

No manager control pipe, manager session-ID-named pipe, shell alias, `git_ipc` command router, request forwarding, resize behavior, or shared-manager discovery is implemented in this stage. The worker rendezvous names contain a per-worker random token required by the existing source hook, **not a per-shell manager session ID**. Running multiple shell-owned managers does not share their worker pipe names, but a future fixed-name public request endpoint cannot safely be shared across simultaneous shells until routing/ownership is specified.

## Idempotence and changes to count

Repeated sourcing with the same count preserves the existing manager and workers. Re-sourcing with a different count **preserves the existing count and reports that resizing is not implemented**; no duplicate manager is launched. To select another pool size in this minimal prototype, terminate that shell and create a new session.

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
