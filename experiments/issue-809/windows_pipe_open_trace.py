#!/usr/bin/env python3
"""Issue 809: trace Git Bash redirection against raw Windows named pipes.

Each test case runs in a fresh Python process, so blocked native pipe server
threads never survive into another case. No Git worker or Python data relay.
"""
from __future__ import annotations
import argparse
import ctypes as C
from ctypes import wintypes as W
import os
import subprocess
import sys
import threading
import time
import uuid

if os.name != "nt":
  raise SystemExit("Windows native Python is required")

K = C.WinDLL("kernel32", use_last_error=True)
K.CreateNamedPipeW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD,
                               W.DWORD, W.DWORD, W.DWORD, W.LPVOID]
K.CreateNamedPipeW.restype = W.HANDLE
K.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
K.CreateFileW.restype = W.HANDLE
K.ConnectNamedPipe.argtypes = [W.HANDLE, W.LPVOID]
K.ConnectNamedPipe.restype = W.BOOL
K.WriteFile.argtypes = [W.HANDLE, W.LPCVOID, W.DWORD,
                        C.POINTER(W.DWORD), W.LPVOID]
K.WriteFile.restype = W.BOOL
K.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD,
                       C.POINTER(W.DWORD), W.LPVOID]
K.ReadFile.restype = W.BOOL
K.FlushFileBuffers.argtypes = [W.HANDLE]
K.FlushFileBuffers.restype = W.BOOL
K.DisconnectNamedPipe.argtypes = [W.HANDLE]
K.DisconnectNamedPipe.restype = W.BOOL
K.CloseHandle.argtypes = [W.HANDLE]
K.CloseHandle.restype = W.BOOL
K.GetNamedPipeClientProcessId.argtypes = [W.HANDLE, C.POINTER(W.ULONG)]
K.GetNamedPipeClientProcessId.restype = W.BOOL

INVALID = C.c_void_p(-1).value
PIPE_ACCESS_INBOUND = 1
PIPE_ACCESS_OUTBOUND = 2
PIPE_TYPE_BYTE = 0
PIPE_WAIT = 0
ERROR_PIPE_CONNECTED = 535


