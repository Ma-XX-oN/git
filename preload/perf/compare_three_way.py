#!/usr/bin/env python3
"""Time unmodified Git directly against two preloaded builds in one job.

The preloaded builds are the Python-manager release (working-version-3, its
own client and Python manager) and the current tree with the C++ manager. All
three builds run on one native runner within one job. Each round times 120
interleaved pairs: a direct ``git --version`` call of unmodified upstream Git
and a preloaded ``git --version`` call served by that round's build. Rounds
alternate the preloaded build in ABBA order (python, cpp, cpp, python), so
every series shares one machine and one time window. Direct samples from all
rounds form the unmodified reference.

On Windows the same three builds are also timed through PowerShell, using each
build's own test_powershell.ps1: unmodified Git launched directly from
PowerShell, and both preload clients with output discarded, as the Bash calls
discard theirs. PowerShell runs one mode per invocation, so its rounds are
ordered direct, python, cpp, cpp, python, direct.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_upstream import describe_build  # noqa: E402
from compare_versions import load_benchmark, machine_metadata  # noqa: E402

ORDER = ("python", "cpp", "cpp", "python")
"""Preloaded build per round; direct calls always run unmodified upstream Git."""

POWERSHELL_ORDER = ("direct", "python", "cpp", "cpp", "python", "direct")
"""Windows PowerShell rounds: unmodified Git direct, or a preloaded build."""

POWERSHELL_WARMUP = 20
"""Unrecorded calls per PowerShell round; working-version-3's script fixes 20."""


def powershell_round(variant, trees, upstream_git, count, output):
  """Time one PowerShell round and return its raw samples in milliseconds.

  Parameters
  ----------
  variant : str
      "direct" (unmodified Git), "python" or "cpp".
  trees : dict
      Built source tree per preloaded variant.
  upstream_git : str
      Unmodified Git executable, used for direct rounds.
  count : int
      Recorded calls in the round.
  output : Path
      New directory for the script's report and session logs.

  Returns
  -------
  list of float
      Recorded call latencies.
  """
  tree = trees["cpp" if variant == "direct" else variant]
  command = [shutil.which("pwsh"), "-NoProfile", "-File", str(tree / "preload/tests/test_powershell.ps1"),
         "-Output", str(output), "-Count", str(count)]
  if variant == "direct":
    command += ["-Mode", "Direct", "-Git", upstream_git]
  else:
    command += ["-Mode", "Preload", "-DiscardOutput", "-Git", str(tree / "git.exe")]
    command += (["-Python", sys.executable] if variant == "python"
          else ["-Manager", str(tree / "preload-manager.exe"), "-Warmup", str(POWERSHELL_WARMUP)])
  result = subprocess.run(command, capture_output=True, text=True, timeout=600)
  if result.returncode:
    raise RuntimeError(f"PowerShell {variant} round failed: {result.stdout}\n{result.stderr}")
  report = json.loads((output / "report.json").read_text(encoding="utf-8-sig"))
  return report["SamplesMs"]


def variant_environment(base, variant, tree, windows):
  """Return the environment that selects one build's manager.

  Parameters
  ----------
  base : dict
      Environment without preload or tracing variables.
  variant : str
      "python" (working-version-3 client and Python manager) or "cpp".
  tree : Path
      Built source tree of that variant.
  windows : bool
      Whether executables carry the .exe suffix.

  Returns
  -------
  dict
      Copy of base selecting the variant's manager.
  """
  environment = dict(base)
  if variant == "python":
    environment["PRELOAD_PYTHON"] = sys.executable
  else:
    environment["PRELOAD_MANAGER"] = str(tree / ("preload-manager.exe" if windows else "preload-manager"))
  return environment


def pooled_median(rounds, kind, variant=None):
  """Return the median of all raw samples of one kind, optionally per variant.

  Parameters
  ----------
  rounds : list of dict
      Round rows from pool_benchmark.run_case with a "variant" key.
  kind : str
      "direct" or "ipc".
  variant : str, optional
      Restrict to rounds whose preloaded build is this variant.

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
  parser.add_argument("--python-tree", required=True, type=Path,
            help="built working-version-3 tree (Python manager)")
  parser.add_argument("--candidate", type=Path, default=Path.cwd(),
            help="built current tree (C++ manager)")
  parser.add_argument("--output", type=Path, default=Path("preload-three-way"))
  parser.add_argument("--powershell-count", type=int, default=120,
            help="recorded calls per Windows PowerShell round")
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=True)
  windows = os.name == "nt"
  trees = {"python": args.python_tree.resolve(), "cpp": args.candidate.resolve()}
  builds = {"upstream": describe_build(args.upstream.resolve(), windows),
        **{name: describe_build(tree, windows) for name, tree in trees.items()}}
  assert len({build["flags"] for build in builds.values()}) == 1, "compiler flags differ"
  bench = load_benchmark(trees["cpp"] / "preload/perf/pool_benchmark.py", "benchmark_three_way")
  bench.OUT = args.output
  base = {key: value for key, value in os.environ.items()
      if not key.startswith(("GIT809_", "GIT_PRELOAD_", "GIT_TRACE", "PRELOAD_"))}
  base["MSYS2_ARG_CONV_EXCL"] = "*"
  report = {"reference": "unmodified upstream Git, direct", "workers": 3,
        "host": os.environ.get("COMPUTERNAME", "" if windows else os.uname().nodename),
        "platform": sys.platform, "tracing": False,
        "workload": "git --version; environment unchanged after manager startup",
        "machine": machine_metadata(), "order": list(ORDER), "builds": builds,
        "rounds": []}
  try:
    for index, name in enumerate(ORDER):
      client = str(trees[name] / "preload/client/manager.sh")
      row = bench.run_case(shutil.which("bash"), client, builds[name]["git"],
                 variant_environment(base, name, trees[name], windows),
                 3, index, windows, direct_git=builds["upstream"]["git"])
      row["variant"] = name
      report["rounds"].append(row)
      print("PRELOAD_THREE_WAY_ROUND", name, row["direct"]["median_ms"],
          row["ipc"]["median_ms"], flush=True)
    medians = {"upstream_direct": pooled_median(report["rounds"], "direct"),
           "python_preloaded": pooled_median(report["rounds"], "ipc", "python"),
           "cpp_preloaded": pooled_median(report["rounds"], "ipc", "cpp")}
    report["medians_ms"] = medians
    report["percent_of_upstream_direct"] = {
      name: 100 * value / medians["upstream_direct"] for name, value in medians.items()}
    print("PRELOAD_THREE_WAY_SUMMARY", json.dumps(medians), flush=True)
    if windows:
      report["powershell_order"] = list(POWERSHELL_ORDER)
      report["powershell_warmup"] = POWERSHELL_WARMUP
      report["powershell_rounds"] = []
      for index, name in enumerate(POWERSHELL_ORDER):
        values = powershell_round(name, trees, builds["upstream"]["git"], args.powershell_count,
                      args.output / f"powershell-{index}-{name}")
        report["powershell_rounds"].append({"variant": name, "samples_ms": values})
        print("PRELOAD_THREE_WAY_POWERSHELL_ROUND", name, statistics.median(values), flush=True)
  finally:
    (args.output / "three-way.json").write_text(json.dumps(report, indent=2) + "\n",
                          encoding="utf-8", newline="\n")


if __name__ == "__main__":
  main()
