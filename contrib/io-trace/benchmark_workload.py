#!/usr/bin/env python3
"""Cross-platform longer Git I/O experiment: test-like Git command series.

No process receives an implicit trace. The same command sequence is first
run untraced, then once more with explicit per-process binary logging.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

from decode import parse, summary


COMMANDS = (
  ("status", ("status", "--porcelain=v1", "-uno")),
  ("diff", ("diff", "--name-only", "HEAD")),
  ("grep", ("grep", "-l", "SEARCH_TOKEN_809")),
  ("ls_files", ("ls-files", "--stage")),
  ("log", ("log", "-1", "--format=%H")),
  ("rev_list", ("rev-list", "--objects", "--all")),
)


def run_checked(argv, cwd=None):
  p = subprocess.run(
    [str(arg) for arg in argv], cwd=cwd,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
  if p.returncode:
    raise RuntimeError(
      f"command returned {p.returncode}: {argv!r}\n"
      f"stderr={p.stderr[:4000]!r}")
  return p


def create_fixture(git, root, count):
  if root.exists():
    raise RuntimeError(f"refusing to reuse existing fixture: {root}")
  root.mkdir(parents=True)
  run_checked([git, "init", "-q", str(root)])
  for key, value in (
      ("core.autocrlf", "false"),
      ("core.filemode", "false"),
      ("core.fsmonitor", "false"),
      ("core.untrackedCache", "false"),
      ("core.preloadIndex", "true"),
  ):
    run_checked([git, "config", key, value], cwd=root)

  payload = (b"contents for filesystem study\n" * 28 +
             b"SEARCH_TOKEN_809\n")
  for n in range(count):
    directory = root / f"folder-{n // 1000:02d}"
    directory.mkdir(exist_ok=True)
    name = directory / f"tracked-{n:06d}.txt"
    name.write_bytes(payload + f"record={n:06d}\n".encode("ascii"))
  run_checked([git, "add", "--all"], cwd=root)
  run_checked([git, "-c", "user.name=I/O Probe",
               "-c", "user.email=io-probe@example.invalid",
               "commit", "-qm", "seed test-like workload"], cwd=root)
  modified = min(count, max(128, count // 25))
  for n in range(modified):
    name = root / f"folder-{n // 1000:02d}" / f"tracked-{n:06d}.txt"
    with name.open("ab") as f:
      f.write(b"worktree-change-for-status-and-diff\n")
  return {"tracked_files": count, "modified_files": modified,
          "bytes_per_file": len(payload) + len(b"record=000000\n")}


def measure(git, root, args, trace_file=None):
  argv = [str(git)]
  if trace_file is not None:
    if trace_file.exists():
      raise RuntimeError(f"refusing to overwrite trace: {trace_file}")
    argv.extend(["--log-fn", str(trace_file.resolve())])
  argv.extend(args)
  start = time.perf_counter_ns()
  result = run_checked(argv, cwd=root)
  elapsed_ns = time.perf_counter_ns() - start
  return {
    "elapsed_ms": elapsed_ns / 1000000.0,
    "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
    "stdout_bytes": len(result.stdout),
    "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
    "stderr_bytes": len(result.stderr),
  }


def group_stats(samples):
  ordered = sorted(samples)
  if not ordered:
    raise AssertionError("empty timing collection")
  return {
    "count": len(ordered),
    "total_ms": round(sum(ordered), 4),
    "median_ms": round(statistics.median(ordered), 4),
    "p95_ms": round(ordered[(95 * (len(ordered) - 1)) // 100], 4),
    "max_ms": round(ordered[-1], 4),
  }


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("--git", required=True, type=Path)
  parser.add_argument("--out", required=True, type=Path)
  parser.add_argument("--files", type=int, default=8000)
  parser.add_argument("--cycles", type=int, default=100)
  args = parser.parse_args()

  if not 1000 <= args.files <= 20000:
    parser.error("--files must be between 1000 and 20000")
  if not 1 <= args.cycles <= 500:
    parser.error("--cycles must be between 1 and 500")
  git = args.git.resolve(strict=True)
  out = args.out.resolve()
  fixture = out.parent / (out.name + "-fixture")
  if out.exists() or fixture.exists():
    raise RuntimeError("workload destination already exists; refusing reuse")
  out.mkdir(parents=True)
  traces = out / "traces"
  traces.mkdir()
  report_path = out / "report.json"
  dobby = os.environ.get("GIT_IO_TRACE_DOBBY")
  if not dobby:
    raise RuntimeError("GIT_IO_TRACE_DOBBY must point to pinned native Dobby")
  library = Path(dobby).resolve(strict=True)
  report = {
    "source_sha": os.environ.get("GITHUB_SHA"),
    "platform": platform.platform(),
    "python_version": platform.python_version(),
    "git_binary_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
    "dobby_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
    "files_requested": args.files,
    "cycles": args.cycles,
    "command_sequence": [
      {"name": name, "argv": list(command)} for name, command in COMMANDS
    ],
    "trace_scope": "explicit one-process log per Git command",
  }

  report["fixture"] = create_fixture(git, fixture, args.files)

  # Prime disk cache and index before the measured runs, consistently.
  signatures = {}
  for _ in range(3):
    for name, command in COMMANDS:
      result = measure(git, fixture, command)
      signatures[name] = (result["stdout_sha256"], result["stdout_bytes"])
  all_ms = {name: [] for name, _ in COMMANDS}
  total_start = time.perf_counter_ns()
  for cycle in range(args.cycles):
    for name, command in COMMANDS:
      result = measure(git, fixture, command)
      if (result["stdout_sha256"], result["stdout_bytes"]) != signatures[name]:
        raise AssertionError(f"unexpected output divergence: {name} cycle={cycle}")
      all_ms[name].append(result["elapsed_ms"])
  report["untraced_wall_ms"] = round(
    (time.perf_counter_ns() - total_start) / 1000000.0, 4)
  report["untraced_invocations"] = args.cycles * len(COMMANDS)
  report["untraced_by_command"] = {
    name: group_stats(all_ms[name]) for name, _ in COMMANDS
  }
  report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
  print("UNTRACED_WORKLOAD_DONE", args.cycles,
        report["untraced_invocations"], report["untraced_wall_ms"],
        flush=True)

  # Save a trace for every command class after the long untraced sequence.
  report["traced"] = {}
  for index, (name, command) in enumerate(COMMANDS, 1):
    filename = traces / f"git-{index:016x}.bin"
    outcome = measure(git, fixture, command, filename)
    if (outcome["stdout_sha256"], outcome["stdout_bytes"]) != signatures[name]:
      raise AssertionError(f"tracing changed observable output for {name}")
    parsed = parse(filename)
    decoded = summary(parsed)
    op_count = sum(x["count"] for x in decoded["by_operation"].values())
    if op_count < 1:
      raise AssertionError(f"no I/O events for {name}")
    report["traced"][name] = {
      "filename": str(filename.relative_to(out)),
      "trace_size_bytes": filename.stat().st_size,
      "wall_ms": round(outcome["elapsed_ms"], 4),
      "decoded": decoded,
      "operations": op_count,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("TRACE_WORKLOAD_DONE", name, op_count,
          round(outcome["elapsed_ms"], 4), flush=True)

  assert set(report["traced"]) == set(all_ms)
  print("WORKLOAD_PASS", report["source_sha"], args.files,
        args.cycles, report["untraced_invocations"], flush=True)


if __name__ == "__main__":
  main()
