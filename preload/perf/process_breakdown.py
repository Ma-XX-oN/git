#!/usr/bin/env python3
"""Native Git launch breakdown: minimal process, upstream Git and preload Git.

Read-only ABBA experiment. Each sample separates Popen construction from
communicate/wait; these intervals are not interpreted as non-overlapping child
startup phases. Samples and build identities are retained, not just medians.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import time


def digest(path):
  return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values, proportion):
  ordered = sorted(values)
  return ordered[math.ceil(len(ordered) * proportion) - 1]


def measure(case, environment):
  command, capture, expected = case["command"], case["capture"], case["expected"]
  started = time.perf_counter_ns()
  child = subprocess.Popen(
    command, stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
    stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
    env=environment,
  )
  created = time.perf_counter_ns()
  try:
    out, err = child.communicate(timeout=20)
  except subprocess.TimeoutExpired:
    child.kill()
    child.communicate()
    raise
  finished = time.perf_counter_ns()
  if child.returncode:
    raise AssertionError(
      f"{case['name']}: exit={child.returncode}, stderr={err!r}"
    )
  if capture and expected not in out:
    raise AssertionError(f"{case['name']}: unexpected output {out!r}")
  return {
    "spawn_return_us": (created - started) / 1000,
    "after_spawn_us": (finished - created) / 1000,
    "total_us": (finished - started) / 1000,
  }


def summarize(rows):
  names = ("spawn_return_us", "after_spawn_us", "total_us")
  return {
    name: {
      "median_us": statistics.median(row[name] for row in rows),
      "p95_us": percentile([row[name] for row in rows], .95),
    }
    for name in names
  }


def dependencies(executable):
  if os.name == "nt":
    command = ["objdump", "-p", str(executable)]
  elif sys.platform == "darwin":
    command = ["otool", "-L", str(executable)]
  else:
    command = ["ldd", str(executable)]
  if not shutil.which(command[0]):
    return {"command": command, "status": "tool-unavailable"}
  result = subprocess.run(command, capture_output=True, text=True, timeout=30)
  return {
    "command": command,
    "returncode": result.returncode,
    "stdout": result.stdout[:30000],
    "stderr": result.stderr[:2000],
  }


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--git", required=True, type=Path)
  parser.add_argument("--control", required=True, type=Path)
  parser.add_argument("--upstream", type=Path)
  parser.add_argument("--output", type=Path, required=True)
  parser.add_argument("--count", type=int, default=120)
  args = parser.parse_args()
  if not 5 <= args.count <= 1000:
    parser.error("--count must be between 5 and 1000")
  output = args.output.resolve()
  output.mkdir(parents=True, exist_ok=True)
  git = args.git.resolve(strict=True)
  control = args.control.resolve(strict=True)
  upstream = args.upstream.resolve(strict=True) if args.upstream else None
  root = Path.cwd().resolve()
  environment = {
    key: value for key, value in os.environ.items()
    if not key.startswith(("GIT_TRACE", "GIT809_", "PRELOAD_"))
  }
  cases = [
    {"name": "control_discard", "command": [str(control)],
     "capture": False, "expected": b""},
    {"name": "control_capture", "command": [str(control), "output"],
     "capture": True, "expected": b"native launch control"},
    {"name": "git_version_discard", "command": [str(git), "--version"],
     "capture": False, "expected": b""},
    {"name": "git_version_capture", "command": [str(git), "--version"],
     "capture": True, "expected": b"git version "},
    {"name": "git_repo_probe", "command": [
      str(git), "-C", str(root), "rev-parse", "--is-inside-work-tree"],
     "capture": True, "expected": b"true"},
  ]
  if upstream:
    cases.extend([
      {"name": "upstream_version_discard", "command": [str(upstream), "--version"],
       "capture": False, "expected": b""},
      {"name": "upstream_version_capture", "command": [str(upstream), "--version"],
       "capture": True, "expected": b"git version "},
    ])
  for case in cases:
    for _ in range(8):
      measure(case, environment)
  samples = {case["name"]: [] for case in cases}
  # Forward/reverse ordering prevents a later case from always running under
  # a colder filesystem cache or later process-scheduling conditions.
  for index in range(args.count):
    sequence = cases if index % 2 == 0 else list(reversed(cases))
    for case in sequence:
      samples[case["name"]].append(measure(case, environment))
  builds = {"git": git, "control": control}
  if upstream:
    builds["upstream"] = upstream
  report = {
    "schema": 1,
    "source_revision": os.environ.get("GITHUB_SHA"),
    "platform": platform.platform(),
    "architecture": platform.machine(),
    "processor": platform.processor(),
    "logical_cpus": os.cpu_count(),
    "python": sys.version,
    "ci_run": os.environ.get("GITHUB_RUN_ID"),
    "ci_job": os.environ.get("GITHUB_JOB"),
    "count_per_case": args.count,
    "environment": "inherited, tracing and PRELOAD_* omitted",
    "instrumentation": "parent-side perf_counter_ns, no process or Git tracing",
    "builds": {
      name: {"path": str(executable), "sha256": digest(executable),
             "dependencies": dependencies(executable)}
      for name, executable in builds.items()
    },
    "cases": {
      case["name"]: {"command": case["command"], "capture": case["capture"],
                     "summary": summarize(samples[case["name"]]),
                     "samples": samples[case["name"]]}
      for case in cases
    },
  }
  result = output / "process-breakdown.json"
  result.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
  for name, item in report["cases"].items():
    summary = item["summary"]
    print(
      f"PROCESS_BREAKDOWN {name}: "
      f"start={summary['spawn_return_us']['median_us']:.1f}us "
      f"finish={summary['after_spawn_us']['median_us']:.1f}us "
      f"total={summary['total_us']['median_us']:.1f}us "
      f"p95={summary['total_us']['p95_us']:.1f}us",
      flush=True,
    )
  print("PROCESS_BREAKDOWN_EVIDENCE", result, flush=True)


if __name__ == "__main__":
  main()
