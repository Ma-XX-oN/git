#!/usr/bin/env python3
"""Time unmodified Git against the tip's Python and C++ managers in one job.

Both preloaded variants come from the same tree (the tip): its worker and
clients, with the C++ manager (default) or the Python manager (--python /
-Python). Bash: each round times 120 interleaved pairs of a direct
``git --version`` of unmodified upstream Git and a preloaded call, alternating
the manager in ABBA order (python, cpp, cpp, python). Direct samples from all
rounds form the unmodified reference.

On Windows, PowerShell rounds run the tree's test_powershell.ps1 in five
variants: unmodified Git launched directly, Invoke-Preload -DiscardOutput with
each manager, and the generated git function (`git --version > $null`) with
each manager, ordered A B C D E E D C B A.
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

BASH_ORDER = ("python", "cpp", "cpp", "python")
"""Manager per Bash round; direct calls always run unmodified upstream Git."""

POWERSHELL_VARIANTS = {
  "direct": ["-Mode", "Direct"],
  "python_invoke": ["-Mode", "Preload", "-DiscardOutput", "-Python"],
  "cpp_invoke": ["-Mode", "Preload", "-DiscardOutput"],
  "python_function": ["-Mode", "Preload", "-ViaFunction", "-Python"],
  "cpp_function": ["-Mode", "Preload", "-ViaFunction"],
}
"""test_powershell.ps1 arguments per PowerShell variant."""

POWERSHELL_ORDER = (*POWERSHELL_VARIANTS, *reversed(POWERSHELL_VARIANTS))
"""PowerShell round order: each variant forward, then backward."""


def bash_client(tree, variant, work):
  """Return the client script a Bash round sources for one manager.

  The benchmark sources its client without arguments, so the Python variant
  uses a one-line wrapper that sources the tree's client with --python.

  Parameters
  ----------
  tree : Path
      Built tip tree.
  variant : str
      "python" or "cpp".
  work : Path
      Existing directory for the wrapper.

  Returns
  -------
  str
      Path of the script to source.
  """
  client = tree / "preload/client/manager.sh"
  if variant == "cpp":
    return str(client)
  wrapper = work / "manager-python.sh"
  shell_path = subprocess.check_output(["cygpath", "-u", str(client)], text=True).strip() \
    if os.name == "nt" else str(client)
  wrapper.write_text(f'source "{shell_path}" --python\n', encoding="utf-8", newline="\n")
  return str(wrapper)


def powershell_round(tree, upstream_git, variant, count, output):
  """Run one PowerShell round and return its recorded latencies.

  Parameters
  ----------
  tree : Path
      Built tip tree whose script, git and manager are used.
  upstream_git : str
      Unmodified Git, timed in the direct variant.
  variant : str
      POWERSHELL_VARIANTS key.
  count : int
      Recorded calls after the script's warmup.
  output : Path
      New directory for the script's report and session logs.

  Returns
  -------
  list of float
      Per-call milliseconds.
  """
  git = upstream_git if variant == "direct" else str(tree / "git.exe")
  command = [shutil.which("pwsh"), "-NoProfile", "-File", str(tree / "preload/tests/test_powershell.ps1"),
         "-Git", git, "-Manager", str(tree / "preload-manager.exe"), "-Output", str(output),
         "-Count", str(count), *POWERSHELL_VARIANTS[variant]]
  result = subprocess.run(command, capture_output=True, text=True, timeout=600)
  if result.returncode:
    raise RuntimeError(f"PowerShell {variant} round failed: {result.stdout}\n{result.stderr}")
  return json.loads((output / "report.json").read_text(encoding="utf-8-sig"))["SamplesMs"]


def main():
  """Run the rounds and write tip-managers.json with raw samples and identities."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--upstream", required=True, type=Path)
  parser.add_argument("--candidate", type=Path, default=Path.cwd(), help="built tip tree")
  parser.add_argument("--output", type=Path, default=Path("preload-tip-managers"))
  parser.add_argument("--powershell-count", type=int, default=120)
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=True)
  windows = os.name == "nt"
  tree = args.candidate.resolve()
  builds = {"upstream": describe_build(args.upstream.resolve(), windows),
        "tip": describe_build(tree, windows)}
  assert builds["upstream"]["flags"] == builds["tip"]["flags"], "compiler flags differ"
  bench = load_benchmark(tree / "preload/perf/pool_benchmark.py", "benchmark_tip_managers")
  bench.OUT = args.output
  base = {key: value for key, value in os.environ.items()
      if not key.startswith(("GIT809_", "GIT_PRELOAD_", "GIT_TRACE", "PRELOAD_"))}
  base.update({"MSYS2_ARG_CONV_EXCL": "*", "PRELOAD_PYTHON": sys.executable,
         "PRELOAD_MANAGER": str(tree / ("preload-manager.exe" if windows else "preload-manager"))})
  report = {"reference": "unmodified upstream Git, direct", "workers": 3,
        "host": os.environ.get("COMPUTERNAME", "" if windows else os.uname().nodename),
        "platform": sys.platform, "tracing": False,
        "workload": "git --version; environment unchanged after manager startup",
        "machine": machine_metadata(), "order": list(BASH_ORDER), "builds": builds,
        "rounds": []}
  try:
    for index, name in enumerate(BASH_ORDER):
      client = bash_client(tree, name, args.output)
      row = bench.run_case(shutil.which("bash"), client, builds["tip"]["git"], base,
                 3, index, windows, direct_git=builds["upstream"]["git"])
      row["variant"] = name
      report["rounds"].append(row)
      print("PRELOAD_TIP_ROUND", name, row["direct"]["median_ms"], row["ipc"]["median_ms"], flush=True)
    if windows:
      os.environ.update({"PRELOAD_PYTHON": sys.executable})
      report["powershell_order"] = list(POWERSHELL_ORDER)
      report["powershell_rounds"] = []
      for index, name in enumerate(POWERSHELL_ORDER):
        values = powershell_round(tree, builds["upstream"]["git"], name, args.powershell_count,
                      args.output / f"powershell-{index}-{name}")
        report["powershell_rounds"].append({"variant": name, "samples_ms": values})
        print("PRELOAD_TIP_POWERSHELL_ROUND", name, statistics.median(values), flush=True)
  finally:
    (args.output / "tip-managers.json").write_text(json.dumps(report, indent=2) + "\n",
                            encoding="utf-8", newline="\n")


if __name__ == "__main__":
  main()
