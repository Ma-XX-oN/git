#!/usr/bin/env python3
"""RWF 809: native Windows shim phase timings on an identical Git fixture.

The instrumented Git process emits one aggregate per completed shim phase.
No phase writes a log while an individual filesystem call is being measured.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import time

from benchmark_workload import create_fixture, COMMANDS
from decode import parse, summary

PHASE = re.compile(r"^RWF809_PHASE ([A-Za-z0-9_]+) ([0-9]+) ([0-9.]+)$")
NAMES = ("grep", "status", "diff", "ls_files")
REPEAT = 5


def timed(git, fixture, name, args, trace_file=None):
  cmd = [str(git)]
  if trace_file:
    if trace_file.exists():
      raise ValueError(f"trace exists: {trace_file}")
    cmd += ["--log-fn", str(trace_file.resolve())]
  cmd += list(args)
  t0 = time.perf_counter_ns()
  p = subprocess.run(cmd, cwd=fixture, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, check=False)
  wall_ms = (time.perf_counter_ns() - t0) / 1e6
  if p.returncode:
    raise RuntimeError(f"{name} failed ({p.returncode}): {p.stderr[:2500]!r}")
  phases = {}
  unknown = []
  for line in p.stderr.decode("utf-8", "replace").splitlines():
    m = PHASE.fullmatch(line)
    if m:
      name_part, count, total_ms = m.groups()
      if name_part in phases:
        raise AssertionError(f"duplicate phase {name_part}")
      phases[name_part] = {"calls": int(count), "total_ms": float(total_ms)}
    else:
      unknown.append(line)
  if not phases:
    raise AssertionError("no Windows phase measurements emitted")
  return {
    "elapsed_ms": round(wall_ms, 6),
    "stdout_sha256": hashlib.sha256(p.stdout).hexdigest(),
    "stdout_bytes": len(p.stdout),
    "phases": phases,
    "other_stderr": unknown[:12],
  }


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("--git", type=Path, required=True)
  parser.add_argument("--out", type=Path, required=True)
  args = parser.parse_args()
  git = args.git.resolve(strict=True)
  out = args.out.resolve()
  fixture = out.parent / (out.name + "-fixture")
  if out.exists() or fixture.exists():
    raise RuntimeError("refusing to reuse existing fixture or result")
  out.mkdir(parents=True)
  dobby = Path(os.environ["GIT_IO_TRACE_DOBBY"]).resolve(strict=True)
  results = {
    "source_sha": os.environ.get("GITHUB_SHA"),
    "git_exe_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
    "dobby_sha256": hashlib.sha256(dobby.read_bytes()).hexdigest(),
    "git_build_flags": "GIT_IO_TRACE=1 GIT_MINGW_PHASE_PROFILE=1",
    "repeats_per_command": REPEAT,
    "tracked_files": 8000,
  }
  results["fixture"] = create_fixture(git, fixture, 8000)
  selected = [(name,cmd) for name,cmd in COMMANDS if name in NAMES]
  results["commands"] = {}
  for name, command in selected:
    # Prime the OS cache and Git index with the same operations being measured.
    for _ in range(3):
      timed(git, fixture, name, command)
    samples = []
    expected = None
    for index in range(REPEAT):
      logfile = out / f"{name}-trace.bin" if index == REPEAT - 1 else None
      sample = timed(git, fixture, name, command, logfile)
      fingerprint = (sample["stdout_sha256"], sample["stdout_bytes"])
      if expected is None:
        expected = fingerprint
      elif fingerprint != expected:
        raise AssertionError(f"Git output changed across {name} repeats")
      if logfile:
        decoded = summary(parse(logfile))
        sample["trace"] = {
          "process_ms": decoded["process_ms"],
          "io_union_ms": decoded["io_union_ms"],
          "by_operation": decoded["by_operation"],
          "operation_count": sum(
            entry["count"] for entry in decoded["by_operation"].values())
        }
        # Store only one trace per command; decoder enforces its consistency.
      samples.append(sample)
    phases = sorted({phase for sample in samples
                     for phase in sample["phases"]})
    aggregate = {}
    for phase in phases:
      sample_total = [s["phases"].get(phase, {}).get("total_ms", 0)
                      for s in samples]
      sample_count = [s["phases"].get(phase, {}).get("calls", 0)
                      for s in samples]
      aggregate[phase] = {
        "median_total_ms": statistics.median(sample_total),
        "median_calls": statistics.median(sample_count),
        "median_us_per_call": (
          1000 * statistics.median(sample_total) /
          statistics.median(sample_count)
          if statistics.median(sample_count) else None),
      }
    results["commands"][name] = {
      "argv": list(command),
      "wall_median_ms": statistics.median(
        [sample["elapsed_ms"] for sample in samples]),
      "by_phase": aggregate,
      "samples": samples,
    }
    (out / "report.json").write_text(
      json.dumps(results, indent=2), encoding="utf-8")
    print("MINGW_PHASE_DONE", name, json.dumps(aggregate, sort_keys=True),
          flush=True)
  if not {"grep", "status", "diff"}.issubset(results["commands"]):
    raise AssertionError("required workload commands absent")
  print("MINGW_PHASE_PASS", results["source_sha"], flush=True)


if __name__ == "__main__":
  main()
