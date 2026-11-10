#!/usr/bin/env python3
"""Verify real FIFO worker framing, context and ordered completion on POSIX."""
import os
import signal
from pathlib import Path
import struct
import subprocess
import sys
import tempfile


def frame(arguments, context=b""):
    """Encode the bounded argv frame followed by context fields."""
    return struct.pack("<I", len(arguments) + 1) + b"git\0" + b"".join(
        argument.encode("utf-8") + b"\0" for argument in arguments) + context + b"end\0"


def request_timeout(signum, frame):
    """Interrupt a stalled FIFO operation so fixture cleanup can kill its worker.

    Parameters
    ----------
    signum : int
        SIGALRM delivered after the ten-second case deadline.
    frame : frame
        Interrupted Python stack frame, supplied by the signal module.
    """
    raise TimeoutError("preload worker FIFO case exceeded ten seconds")


def run_case(git, name, payload, status, output, hold_request=False):
    """Send one FIFO request and require output EOF before status consumption."""
    with tempfile.TemporaryDirectory(prefix="preload-worker-") as directory:
        environment = os.environ.copy()
        environment["GIT_PRELOAD_OWNER_PID"] = str(os.getpid())
        environment["GIT_EDITOR"] = "preload-worker-inherited-editor"
        worker = subprocess.Popen([git, "--preload-worker", directory], env=environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        streams = []
        worker_directory = Path(directory) / f"worker-{worker.pid}"
        previous = signal.signal(signal.SIGALRM, request_timeout)
        signal.alarm(10)
        try:
            assert worker.stdout.readline() == f"READY {worker.pid}\n".encode()
            for channel, mode in (("stdout", "rb"), ("stderr", "rb"), ("exit-code", "rb"),
                                  ("stdin", "wb"), ("param-env-cwd", "wb")):
                streams.append(open(worker_directory / channel, mode, buffering=0))
            streams[3].close()
            streams[4].write(payload)
            if not hold_request:
                streams[4].close()
            assert streams[0].read() == output, name
            errors = streams[1].read()
            if status == 0:
                assert errors == b"", (name, errors)
            assert streams[2].read() == f"EXIT {status}\n".encode(), name
            assert worker.wait(timeout=5) == status, name
            assert not worker_directory.exists(), "worker retained FIFO endpoints"
            print("PRELOAD_POSIX_WORKER_PASS", name, flush=True)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
            for stream in streams:
                stream.close()
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)
            worker.stdout.close()
            worker.stderr.close()


def main():
    """Check valid commands and malformed frames through the native entry point."""
    assert os.name != "nt"
    git = str(Path(sys.argv[1]).resolve())
    version = subprocess.check_output([git, "--version"])
    run_case(git, "version-with-context-writer-held", frame(["--version"]),
             0, version, hold_request=True)
    run_case(git, "stdin-eof", frame(["hash-object", "--stdin"]), 0,
             subprocess.check_output([git, "hash-object", "--stdin"], input=b""))
    run_case(git, "literal-end-argument", frame(["check-ref-format", "refs/heads/end"]), 0, b"")
    run_case(git, "short-header", b"\0", 124, b"")
    run_case(git, "zero-argc", struct.pack("<I", 0), 124, b"")
    run_case(git, "excess-argc", struct.pack("<I", 33), 124, b"")
    run_case(git, "high-bit-argc", struct.pack("<I", 0x80000000), 124, b"")
    run_case(git, "maximum-argc", struct.pack("<I", 0xffffffff), 124, b"")
    run_case(git, "unterminated-argv", struct.pack("<I", 1) + b"git", 124, b"")
    run_case(git, "wrong-program", struct.pack("<I", 1) + b"other\0end\0", 128, b"")
    run_case(git, "malformed-context", frame(["--version"], b"unknown\0"), 124, b"")
    run_case(git, "unsupported-environment-field", frame(["--version"], b"env\0"),
             124, b"")
    run_case(git, "unsupported-export-field", frame(["--version"], b"env-export\0"),
             124, b"")
    run_case(git, "environment-inherited-from-spawn",
             frame(["var", "GIT_EDITOR"]), 0, b"preload-worker-inherited-editor\n")
    for bad in (b"\xff", b"\xc0\x80", b"\xed\xa0\x80"):
        run_case(git, "invalid-utf8-argv-" + bad.hex(),
                 struct.pack("<I", 2) + b"git\0" + bad + b"\0end\0", 128, b"")


if __name__ == "__main__":
    main()
