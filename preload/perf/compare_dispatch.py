#!/usr/bin/env python3
"""Compare two generic preload libraries with matching worker protocols.

Run with --baseline-library PATH --git PATH --output DIRECTORY. Use
--baseline-git PATH when the baseline library requires an older wire protocol.
Otherwise both libraries use the same worker binary. Measures repeated shell
calls and command substitutions in separate ABBA rounds, using
the existing paired benchmark and unmodified library protocols. matched.json
retains raw samples, source hashes, binary identity and machine details.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import sys

from compare_versions import machine_metadata
import pool_benchmark as benchmark
from stage_timing import shell_path


def library_identity(path):
  """Hash the library and manager sources outside the timed command loop.

  Parameters
  ----------
  path : Path
      Resolved manager.sh path for one variant.

  Returns
  -------
  dict
      Absolute library path and SHA-256 hashes indexed by source filename.
  """
  sources = [path, *sorted(path.parent.glob("*.py"))]
  return {"library": str(path), "sha256": {
    source.name: hashlib.sha256(source.read_bytes()).hexdigest() for source in sources}}


def measure(args, environment):
  """Run separate workloads in ABBA order and retain each completed round.

  Parameters
  ----------
  args : argparse.Namespace
      Parsed library/worker paths, positive ABBA count, and revision labels.
  environment : dict
      Child environment with tracing disabled and the manager Python selected.

  Returns
  -------
  dict
      Report containing raw samples, identity, and baseline-normalized results.
      A partial report is also written if a later round fails.
  """
  libraries = {"baseline": args.baseline_library.resolve(strict=True),
         "candidate": args.candidate_library.resolve(strict=True)}
  git = args.git.resolve(strict=True)
  baseline_git = args.baseline_git.resolve(strict=True) if args.baseline_git else git
  report = {"machine": machine_metadata(), "tracing": False,
        "baseline_ref": args.baseline_ref,
        "candidate_ref": os.environ.get("GITHUB_SHA", "working-tree"),
        "worker": {"path": str(git), "sha256": hashlib.sha256(git.read_bytes()).hexdigest()},
        "baseline_worker": {"path": str(baseline_git),
                  "sha256": hashlib.sha256(baseline_git.read_bytes()).hexdigest()},
        "same_worker_binary": baseline_git == git,
        "variants": {name: library_identity(path) for name, path in libraries.items()},
        "initial_workers": 3, "startup_included_in_samples": False,
        "order": ["baseline", "candidate", "candidate", "baseline"] * args.rounds,
        "rounds": [], "summary": {}}
  args.output.mkdir(parents=True, exist_ok=True)
  try:
    for workload in ("shell", "subshell"):
      for index, name in enumerate(report["order"]):
        row = benchmark.run_case(shutil.which("bash"), shell_path(libraries[name]),
                     shell_path(baseline_git if name == "baseline" else git),
                     environment, 3, index,
                     os.name == "nt", workload=workload)
        row["variant"] = name
        report["rounds"].append(row)
        (args.output / "matched.json").write_text(
          json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("PRELOAD_DISPATCH_ROUND", workload, name, row["ipc"]["median_ms"], flush=True)
      medians = {name: statistics.median(
        sample for row in report["rounds"]
        if row["variant"] == name and row["workload"] == workload
        for sample in row["ipc"]["samples_ms"]) for name in libraries}
      summary = {"medians_ms": medians,
             "candidate_percent_of_baseline": 100 * medians["candidate"] / medians["baseline"],
             "reduction_ms": medians["baseline"] - medians["candidate"]}
      report["summary"][workload] = summary
      print("PRELOAD_DISPATCH_SUMMARY", workload, json.dumps(summary), flush=True)
  finally:
    (args.output / "matched.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
  return report


def main():
  """Parse library paths and run an untraced comparison of both workloads."""
  root = Path(__file__).resolve().parents[2]
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--baseline-library", required=True, type=Path)
  parser.add_argument("--baseline-ref", default="346517302dfc305cf122f359714c0dc12e4b0884")
  parser.add_argument("--candidate-library", type=Path,
            default=root / "preload/client/manager.sh")
  parser.add_argument("--git", required=True, type=Path)
  parser.add_argument("--baseline-git", type=Path,
            help="Worker matching the baseline protocol; defaults to --git")
  parser.add_argument("--rounds", default=3, type=int)
  parser.add_argument("--output", type=Path, default=Path("preload-dispatch-comparison"))
  args = parser.parse_args()
  if args.rounds < 1:
    parser.error("--rounds must be positive")
  environment = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT809_", "GIT_PRELOAD_", "GIT_TRACE", "PRELOAD_"))}
  environment["PRELOAD_PYTHON"] = sys.executable
  environment["MSYS2_ARG_CONV_EXCL"] = "*"
  measure(args, environment)


if __name__ == "__main__":
  main()
