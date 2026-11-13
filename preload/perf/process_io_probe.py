#!/usr/bin/env python3
"""Compare native Git latency, process CPU/I/O, and warmed filesystem operations.

Diagnostic only. Run the same source-built Git and minimal C control on each
native runner. Windows I/O counters are logical process I/O (including cached
and non-disk I/O), NOT counts of physical disk transfers. POSIX rusage block
I/O counters are not equivalent to Windows I/O counters.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

if os.name == "nt":
  import ctypes
  from ctypes import wintypes

  class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
      (name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")
    ]

  K = ctypes.WinDLL("kernel32", use_last_error=True)
  K.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO_COUNTERS)]
  K.GetProcessIoCounters.restype = wintypes.BOOL
  K.GetProcessTimes.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME)]
  K.GetProcessTimes.restype = wintypes.BOOL
else:
  import resource


def percentile(rows, proportion):
  values = sorted(rows)
  return values[math.ceil(len(values) * proportion) - 1]


def win_process_metrics(child):
  data = IO_COUNTERS()
  handle = wintypes.HANDLE(int(child._handle))
  if not K.GetProcessIoCounters(handle, ctypes.byref(data)):
    raise OSError(ctypes.get_last_error(), "GetProcessIoCounters failed")
  creation = wintypes.FILETIME()
  exit_time = wintypes.FILETIME()
  kernel = wintypes.FILETIME()
  user = wintypes.FILETIME()
  if not K.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                           ctypes.byref(kernel), ctypes.byref(user)):
    raise OSError(ctypes.get_last_error(), "GetProcessTimes failed")

  def to_ms(x):
    return (x.dwHighDateTime << 32 | x.dwLowDateTime) / 10000

  return {
    "user_cpu_ms": to_ms(user),
    "kernel_cpu_ms": to_ms(kernel),
    "read_operations": data.ReadOperationCount,
    "write_operations": data.WriteOperationCount,
    "read_bytes_logical": data.ReadTransferCount,
    "write_bytes_logical": data.WriteTransferCount,
  }


def posix_usage_delta(before, after):
  return {
    "user_cpu_ms": (after.ru_utime - before.ru_utime) * 1000,
    "kernel_cpu_ms": (after.ru_stime - before.ru_stime) * 1000,
    "block_input": after.ru_inblock - before.ru_inblock,
    "block_output": after.ru_oublock - before.ru_oublock,
    "minor_faults": after.ru_minflt - before.ru_minflt,
    "major_faults": after.ru_majflt - before.ru_majflt,
  }


def measure(command, env, cwd=None, expect=None):
  before = None if os.name == "nt" else resource.getrusage(resource.RUSAGE_CHILDREN)
  start = time.perf_counter_ns()
  child = subprocess.Popen(
    command, cwd=cwd, env=env,
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  created = time.perf_counter_ns()
  try:
    stdout, stderr = child.communicate(timeout=20)
  except subprocess.TimeoutExpired:
    child.kill()
    child.communicate()
    raise
  finished = time.perf_counter_ns()
  if child.returncode != 0:
    raise AssertionError(f"exit={child.returncode} cmd={command!r} stderr={stderr!r}")
  if expect is not None and expect not in stdout:
    raise AssertionError(f"unexpected stdout for {command!r}: {stdout!r}")
  metrics = (win_process_metrics(child) if os.name == "nt"
             else posix_usage_delta(before, resource.getrusage(resource.RUSAGE_CHILDREN)))
  return {
    "total_ms": (finished - start) / 1e6,
    "popen_return_ms": (created - start) / 1e6,
    "after_popen_ms": (finished - created) / 1e6,
    **metrics,
  }


def make_repo(git, location, env, count):
  location.mkdir(parents=True)
  commands = [
    [git, "init", "-q", "-b", "benchmark"],
    [git, "config", "user.name", "Profiling"],
    [git, "config", "user.email", "profiling@example.invalid"],
  ]
  for command in commands:
    subprocess.run(command, cwd=location, env=env, capture_output=True, check=True)
  files = []
  for index in range(count):
    path = location / ("fixture-%04d.txt" % index)
    path.write_bytes(("fixture %d\n" % index).encode("ascii") * 20)
    files.append(path)
  subprocess.run([git, "add", "."], cwd=location, env=env, check=True, capture_output=True)
  subprocess.run([git, "commit", "-qm", "baseline"], cwd=location, env=env,
                 check=True, capture_output=True)
  return files


def fs_probe(files, action):
  started = time.perf_counter_ns()
  total = 0
  for path in files:
    if action == "stat":
      total += path.stat().st_size
    elif action == "read":
      total += len(path.read_bytes())
    else:
      raise AssertionError(action)
  elapsed = time.perf_counter_ns() - started
  if total <= 0:
    raise AssertionError("filesystem probe did not read fixture metadata")
  return elapsed / 1e6


def summary(samples, metrics):
  result = {}
  for name in metrics:
    vals = [s[name] for s in samples]
    result[name] = {
      "median": statistics.median(vals),
      "p95": percentile(vals, .95),
      "mean": statistics.mean(vals),
    }
  return result


def main():
  p = argparse.ArgumentParser(description=__doc__)
  p.add_argument("--git", type=Path, required=True)
  p.add_argument("--control", type=Path, required=True)
  p.add_argument("--count", type=int, default=120)
  p.add_argument("--output", type=Path, required=True)
  args = p.parse_args()
  if not 5 <= args.count <= 500:
    p.error("--count must be 5..500")
  git = args.git.resolve(strict=True)
  control = args.control.resolve(strict=True)
  output = args.output.resolve()
  output.mkdir(parents=True, exist_ok=True)
  env = {
    k: v for k, v in os.environ.items()
    if not k.startswith(("PRELOAD_", "GIT_TRACE", "GIT809_"))
  }
  with tempfile.TemporaryDirectory(prefix="preload-process-io-") as tmp:
    root = Path(tmp) / "fixture"
    files = make_repo(str(git), root, env, 100)
    cases = {
      "control": ([str(control)], None),
      "git_version": ([str(git), "--version"], b"git version"),
      "git_rev_parse": ([str(git), "rev-parse", "--verify", "HEAD"], None),
      "git_status_clean": ([str(git), "status", "--porcelain"], None),
    }
    # The empty tracked clean-status response is asserted separately.
    for case, (cmd, expect) in cases.items():
      for _ in range(5):
        record = measure(cmd, env, cwd=root, expect=expect)
    samples = {name: [] for name in cases}
    metadata = {"stat": [], "read": []}
    names = list(cases)
    for index in range(args.count):
      order = names if index % 2 == 0 else list(reversed(names))
      for name in order:
        cmd, expect = cases[name]
        samples[name].append(measure(cmd, env, cwd=root, expect=expect))
      for action in ("stat", "read"):
        metadata[action].append(fs_probe(files, action))
    result = {
      "schema": 1,
      "source_sha": os.environ.get("GITHUB_SHA"),
      "platform": platform.platform(),
      "cpu": platform.processor(),
      "python": sys.version,
      "git_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
      "control_sha256": hashlib.sha256(control.read_bytes()).hexdigest(),
      "sample_count": args.count,
      "fixture": {"tracked_files": len(files), "location": str(root)},
      "semantics": {
        "Windows": "logical process IO counters: not physical disk reads",
        "POSIX": "resource RUSAGE_CHILDREN cumulative block IO and process CPU",
        "warm_cache": "All timed loops run after setup and warmup; cold-cache not established",
        "timing": "Inclusive wall-clock time, parent-side perf_counter_ns",
      },
      "cases": {
        name: {
          "summary": summary(values, values[0]),
          "samples": values,
        } for name, values in samples.items()
      },
      "filesystem": {
        action: {"total_100_files_ms": summary(
          [{"total_100_files_ms": value} for value in values],
          ("total_100_files_ms",))["total_100_files_ms"],
          "samples_ms": values}
        for action, values in metadata.items()
      },
    }
  (output / "process-io.json").write_text(
    json.dumps(result, indent=2) + "\n", encoding="utf-8")
  for name, data in result["cases"].items():
    avg = data["summary"]["total_ms"]
    cpu = data["summary"]["user_cpu_ms"]["median"] + data["summary"]["kernel_cpu_ms"]["median"]
    more = ""
    if os.name == "nt":
      more = " logical_read_ops_median=%.1f" % data["summary"]["read_operations"]["median"]
    print("PROCESS_IO", name, "total_ms_median=%.3f" % avg["median"],
          "p95=%.3f" % avg["p95"], "child_cpu_ms_median=%.3f" % cpu,
          more, flush=True)
  for name, data in result["filesystem"].items():
    print("FILESYSTEM_IO", name, "100_files_ms_median=%.3f" %
          data["total_100_files_ms"]["median"], flush=True)


if __name__ == "__main__":
  main()
