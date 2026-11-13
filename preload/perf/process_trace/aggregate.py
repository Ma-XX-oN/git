#!/usr/bin/env python3
"""Aggregate opt-in native test subprocess trace without nesting double-counts."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("directory", type=Path)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  sources = sorted(args.directory.glob("proc-*.json"))
  if not sources:
    raise SystemExit("FAIL: no instrumented Python processes reported")
  values = defaultdict(list)
  exit_nonzero = Counter()
  audit = Counter()
  process_wall = []
  process_cpu = []
  for source in sources:
    entry = json.loads(source.read_text(encoding="utf-8"))
    if entry["schema"] != 1 or entry["pid"] <= 0:
      raise ValueError("invalid trace: " + str(source))
    process_wall.append(entry["wall_ms"])
    process_cpu.append(entry["cpu_ms"])
    audit.update(entry["audit_counts"])
    for name, code, duration in entry["events"]:
      if duration < 0:
        raise ValueError("negative subprocess duration")
      values[name].append(duration)
      if code != 0:
        exit_nonzero[name] += 1
  kinds = {}
  for name, samples in sorted(values.items()):
    ordered = sorted(samples)
    kinds[name] = {
      "count": len(samples),
      "sum_ms_inclusive": sum(samples),
      "median_ms": statistics.median(ordered),
      "p95_ms": ordered[(95 * (len(ordered) - 1)) // 100],
      "nonzero_or_failed": exit_nonzero[name],
    }
  report = {
    "schema": 1,
    "instrumented_python_processes": len(sources),
    "sum_python_process_cpu_ms": sum(process_cpu),
    "max_python_process_wall_ms": max(process_wall),
    "subprocess_kinds": kinds,
    "python_audit_counts": dict(sorted(audit.items())),
    "interpretation": (
      "Each Popen duration is inclusive of child execution. Durations of nested "
      "Python processes overlap with their own Git calls; do not add kinds to "
      "estimate total suite wall time. Audit counts cover Python's audited "
      "filesystem operations, not all filesystem system calls."
    ),
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
  for name, item in kinds.items():
    print("PROCESS_PROFILE", name, "calls", item["count"],
          "sum_seconds", round(item["sum_ms_inclusive"] / 1000, 3),
          "median_ms", round(item["median_ms"], 3),
          "p95_ms", round(item["p95_ms"], 3), flush=True)
  print("PROCESS_PROFILE_PYTHON_INTERPRETERS", len(sources), flush=True)
  print("PROCESS_PROFILE_AUDIT", json.dumps(report["python_audit_counts"]), flush=True)


if __name__ == "__main__":
  main()
