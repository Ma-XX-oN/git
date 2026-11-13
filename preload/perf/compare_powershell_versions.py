#!/usr/bin/env python3
"""Compare warm PowerShell preload latency of two built trees in ABBA rounds.

Each round runs one tree's own preload/tests/test_powershell.ps1 in Preload
mode with -DiscardOutput, using that tree's git and preload-manager, so each
side keeps its own client, worker and manager. Rounds alternate baseline,
candidate, candidate, baseline per block. The report retains raw samples,
tree revisions and binary digests.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_versions import machine_metadata  # noqa: E402

ORDER = ("baseline", "candidate", "candidate", "baseline")
"""Tree timed in each round of one ABBA block."""


def describe(tree):
  """Return a built tree's revision and the digests of its git and manager.

  Parameters
  ----------
  tree : Path
      Built source tree containing git.exe and preload-manager.exe.

  Returns
  -------
  dict
      Revision and SHA-256 digests.
  """
  return {"tree": str(tree),
      "revision": subprocess.check_output(["git", "-C", str(tree), "rev-parse", "HEAD"],
                        text=True).strip(),
      **{name: hashlib.sha256((tree / name).read_bytes()).hexdigest()
         for name in ("git.exe", "preload-manager.exe")}}


def run_round(tree, count, output):
  """Run one Preload-mode round and return its recorded latencies.

  Parameters
  ----------
  tree : Path
      Built tree whose script, git and manager are used.
  count : int
      Recorded calls after the script's warmup.
  output : Path
      New directory for the script's report and session logs.

  Returns
  -------
  list of float
      Per-call milliseconds.
  """
  command = [shutil.which("pwsh"), "-NoProfile", "-File",
         str(tree / "preload/tests/test_powershell.ps1"), "-Mode", "Preload",
         "-DiscardOutput", "-Count", str(count), "-Git", str(tree / "git.exe"),
         "-Manager", str(tree / "preload-manager.exe"), "-Output", str(output)]
  result = subprocess.run(command, capture_output=True, text=True, timeout=600)
  if result.returncode:
    raise RuntimeError(f"{tree}: {result.stdout}\n{result.stderr}")
  return json.loads((output / "report.json").read_text(encoding="utf-8-sig"))["SamplesMs"]


def main():
  """Run the ABBA blocks and write powershell-versions.json."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--baseline", required=True, type=Path)
  parser.add_argument("--candidate", required=True, type=Path)
  parser.add_argument("--blocks", type=int, default=3)
  parser.add_argument("--count", type=int, default=300)
  parser.add_argument("--output", required=True, type=Path)
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=False)
  trees = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
  report = {"workload": "Invoke-Preload --version -DiscardOutput, warmed",
        "machine": machine_metadata(), "count": args.count,
        "trees": {name: describe(tree) for name, tree in trees.items()}, "rounds": []}
  for block in range(args.blocks):
    for index, name in enumerate(ORDER):
      values = run_round(trees[name], args.count, args.output / f"{block}-{index}-{name}")
      report["rounds"].append({"variant": name, "samples_ms": values})
      print("POWERSHELL_VERSIONS_ROUND", name, statistics.median(values), flush=True)
  medians = {name: statistics.median(s for row in report["rounds"] if row["variant"] == name
                     for s in row["samples_ms"]) for name in trees}
  report["medians_ms"] = medians
  report["candidate_percent_of_baseline"] = 100 * medians["candidate"] / medians["baseline"]
  print("POWERSHELL_VERSIONS_SUMMARY", json.dumps(report["medians_ms"]),
      f"{report['candidate_percent_of_baseline']:.2f}%", flush=True)
  (args.output / "powershell-versions.json").write_text(json.dumps(report, indent=2) + "\n",
                              encoding="utf-8", newline="\n")


if __name__ == "__main__":
  main()
