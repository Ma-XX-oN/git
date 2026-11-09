#!/usr/bin/env python3
"""Preload Windows-only, read-only process-tree minidumps before timeout kill.

Captures thread/handle metadata but deliberately excludes full process memory.
No production process, process injection, or external debugger is required.
"""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import sys

if os.name != "nt":
  raise RuntimeError("Native Windows required")

class PROCESSENTRY32W(c.Structure):
  _fields_ = [
    ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
    ("th32DefaultHeapID", c.c_size_t), ("th32ModuleID", w.DWORD),
    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
    ("pcPriClassBase", c.c_long), ("dwFlags", w.DWORD),
    ("szExeFile", w.WCHAR * 260),
  ]

kernel = c.WinDLL("kernel32", use_last_error=True)
dbghelp = c.WinDLL("Dbghelp", use_last_error=True)
kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
kernel.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(PROCESSENTRY32W)]
kernel.Process32FirstW.restype = w.BOOL
kernel.Process32NextW.argtypes = [w.HANDLE, c.POINTER(PROCESSENTRY32W)]
kernel.Process32NextW.restype = w.BOOL
kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel.OpenProcess.restype = w.HANDLE
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.CloseHandle.restype = w.BOOL
dbghelp.MiniDumpWriteDump.argtypes = [
  w.HANDLE, w.DWORD, w.HANDLE, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p
]
dbghelp.MiniDumpWriteDump.restype = w.BOOL

def capture(root_pid: int, folder: Path) -> list[dict]:
  """Capture native process dumps before terminating a timed-out fixture."""
  if not (0 < root_pid < 0xFFFFFFFF):
    raise ValueError("invalid root PID")
  folder.mkdir(parents=True, exist_ok=True)
  snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
  if snapshot == c.c_void_p(-1).value:
    raise OSError(c.get_last_error(), "CreateToolhelp32Snapshot")
  entries = {}
  try:
    e = PROCESSENTRY32W()
    e.dwSize = c.sizeof(e)
    ok = kernel.Process32FirstW(snapshot, c.byref(e))
    while ok:
      entries[e.th32ProcessID] = (e.th32ParentProcessID, str(e.szExeFile))
      ok = kernel.Process32NextW(snapshot, c.byref(e))
  finally:
    kernel.CloseHandle(snapshot)
  selected = {root_pid}
  while True:
    next_set = selected | {pid for pid, (parent, _) in entries.items()
                           if parent in selected}
    if selected == next_set:
      break
    selected = next_set
  results = []
  for pid in sorted(selected):
    name = entries.get(pid, (0, "unknown"))[1]
    entry = {"pid": pid, "parent": entries.get(pid, (0, ""))[0], "image": name}
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
      entry["error"] = f"OpenProcess {c.get_last_error()}"
    else:
      try:
        path = folder / f"git-preload-pid-{pid}.dmp"
        import msvcrt
        with path.open("wb") as out:
          native_file = msvcrt.get_osfhandle(out.fileno())
          # MiniDumpWithThreadInfo | WithHandleData | WithUnloadedModules.
          if dbghelp.MiniDumpWriteDump(handle, pid, native_file,
                                       0x1000 | 0x4 | 0x20,
                                       None, None, None):
            entry["dump"] = str(path)
            entry["bytes"] = path.stat().st_size
          else:
            entry["error"] = f"MiniDumpWriteDump {c.get_last_error()}"
      finally:
        kernel.CloseHandle(handle)
    results.append(entry)
  (folder / "process-tree.json").write_text(
    json.dumps(results, indent=2) + "\n", encoding="utf-8")
  for entry in results:
    print("GIT_PRELOAD_DUMP " + json.dumps(entry), flush=True)
  return results

if __name__ == "__main__":
  if len(sys.argv) != 3:
    raise SystemExit("usage: windows_ipc_timeout_dumps.py PID DIRECTORY")
  capture(int(sys.argv[1]), Path(sys.argv[2]))
