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

For allocation, GET and worker ID use two **persistent, separate logical
channels**, connected once when the owning Bash shell sources git-manager.sh.
The manager publishes both named-pipe pairs before READY. Both connections
consume the initial ASCII `0` marker once, after which the shell sends
`GET\n` and reads one worker ID followed by newline for each command.
There is exactly one outstanding allocation per owning shell; a subsequent
GET is not sent before the previous reply is consumed. Both descriptors
remain open across commands and close on shell termination. The five
per-worker channels remain one-shot and continue to use the per-command
decimal digit `sequence % 10`.

Concurrent git-ipc calls using the same allocation descriptors are outside
this serial-client protocol; no per-request reply pipe or identification is
created. All application data remains unidirectional even though Windows
named-pipe instances have duplex capability for the initial marker.


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

The Python `git-manager` uses two `TransportPair` objects during
allocator startup: one for GET and one for worker-ID replies. Production
and selector tests use the same overlapped marker-selection implementation.
Each selected `Pipe` remains connected and retains its Win32 handle for
the lifetime of the shell. The allocation loop reads `GET\n`, waits for a
READY worker (at most five seconds), writes its PID or `WAIT\n`, and then
reads the next GET. It never creates a per-command reply pipe or performs a
blocking reply flush. Pool replenishment is asynchronous. The same I/O
completion port handles both long-lived allocator channels and worker-ready
notifications.


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

The persistent allocator pair is owned by the sourcing shell. Command
substitutions inherit its descriptors for serial invocations. The manager
has one outstanding allocation at a time and handles EOF, owner death, and
pending I/O cancellation at shutdown. Native worker channels are
unchanged; their call-specific marker must still match across all five
channels.


The original Windows single duplex GET/PID pipe and per-request reply names are superseded. The implementation branch introduces separate GET/ID names, T-based selection, and a separate command/context channel; native CI must verify all changes at the same SHA. Unix now uses separate FIFO descriptors rather than `O_RDWR` for its manager request FIFO.
