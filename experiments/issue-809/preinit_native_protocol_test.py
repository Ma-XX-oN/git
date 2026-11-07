#!/usr/bin/env python3
"""Native Win32 protocol regression for Git's explicit --preinit worker.

Independent direct client: no Python stdout/stderr relay in the Bash fast
path. Exercises exact committed bytes using Win32 handles and two instances
per endpoint, as required by the Git Bash interoperability protocol.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
import time

K = C.WinDLL("kernel32", use_last_error=True)
K.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID,
                          W.DWORD, W.DWORD, W.HANDLE]
K.CreateFileW.restype = W.HANDLE
K.WaitNamedPipeW.argtypes = [W.LPCWSTR, W.DWORD]
K.WaitNamedPipeW.restype = W.BOOL
K.CloseHandle.argtypes = [W.HANDLE]
K.CloseHandle.restype = W.BOOL
K.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD,
                       C.POINTER(W.DWORD), W.LPVOID]
K.ReadFile.restype = W.BOOL
K.WriteFile.argtypes = [W.HANDLE, W.LPCVOID, W.DWORD,
                        C.POINTER(W.DWORD), W.LPVOID]
K.WriteFile.restype = W.BOOL

INVALID = C.c_void_p(-1).value
READ = 0x80000000
WRITE = 0x40000000
OPEN_EXISTING = 3
BROKEN = {109, 232, 233}


def endpoint(pid: int, channel: str, access: int):
    address = "\\\\.\\pipe\\" + str(pid) + "-" + channel
    if not K.WaitNamedPipeW(address, 10000):
        raise OSError(C.get_last_error(), "pipe unavailable " + address)
    handles = []
    try:
        for i in range(2):
            h = K.CreateFileW(address, access, 0, None, OPEN_EXISTING, 0, None)
            if h == INVALID:
                raise OSError(C.get_last_error(), "CreateFile " + channel +
                              " instance " + str(i))
            handles.append(h)
        K.CloseHandle(handles[0])
        return handles[1]
    except BaseException:
        for h in handles:
            K.CloseHandle(h)
        raise


def read_all(handle: int):
    chunks = []
    buf = C.create_string_buffer(65536)
    count = W.DWORD()
    try:
        while True:
            if not K.ReadFile(handle, buf, len(buf), C.byref(count), None):
                err = C.get_last_error()
                if err in BROKEN:
                    break
                raise OSError(err, "native pipe ReadFile")
            if count.value == 0:
                break
            chunks.append(buf.raw[:count.value])
        return b"".join(chunks)
    finally:
        K.CloseHandle(handle)


def write_all(handle: int, data: bytes):
    try:
        written = W.DWORD()
        pos = 0
        while pos < len(data):
            block = data[pos:pos + 4096]
            if not K.WriteFile(handle, block, len(block),
                               C.byref(written), None):
                raise OSError(C.get_last_error(), "native pipe WriteFile")
            if not written.value:
                raise AssertionError("zero-byte request write")
            pos += written.value
    finally:
        K.CloseHandle(handle)


def pipe_call(git: Path, argv, *, slow_reader=False):
    p = subprocess.Popen([str(git), "--preinit"], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, bufsize=0)
    fds = {}
    results = {}
    threads = {}
    try:
        ready = p.stdout.readline()
        assert ready == ("READY " + str(p.pid) + "\n").encode(), (
            "unexpected worker handshake", ready, p.poll())
        assert p.poll() is None
        for name in ("stdout", "stderr", "exit-code", "stdin"):
            fds[name] = endpoint(p.pid, name,
                                 WRITE if name == "stdin" else READ)
        frame = struct.pack("<I", len(argv) + 1)
        for arg in ("git", *argv):
            value = arg.encode("utf-8")
            assert b"\0" not in value
            frame += value + b"\0"
        assert len(frame) <= 4096

        if not slow_reader:
            for name in ("stdout", "stderr"):
                handle = fds.pop(name)
                t = threading.Thread(
                    target=lambda n, h: results.setdefault(n, read_all(h)),
                    args=(name, handle), daemon=True)
                t.start()
                threads[name] = t
        write_all(fds.pop("stdin"), frame)
        if slow_reader:
            time.sleep(.2)
            assert p.poll() is None, "worker exited before output consumed"
            for name in ("stdout", "stderr"):
                handle = fds.pop(name)
                t = threading.Thread(
                    target=lambda n, h: results.setdefault(n, read_all(h)),
                    args=(name, handle), daemon=True)
                t.start()
                threads[name] = t
        for name, t in threads.items():
            t.join(timeout=15)
            assert not t.is_alive(), "output channel did not reach EOF: " + name
        status_data = read_all(fds.pop("exit-code"))
        match = re.fullmatch(rb"EXIT ([0-9]+)\n", status_data)
        assert match, ("malformed exit status", status_data)
        native_exit = int(match.group(1))
        p.wait(timeout=5)
        assert p.returncode == native_exit, (p.returncode, native_exit)
        return results["stdout"], results["stderr"], native_exit
    finally:
        for handle in fds.values():
            K.CloseHandle(handle)
        if p.poll() is None:
            p.kill()
        p.communicate(timeout=5)


def main():
    if sys.platform != "win32":
        raise RuntimeError("Windows native test only")
    root = Path.cwd()
    git = (root / "git.exe").resolve(strict=True)
    blob = bytes(range(256)) * 2048
    recorded = subprocess.run([str(git), "hash-object", "-w", "--stdin"],
                              input=blob, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    object_id = recorded.stdout.decode("ascii").strip()
    assert re.fullmatch("[0-9a-f]{40,64}", object_id)
    cases = [
        (["--version"], False),
        (["help", "-a"], False),
        (["-c", "user.name=Δ→猫", "config", "user.name"], False),
        (["config", "--get", "git809.preinit.missing"], False),
        (["rev-parse", "--verify", "refs/heads/git809-preinit-missing"], False),
        (["cat-file", "blob", object_id], True),
    ]
    for argv, slow in cases:
        direct = subprocess.run([str(git), *argv], cwd=root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=15)
        output, errors, rc = pipe_call(git, argv, slow_reader=slow)
        assert (output, errors, rc) == (
            direct.stdout, direct.stderr, direct.returncode), (
            argv, len(output), len(direct.stdout),
            len(errors), len(direct.stderr), rc, direct.returncode)
        if slow:
            assert output == blob
        print("PREINIT_PROTOCOL_PASS " + repr(argv) +
              " bytes=" + str(len(output)) + " rc=" + str(rc), flush=True)


if __name__ == "__main__":
    main()
