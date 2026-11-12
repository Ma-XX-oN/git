#!/usr/bin/env python3
"""Time unmodified upstream Git directly against the migrated preload client.

Both builds run on one native runner within one job. Each round times 120
interleaved pairs: a direct ``--version`` call and a preloaded
``git --version`` call served by the migrated build. Rounds alternate the
directly timed binary in ABBA order (upstream, migrated, migrated, upstream),
so the upstream reference, the migrated direct path and the preloaded path
share one machine and one time window.
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
from compare_versions import load_benchmark, machine_metadata  # noqa: E402

ORDER = ("upstream", "migrated", "migrated", "upstream")
"""Directly timed build per round; preloaded calls always use the migrated build."""


def describe_build(root, windows):
  """Identify one built tree's executable, revision and compiler flags.

  Parameters
  ----------
  root : Path
      Built Git source tree.
  windows : bool
      Whether the executable carries the .exe suffix.

  Returns
  -------
  dict
      Executable path, binary digest, source revision and GIT-CFLAGS text.
  """
  git = root / ("git.exe" if windows else "git")
  return {"git": str(git), "binary_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
      "revision": subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
      "flags": (root / "GIT-CFLAGS").read_text(encoding="utf-8")}


def pooled_median(rounds, kind, variant=None):
  """Return the median of all raw samples of one kind, optionally per variant.

  Parameters
  ----------
  rounds : list of dict
      Round rows from pool_benchmark.run_case with a "variant" key.
  kind : str
      "direct" or "ipc".
  variant : str, optional
      Restrict to rounds whose directly timed build is this variant.

  Returns
  -------
  float
      Median milliseconds over the pooled samples.
  """
  return statistics.median(sample for row in rounds
               if variant is None or row["variant"] == variant
               for sample in row[kind]["samples_ms"])


def main():
  """Run the ABBA rounds and retain raw samples, binary identities and flags."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--upstream", required=True, type=Path)
  parser.add_argument("--candidate", type=Path, default=Path.cwd())
  parser.add_argument("--output", type=Path, default=Path("preload-upstream"))
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=True)
  windows = os.name == "nt"
  candidate = args.candidate.resolve()
  builds = {"upstream": describe_build(args.upstream.resolve(), windows),
        "migrated": describe_build(candidate, windows)}
  assert builds["upstream"]["flags"] == builds["migrated"]["flags"], "compiler flags differ"
  bench = load_benchmark(candidate / "preload/perf/pool_benchmark.py", "benchmark_migrated")
  bench.OUT = args.output
  environment = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT809_", "GIT_PRELOAD_", "GIT_TRACE", "PRELOAD_"))}
  environment.update({"PRELOAD_PYTHON": sys.executable, "MSYS2_ARG_CONV_EXCL": "*"})
  client = str(candidate / "preload/client/manager.sh")
  report = {"reference": "unmodified upstream Git, direct", "workers": 3,
        "host": os.environ.get("COMPUTERNAME", "" if windows else os.uname().nodename),
        "platform": sys.platform, "tracing": False,
        "workload": "git --version; environment unchanged after manager startup",
        "machine": machine_metadata(), "order": list(ORDER), "builds": builds,
        "rounds": []}
  try:
    for index, name in enumerate(ORDER):
      row = bench.run_case(shutil.which("bash"), client, builds["migrated"]["git"],
                 environment, 3, index, windows,
                 direct_git=builds[name]["git"])
      row["variant"] = name
      report["rounds"].append(row)
      print("PRELOAD_UPSTREAM_ROUND", name, row["direct"]["median_ms"],
          row["ipc"]["median_ms"], flush=True)
    medians = {"upstream_direct": pooled_median(report["rounds"], "direct", "upstream"),
           "migrated_direct": pooled_median(report["rounds"], "direct", "migrated"),
           "migrated_preloaded": pooled_median(report["rounds"], "ipc")}
    report["medians_ms"] = medians
    report["percent_of_upstream_direct"] = {
      name: 100 * value / medians["upstream_direct"] for name, value in medians.items()}
    print("PRELOAD_UPSTREAM_SUMMARY", json.dumps(report["medians_ms"]), flush=True)
  finally:
    (args.output / "upstream.json").write_text(json.dumps(report, indent=2) + "\n",
                           encoding="utf-8", newline="\n")


if __name__ == "__main__":
  main()
