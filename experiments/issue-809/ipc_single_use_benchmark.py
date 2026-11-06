#!/usr/bin/env python3
"""#809: same-host native Windows IPC vs direct Git microbenchmark.

Prototype only. Each prestarted no-op worker accepts one request and exits.
The manager replenishes the consumed worker. The native client measures
startup+IPC+reply; no Git sources are changed.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path.cwd()
OUT = ROOT / "git809-ipc-results"
N = 120
WARMUPS = 8
TIMEOUT = 30

def sha(path):
  result = hashlib.sha256()
  with path.open("rb") as stream:
    for block in iter(lambda: stream.read(65536), b""):
      result.update(block)
  return result.hexdigest()

def percentile(values, fraction):
  sorted_values = sorted(values)
  return sorted_values[int((len(sorted_values) - 1) * fraction)]

def run(argv, expected, timeout=TIMEOUT):
  start = time.perf_counter_ns()
  p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, timeout=timeout, check=False)
  ms = (time.perf_counter_ns() - start) / 1e6
  if p.returncode != 0 or p.stdout != expected or p.stderr:
    raise AssertionError(
      f"command failed: {argv[1:]} rc={p.returncode} stdout={p.stdout!r} "
      f"stderr={p.stderr!r} expected={expected!r}")
  return ms

def main():
  if os.name != "nt":
    raise RuntimeError("Windows native runner is required")
  OUT.mkdir(parents=True, exist_ok=True)
  exe = (OUT / "git809-ipc.exe").resolve()
  command = ["gcc", "-std=c11", "-O2", "-Wall", "-Wextra",
             "-Werror", "-o", str(exe),
             str(ROOT / "experiments/issue-809/ipc_single_use.c")]
  build = subprocess.run(command, check=False, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=120)
  if build.returncode:
    raise RuntimeError("native build failed: " +
                       build.stderr.decode("utf-8", "replace")[-3500:])
  run([str(exe), "selftest"], b"argv decoder PASS\r\n")
  installed = Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Git" / "ucrt64" / "bin" / "git.exe"
  if not installed.is_file():
    raise RuntimeError("direct native installed Git unavailable: " + str(installed))
  token = uuid.uuid4().hex[:16]
  managers = {}
  report = {
    "experiment": "Git809-Windows-single-use-counted-argv-worker",
    "source_sha": os.environ.get("GITHUB_SHA", ""),
    "runner_os": os.environ.get("RUNNER_OS", ""),
    "runner_image": os.environ.get("ImageOS", ""),
    "python": sys.version,
    "platform": platform.platform(),
    "samples_per_case": N,
    "warmups_per_case": WARMUPS,
    "native_client": str(exe),
    "native_client_sha256": sha(exe),
    "direct_git": str(installed),
    "direct_git_sha256": sha(installed),
    "worker_pool_size": 3,
    "protocol": {
      "pipe": "counted UTF-8 argv via wrapper->manager named pipe; manager->single-use worker named pipe; manager returns validated digest",
      "shm": "counted UTF-8 argv via named mapping + auto-reset events; manager->single-use worker named pipe",
      "baseline": "native no-op client prints ok and exits",
      "git": "direct native installed git --version",
      "argv_wire_format": "little-endian u32 argc, then exactly argc NUL-terminated UTF-8 strings (including an empty argument); maximum 32 arguments and 4096 bytes",
      "limitations": [
        "No Git command execution in IPC prototype; client sends a fixed representative argument fixture",
        "One client at a time; not a concurrency or contention acceptance test",
        "Manager replenishment runs after reply, potentially delaying the next request",
        "Shared memory only replaces client-manager channel; manager-worker remains named pipe",
        "Cannot claim pure transport latency from full-process measurements",
      ],
    },
    "measurements": {},
    "status": "started",
  }
  try:
    for mode in ("pipe", "shm"):
      args = [str(exe), "manager", mode, token + mode]
      manager = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8",
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
      managers[mode] = manager
      # Manager writes READY only after three workers connected.
      # A startup failure is bounded by the CI job timeout.
      line = manager.stdout.readline().strip()
      if line != "READY":
        raise RuntimeError(f"manager {mode} failed to become ready: {line!r}")
    version = subprocess.run([str(installed), "--version"], check=True,
                             capture_output=True, timeout=TIMEOUT).stdout
    assert version.startswith(b"git version "), version
    cases = {
      "baseline": ([str(exe), "base"], b"ok\r\n"),
      "pipe": ([str(exe), "client", "pipe", token + "pipe"], b"ok\r\n"),
      "shm": ([str(exe), "client", "shm", token + "shm"], b"ok\r\n"),
      "git": ([str(installed), "--version"], version),
    }
    values = {case: [] for case in cases}
    for i in range(WARMUPS + N):
      labels = list(cases)
      if i % 2:
        labels.reverse()
      for case in labels:
        argv, expected = cases[case]
        elapsed = run(argv, expected)
        if i >= WARMUPS:
          values[case].append(elapsed)
      if i in (WARMUPS - 1, WARMUPS + N // 2 - 1, WARMUPS + N - 1):
        print(f"benchmark round {i+1}/{WARMUPS + N} complete", flush=True)
    report["measurements"] = {
      case: {
        "n": len(values[case]),
        "median_ms": statistics.median(values[case]),
        "p95_ms": percentile(values[case], .95),
        "min_ms": min(values[case]),
        "max_ms": max(values[case]),
        "samples_ms": values[case],
      }
      for case in cases
    }
    report["status"] = "completed"
  finally:
    for manager in managers.values():
      if manager.poll() is None:
        # Kill the Windows process tree, including its idle worker children.
        subprocess.run(["taskkill", "/PID", str(manager.pid), "/T", "/F"],
                       capture_output=True, timeout=15, check=False)
      try:
        manager.communicate(timeout=5)
      except subprocess.TimeoutExpired:
        manager.kill()
    path = OUT / "single-use-worker-results.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("REPORT", str(path), flush=True)
    print("SUMMARY", json.dumps({
      "status": report["status"],
      "source_sha": report["source_sha"],
      "medians": {k: round(v["median_ms"], 4)
                  for k, v in report["measurements"].items()},
      "p95": {k: round(v["p95_ms"], 4)
              for k, v in report["measurements"].items()},
    }), flush=True)

if __name__ == "__main__":
  main()
