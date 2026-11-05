#!/usr/bin/env python3
"""Read-only Git-specific Windows process launch/CPU diagnostic (issue 809)."""
import ctypes
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

class FILETIME(ctypes.Structure):
  _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

class IO_COUNTERS(ctypes.Structure):
  _fields_ = [(name, ctypes.c_uint64) for name in (
    "read_operations", "write_operations", "other_operations",
    "read_bytes", "write_bytes", "other_bytes")]

def filetime_ms(x):
  return ((x.high << 32) | x.low) / 10000.0

def usage(handle):
  k32 = ctypes.WinDLL("kernel32", use_last_error=True)
  k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(FILETIME)] * 4
  k32.GetProcessTimes.restype = ctypes.c_int
  a, b, kernel, user = (FILETIME() for _ in range(4))
  if not k32.GetProcessTimes(handle, ctypes.byref(a), ctypes.byref(b),
                            ctypes.byref(kernel), ctypes.byref(user)):
    raise ctypes.WinError(ctypes.get_last_error())
  k32.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.POINTER(IO_COUNTERS)]
  k32.GetProcessIoCounters.restype = ctypes.c_int
  io = IO_COUNTERS()
  if not k32.GetProcessIoCounters(handle, ctypes.byref(io)):
    raise ctypes.WinError(ctypes.get_last_error())
  process_lifetime_ms = filetime_ms(b) - filetime_ms(a)
  if process_lifetime_ms < 0:
    raise RuntimeError("negative process lifetime from GetProcessTimes")
  return filetime_ms(kernel) + filetime_ms(user), process_lifetime_ms, io

def measure(args, capture):
  started = time.perf_counter_ns()
  with subprocess.Popen(args, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL) as p:
    launched = time.perf_counter_ns()
    output, _ = p.communicate(timeout=20)
    finished = time.perf_counter_ns()
    if p.returncode != 0:
      raise RuntimeError("child failed: " + str(p.returncode))
    if capture and not output.startswith(b"git version "):
      raise RuntimeError("Git version output mismatch")
    cpu, process_lifetime, io = usage(int(p._handle))
  return {
    "popen_constructor_ms": (launched - started) / 1e6,
    "wait_and_output_ms": (finished - launched) / 1e6,
    "total_wall_ms": (finished - started) / 1e6,
    "child_cpu_ms": cpu,
    "child_creation_to_exit_ms": process_lifetime,
    "child_read_operations": io.read_operations,
    "child_read_bytes": io.read_bytes,
  }

def summarize(samples):
  return {key: {"median": statistics.median(x[key] for x in samples),
                "p95": sorted(x[key] for x in samples)[int(.95 * (len(samples)-1))]}
          for key in samples[0]}

def main():
  if os.name != "nt":
    raise SystemExit("Windows-specific Git process probe")
  git = shutil.which("git")
  if not git:
    raise RuntimeError("Git not on PATH")
  from git_executable_startup import build_noop
  with tempfile.TemporaryDirectory(prefix="git809-") as folder:
    noop, result = build_noop(Path(folder))
    if noop is None or not noop.is_file():
      raise RuntimeError("native C control compile failed: " + result)
    options = {
      "minimal_c_noop": ([str(noop)], False),
      "git_version_discard": ([git, "--version"], False),
      "git_version_capture": ([git, "--version"], True),
      "python_nop": ([sys.executable, "-c", "pass"], False)
    }
    for args, capture in options.values():
      for i in range(5):
        measure(args, capture)
    samples = {name: [] for name in options}
    for i in range(120):
      names = list(options)
      if i % 2:
        names.reverse()
      for name in names:
        args, capture = options[name]
        samples[name].append(measure(args, capture))
  data = {
    "experiment": "Git-809-Windows-Popen-constructor-vs-child-run",
    "source_sha": os.environ.get("GITHUB_SHA", ""),
    "platform": platform.platform(),
    "git_binary": git,
    "n": 120,
    "medians": {k: summarize(v) for k, v in samples.items()},
    "raw_samples": samples,
    "limitations": "Popen constructor includes Python preparation + CreateProcessW; child wait includes exe/DLL initialization + teardown. GetProcessTimes creation-to-exit includes whole child process lifetime; CPU is coarse user+kernel. Not direct ETW loader timings."
  }
  target = Path("git809-results")
  target.mkdir(exist_ok=True)
  (target / "windows-process-accounting.json").write_text(
    json.dumps(data, indent=2) + "\n", encoding="utf-8")
  print(json.dumps({k:v for k,v in data.items() if k != "raw_samples"}, indent=2))

if __name__ == "__main__":
  main()
