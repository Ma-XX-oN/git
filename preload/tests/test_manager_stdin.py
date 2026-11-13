#!/usr/bin/env python3
"""Check stdin data and EOF through both Windows managers and real Git workers.

Run --git PATH --output NEW_DIRECTORY after building the current worker and
manager. This exercises manager-launched workers, unlike the direct-worker
lifecycle test, and keeps raw manager logs in the output directory.
"""
import argparse
import ctypes as C
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

from test_manager import Session, ROOT, WINDOWS

if WINDOWS:
    from test_windows_worker_lifecycle import K, W, INVALID, read_to_eof, write


def acknowledge(handle):
    """Read exactly one ACK within ten seconds; raise on failure.

    Parameters
    ----------
    handle : int
        Borrowed, connected native stdin pipe handle; remains open on return.
    """
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        available = W.DWORD()
        if not K.PeekNamedPipe(handle, None, 0, None, C.byref(available), None):
            raise C.WinError(C.get_last_error())
        if available.value:
            byte = C.create_string_buffer(1)
            count = W.DWORD()
            assert K.ReadFile(handle, byte, 1, C.byref(count), None)
            assert count.value == 1 and byte.raw == b"\x06", byte.raw
            return
        time.sleep(.001)
    raise TimeoutError("worker did not acknowledge stdin")


def run_case(session, arguments, data, expected, interactive=False):
    """Run one manager-backed call; verify concurrent streams and client-owned EOF.

    Parameters
    ----------
    session : Session
        Borrowed manager session with a registered git pool.
    arguments : list of str
        Exact command arguments, excluding the executable.
    data, expected : bytes
        Exact stdin input and expected stdout output.
    interactive : bool
        Require the first output line before closing the client's stdin.

    Notes
    -----
    Returns None on success; assertions or I/O failures propagate. Every opened
    native handle and thread is released on success or failure.
    """
    request = session.connect()
    handles, threads, failures, output = {}, [], [], {}
    first_line = threading.Event()
    try:
        frame = struct.pack("<I", len(arguments)) + b"git\0"
        frame += b"".join(word.encode("utf-8") + b"\0" for word in arguments)
        frame += b"cwd\0" + str(ROOT).encode("utf-8") + b"\n\0end\0"
        write(request, b"GET git 1\n" + frame)
        reply, count = C.create_string_buffer(8), W.DWORD()
        assert K.ReadFile(request, reply, 8, C.byref(count), None) and count.value == 8
        pid = int(reply.raw, 16)
        assert pid
        for channel in ("stdin", "stdout", "stderr", "exit-code"):
            handle = K.CreateFileW(rf"{session.pipe_dir}\worker-{pid}\{channel}",
                                   0xc0000000, 0, None, 3, 0, None)
            assert handle != INVALID, (channel, C.get_last_error())
            handles[channel] = handle
            write(handle, b"1")
        acknowledge(handles["stdin"])

        def drain(channel):
            """Drain one borrowed output concurrently; capture failures for caller."""
            try:
                if interactive and channel == "stdout":
                    pending = bytearray()
                    while b"\n" not in pending:
                        byte = C.create_string_buffer(1)
                        got = W.DWORD()
                        if not K.ReadFile(handles[channel], byte, 1, C.byref(got), None) or not got.value:
                            raise AssertionError("interactive stdout ended before response")
                        pending.extend(byte.raw)
                    output[channel] = bytes(pending)
                    first_line.set()
                    output[channel] += read_to_eof(handles[channel])
                else:
                    output[channel] = read_to_eof(handles[channel])
            except BaseException as error:
                failures.append(error)

        for channel in ("stdout", "stderr"):
            thread = threading.Thread(target=drain, args=(channel,))
            thread.start()
            threads.append(thread)

        def feed():
            """Send exact input, then close the client's stdin endpoint to deliver EOF."""
            try:
                if data: write(handles["stdin"], data)
                if interactive and not first_line.wait(5):
                    raise AssertionError("command did not receive stdin before EOF")
            except BaseException as error:
                failures.append(error)
            finally:
                K.CloseHandle(handles.pop("stdin"))

        thread = threading.Thread(target=feed)
        thread.start()
        threads.append(thread)
        for thread in threads:
            thread.join(15)
        assert not any(thread.is_alive() for thread in threads), "stdin/output stalled"
        if failures: raise failures[0]
        assert output["stdout"] == expected, (output["stdout"][:200], expected[:200])
        assert output["stderr"] == b"", output["stderr"]
        assert read_to_eof(handles["exit-code"]) == b"EXIT 0\n"
    finally:
        if any(thread.is_alive() for thread in threads):
            session.stop()  # Worker exit unblocks I/O before native handles are closed.
            for thread in threads: thread.join(5)
        for handle in handles.values(): K.CloseHandle(handle)
        K.CloseHandle(request)


def main():
    """Compare both manager launch paths with the same worker binary and input."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("both", "cpp", "python"), default="both")
    args = parser.parse_args()
    if not WINDOWS: parser.error("Windows named-pipe test")
    git = args.git.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=False)
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    cases = [("empty-eof", ["hash-object", "--stdin"], b"", False),
             ("binary-stdin", ["hash-object", "--stdin"], bytes(range(256)) * 5, False),
             ("large-input-output", ["stripspace"], b"preload stdin line\n" * 60000, False),
             ("interactive-before-eof", ["cat-file", "--batch-check"], b"HEAD\n", True)]
    for backend in (("cpp", "python") if args.backend == "both" else (args.backend,)):
        directory = args.output / backend
        directory.mkdir()
        command = [sys.executable, str(ROOT / "preload/manager/manager_windows.py")] if backend == "python" else None
        session = Session(directory, manager_command=command, environment=environment)
        try:
            assert session.request(f"PRELOAD git {git}") == "OK", session.read_log()
            for name, words, data, interactive in cases:
                direct = subprocess.run([str(git), *words], input=data, capture_output=True,
                                        cwd=ROOT, env=environment, timeout=15, check=True)
                run_case(session, words, data, direct.stdout, interactive)
                print("PRELOAD_MANAGER_STDIN_PASS", backend, name, flush=True)
        finally:
            if session.process.poll() is None: session.stop()


if __name__ == "__main__":
    main()
