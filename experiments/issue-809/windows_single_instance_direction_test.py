#!/usr/bin/env python3
"""Native Windows Git Bash single-instance unidirectional pipe acceptance."""
import ctypes as C
from ctypes import wintypes as W
import json
import os
import subprocess
import threading
import uuid

if os.name != "nt":
  raise RuntimeError("Windows only")
k = C.WinDLL("kernel32", use_last_error=True)
k.CreateNamedPipeW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.LPVOID]
k.CreateNamedPipeW.restype = W.HANDLE
k.ConnectNamedPipe.argtypes = [W.HANDLE, W.LPVOID]
k.ConnectNamedPipe.restype = W.BOOL
k.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID]
k.ReadFile.restype = W.BOOL
k.WriteFile.argtypes = [W.HANDLE, W.LPCVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID]
k.WriteFile.restype = W.BOOL
k.CloseHandle.argtypes = [W.HANDLE]
k.CloseHandle.restype = W.BOOL
INVALID = C.c_void_p(-1).value
PAYLOAD = b"ONE_INSTANCE_OK\\n"

def run_case(bash, mode):
  assert mode in ("read", "append")
  # Bash reads from server OUTBOUND; Bash appends to server INBOUND.
  access = 2 if mode == "read" else 1
  path = r"\\\\.\\pipe\\git809-one-instance-" + uuid.uuid4().hex
  handle = k.CreateNamedPipeW(path, access, 0, 1, 65536, 65536, 5000, None)
  if handle == INVALID:
    raise OSError(C.get_last_error(), "CreateNamedPipeW")
  record = {"mode": mode, "instances": 1, "connections": 0, "server_bytes": "", "errors": []}
  def serve():
    try:
      connected = k.ConnectNamedPipe(handle, None)
      error = C.get_last_error()
      if not connected and error != 535:
        record["errors"].append("connect:" + str(error))
        return
      record["connections"] += 1
      n = W.DWORD()
      if mode == "read":
        if not k.WriteFile(handle, PAYLOAD, len(PAYLOAD), C.byref(n), None):
          record["errors"].append("write:" + str(C.get_last_error()))
        else:
          record["server_bytes"] = PAYLOAD[:n.value].hex()
      else:
        buf = C.create_string_buffer(256)
        if not k.ReadFile(handle, buf, 256, C.byref(n), None):
          record["errors"].append("read:" + str(C.get_last_error()))
        else:
          record["server_bytes"] = buf.raw[:n.value].hex()
    finally:
      k.CloseHandle(handle)
  worker = threading.Thread(target=serve, daemon=True)
  worker.start()
  script = ('exec 3<"$1"; IFS= read -r result <&3; [[ $result == ONE_INSTANCE_OK ]]'
    if mode == "read" else
    'exec 3>>"$1"; printf "ONE_INSTANCE_OK\\n" >&3')
  try:
    p = subprocess.run([bash, "--noprofile", "--norc", "-c", script, "bench", path],
                       capture_output=True, timeout=8, check=False)
    record["exit"] = p.returncode
    record["stderr"] = p.stderr.decode("utf-8", "replace")[-500:]
  except subprocess.TimeoutExpired:
    record["exit"] = "TIMEOUT"
  worker.join(timeout=2)
  record["thread_alive"] = worker.is_alive()
  record["pass"] = (record["exit"] == 0 and record["connections"] == 1
    and record["server_bytes"] == PAYLOAD.hex() and not record["errors"] and not worker.is_alive())
  print("ONE_INSTANCE_RESULT " + json.dumps(record), flush=True)
  return record

def main():
  bash = os.environ["GIT809_BASH"]
  cases = [run_case(bash, x) for x in ("read", "append")]
  if not all(x["pass"] for x in cases):
    raise AssertionError("Single-instance unidirectional acceptance failed")

if __name__ == "__main__":
  main()
