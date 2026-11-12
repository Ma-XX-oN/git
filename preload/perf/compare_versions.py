#!/usr/bin/env python3
"""Compare matching baseline and production clients on one native runner."""
import argparse
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path
import shutil
import statistics
import subprocess
import sys


def load_benchmark(path, name):
  """Load a revision's benchmark without modifying its client or protocol.

  Parameters
  ----------
  path : Path
      Benchmark source from the revision being measured.
  name : str
      Distinct module name for this variant.

  Returns
  -------
  module
      Loaded benchmark; its own client and wire protocol remain in use.
  """
  sys.path.insert(0, str(path.parent))
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def machine_metadata():
  """Capture hardware and runtime identity outside the measured command loop.

  Returns
  -------
  dict
      OS, architecture, processor identity, logical CPU count, runtimes and CI
      identifiers. A failed processor-name probe is recorded explicitly.
  """
  record = {"os": platform.platform(), "architecture": platform.machine(),
        "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        "python": sys.version, "bash": subprocess.check_output(
          [shutil.which("bash"), "--version"], text=True).splitlines()[0],
        "ci_run": os.environ.get("GITHUB_RUN_ID"),
        "ci_job": os.environ.get("GITHUB_JOB")}
  try:
    if os.name == "nt":
      import winreg
      with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
        record["processor_name"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    elif sys.platform == "darwin":
      record["processor_name"] = subprocess.check_output(
        ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
    else:
      names = [line.partition(":")[2].strip() for line in
           Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")]
      record["processor_name"] = names[0] if names else record["processor"]
  except (OSError, subprocess.CalledProcessError) as error:
    record["processor_probe_error"] = str(error)
  return record


def main():
  """Retain ABBA raw timings, binary identities and matching build flags."""
  parser = argparse.ArgumentParser()
  parser.add_argument("--baseline", required=True, type=Path)
  parser.add_argument("--baseline-ref", default="production/preload",
            help="Name recorded for the baseline revision.")
  parser.add_argument("--candidate", type=Path, default=Path.cwd())
  parser.add_argument("--output", type=Path, default=Path("preload-comparison"))
  parser.add_argument("--rounds", type=int, default=2,
            help="ABBA repetitions; each adds two rounds per variant.")
  parser.add_argument("--upstream", type=Path,
            help="Built unmodified Git tree; when given, every round's "
               "direct calls run its executable instead of the variant's.")
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=True)
  windows = os.name == "nt"
  report = {"baseline": args.baseline_ref, "workers": 3,
        "host": os.environ.get("COMPUTERNAME", os.uname().nodename if not windows else ""),
        "platform": sys.platform, "tracing": False,
        "workload": "git --version; environment unchanged after manager startup",
        "environment_policy": {
          "baseline": "environment inherited from manager startup",
          "candidate": "environment inherited from manager startup"},
        "machine": machine_metadata(),
        "order": ["baseline", "candidate", "candidate", "baseline"] * args.rounds,
        "rounds": [], "variants": {}, "layouts": {}}
  variants = {}
  for name, root in (("baseline", args.baseline.resolve()), ("candidate", args.candidate.resolve())):
    git = root / ("git.exe" if windows else "git")
    environment = os.environ.copy()
    for key in list(environment):
      if key.startswith(("GIT809_", "GIT_PRELOAD_", "GIT_TRACE", "PRELOAD_")):
        environment.pop(key)
    if (root / "preload/client/manager.sh").exists():
      report["layouts"][name] = "preload/client/manager.sh"
      client = root / "preload/client/manager.sh"
      benchfile = root / "preload/perf/pool_benchmark.py"
      environment["PRELOAD_PYTHON"] = sys.executable
    elif (root / "contrib/preload/preload-manager.sh").exists():
      # Generic library: one manager, per-call request/reply handshake.
      report["layouts"][name] = "generic preload-manager.sh"
      client = root / "contrib/preload/preload-manager.sh"
      benchfile = root / "t/perf/preload_pool_benchmark.py"
      environment["PRELOAD_PYTHON"] = sys.executable
    else:
      old = not (root / "contrib/preload/git-manager.sh").exists()
      report["layouts"][name] = ("experiments/issue-809" if old
                     else "git-manager.sh persistent allocator")
      directory = root / ("experiments/issue-809" if old else "contrib/preload")
      client = directory / "git-manager.sh"
      benchfile = (directory / "pool_size_ab_benchmark.py" if old
             else root / "t/perf/preload_pool_benchmark.py")
      prefix = "GIT809" if old else "GIT_PRELOAD"
      environment[prefix + "_PLATFORM"] = "windows" if windows else "unix"
      environment[prefix + "_MANAGER_PYTHON"] = sys.executable
      environment[prefix + "_WORKER_GIT"] = str(git)
    bench = load_benchmark(benchfile, "benchmark_" + name)
    bench.OUT = args.output
    environment["MSYS2_ARG_CONV_EXCL"] = "*"
    variants[name] = (bench, str(client), str(git), environment)
    report["variants"][name] = {
      "git": str(git), "binary_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
      "revision": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
      "flags": (root / "GIT-CFLAGS").read_text(encoding="utf-8")}
  assert report["variants"]["baseline"]["flags"] == report["variants"]["candidate"]["flags"], (
    "baseline and candidate compiler flags differ")
  direct = None
  if args.upstream:
    upstream = args.upstream.resolve()
    direct = str(upstream / ("git.exe" if windows else "git"))
    report["upstream"] = {
      "git": direct,
      "binary_sha256": hashlib.sha256(Path(direct).read_bytes()).hexdigest(),
      "revision": subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"],
                        text=True).strip(),
      "flags": (upstream / "GIT-CFLAGS").read_text(encoding="utf-8")}
    assert report["upstream"]["flags"] == report["variants"]["candidate"]["flags"], (
      "upstream and candidate compiler flags differ")
  try:
    for index, name in enumerate(report["order"]):
      bench, client, git, environment = variants[name]
      # Direct calls time the variant's own build, or unmodified upstream.
      row = bench.run_case(shutil.which("bash"), client, git, environment, 3, index, windows,
                 direct_git=direct)
      row["variant"] = name
      report["rounds"].append(row)
      print("PRELOAD_MATCHED_ROUND", name, row["ipc"]["median_ms"], flush=True)
    report["medians_ms"] = {
      name: statistics.median(sample for row in report["rounds"] if row["variant"] == name
                  for sample in row["ipc"]["samples_ms"])
      for name in variants}
    report["direct_medians_ms"] = {
      name: statistics.median(sample for row in report["rounds"] if row["variant"] == name
                  for sample in row["direct"]["samples_ms"])
      for name in variants}
    report["reduction_ms"] = report["medians_ms"]["baseline"] - report["medians_ms"]["candidate"]
    report["candidate_percent_of_baseline"] = (
      100 * report["medians_ms"]["candidate"] / report["medians_ms"]["baseline"])
    report["change_percent"] = report["candidate_percent_of_baseline"] - 100
    print("PRELOAD_MATCHED_SUMMARY", json.dumps(report["medians_ms"]), flush=True)
    if direct:
      report["upstream_direct_ms"] = statistics.median(
        sample for row in report["rounds"] for sample in row["direct"]["samples_ms"])
      print("PRELOAD_THREEWAY_SUMMARY", json.dumps({
        "upstream_direct": report["upstream_direct_ms"],
        "pre_migration_preloaded": report["medians_ms"]["baseline"],
        "migrated_preloaded": report["medians_ms"]["candidate"]}), flush=True)
  finally:
    (args.output / "matched.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
  main()
