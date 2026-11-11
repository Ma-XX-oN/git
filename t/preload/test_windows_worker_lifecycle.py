#!/usr/bin/env python3
"""Native worker regressions for stdin lifetime and output-before-status cleanup."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import uuid

if os.name != "nt":
    raise RuntimeError("Windows native worker test only")

K = C.WinDLL("kernel32", use_last_error=True)
K.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID,
                         W.DWORD, W.DWORD, W.HANDLE]
K.CreateFileW.restype = W.HANDLE
K.WriteFile.argtypes = [W.HANDLE, W.LPCVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID]
K.WriteFile.restype = W.BOOL
K.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID]
K.ReadFile.restype = W.BOOL
K.PeekNamedPipe.argtypes = [W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD),
                           C.POINTER(W.DWORD), C.POINTER(W.DWORD)]
K.PeekNamedPipe.restype = W.BOOL
K.FlushFileBuffers.argtypes = [W.HANDLE]
K.FlushFileBuffers.restype = W.BOOL
K.CloseHandle.argtypes = [W.HANDLE]
K.CloseHandle.restype = W.BOOL
INVALID = C.c_void_p(-1).value


def write(handle, data):
    """Write all bytes to one native pipe handle, failing on a short write."""
    count = W.DWORD()
    if not K.WriteFile(handle, data, len(data), C.byref(count), None):
        raise C.WinError(C.get_last_error())
    assert count.value == len(data), count.value


def read_to_eof(handle):
    """Drain a pipe with a deadline, preserving the client's stream order."""
    result = bytearray()
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        available = W.DWORD()
        if not K.PeekNamedPipe(handle, None, 0, None, C.byref(available), None):
            error = C.get_last_error()
            if error in (109, 233):
                return bytes(result)
            raise C.WinError(error)
        if available.value:
            buffer = C.create_string_buffer(available.value)
            count = W.DWORD()
            if not K.ReadFile(handle, buffer, len(buffer), C.byref(count), None):
                raise C.WinError(C.get_last_error())
            result.extend(buffer.raw[:count.value])
        else:
            time.sleep(.001)
    raise TimeoutError("worker did not close output before status consumption")


def run_case(git, name, arguments, expected_status, expected_output,
             stdin_marker=b"1", malformed=False, fragmented=False,
             missing_end=False, delayed_request=False, delayed_output=False,
             abandoned_probes=False, duplicate_control=False, held_probes=False,
             marker_order=None, status_after_output=False, delayed_status=False,
             expected_errors=b"", context=b""):
    """Run a worker with stdin held open while output and status are drained."""
    env = os.environ.copy()
    env["PRELOAD_PID"] = str(os.getpid())
    pipe_dir = env["PRELOAD_WIN_DIR"] = rf"\\.\pipe\preload-test-{uuid.uuid4().hex}"
    env["GIT_EDITOR"] = "preload-worker-inherited-editor"
    directory = tempfile.TemporaryDirectory(prefix="preload-worker-")
    worker = subprocess.Popen([git, "--preload-worker", directory.name], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    handles = {}
    try:
        assert worker.stdout.readline() == f"READY {worker.pid}\n".encode()
        worker.stdout.close()
        for index, channel in enumerate(("stdout", "stderr", "exit-code", "stdin", "params-cwd")):
            address = rf"{pipe_dir}\worker-{worker.pid}\{channel}"
            if abandoned_probes or held_probes or marker_order is not None:
                probe = K.CreateFileW(address, 0xc0000000, 0, None, 3, 0, None)
                assert probe != INVALID
                if held_probes or marker_order is not None:
                    handles[channel + "-probe"] = probe
                else:
                    K.CloseHandle(probe)
            handle = K.CreateFileW(address, 0xc0000000, 0, None, 3, 0, None)
            if handle == INVALID:
                raise C.WinError(C.get_last_error())
            if marker_order is not None and marker_order[index] == 0:
                handle, handles[channel + "-probe"] = handles[channel + "-probe"], handle
            handles[channel] = handle
            if channel != "stdin" or not duplicate_control:
                write(handle, stdin_marker if channel == "stdin" else b"1")
            if channel == "stdin" and stdin_marker != b"1":
                break
        if duplicate_control:
            # Hold the preceding channel so both markers precede selection.
            address = rf"{pipe_dir}\worker-{worker.pid}\params-cwd"
            duplicate = K.CreateFileW(address, 0xc0000000, 0, None, 3, 0, None)
            assert duplicate != INVALID
            handles["duplicate-control"] = duplicate
            write(duplicate, b"1")
            write(handles["stdin"], b"1")
        elif "params-cwd" in handles:
            frame = struct.pack("<I", len(arguments) + 1) + b"git\0"
            frame += b"".join((arg.encode() if isinstance(arg, str) else arg) + b"\0"
                              for arg in arguments) + context + b"end\0"
            payload = b"\0" if malformed else frame
            if missing_end:
                payload = payload[:-4]
            if delayed_request:
                write(handles["params-cwd"], payload[:4])
                assert K.FlushFileBuffers(handles["params-cwd"])
                time.sleep(.05)
                write(handles["params-cwd"], payload[4:])
            elif fragmented:
                for byte in payload:
                    write(handles["params-cwd"], bytes([byte]))
            else:
                write(handles["params-cwd"], payload)
            if malformed or missing_end:
                assert K.FlushFileBuffers(handles["params-cwd"])
                K.CloseHandle(handles.pop("params-cwd"))
        if delayed_output:
            time.sleep(.1)
        if status_after_output:
            # Leave command output unread: its flush must prevent an early result.
            deadline = time.monotonic() + 3
            available = W.DWORD()
            while time.monotonic() < deadline:
                assert K.PeekNamedPipe(handles["stdout"], None, 0, None,
                                       C.byref(available), None)
                if available.value:
                    break
                time.sleep(.001)
            assert available.value, "command did not produce output"
            time.sleep(.05)
            assert K.PeekNamedPipe(handles["exit-code"], None, 0, None,
                                   C.byref(available), None)
            assert available.value == 0, "exit result preceded output consumption"
        output = read_to_eof(handles["stdout"])
        errors = read_to_eof(handles["stderr"])
        if delayed_status:
            # The worker must retain the record until the client consumes it.
            time.sleep(.05)
            assert worker.poll() is None, "worker exited before status consumption"
        status = read_to_eof(handles["exit-code"])
        assert output == expected_output, (name, output)
        assert errors == expected_errors, (name, errors)
        assert status == f"EXIT {expected_status}\n".encode(), (name, status)
        assert worker.wait(timeout=5) == expected_status
        print("GIT_PRELOAD_WORKER_LIFECYCLE_PASS", name, flush=True)
    finally:
        for handle in handles.values():
            K.CloseHandle(handle)
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=5)
        worker.stdout.close()
        directory.cleanup()


