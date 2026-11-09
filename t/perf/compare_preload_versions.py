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


def main():
    """Retain ABBA raw timings, binary identities and matching build flags."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("preload-comparison"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    windows = os.name == "nt"
    report = {"baseline": "working-version-two", "workers": 3,
              "host": os.environ.get("COMPUTERNAME", os.uname().nodename if not windows else ""),
              "platform": sys.platform, "tracing": False, "full_context": True,
              "machine": {"os": platform.platform(), "architecture": platform.machine(),
                          "processor": platform.processor(), "logical_cpus": os.cpu_count(),
                          "python": sys.version, "bash": subprocess.check_output(
                              [shutil.which("bash"), "--version"], text=True).splitlines()[0],
                          "ci_run": os.environ.get("GITHUB_RUN_ID"),
                          "ci_job": os.environ.get("GITHUB_JOB")},
              "order": ["baseline", "candidate", "candidate", "baseline"], "rounds": [],
              "variants": {}}
    variants = {}
    for name, root in (("baseline", args.baseline.resolve()), ("candidate", args.candidate.resolve())):
        old = name == "baseline"
        directory = root / ("experiments/issue-809" if old else "contrib/preload")
        benchfile = directory / "pool_size_ab_benchmark.py" if old else root / "t/perf/preload_pool_benchmark.py"
        bench = load_benchmark(benchfile, "benchmark_" + name)
        bench.OUT = args.output
        git = root / ("git.exe" if windows else "git")
        environment = os.environ.copy()
        for key in list(environment):
            if key.startswith(("GIT809_", "GIT_PRELOAD_", "GIT_TRACE")):
                environment.pop(key)
        prefix = "GIT809" if old else "GIT_PRELOAD"
        environment[prefix + "_PLATFORM"] = "windows" if windows else "unix"
        environment[prefix + "_MANAGER_PYTHON"] = sys.executable
        environment[prefix + "_WORKER_GIT"] = str(git)
        environment["MSYS2_ARG_CONV_EXCL"] = "*"
        client = directory / "git-manager.sh"
        variants[name] = (bench, str(client), str(git), environment)
        report["variants"][name] = {
            "git": str(git), "binary_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
            "revision": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
            "flags": (root / "GIT-CFLAGS").read_text(encoding="utf-8")}
    assert report["variants"]["baseline"]["flags"] == report["variants"]["candidate"]["flags"], (
        "baseline and candidate compiler flags differ")
    try:
        for index, name in enumerate(report["order"]):
            bench, client, git, environment = variants[name]
            row = bench.run_case(shutil.which("bash"), client, git, environment, 3, index, windows)
            row["variant"] = name
            report["rounds"].append(row)
            print("PRELOAD_MATCHED_ROUND", name, row["ipc"]["median_ms"], flush=True)
        report["medians_ms"] = {
            name: statistics.median(sample for row in report["rounds"] if row["variant"] == name
                                    for sample in row["ipc"]["samples_ms"])
            for name in variants}
        report["reduction_ms"] = report["medians_ms"]["baseline"] - report["medians_ms"]["candidate"]
        report["candidate_percent_of_baseline"] = (
            100 * report["medians_ms"]["candidate"] / report["medians_ms"]["baseline"])
        report["change_percent"] = report["candidate_percent_of_baseline"] - 100
        print("PRELOAD_MATCHED_SUMMARY", json.dumps(report["medians_ms"]), flush=True)
    finally:
        (args.output / "matched.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