def main() -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--instances", required=True, type=int, choices=(1, 2))
  parser.add_argument("--direction", required=True, choices=("read", "write"))
  parser.add_argument("--bash", required=True)
  parser.add_argument("--client", choices=("bash", "native", "attached"), default="bash")
  parser.add_argument("--builtin-dll")
  parser.add_argument("--expect", choices=("any", "success", "failure"), default="any")
  parser.add_argument("--name-kind", choices=("matched", "unmatched"), default="matched")
  args = parser.parse_args()
  prefix = "git809-redirect-" if args.name_kind == "matched" else "git809-unmatched-"
  pipe_name = "\\\\.\\pipe\\" + prefix + uuid.uuid4().hex
  handles = []
  logs = []
  lock = threading.Lock()
  def record(message: str) -> None:
    with lock:
      line = f"PIPE_TRACE time_ns={time.time_ns()} {message}"
      logs.append(line)
      print(line, flush=True)

  for index in range(args.instances):
    handle = K.CreateNamedPipeW(
      pipe_name,
      PIPE_ACCESS_OUTBOUND if args.direction == "read" else PIPE_ACCESS_INBOUND,
      PIPE_TYPE_BYTE | PIPE_WAIT, args.instances, 4096, 4096, 5000, None)
    if handle == INVALID:
      raise OSError(C.get_last_error(), f"CreateNamedPipe instance {index}")
    handles.append(handle)

  def serve(index: int, handle: int) -> None:
    connected = K.ConnectNamedPipe(handle, None)
    error = C.get_last_error() if not connected else 0
    if not connected and error != ERROR_PIPE_CONNECTED:
      record(f"instance={index} connect_error={error}")
      return
    pid = W.ULONG()
    K.GetNamedPipeClientProcessId(handle, C.byref(pid))
    record(f"instance={index} CONNECT client_pid={pid.value}")
    n = W.DWORD()
    if args.direction == "read":
      payload = f"INSTANCE_{index}\n".encode("ascii")
      buf = C.create_string_buffer(payload)
      success = K.WriteFile(handle, buf, len(payload), C.byref(n), None)
      record(f"instance={index} write_success={int(success)} bytes={n.value} error={C.get_last_error() if not success else 0}")
      if success:
        flushed = K.FlushFileBuffers(handle)
        record(f"instance={index} flushed={int(flushed)} flush_error={C.get_last_error() if not flushed else 0}")
    else:
      buf = C.create_string_buffer(256)
      success = K.ReadFile(handle, buf, 256, C.byref(n), None)
      record(f"instance={index} read_success={int(success)} bytes={n.value} data={buf.raw[:n.value]!r} error={C.get_last_error() if not success else 0}")
    K.DisconnectNamedPipe(handle)
    record(f"instance={index} DISCONNECT")

  threads = [threading.Thread(target=serve, args=(i, h), daemon=True)
             for i, h in enumerate(handles)]
  for thread in threads:
    thread.start()

  if args.direction == "read":
    code = 'exec 9<"$1" || exit 81; IFS= read -r line <&9 || exit 82; printf "CLIENT_DATA=%s\\n" "$line"; exec 9<&-'
  else:
    code = 'exec 9>>"$1" || exit 81; printf "CLIENT_WRITE\\n" >&9 || exit 82; exec 9>&-; printf "CLIENT_DONE\\n"'
  if args.client == "attached":
    if not args.builtin_dll or not os.path.isfile(args.builtin_dll):
      parser.error("attached client needs an existing --builtin-dll")
    setup = ('enable -f "$(cygpath -u "$2")" git809attach || exit 80; '
             'fake="/tmp/git809-attach-metadata-probe"; : > "$fake" || exit 79; '
             f'git809attach "$1" "$fake" "{args.direction}" || exit 78; ')
    if args.direction == "read":
      code = setup + 'IFS= read -r line <&9 || exit 82; printf "CLIENT_DATA=%s\\n" "$line"; exec 9<&-'
    else:
      code = setup + 'printf "CLIENT_WRITE\\n" >&9 || exit 82; exec 9>&-; printf "CLIENT_DONE\\n"'
  record(f"START direction={args.direction} instances={args.instances} client={args.client} pipe={pipe_name}")
  if args.client == "native":
    access = 0x80000000 if args.direction == "read" else 0x40000000
    native = K.CreateFileW(pipe_name, access, 0, None, 3, 0, None)
    if native == INVALID:
      record(f"NATIVE_OPEN_FAILED error={C.get_last_error()}")
    else:
      n = W.DWORD()
      if args.direction == "read":
        buf = C.create_string_buffer(256)
        success = K.ReadFile(native, buf, 256, C.byref(n), None)
        record(f"NATIVE_READ success={int(success)} bytes={n.value} data={buf.raw[:n.value]!r}")
      else:
        payload = C.create_string_buffer(b"CLIENT_WRITE\n")
        success = K.WriteFile(native, payload, 13, C.byref(n), None)
        record(f"NATIVE_WRITE success={int(success)} bytes={n.value}")
      K.CloseHandle(native)
  else:
    try:
      child = subprocess.run([args.bash, "--noprofile", "--norc", "-c",
                              code, "git809-probe", pipe_name, args.builtin_dll or ""],
                             capture_output=True, timeout=7, check=False)
      record(f"BASH_DONE rc={child.returncode} stdout={child.stdout!r} stderr={child.stderr!r}")
      if args.expect == "success":
        if child.returncode != 0:
          raise RuntimeError(f"Bash redirection expected success, rc={child.returncode}")
        if args.direction == "read" and child.stdout != b"CLIENT_DATA=INSTANCE_0\n":
          raise RuntimeError(f"Bash read payload mismatch: {child.stdout!r}")
        if args.direction == "write" and child.stdout != b"CLIENT_DONE\n":
          raise RuntimeError(f"Bash write completion mismatch: {child.stdout!r}")
      if args.expect == "failure" and child.returncode == 0:
        raise RuntimeError("Bash redirection unexpectedly succeeded")
    except subprocess.TimeoutExpired as exc:
      record(f"BASH_TIMEOUT stdout={exc.stdout!r} stderr={exc.stderr!r}")
      if args.expect != "any":
        raise RuntimeError("Bash redirection timed out") from exc
  for t in threads:
    t.join(timeout=0.3)
  connected = sum(' CONNECT ' in line for line in logs)
  record(f"SUMMARY connected={connected} instances={args.instances} "
         f"direction={args.direction} name_kind={args.name_kind}")
  if args.expect == "success" and args.instances == 1 and connected != 1:
    raise RuntimeError(f"Successful command consumed {connected} connections, not one")
  if args.expect == "success" and args.direction == "write" and not any(
    "read_success=1 bytes=13 data=b'CLIENT_WRITE\\n'" in line for line in logs
  ):
    raise RuntimeError("Native server did not receive expected write payload")
  # A probe is diagnostic, not a pass/fail assertion: each combination
  # is expected to expose its actual behavior, including timeouts.
  return 0


if __name__ == "__main__":
  sys.exit(main())
