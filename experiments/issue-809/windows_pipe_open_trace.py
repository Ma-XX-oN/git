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
from pathlib import Path
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
  parser.add_argument("--strace", help="Windows Cygwin/MSYS strace.exe for runtime event logging")
  parser.add_argument("--detours-launcher", help="Detours injector exe")
  parser.add_argument("--detours-dll", help="Detours Nt API hook DLL")
  parser.add_argument("--expect", choices=("any", "success", "failure"), default="any")
  parser.add_argument("--name-kind", choices=("matched", "unmatched", "malformed"), default="matched")
  args = parser.parse_args()
  prefix = "git809-unmatched-" if args.name_kind == "unmatched" else "git809-redirect-"
  identifier = uuid.uuid4().hex
  if args.name_kind == "malformed":
    identifier = "g" + identifier[1:]
  pipe_name = "\\\\.\\pipe\\" + prefix + identifier
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
      command = [args.bash, "--noprofile", "--norc", "-c",
                 code, "git809-probe", pipe_name, args.builtin_dll or ""]
      trace_path = None
      if args.strace:
        trace_path = Path(os.environ.get("RUNNER_TEMP", os.getcwd())) / (
          f"git809-runtime-{args.direction}-{args.name_kind}-{identifier}.strace")
        command = [args.strace, "-m", "debug+syscall+flush+inherit",
                   "-o", str(trace_path)] + command
        record(f"TRACE_FILE={trace_path}")
      injected_log = None
      env = os.environ.copy()
      if args.detours_launcher:
        if not args.detours_dll or not os.path.isfile(args.detours_dll):
          parser.error("--detours-launcher requires existing --detours-dll")
        injected_log = Path(os.environ.get("RUNNER_TEMP", os.getcwd())) / (
          f"git809-detours-{args.direction}-{args.name_kind}-{identifier}.log")
        env["GIT809_PIPE_LEAF"] = prefix + identifier
        env["GIT809_DETOURS_LOG"] = str(injected_log)
        # MSYS argument conversion under injected CreateProcess can rewrite
        # native backslashes. Export the exact Unicode pipe path directly.
        env["GIT809_PIPE_PATH"] = pipe_name
        command[4] = code.replace('"$1"', '"$GIT809_PIPE_PATH"')
        command = [args.detours_launcher, args.detours_dll] + command
        record(f"DETOURS_LOG={injected_log} leaf={prefix + identifier}")
      child = subprocess.run(command, capture_output=True, timeout=15,
                             check=False, env=env)
      if injected_log:
        if not injected_log.exists():
          raise RuntimeError(f"No Detours native log produced: {injected_log}; stderr={child.stderr!r}")
        native_lines = injected_log.read_text(encoding="utf-8",errors="replace").splitlines()
        record(f"DETOURS_NATIVE_COUNT={len(native_lines)} trace_file={injected_log}")
        for line in native_lines[-100:]:
          record(f"NATIVE {line}")
        if not any("op=HOOK_READY" in x for x in native_lines):
          raise RuntimeError(f"Detours did not confirm hook installation: {injected_log}")
      if trace_path:
        if not trace_path.is_file():
          raise RuntimeError(f"Runtime trace not produced: {trace_path}")
        runtime_lines = [
          line for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
          if "GIT809_META" in line
        ]
        record(f"RUNTIME_MARKER_COUNT={len(runtime_lines)} trace_file={trace_path}")
        for line in runtime_lines[-100:]:
          record(f"RUNTIME {line}")
        if not runtime_lines:
          raise RuntimeError(f"No instrumented runtime events captured: {trace_path}")
      # Capture the instrumented runtime's exact branch markers in the same
      # artifact as the native API events; keep the original stderr intact.
      runtime_markers = [line for line in
        child.stderr.decode("utf-8", "strict").splitlines()
        if line.startswith("GIT809_PATH ")]
      if injected_log:
        with injected_log.open("a", encoding="utf-8", newline="\n") as log:
          for marker in runtime_markers:
            log.write(marker + "\n")
        record(f"MSYS_PATH_MARKERS count={len(runtime_markers)}")
        for marker in runtime_markers:
          record(f"MSYS {marker}")
        if not runtime_markers:
          raise RuntimeError("No MSYS control-flow markers were emitted; the bypass decision was not observed")
      record(f"BASH_DONE rc={child.returncode} stdout={child.stdout!r} stderr={child.stderr!r}")
      if args.expect == "success":
        if child.returncode != 0:
          raise RuntimeError(f"Bash redirection expected success, rc={child.returncode}")
        if args.direction == "read":
          possible = [f"CLIENT_DATA=INSTANCE_{i}\n".encode("ascii") for i in range(args.instances)]
          if child.stdout not in possible:
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
  if args.detours_launcher and connected != args.instances:
    raise RuntimeError(
      f"Detours diagnostic invalid: {connected} server connections; expected {args.instances}")
  if args.expect == "success" and connected != args.instances:
    raise RuntimeError(f"Successful command consumed {connected} connections, expected {args.instances}")
  if args.expect == "success" and args.direction == "write" and not any(
    "read_success=1 bytes=13 data=b'CLIENT_WRITE\\n'" in line for line in logs
  ):
    raise RuntimeError("Native server did not receive expected write payload")
  # A probe is diagnostic, not a pass/fail assertion: each combination
  # is expected to expose its actual behavior, including timeouts.
  return 0


if __name__ == "__main__":
  sys.exit(main())