def test_marker_timeout(git):
    """A missing marker must cancel both connection threads and exit promptly."""
    env = os.environ.copy()
    env["PRELOAD_PID"] = str(os.getpid())
    env["PRELOAD_WIN_DIR"] = rf"\\.\pipe\preload-test-{uuid.uuid4().hex}"
    directory = tempfile.TemporaryDirectory(prefix="preload-worker-")
    worker = subprocess.Popen([git, "--preload-worker", directory.name], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    try:
        assert worker.stdout.readline() == f"READY {worker.pid}\n".encode()
        assert worker.wait(timeout=4) == 122
        print("GIT_PRELOAD_WORKER_LIFECYCLE_PASS missing-marker-timeout", flush=True)
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=5)
        worker.stdout.close()
        directory.cleanup()


def test_missing_pipe_dir(git):
    """Without PRELOAD_WIN_DIR the worker must fail before READY."""
    env = os.environ.copy()
    env["PRELOAD_PID"] = str(os.getpid())
    env.pop("PRELOAD_WIN_DIR", None)
    with tempfile.TemporaryDirectory(prefix="preload-worker-") as directory:
        result = subprocess.run([git, "--preload-worker", directory], env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    assert result.returncode == 136, result.returncode
    assert b"READY" not in result.stdout, result.stdout
    assert b"PRELOAD_WIN_DIR" in result.stderr, result.stderr
    print("GIT_PRELOAD_WORKER_LIFECYCLE_PASS missing-pipe-dir", flush=True)


if __name__ == "__main__":
    git = str(Path(sys.argv[1] if len(sys.argv) > 1 else
                   os.environ["GIT_PRELOAD_WORKER_GIT"]).resolve())
    test_missing_pipe_dir(git)
    version = subprocess.check_output([git, "--version"])
    run_case(git, "stdin-held-open", ["--version"], 0, version)
    run_case(git, "output-flush-precedes-status", ["--version"], 0, version,
             status_after_output=True, delayed_status=True)
    failure = subprocess.run([git, "--git-preload-invalid-option"], capture_output=True)
    assert failure.returncode != 0 and failure.stderr
    run_case(git, "nonzero-exit-with-stderr", ["--git-preload-invalid-option"],
             failure.returncode, failure.stdout, expected_errors=failure.stderr,
             delayed_status=True)
    run_case(git, "abandoned-probes-on-every-channel", ["--version"],
             0, version, abandoned_probes=True)
    run_case(git, "probes-stay-open-on-every-channel", ["--version"],
             0, version, held_probes=True)
    run_case(git, "alternating-marked-connections", ["--version"],
             0, version, marker_order=(0, 1, 0, 1, 0))
    run_case(git, "reverse-alternating-marked-connections", ["--version"],
             0, version, marker_order=(1, 0, 1, 0, 1))
    run_case(git, "request-read-pends-between-fragments", ["--version"],
             0, version, delayed_request=True)
    with tempfile.TemporaryDirectory(prefix="git-preload-output-") as directory:
        config = Path(directory) / "large.config"
        value = b"x" * (1024 * 1024)
        config.write_bytes(b"[large]\nvalue = " + value + b"\n")
        run_case(git, "output-exceeds-pipe-buffer", ["config", "--file",
                 str(config), "--get", "large.value"], 0, value + b"\n",
                 delayed_output=True)
    run_case(git, "end-is-an-argument", ["--version", "end"], 0, version,
             fragmented=True)
    run_case(git, "stdin-produces-eof", ["hash-object", "--stdin"], 0,
             b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391\n")
    run_case(git, "invalid-stdin-marker-closes-output", [], 122, b"", stdin_marker=b"X")
    run_case(git, "nul-stdin-marker-closes-output", [], 122, b"", stdin_marker=b"\0")
    run_case(git, "malformed-request-closes-output", [], 124, b"", malformed=True)
    run_case(git, "missing-end-tag-closes-output", ["--version"], 124, b"",
             missing_end=True)
    run_case(git, "unsupported-environment-field", ["--version"], 124, b"",
             context=b"env\0")
    run_case(git, "unsupported-export-field", ["--version"], 124, b"",
             context=b"env-export\0")
    run_case(git, "environment-inherited-from-spawn", ["var", "GIT_EDITOR"],
             0, b"preload-worker-inherited-editor\n")
    for bad in (b"\xff", b"\xc0\x80", b"\xed\xa0\x80"):
        run_case(git, "invalid-utf8-argv-" + bad.hex(), [bad], 128, b"")
    for iteration in range(10):
        run_case(git, f"duplicate-control-markers-{iteration}", [], 122, b"",
                 duplicate_control=True)
    test_marker_timeout(git)
