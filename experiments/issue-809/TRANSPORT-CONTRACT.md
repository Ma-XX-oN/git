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

### Standard-input payload and fast path

The `stdin` channel is for application stdin bytes, not for the argument,
cwd or environment frame. On Windows, Git Bash writes `T` first and the
worker consumes it before exposing that handle as Git's stdin. On Unix,
stdin carries application bytes without a marker. Redirected file or FIFO
stdin is copied from git-ipc to the stdin channel by a short-lived shell
child; the child must not inherit the `param-env-cwd` writer, because that
would prevent the worker from observing the command frame's EOF. The shell
reaps this child on success or error.

When stdin is the same descriptor the owning shell had when git-manager was
sourced, the client does not spawn a copy process. This keeps normal
no-input commands on the low-overhead path. **Interactive terminal input
forwarding is not implemented in this experiment**; it must not be claimed
to work until a separate interactive-input acceptance test passes.

Native acceptance must verify binary bytes crossing the separate stdin
channel, including an input larger than one Windows pipe buffer, and confirm
the normal no-input benchmark does not add a per-command forwarding process.



## Windows-only connection identification

**Why Windows needs the marker:** In this Git Bash/MSYS2 named-pipe redirection path, tests observed an additional connection that does not carry the application's transport bytes (the *probe*), alongside the actual transport connection. The earlier allocator tried to identify the real connection by assuming the probe closed first. That assumption failed in native stress testing: two instances reached the same closing state, and allocation timed out. The `T` marker identifies the connection explicitly instead of guessing from connection order or disconnection state. This is a documented Git Bash/MSYS2 interoperability defect in our experimental transport; the evidence does **not** establish an upstream Git Bash bug or its root cause. Linux and macOS do not require this Windows-specific workaround.

For every logical channel, the Windows server/receiving endpoint creates a pair of named-pipe instances for the same pipe name. The instances are created using duplex capability **only so git-ipc can write a connection marker even on normal server-to-client channels** (including stdout, stderr, and exit-code). This capability must not be used for reverse-direction application traffic.

1. git-ipc opens the actual transport instance and immediately sends the single byte ASCII `T` on that instance, **including when its subsequent role is only to read**.
2. The endpoint reads and validates exactly one `T` before forwarding or reading any application bytes. The marker is consumed; it must not enter stdin, stdout, stderr, status, parameter, environment, or cwd payloads.
3. The instance receiving `T` is selected as that logical channel's transport. Close the unused probe instance of **that pair only**, not other clients' independent connections.
4. No marker within the connection establishment deadline (initially two seconds) is a connection-establishment failure. A valid marker must be processed immediately, without waiting for the deadline.
5. Two `T` markers on distinct instances of a pair indicate a protocol violation; report an error, do not arbitrarily select one.
6. Close and release all handles, pending accept operations, and per-connection state on success, error, cancellation, and owner death.

For allocation, GET and ID travel over distinct logical named-pipe names. The Windows reply name is unique for **every request**, identified by the Bash process ID and a monotonically increasing shell-local sequence; the GET frame carries that identity. Both the request parser and reply-name constructor validate its two unsigned decimal components. PID-only reply names are prohibited because successive calls from one Bash process overlap server-side close/recreation. The same T identification rule applies to both channels; concurrent clients must receive only their own replies.

## Transport-selection API

The Windows C Git worker exposes the internal helper
`int select_transport(HANDLE instances[2], DWORD timeout_ms, HANDLE *transport)`.
The return value is an error code: `TRANSPORT_OK=0`,
`TRANSPORT_TIMEOUT=1`, `TRANSPORT_INVALID_MARKER=2`,
`TRANSPORT_DUPLICATE_MARKER=3`, or `TRANSPORT_WIN32_ERROR=4`.
The output handle is valid only on success. The function owns both input
handles and closes all unselected handles; the caller owns the selected handle.

The Python `git-manager` implements `select_transport(address)` separately.
It returns the selected native handle, raising on failure. No cross-language
C library is introduced: GET and ID connect Bash with Python, while the five
Git worker channels connect Bash with C.

## Existing output limitation

The current Bash client drains stdout/stderr using Bash `read -d ''` into
shell strings. Bash variables cannot contain NUL bytes. Consequently the
existing Git IPC experiment is **not yet a byte-transparent stdout/stderr
transport for binary Git output**. This pre-existing limitation is separate
from, and is not corrected by, Windows `T` identification or by the new
binary stdin forwarding. No full-Git-compatibility claim is permitted until
binary stdout/stderr and interactive stdin tests are implemented and pass.

## Acceptance criteria

- Windows: identify selected pipe by `T`, not `NtQueryInformationFile` state or probe close; test both possible instance orders, two markers, no marker, probe remaining open, and client disconnect mid-handshake.
- All OSes: GET/ID use separate logical unidirectional channels; every channel carries exactly the intended application bytes with no `T` contamination; cwd/environment and stdin/stdout/stderr/exit-code semantics preserved.
- Native same-SHA tests on Windows, Linux, macOS: command correctness, UTF-8 and environment isolation, distinct cwd, 6,000-command stress, concurrent requests, allocation/wait behaviour, bounded manager handles and FIFO cleanup, and owner-shell death.
- Starting worker count is unchanged unless shortage is measured. Replacement on allocation remains asynchronous; do not introduce speculative grow-only pool sizing.

## Implementation notes

### Open design discrepancy: shared GET listener

The current Python git-manager request side retains a **replenished pool of
listener instances for the shared GET pipe name**, and identifies each real
request by its `T` prefix. It does **not** call the same two-instance
`select_transport()` used for each unique ID reply pipe. This is **not
yet verified as satisfying the agreed pair-selection contract**. Unlike a
private worker channel, GET accepts concurrent independent git-ipc clients:
two distinct GET connections can both carry valid `T` markers without
representing a duplicate marker on one client's transport pair.

Do not replace the shared request listener with a single global two-instance
pair without preserving concurrent-client isolation. The exact GET
pair/acceptance model must be reconciled with this concurrency requirement
before claiming full protocol acceptance.



The original Windows single duplex GET/PID pipe and instance-order selection are superseded. The implementation branch introduces separate GET/ID names, T-based selection, and a separate command/context channel; native CI must verify all changes at the same SHA. Unix now uses separate FIFO descriptors rather than `O_RDWR` for its manager request FIFO.
