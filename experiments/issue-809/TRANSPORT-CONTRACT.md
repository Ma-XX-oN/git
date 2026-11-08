# RWF 809 — Git IPC transport contract

Status: approved design; implementation and native acceptance must be verified separately.

## Terminology and ownership

- **git-manager** is the shell-owned process that maintains a configured number of READY Git workers, allocates one ready worker to `git-ipc`, and asynchronously starts its replacement.
- **git-ipc** is the Git Bash client function. It requests an allocated worker ID and exchanges command arguments, cwd, environment, standard streams, and exit status directly with that worker.
- An allocated Git worker is no longer owned or retained by git-manager. It watches its original Bash owner for termination.
- A *logical channel* has one direction of application data. Windows pipe instances required by Git Bash's opening behaviour are not additional logical channels.

## Cross-platform contract

Every logical FIFO or named-pipe channel is treated as **unidirectional** on Linux, macOS, and Windows. No application protocol depends on Linux's non-POSIX `O_RDWR` FIFO extension or on Windows full-duplex application transport.

Allocation uses two distinct logical channels on **all** platforms:

1. `allocate-req`: git-ipc -> git-manager, carrying `GET\n`.
2. `allocate-reply`: git-manager -> git-ipc, carrying one worker identifier followed by newline (or a defined error).

The allocated worker has **five distinct logical channels**: `stdin` and `param-env-cwd` (git-ipc -> worker); `stdout`, `stderr`, and `exit-code` (worker -> git-ipc). Arguments, cwd and environment are carried exclusively by `param-env-cwd`; stdin is a separate stream. Windows sends and consumes `T` separately on each channel, including stdin. No handshake byte enters application data.

Linux/macOS use ordinary unidirectional FIFOs, with **no** `T` marker.

### Standard-input limitation: no additional relay processes

The `stdin` logical channel remains separately established, and on Windows
the Git worker consumes the initial `T` marker before application I/O.
However, **application stdin forwarding is not implemented**: the Bash client
closes the stdin writer after connection setup, and the worker sees EOF.
This applies to interactive terminals, redirected regular files, and pipes.

An attempted implementation used a per-command background `cat` relay.
That design has been removed because `cat` is an external executable and
a new process per Git invocation defeats the experiment's startup-performance
objective. The client must not spawn a helper process to forward stdin.
Do not claim that `git hash-object --stdin` or interactive Git commands
work through this experimental transport.

A future process-free implementation requires transfer of the caller's
actual standard handles to the prestarted worker, or a demonstrated
equivalent that introduces **no new process per command**, and native
verification on all three platforms. The existing worker/manager design
has not established that mechanism.

## Windows allocation markers and instance identity

The Windows allocation protocol has distinct request and reply pipe names.
The request channel is shared by the manager; its data frame is now
`<digit>GET <BashPID>-<sequence>\\n`, where `digit = sequence % 10`.
The manager validates the digit against the complete request sequence.
The reply name includes the manager PID, Bash PID, and complete request
sequence. The caller writes the same decimal digit immediately after opening
that reply connection. The manager validates that digit before replying with
the allocated worker PID.

The digit cycles through 0–9 for **successive git-ipc calls**. It is a
diagnostic marker, not a request ID and not the worker ID. Each reply pipe
still has a unique name. This allows the manager to detect unexpected bytes
or mismatched call markers; a repeated digit alone is not a duplicate
allocation.

On Windows Git Bash, opening a named pipe has been observed to consume an
additional *probe* connection that does not carry protocol data. To select
the correct connection, the server creates two native instances of the
same uniquely named reply pipe. The instance receiving the expected digit
is the transport; the other must remain unmarked. If both instances have
pending bytes, selection fails rather than silently choosing an arbitrary
instance. The manager records the full request ID, chosen instance, actual
marker, and returned worker PID.

The **five Git worker data channels** (`stdin`, `param-env-cwd`,
`stdout`, `stderr`, `exit-code`) still use the original `T` handshake
and are not changed by this focused allocation diagnostic.

No simultaneous `git-ipc` calls are authorized in this experiment.
A single request completes before the next starts; the manager may
replenish its worker pool asynchronously.

## Transport-selection API

The Windows C Git worker exposes the internal helper
`int select_transport(HANDLE instances[2], DWORD timeout_ms, HANDLE *transport)`.
The return value is an error code: `TRANSPORT_OK=0`,
`TRANSPORT_TIMEOUT=1`, `TRANSPORT_INVALID_MARKER=2`,
`TRANSPORT_DUPLICATE_MARKER=3`, or `TRANSPORT_WIN32_ERROR=4`.
The output handle is valid only on success. The function owns both input
handles and closes all unselected handles; the caller owns the selected handle.

The Python `git-manager` implements `select_transport(address, request_id=None)` separately.
It returns the selected native handle, raising on failure. No cross-language
C library is introduced: GET and ID connect Bash with Python, while the five
Git worker channels connect Bash with C.

## Existing output limitation

The current Bash client drains stdout/stderr using Bash `read -d ''` into
shell strings. Bash variables cannot contain NUL bytes. Consequently the
existing Git IPC experiment is **not yet a byte-transparent stdout/stderr
transport for binary Git output**. This pre-existing limitation is separate
from, and is not corrected by, Windows `T` identification. No full-Git-compatibility claim is permitted until
binary stdout/stderr and interactive stdin tests are implemented and pass.

## Acceptance criteria

- Windows: reply-pipe selection must identify the actual connection by
  its decimal marker, independently of probe disconnection timing; invalid,
  missing, and simultaneous markers must fail safely.
- Windows: record both reply-instance identities and which one carried
  the marker. For every serial GET, verify a unique request identity,
  exactly one matching reply and reply-instance selection, the expected
  digit, and a valid allocated worker PID.
- Windows: verify the actual Git Bash GET/reply behaviour and the native
  selector regression, including 0 and 9 and the synthetic double-marker
  case. A synthetic double-marker test alone is not evidence that two real
  Git Bash calls were mixed.
- All platforms: cwd, environment, text stdout/stderr, and exit status
  must be verified against the same tested SHA. stdin forwarding and binary
  output remain explicitly unsupported.
- Native same-SHA acceptance on Windows, Linux, and macOS: correctness,
  6,000 serial commands, bounded manager handles, cleanup and owner death.
  Do not add parallel Git commands to the test workload.

The original Windows single duplex GET/PID pipe and instance-order selection are superseded. The implementation branch introduces separate GET/ID names, T-based selection, and a separate command/context channel; native CI must verify all changes at the same SHA. Unix now uses separate FIFO descriptors rather than `O_RDWR` for its manager request FIFO.
