#!/usr/bin/env python3
"""Profile the real Unix shell/manager exchange without changing production code.

Run with --git PATH --output DIRECTORY on Linux or macOS. Retains stage-only
and nested wall/thread-CPU measurements for shell and subshell workloads,
optional syscall and Linux scheduler traces, source identities and summaries.
Instrumentation overhead is reported; traced durations are not speedup claims.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import statistics
import subprocess
import sys

from compare_preload_versions import machine_metadata

ROOT = Path(__file__).resolve().parents[2]
STAGES = ROOT / "t/perf/preload_stage_timing.py"
PROFILER = ROOT / "t/perf/profile_preload_manager.py"


def run(command, output):
    """Run one diagnostic and retain stdout/stderr; return its exit status.

    Parameters
    ----------
    command : list of str
        Argument vector, without shell evaluation.
    output : Path
        File receiving combined stdout/stderr. Parent must already exist.
    """
    with output.open("wb") as log:
        return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                              timeout=600, check=False).returncode


def launcher(directory, focus="all"):
    """Create a manager interpreter launcher and return its executable path.

    Parameters
    ----------
    directory : Path
        Run directory, also receiving manager-profile.json at manager shutdown.
    focus : str
        all, root, or a single operation label for lighter boundary probing.
    """
    path = directory / "profile-python"
    argv = [sys.executable, str(PROFILER), str(directory / "manager-profile.json"), focus]
    path.write_text("#!/bin/sh\nexec " + shlex.join(argv) + ' "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def stage_command(args, directory, workload, nested=False, count=None, focus="all"):
    """Construct the real stage benchmark command; optionally profile its manager.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed worker path and positive sample count.
    directory : Path
        Existing output directory for this diagnostic pass.
    workload : str
        shell or subshell, retaining the production Bash call path.
    nested : bool
        Enable nested manager spans using a launcher interpreter.
    count : int, optional
        Override the sample count for intrusive kernel tracing.
    focus : str
        Operation label, all, or root; used only with nested=True.

    Returns
    -------
    list of str
        Child process argument vector.
    """
    command = [sys.executable, str(STAGES), "--git", str(args.git), "--count",
               str(count or args.count), "--workload", workload, "--output", str(directory)]
    if nested:
        command += ["--manager-python", str(launcher(directory, focus))]
    return command


def summarize_profile(path, warmup=20):
    """Aggregate spans per measured GET, keeping CPU and wall time distinct.

    Parameters
    ----------
    path : Path
        Raw manager profile JSON.
    warmup : int
        Number of initial dispatch records to exclude.

    Returns
    -------
    dict
        Per-call means and medians in microseconds; self values exclude direct
        child spans but include instrumentation bookkeeping. Waiting is wall
        minus thread CPU, not a claim about its scheduling or blocking cause.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = [r for r in raw["records"] if r["kind"] == "Server.handle"][warmup:]
    values = defaultdict(list)
    for record in records:
        children = defaultdict(lambda: [0, 0])
        for event in record["events"]:
            children[event["parent"]][0] += event["end_ns"] - event["start_ns"]
            children[event["parent"]][1] += event["cpu_ns"]
        totals = defaultdict(lambda: [0, 0, 0, 0, 0])
        for index, event in enumerate(record["events"]):
            wall, cpu = event["end_ns"] - event["start_ns"], event["cpu_ns"]
            for metric, value in enumerate((wall, cpu, wall - children[index][0],
                                             cpu - children[index][1], 1)):
                totals[event["name"]][metric] += value
        for name, totals_for_call in totals.items():
            values[name].append(totals_for_call)
    metrics = ("wall_us", "cpu_us", "self_wall_us", "self_cpu_us", "calls")
    summary = {name: {metric: {"median": statistics.median(row[i] for row in rows) / scale,
                               "mean": statistics.mean(row[i] for row in rows) / scale}
                       for i, metric in enumerate(metrics)
                       for scale in [1 if metric == "calls" else 1000]}
               for name, rows in values.items()}
    return {"dispatches": len(records), "calibration": raw["calibration"],
            "queued_attempts_total": sum(r["kind"] == "Server._get" for r in raw["records"]),
            "operations": summary}


