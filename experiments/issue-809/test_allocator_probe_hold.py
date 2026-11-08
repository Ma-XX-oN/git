#!/usr/bin/env python3
"""Regression: allocation GET must work while the unused probe stays connected.

The original server waited for one named-pipe instance to disconnect and
failed with states=(4,4). Both connections intentionally remain open here.
"""
import ctypes as C
from ctypes import wintypes as W
import os
import sys
import threading

assert os.name == "nt", "requires native Windows"
assert len(sys.argv) == 2 and sys.argv[1].isdigit(), "expected owner WinPID"
K = C.WinDLL("kernel32", use_last_error=True)
K.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID,
                          W.DWORD, W.DWORD, W.HANDLE]
K.CreateFileW.restype = W.HANDLE
K.WaitNamedPipeW.argtypes = [W.LPCWSTR, W.DWORD]
K.WaitNamedPipeW.restype = W.BOOL
K.WriteFile.argtypes = [W.HANDLE, W.LPCVOID, W.DWORD,
                        C.POINTER(W.DWORD), W.LPVOID]
K.WriteFile.restype = W.BOOL
K.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD,
                       C.POINTER(W.DWORD), W.LPVOID]
K.ReadFile.restype = W.BOOL
K.CloseHandle.argtypes = [W.HANDLE]
K.CloseHandle.restype = W.BOOL
INVALID = C.c_void_p(-1).value
address = r"\\.\pipe\git809-allocate-req-" + sys.argv[1]

def timeout():
    print("GIT809_PROBE_HOLD_TIMEOUT", flush=True)
    os._exit(83)

watchdog = threading.Timer(12, timeout)
watchdog.daemon = True
watchdog.start()
handles = []
try:
    for index in range(2):
        if not K.WaitNamedPipeW(address, 5000):
            raise OSError(C.get_last_error(), "WaitNamedPipe")
        h = K.CreateFileW(address, 0xc0000000, 0, None, 3, 0, None)
        if h == INVALID:
            raise OSError(C.get_last_error(), "CreateFileW")
        handles.append(h)
    # The first pipe stays connected as a probe. The second sends GET.
    # The ID is delivered on a distinct, digit-qualified logical channel.
    client_id = f"{os.getpid()}-1"
    request = (f"1GET {client_id}\n").encode("ascii")
    n = W.DWORD()
    if not K.WriteFile(handles[1], request, len(request), C.byref(n), None):
        raise OSError(C.get_last_error(), "WriteFile(request)")
    assert n.value == len(request)
    reply = (r"\\.\pipe\git809-allocate-reply-" + sys.argv[1] +
             "-" + str(client_id))
    for attempt in range(500):
        if K.WaitNamedPipeW(reply, 10):
            response_handle = K.CreateFileW(reply, 0xc0000000, 0, None, 3, 0, None)
            if response_handle != INVALID:
                handles.append(response_handle)
                break
        import time
        time.sleep(.002)
    else:
        raise TimeoutError("separate reply unavailable")
    if not K.WriteFile(response_handle, b"1", 1, C.byref(n), None):
        raise OSError(C.get_last_error(), "WriteFile(reply marker)")
    data = C.create_string_buffer(64)
    if not K.ReadFile(response_handle, data, 64, C.byref(n), None):
        raise OSError(C.get_last_error(), "ReadFile(reply)")
    response = data.raw[:n.value]
    assert response.endswith(b"\n") and response[:-1].isdigit(), response
    print("GIT809_PROBE_HOLD_PASS allocated_pid", response.decode().strip(),
          flush=True)
finally:
    watchdog.cancel()
    for h in handles:
        K.CloseHandle(h)
