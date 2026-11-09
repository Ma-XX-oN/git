#!/usr/bin/env python3
"""Verify real FIFO worker framing, context and ordered completion on POSIX."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile


def frame(arguments, context=b""):
    """Encode the bounded argv frame followed by context fields."""
    return struct.pack("<I", len(arguments) + 1) + b"git\0" + b"".join(
        argument.encode("utf-8") + b"\0" for argument in arguments) + context


def run_case(git, name, payload, status, output):
    """Send one FIFO request and require output EOF before status consumption."""
    with tempfile.TemporaryDirectory(prefix="preload-worker-") as directory:
        for channel in ("stdout", "stderr", "exit-code", "stdin", "param-env-cwd"):
            os.mkfifo(Path(directory) / channel)
        environment = os.environ.copy()
        environment["GIT_PRELOAD_OWNER_PID"] = str(os.getpid())
        worker = subprocess.Popen([git, "--preload-worker", directory], env=environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        streams = []
        try:
            assert worker.stdout.readline() == f"READY {worker.pid}\n".encode()
            for channel, mode in (("stdout", "rb"), ("stderr", "rb"), ("exit-code", "rb"),
                                  ("stdin", "wb"), ("param-env-cwd", "wb")):
                streams.append(open(Path(directory) / channel, mode, buffering=0))
            streams[3].close()
            streams[4].write(payload)
            streams[4].close()
            assert streams[0].read() == output, name
            errors = streams[1].read()
            if status == 0:
                assert errors == b"", (name, errors)
            assert streams[2].read() == f"EXIT {status}\n".encode(), name
            assert worker.wait(timeout=5) == status, name
            assert not Path(directory).exists(), "worker retained FIFO endpoints"
            print("PRELOAD_POSIX_WORKER_PASS", name, flush=True)
        finally:
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
    run_case(git, "version", frame(["--version"]), 0, version)
    run_case(git, "stdin-eof", frame(["hash-object", "--stdin"]), 0,
             subprocess.check_output([git, "hash-object", "--stdin"], input=b""))
    run_case(git, "literal-end-argument", frame(["check-ref-format", "refs/heads/end"]), 0, b"")
    run_case(git, "short-header", b"\0", 125, b"")
    run_case(git, "zero-argc", struct.pack("<I", 0), 126, b"")
    run_case(git, "excess-argc", struct.pack("<I", 33), 126, b"")
    run_case(git, "unterminated-argv", struct.pack("<I", 1) + b"git", 128, b"")
    run_case(git, "wrong-program", struct.pack("<I", 1) + b"other\0", 129, b"")
    run_case(git, "malformed-context", frame(["--version"], b"unknown\0"), 129, b"")
    run_case(git, "export-value", frame(["var", "GIT_EDITOR"],
             b'env-export\0declare -x GIT_EDITOR="literal $(do not execute)"\n\0'),
             0, b"literal $(do not execute)\n")


if __name__ == "__main__":
    main()