def kernel_traces(args, output):
    """Collect separate intrusive traces, reporting unavailable facilities explicitly.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed worker path and count.
    output : Path
        Parent run directory. Each tracing pass uses its own session and outputs.

    Returns
    -------
    dict
        Tracer exit statuses and raw paths; absence is never reported as success.
    """
    results = {}
    for workload in ("shell", "subshell"):
        directory = output / ("syscalls-" + workload)
        directory.mkdir()
        command = stage_command(args, directory, workload, count=40)
        if sys.platform == "linux":
            prefix = ["strace", "-f", "-ttt", "-T", "-yy", "-o", str(directory / "trace.txt")]
        else:
            prefix = []
            command += ["--shell-tracer", "dtruss"]
        results[workload] = {"status": run(prefix + command, directory / "tracer.log"),
                             "workload_completed": (directory / "paced/stages.json").exists()}
    if sys.platform == "linux":
        directory = output / "scheduler"
        directory.mkdir()
        candidates = sorted(Path("/usr/lib/linux-tools").glob("*/perf"))
        candidates += sorted(Path("/usr/lib").glob("linux-tools*/perf"))
        if not candidates and shutil.which("perf"):
            candidates = [Path(shutil.which("perf"))]
        if candidates:
            perf = str(candidates[-1])
            command = stage_command(args, directory, "shell", nested=True, count=100, focus="os.write")
            prefix = ["sudo", "-n", perf, "record", "-a", "--clockid", "mono",
                      "-e", "sched:sched_switch", "-e", "sched:sched_wakeup",
                      "-o", str(directory / "sched.data"), "--"]
            status = run(prefix + command, directory / "record.log")
            results["scheduler"] = {"status": status, "perf": perf}
            data = directory / "sched.data"
            if data.exists():
                subprocess.run(["sudo", "-n", "chmod", "0644", str(data)], check=True, timeout=30)
            if status == 0:
                with (directory / "sched.txt").open("w") as trace:
                    decoded = subprocess.run(["sudo", "-n", perf, "script", "--ns", "-i",
                                              str(directory / "sched.data")], stdout=trace,
                                             stderr=subprocess.PIPE, text=True, timeout=120)
                (directory / "decode.log").write_text(decoded.stderr, encoding="utf-8")
                results["scheduler"]["decode_status"] = decoded.returncode
        else:
            results["scheduler"] = {"status": "unavailable: no native perf binary"}
    return results


def focused_profiles(args, output, workload):
    """Measure selected boundaries separately to reduce profiler perturbation.

    Parameters
    ----------
    args : argparse.Namespace
        Worker path and sample count.
    output : Path
        Parent output directory.
    workload : str
        shell or subshell.

    Returns
    -------
    dict
        Independent cohorts for each boundary, including stage-only comparisons.
    """
    results = {}
    for focus in ("root", "Manager.allocate", "Mailbox.deliver", "Path.exists",
                  "os.stat", "os.write", "Path.__truediv__", "Path.__str__"):
        directory = output / (workload + "-focus-" + focus)
        directory.mkdir()
        command = stage_command(args, directory, workload, nested=True, focus=focus)
        if run(command, directory / "run.log") != 0:
            raise RuntimeError("focused profile failed: " + str(directory))
        results[focus] = {"profile": summarize_profile(directory / "manager-profile.json"),
                          "stages": json.loads((directory / "paced/stages.json").read_text())}
    return results


def main():
    """Run both workloads and save profiling provenance, raw data and summaries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--count", type=int, default=400)
    args = parser.parse_args()
    if sys.platform not in ("linux", "darwin") or args.count < 1:
        parser.error("requires Linux/macOS and a positive count")
    args.git, args.output = args.git.resolve(strict=True), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"machine": machine_metadata(), "worker_sha256": hashlib.sha256(args.git.read_bytes()).hexdigest(),
              "sources": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in (ROOT / "contrib/preload").glob("*") if p.is_file()},
              "workloads": {}}
    for workload in ("shell", "subshell"):
        results = {}
        for mode in ("stage-only", "nested-profile"):
            directory = args.output / (workload + "-" + mode)
            directory.mkdir()
            command = stage_command(args, directory, workload, nested=mode == "nested-profile")
            if run(command, directory / "run.log") != 0:
                raise RuntimeError("stage measurement failed: " + str(directory / "run.log"))
            results[mode] = json.loads((directory / "paced/stages.json").read_text())
            if mode == "nested-profile":
                results["profile"] = summarize_profile(directory / "manager-profile.json")
                if results["profile"]["dispatches"] != args.count:
                    raise AssertionError("missing measured GET profiles")
        report["workloads"][workload] = results
        results["focused"] = focused_profiles(args, args.output, workload)
    report["kernel_traces"] = kernel_traces(args, args.output)
    (args.output / "profile-summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("PRELOAD_PROFILE_SUMMARY", json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
