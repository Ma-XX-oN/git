#!/usr/bin/env python3
"""Issue 809: minimal shell-owned Windows preinitialized Git worker pool.

No request dispatch and no pool-resize control channel in this iteration.
Starts N genuine source-built Git workers at their wmain rendezvous, and
checks its owning native shell process handle every ten seconds.
"""
from __future__ import annotations
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

if os.name != "nt":
  raise RuntimeError("Windows native Git worker pool requires Windows")

K = C.WinDLL("kernel32", use_last_error=True)
K.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
K.OpenProcess.restype = W.HANDLE
K.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
K.WaitForSingleObject.restype = W.DWORD
K.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID,
                          W.DWORD, W.DWORD, W.HANDLE]
K.CreateFileW.restype = W.HANDLE
K.CloseHandle.argtypes = [W.HANDLE]
K.CloseHandle.restype = W.BOOL
PROCESS_SYNCHRONIZE = 0x00100000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID = C.c_void_p(-1).value
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
POLL_SECONDS = 10

def connect_preinitialized(child: subprocess.Popen, token: str):
  name = "\\\\.\\pipe\\git809-worker-" + token
  deadline = time.monotonic() + 10
  while time.monotonic() < deadline:
    handle = K.CreateFileW(name, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if handle != INVALID:
      return handle
    if child.poll() is not None:
      raise RuntimeError("Git worker exited before IPC rendezvous: "
                         + str(child.returncode))
    time.sleep(0.01)
  raise RuntimeError("Git worker IPC rendezvous timeout")

def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("--git", required=True, type=Path)
  parser.add_argument("--workers", required=True, type=int)
  parser.add_argument("--ready-file", required=True, type=Path)
  args = parser.parse_args()
  if not 1 <= args.workers <= 32:
    parser.error("workers must be between 1 and 32")
  git = args.git.resolve(strict=True)
  if not git.is_file():
    parser.error("git executable is not a file")
  # Native Python is launched directly by the sourced Git Bash process.
  # Retaining the Windows handle prevents PID reuse from changing ownership.
  owner_pid = os.getppid()
  owner = K.OpenProcess(PROCESS_SYNCHRONIZE, False, owner_pid)
  if not owner:
    raise OSError(C.get_last_error(), "OpenProcess(owner)")
  if K.WaitForSingleObject(owner, 0) != WAIT_TIMEOUT:
    raise RuntimeError("owning Bash process is not alive")
  children = []
  handles = []
  try:
    for _ in range(args.workers):
      token = uuid.uuid4().hex[:16]  # Worker endpoint identifier, not a shell ID.
      child = subprocess.Popen([str(git), "--git809-worker", token],
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
      children.append(child)
      handles.append(connect_preinitialized(child, token))
    record = {
      "manager_pid": os.getpid(),
      "owner_winpid": owner_pid,
      "worker_pids": [p.pid for p in children],
      "worker_count": args.workers,
      "poll_seconds": POLL_SECONDS,
      "state": "READY",
    }
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    temp = args.ready_file.with_name(args.ready_file.name + ".tmp")
    temp.write_text(json.dumps(record) + "\n", encoding="utf-8")
    temp.replace(args.ready_file)
    print("READY " + json.dumps(record), flush=True)
    while K.WaitForSingleObject(owner, POLL_SECONDS * 1000) == WAIT_TIMEOUT:
      if any(p.poll() is not None for p in children):
        raise RuntimeError("preinitialized worker unexpectedly exited")
    print("OWNER_EXIT " + str(owner_pid), flush=True)
    return 0
  finally:
    try:
      args.ready_file.unlink(missing_ok=True)
    except OSError:
      pass
    for handle in handles:
      K.CloseHandle(handle)
    for child in children:
      if child.poll() is None:
        child.terminate()
    for child in children:
      try:
        child.wait(timeout=3)
      except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=3)
    K.CloseHandle(owner)

if __name__ == "__main__":
  sys.exit(main())
