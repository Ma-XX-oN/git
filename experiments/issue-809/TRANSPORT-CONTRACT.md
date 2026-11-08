# RWF 809 — Git IPC transport contract

Status: approved design; implementation and native acceptance must be verified separately.

## Terminology and ownership

- **git-manager** is the shell-owned process that maintains a configured number of READY Git workers, allocates one ready worker to `git-ipc`, and asynchronously starts its replacement.
- **git-ipc** is the Git Bash client function. It requests an allocated worker ID and exchanges command arguments, cwd, environment, standard streams, and exit status directly with that worker.
- An allocated Git worker is no longer owned or retained by git-manager. It watches its original Bash owner for termination.
- A *logical channel* has one direction of application data. Windows pipe instances required by Git Bash's opening behaviour are not additional logical channels.

## Cross-platform contract

Source `experiments/issue-809/git-manager.sh [worker-count]` as the common
entry point. It uses Bash's `OSTYPE`: `msys*` and `cygwin*` select the Windows client;
`linux*` and `darwin*` source `git-manager-unix.sh`, forwarding the arguments
and return status. Other platforms return an unsupported-platform error.

Every logical FIFO or named-pipe channel is treated as **unidirectional** on Linux, macOS, and Windows. No application protocol depends on Linux's non-POSIX `O_RDWR` FIFO extension or on Windows full-duplex application transport.

Allocation uses two distinct logical channels on **all** platforms:

1. `allocate-req`: git-ipc -> git-manager, carrying `GET\n`.
2. `allocate-reply`: git-manager -> git-ipc, carrying one worker identifier followed by newline (or a defined error).

The allocated worker has **five distinct logical channels**: `stdin` and `param-env-cwd` (git-ipc -> worker); `stdout`, `stderr`, and `exit-code` (worker -> git-ipc). Arguments, cwd and environment are carried exclusively by `param-env-cwd`; stdin is a separate stream. Windows sends and consumes the call-specific decimal digit in place of `T` on each channel, including stdin. No handshake byte enters application data.

Linux/macOS use ordinary unidirectional FIFOs, with **no** `T` marker.

### Standard-input limitation: no additional relay processes

The `stdin` logical channel remains separately established, and on Windows
the Git worker consumes the initial decimal marker before application I/O.
However, **application stdin forwarding is not implemented**. On Unix the
Bash client closes the stdin writer after connection setup. On Windows the
client keeps it open through output EOF so the connection marker cannot be
discarded before consumption; the native worker supplies stdin EOF from NUL.
The Windows command/context frame ends with an `end` NUL-terminated tag after
argv and context fields. The worker recognizes this tag at a field boundary;
the client keeps the control writer open through output EOF instead of using
pipe closure to delimit the request. Unix framing is unchanged.
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

## Windows call marker

For serial calls, `git-ipc` replaces each Windows `T` connection marker
with the decimal digit `sequence % 10`, including GET, reply, and all
five worker channels. The request ID remains `BashPID-sequence` and
continues to determine the unique reply-pipe name. The manager verifies
that the GET and reply marker match the sequence. The native worker
requires the same digit on all five channels. No acknowledgment, additional
connection, or stream-handling change is introduced.

## Transport-selection API

The Windows C Git worker uses `git809_start_connections(context, instances)`
before publishing READY and `git809_finish_connections(context)` to join the
two connection threads and transfer their accepted handles into `instances`.
The return value is an error code: `TRANSPORT_OK=0`,
`TRANSPORT_TIMEOUT=1`, `TRANSPORT_INVALID_MARKER=2`,
`TRANSPORT_DUPLICATE_MARKER=3`, or `TRANSPORT_WIN32_ERROR=4`.
Each accepted handle is owned by the caller. Unselected handles close after
both threads finish; the shared context remains alive until both joins complete.
The first channel accepts any decimal digit; subsequent channels must match
that digit. Two connection threads are created before READY, traverse all five
channels themselves, and are joined before request processing.
They connect and consume exactly one marker, without relaying application
bytes. The selected handles remain synchronous for Git's CRT streams.
After each candidate operation returns, the threads publish their arrival
generation. The first thread to read a marker atomically becomes the winner,
cancels the other instance with `CancelIoEx`, and spins with `YieldProcessor()`
until the peer arrives. `ERROR_NOT_FOUND` causes cancellation to be retried
while the peer has not acknowledged completion. Once cancellation is accepted,
only the acknowledgement is awaited. The winner checks both marker results and
publishes the accepted handle or error, then advances the completed generation
so both threads can continue. Cancellation and duplicate-marker inspection
stay on the same thread, so cancellation cannot interrupt that final inspection.
Monotonically increasing arrival/completion generations preserve acknowledgements
across successive channels. Connect and marker reads remain blocking; the
main thread does not dispatch or acknowledge individual pairs. Connection
deadlines and owner death still terminate stalled handshakes.

The Python `git-manager` implements `TransportPair(port, address, marker)`.
Its `advance()` returns the selected `Pipe` or None while selection is pending,
and raises on failure. Production replies and selector tests use this same
implementation. Each `Pipe` owns its native handle, OVERLAPPED, buffer, and
operation result until completion or cancellation has been dequeued. A shared
I/O completion port waits for all GET and reply operations without the
64-event limit. No cross-language
C library is introduced: GET and ID connect Bash with Python, while the five
Git worker channels connect Bash with C.

Worker allocation is nonblocking: an empty READY queue leaves that request
waiting for a readiness notification, with a five-second deadline before WAIT.
After writing a reply, the manager waits asynchronously for client disconnect
before closing the instance. It does not block other clients in a pipe flush.

## Existing output limitation

The current Bash client drains stdout/stderr using Bash `read -d ''` into
shell strings. Bash variables cannot contain NUL bytes. Consequently the
existing Git IPC experiment is **not yet a byte-transparent stdout/stderr
transport for binary Git output**. This pre-existing limitation is separate
from, and is not corrected by, Windows `T` identification. No full-Git-compatibility claim is permitted until
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
`TransportPair` used for each unique ID reply pipe. This is **not
yet verified as satisfying the agreed pair-selection contract**. Unlike a
private worker channel, GET accepts concurrent independent git-ipc clients:
two distinct GET connections can both carry valid `T` markers without
representing a duplicate marker on one client's transport pair.

Do not replace the shared request listener with a single global two-instance
pair without preserving concurrent-client isolation. The exact GET
pair/acceptance model must be reconciled with this concurrency requirement
before claiming full protocol acceptance.



The original Windows single duplex GET/PID pipe and instance-order selection are superseded. The implementation branch introduces separate GET/ID names, T-based selection, and a separate command/context channel; native CI must verify all changes at the same SHA. Unix now uses separate FIFO descriptors rather than `O_RDWR` for its manager request FIFO.
