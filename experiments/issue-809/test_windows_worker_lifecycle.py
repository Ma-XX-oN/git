#!/usr/bin/env python3
"""Native worker regressions for stdin lifetime and output-before-status cleanup."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

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
             missing_end=False):
    """Run a worker with stdin held open while output and status are drained."""
    env = os.environ.copy()
    env["GIT809_PRELOAD_OWNER_WINPID"] = str(os.getpid())
    worker = subprocess.Popen([git, "--preload"], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    handles = {}
    try:
        assert worker.stdout.readline() == f"READY {worker.pid}\n".encode()
        worker.stdout.close()
        for channel in ("stdout", "stderr", "exit-code", "stdin", "param-env-cwd"):
            address = rf"\\.\pipe\{worker.pid}-{channel}"
            handle = K.CreateFileW(address, 0xc0000000, 0, None, 3, 0, None)
            if handle == INVALID:
                raise C.WinError(C.get_last_error())
            handles[channel] = handle
            write(handle, stdin_marker if channel == "stdin" else b"1")
            if channel == "stdin" and stdin_marker != b"1":
                break
        if "param-env-cwd" in handles:
            frame = struct.pack("<I", len(arguments) + 1) + b"git\0"
            frame += b"".join(arg.encode() + b"\0" for arg in arguments) + b"end\0"
            payload = b"\0" if malformed else frame
            if missing_end:
                payload = payload[:-4]
            if fragmented:
                for byte in payload:
                    write(handles["param-env-cwd"], bytes([byte]))
            else:
                write(handles["param-env-cwd"], payload)
            if malformed or missing_end:
                assert K.FlushFileBuffers(handles["param-env-cwd"])
                K.CloseHandle(handles.pop("param-env-cwd"))
        output = read_to_eof(handles["stdout"])
        errors = read_to_eof(handles["stderr"])
        status = read_to_eof(handles["exit-code"])
        assert output == expected_output, (name, output)
        assert not errors, (name, errors)
        assert status == f"EXIT {expected_status}\n".encode(), (name, status)
        assert worker.wait(timeout=5) == expected_status
        print("GIT809_WORKER_LIFECYCLE_PASS", name, flush=True)
    finally:
        for handle in handles.values():
            K.CloseHandle(handle)
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=5)
        worker.stdout.close()


if __name__ == "__main__":
    git = str(Path(sys.argv[1] if len(sys.argv) > 1 else
                   os.environ["GIT809_WORKER_GIT"]).resolve())
    version = subprocess.check_output([git, "--version"])
    run_case(git, "stdin-held-open", ["--version"], 0, version)
    run_case(git, "end-is-an-argument", ["--version", "end"], 0, version,
             fragmented=True)
    run_case(git, "stdin-produces-eof", ["hash-object", "--stdin"], 0,
             b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391\n")
    run_case(git, "invalid-stdin-marker-closes-output", [], 122, b"", stdin_marker=b"X")
    run_case(git, "malformed-request-closes-output", [], 124, b"", malformed=True)
    run_case(git, "missing-end-tag-closes-output", ["--version"], 124, b"",
             missing_end=True)
