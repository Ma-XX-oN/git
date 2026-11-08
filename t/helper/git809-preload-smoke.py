#!/usr/bin/env python3
"""Native Win32 smoke test of the production-source --preload IPC path."""
import ctypes
from ctypes import wintypes
import subprocess
import struct
import sys
import threading
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
    wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
BAD_HANDLE = ctypes.c_void_p(-1).value
READ, WRITE = 0x80000000, 0x40000000
OPEN_EXISTING = 3
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_NOT_CONNECTED = 233

def connect(pid, suffix, access):
    path = rf"\\.\pipe\{pid}-{suffix}"
    deadline = time.monotonic() + 10
    while True:
        h = kernel32.CreateFileW(path, access, 0, None, OPEN_EXISTING, 0, None)
        if h != BAD_HANDLE:
            return h
        err = ctypes.get_last_error()
        if time.monotonic() >= deadline:
            raise OSError(err, f"cannot open {path}")
        kernel32.WaitNamedPipeW(path, 100)

def read_all(handle):
    output = bytearray()
    buf = ctypes.create_string_buffer(65536)
    count = wintypes.DWORD()
    while kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(count), None):
        if not count.value:
            break
        output.extend(buf.raw[:count.value])
    err = ctypes.get_last_error()
    if err not in (0, ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED):
        raise OSError(err, "named pipe read failed")
    return bytes(output)

def run(git, args):
    worker = subprocess.Popen([git, "--preload"], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    handles = []
    try:
        ready = worker.stdout.readline().decode("utf-8", "strict")
        expected = f"READY {worker.pid}\n"
        assert ready == expected, (ready, expected)
        for suffix, access in (("stdout", READ), ("stderr", READ),
                               ("exit-code", READ), ("stdin", WRITE)):
            # Emulate MSYS's extra metadata open before the data connection.
            probe = connect(worker.pid, suffix, access)
            kernel32.CloseHandle(probe)
            handles.append(connect(worker.pid, suffix, access))
        output = {}
        errors = []
        def collect(name, handle):
            try:
                output[name] = read_all(handle)
            except Exception as exc:
                errors.append(exc)
        readers = [threading.Thread(target=collect, args=(name, h), daemon=True)
                   for name, h in (("out", handles[0]), ("err", handles[1]))]
        for reader in readers:
            reader.start()
        argv = ["git", *args]
        frame = struct.pack("<I", len(argv)) + b"".join(
            value.encode("utf-8") + b"\0" for value in argv)
        assert len(frame) <= 4096
        buf = ctypes.create_string_buffer(frame)
        sent = wintypes.DWORD()
        if not kernel32.WriteFile(handles[3], buf, len(frame),
                                  ctypes.byref(sent), None):
            raise OSError(ctypes.get_last_error(), "failed to submit command")
        assert sent.value == len(frame)
        kernel32.CloseHandle(handles[3])
        handles[3] = None
        for reader in readers:
            reader.join(timeout=15)
            assert not reader.is_alive(), "stdout/stderr drain stalled"
        assert not errors, errors
        status = read_all(handles[2])
        assert status.startswith(b"EXIT ") and status.endswith(b"\n"), status
        code = int(status[5:-1])
        worker.wait(timeout=15)
        assert worker.returncode == code, (worker.returncode, code)
        return code, output["out"], output["err"]
    finally:
        for h in handles:
            if h is not None:
                kernel32.CloseHandle(h)
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)

def main():
    git = sys.argv[1]
    code, out, err = run(git, ["--version"])
    assert code == 0 and out.startswith(b"git version ") and not err, (
        code, out, err)
    print("PASS: --preload READY, closed stdin, stdout/stderr, exit-code")
    code, out, err = run(git, ["nonexistent-git809-command"])
    assert code != 0 and err, (code, out, err)
    print("PASS: nonzero command exit and stderr delivery")

if __name__ == "__main__":
    main()
