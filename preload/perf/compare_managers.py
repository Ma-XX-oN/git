#!/usr/bin/env python3
"""Matched Python/C++ manager comparison, preserving raw per-call and resource data.

Build preload-manager, preload/perf/manager-protocol and preload/perf/ready-worker.
Run --git PATH --output NEW_DIRECTORY. Defaults implement five ABBA blocks,
50 warmups, 500 samples, five 10-second concurrency rounds and 30 sessions.
--blocks/--count/--duration/--sessions permit diagnostic runs, labeled in JSON.
Python baseline sources are exported from --baseline-ref, never taken from a
different worker build. psutil is required for separate resource measurements.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "preload/tests"))
from test_manager import Session, WINDOWS
from acceptance import shell_path


def run(command, **kwargs):
    """Run a bounded child, returning stdout or raising with its stderr."""
    result = subprocess.run(command, capture_output=True, text=True, timeout=kwargs.pop("timeout", 240), **kwargs)
    if result.returncode:
        raise RuntimeError(f"{command}: {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def export_baseline(ref, destination):
    """Export only matching baseline clients/managers/test driver; retain hashes."""
    files = ["preload/client/manager.sh", "preload/client/manager.ps1", "preload/src/client/pipe.cs",
             "preload/manager/pool.py", "preload/manager/manager_windows.py",
             "preload/manager/manager_unix.py", "preload/tests/test_powershell.ps1"]
    hashes = {}
    for name in files:
        data = subprocess.check_output(["git", "show", f"{ref}:{name}"], cwd=ROOT)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    return hashes


def summary(rows):
    """Summarize calls and bootstrap run medians, preserving runs as sampling units."""
    samples = sorted(sample for row in rows for sample in row["samples_ms"])
    medians = [statistics.median(row["samples_ms"]) for row in rows]
    rng = random.Random(1701)
    boot = sorted(statistics.median(rng.choices(medians, k=len(medians))) for _ in range(2000))
    return {"calls": len(samples), "median_ms": statistics.median(samples),
            "p95_ms": samples[math.ceil(.95 * len(samples)) - 1],
            "p99_ms": samples[math.ceil(.99 * len(samples)) - 1],
            "round_median_ms": statistics.median(medians),
            "round_median_95ci_ms": [boot[50], boot[1949]],
            "throughput_calls_per_second": sum(len(row["samples_ms"]) for row in rows) /
                sum(row["measured_wall_seconds"] for row in rows)}


def comparison(rows):
    """Compare variants and bootstrap ratios of independent run medians."""
    by_variant = {name: [row for row in rows if row["variant"] == name] for name in ("python", "cpp")}
    stats = {name: summary(values) for name, values in by_variant.items()}
    rng = random.Random(1701)
    medians = {name: [statistics.median(row["samples_ms"]) for row in values] for name, values in by_variant.items()}
    ratios = sorted(100 * statistics.median(rng.choices(medians["cpp"], k=len(medians["cpp"]))) /
                    statistics.median(rng.choices(medians["python"], k=len(medians["python"]))) for _ in range(2000))
    ratio = 100 * stats["cpp"]["median_ms"] / stats["python"]["median_ms"]
    stats.update(cpp_percent_of_python=ratio, change_percent=ratio - 100,
                 speedup=100 / ratio, percent_95ci=[ratios[50], ratios[1949]],
                 conclusion="faster" if ratios[1949] < 100 else "slower" if ratios[50] > 100 else "inconclusive")
    return stats


BASH = r'''
source "$1" || exit 10
preload "$2" || exit 11
for ((i=0;i<$3;i++)); do git --version >/dev/null || exit 12; done
for ((i=0;i<$4;i++)); do
  if [[ $5 != sustained ]]; then "$2" --version >/dev/null || exit 13; fi
  a=$EPOCHREALTIME
  if [[ $5 == subshell ]]; then value=$(git --version) || exit 14
  else git --version >/dev/null || exit 15; fi
  b=$EPOCHREALTIME
  printf '%s %s\n' "$a" "$b"
done
_preload_stop || exit 16
'''


def client_round(args, baseline, variant, workload, output):
    """Time real clients with identical calls and fixed direct-call refill pacing."""
    source = baseline if variant == "python" else ROOT
    env = os.environ.copy()
    env["PRELOAD_PYTHON"] = sys.executable
    env["PRELOAD_MANAGER"] = str(args.manager)
    if workload.startswith("powershell"):
        command = [shutil.which("pwsh"), "-NoProfile", "-File", str(source / "preload/tests/test_powershell.ps1"),
                   "-Git", str(args.git), "-Output", str(output), "-Mode", "Preload", "-Count", str(args.count)]
        command += ["-Python", sys.executable] if variant == "python" else ["-Manager", str(args.manager)]
        if workload.endswith("discard"): command.append("-DiscardOutput")
        # Both snapshots use the same explicit warmup count; this is test-driver configuration.
        script = source / "preload/tests/test_powershell.ps1"
        original = script.read_text(encoding="utf-8")
        if variant == "python":
            script.write_text(original.replace("$i = -20", f"$i = -{args.warmup}"), encoding="utf-8")
        else:
            command += ["-Warmup", str(args.warmup)]
        started = time.perf_counter()
        run(command, env=env)
        elapsed = time.perf_counter() - started
        values = json.loads((output / "report.json").read_text(encoding="utf-8-sig"))["SamplesMs"]
        return {"samples_ms": values, "measured_wall_seconds": sum(values) / 1000,
                "session_wall_seconds": elapsed, "throughput_basis": "sum of timed calls, excludes refill pacing"}
    from decimal import Decimal
    text = run([shutil.which("bash"), "--noprofile", "--norc", "-c", BASH, "compare-managers",
                shell_path(source / "preload/client/manager.sh"), shell_path(args.git),
                str(args.warmup), str(args.count), "sustained" if workload == "shell-sustained" else workload], env=env)
    values = [float((Decimal(b) - Decimal(a)) * 1000) for a, b in (line.split() for line in text.splitlines())]
    return {"samples_ms": values, "measured_wall_seconds": sum(values) / 1000,
            "throughput_basis": "sum of timed calls, excludes refill pacing"}


def native_round(args, command, directory, threads=1, duration=0, pace=0, resources=False):
    """Use the native protocol harness; resource sampling is a separate run."""
    import psutil
    directory.mkdir()
    started = time.perf_counter()
    session = Session(directory, command, startup_poll=.001)
    launch_ms = (time.perf_counter() - started) * 1000
    try:
        started = time.perf_counter()
        registration = f"PRELOAD probe {args.fixture}" if WINDOWS else f"PRELOAD probe {{slot}} {args.fixture}"
        assert session.request(registration) == "OK", session.read_log()
        registration_ms = (time.perf_counter() - started) * 1000
        process = psutil.Process(session.process.pid)
        resource_data = None
        if resources:
            before_idle = process.cpu_times()
            time.sleep(1)
            after_idle = process.cpu_times()
            resource_data = {"idle_cpu_seconds_per_second": (after_idle.user + after_idle.system - before_idle.user - before_idle.system)}
        harness = [str(args.harness), "--directory", str(directory), "--pipe-dir", session.pipe_dir,
                   "--count", str(args.count), "--warmup", str(args.warmup), "--threads", str(threads),
                   "--duration", str(duration), "--pace", str(pace)]
        before = process.cpu_times()
        start = time.perf_counter()
        child = subprocess.Popen(harness, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        snapshots = []
        with ThreadPoolExecutor(max_workers=1) as executor:
            completed = executor.submit(child.communicate, timeout=120)
            if resources:
                while not completed.done():
                    memory = process.memory_info()
                    snapshots.append({"rss": memory.rss, "private": getattr(memory, "private", None),
                                      "threads": process.num_threads(),
                                      "handles_or_fds": process.num_handles() if WINDOWS else process.num_fds()})
                    time.sleep(.05)
            stdout, stderr = completed.result()
        assert child.returncode == 0, (stderr, session.read_log())
        after = process.cpu_times()
        row = json.loads(stdout)
        row.update(launch_ms=launch_ms, registration_ms=registration_ms)
        if resources:
            cpu = after.user + after.system - before.user - before.system
            resource_data.update(manager_cpu_seconds=cpu, observation_wall_seconds=time.perf_counter() - start,
                                 cpu_ms_per_completed_call=cpu * 1000 / (len(row["samples_ms"]) + args.warmup * threads),
                                 completed_calls_including_warmup=len(row["samples_ms"]) + args.warmup * threads,
                                 cpu_interval_includes_warmup=True, snapshots=snapshots)
            row["resources"] = resource_data
        return row
    finally:
        started = time.perf_counter()
        session.stop()
        if "row" in locals(): row["shutdown_ms"] = (time.perf_counter() - started) * 1000


def report(args, data):
    """Persist partial evidence after every round; regenerate concise tables and chart."""
    (args.output / "comparison.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    lines = ["# Python versus C++ preload manager", "", "Same worker binary and machine for both variants. Lower latency is better.", "",
             "| Workload | Python median ms | C++ median ms | C++ / Python | Change | p95 Python / C++ ms | Conclusion |",
             "|---|---:|---:|---:|---:|---:|---|"]
    for name, stats in data["summary"].items():
        lines.append(f"| {name} | {stats['python']['median_ms']:.4f} | {stats['cpp']['median_ms']:.4f} | "
                     f"{stats['cpp_percent_of_python']:.2f}% | {stats['change_percent']:+.2f}% | "
                     f"{stats['python']['p95_ms']:.4f} / {stats['cpp']['p95_ms']:.4f} | {stats['conclusion']} |")
    lines += ["", "Intervals bootstrap whole-run medians (2000 resamples, fixed seed); raw per-call p95/p99 and throughput are in comparison.json.",
              "Native throughput includes connection setup and fixture disposal. Client throughput excludes deliberate direct-call refill pacing.",
              "Manager-only runs use a READY/lifetime fixture; real command behavior is measured in the client runs.",
              "", "## Machine and controls", "", "```json", json.dumps(data["metadata"], indent=2), "```"]
    (args.output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if data["summary"]:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        names = list(data["summary"])
        figure, axis = plt.subplots(figsize=(9, max(3, len(names) * .45)))
        values = [data["summary"][name]["cpp_percent_of_python"] for name in names]
        axis.barh(names, values)
        axis.axvline(100, color="black", linestyle="--", label="Python = 100%")
        axis.set_xlabel("C++ median latency as % of Python (lower is better)")
        axis.legend()
        figure.tight_layout()
        figure.savefig(args.output / "comparison.png", dpi=160)
        plt.close(figure)


def main():
    """Run configured workloads and retain diagnostics on interruption/failure."""
    suffix = ".exe" if WINDOWS else ""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git", type=Path, required=True)
    parser.add_argument("--manager", type=Path, default=ROOT / f"preload-manager{suffix}")
    parser.add_argument("--harness", type=Path, default=ROOT / f"preload/perf/manager-protocol{suffix}")
    parser.add_argument("--fixture", type=Path, default=ROOT / f"preload/perf/ready-worker{suffix}")
    parser.add_argument("--baseline-ref", default="2c271ea5357d7993d3f82affdb348aa2ad1f1ddc")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--blocks", type=int, default=5)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--sessions", type=int, default=30)
    parser.add_argument("--workloads", nargs="+", default=["ready", "serial", "concurrent-2", "concurrent-8", "concurrent-32", "shortage",
        "shell", "shell-sustained", "subshell", *(["powershell-capture", "powershell-discard"] if WINDOWS else []), "resources", "sessions"])
    args = parser.parse_args()
    for key in list(os.environ):
        if key.startswith("GIT_TRACE") or key in ("PRELOAD_TIMING", "EXP_PRELOAD_CONNECT"):
            os.environ.pop(key)
    if min(args.blocks, args.count, args.duration, args.sessions) < 1 or args.warmup < 0: parser.error("invalid counts")
    for name in ("git", "manager", "fixture", "harness"): setattr(args, name, getattr(args, name).resolve(strict=True))
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    baseline = args.output / "baseline"
    sources = export_baseline(args.baseline_ref, baseline)
    commands = {"python": [sys.executable, str(baseline / "preload/manager" / ("manager_windows.py" if WINDOWS else "manager_unix.py"))],
                "cpp": [str(args.manager)]}
    metadata = {"os": platform.platform(), "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
        "python": sys.version, "baseline_ref": args.baseline_ref,
        "candidate_ref": run(["git", "rev-parse", "HEAD"], cwd=ROOT).strip(), "candidate_has_working_tree_changes": True,
        "baseline_hashes": sources, "parameters": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "sha256": {name: hashlib.sha256(getattr(args, name).read_bytes()).hexdigest() for name in ("git", "manager", "fixture", "harness")},
        "compiler": run([os.environ.get("CXX", "g++"), "--version"]).splitlines()[0],
        "compiler_flags": "-O2 -g -Wall -Wextra -std=c++17", "tracing": False,
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in ("preload/src/manager", "preload/src/shared", "preload/include/manager", "preload/include/shared")
            for path in (ROOT / directory).iterdir() if path.suffix in (".c", ".cpp", ".h", ".hpp")},
        "bash": run([shutil.which("bash"), "--version"]).splitlines()[0]}
    import psutil
    metadata["physical_memory_bytes"] = psutil.virtual_memory().total
    flags = args.git.parent / "GIT-CFLAGS"
    if flags.exists(): metadata["worker_build_flags"] = flags.read_text(encoding="utf-8").strip()
    if WINDOWS:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            metadata["cpu"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    if WINDOWS: metadata["powershell"] = run(["pwsh", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"]).strip()
    data = {"metadata": metadata, "rounds": [], "summary": {}, "resources": [], "sessions": []}
    try:
        for workload in args.workloads:
            if workload == "sessions":
                for i in range(args.sessions):
                    for variant in (["python", "cpp"] if i % 2 == 0 else ["cpp", "python"]):
                        saved_count, saved_warmup = args.count, args.warmup
                        args.count, args.warmup = 1, 0
                        row = native_round(args, commands[variant], args.output / f"session-{i}-{variant}")
                        args.count, args.warmup = saved_count, saved_warmup
                        data["sessions"].append({"variant": variant, **{key: row[key] for key in ("launch_ms", "registration_ms", "shutdown_ms")}})
                        report(args, data)
                continue
            if workload == "resources":
                for threads in (1, 32):
                    for variant in ("python", "cpp"):
                        row = native_round(args, commands[variant], args.output / f"resources-{threads}-{variant}", threads, args.duration, resources=True)
                        data["resources"].append({"variant": variant, "threads": threads, **row})
                        report(args, data)
                continue
            order = ["python", "cpp", "cpp", "python"] * args.blocks
            if workload.startswith("concurrent-"):
                order = [variant for i in range(args.blocks) for variant in (["python", "cpp"] if i % 2 == 0 else ["cpp", "python"])]
            for index, variant in enumerate(order):
                directory = args.output / f"{workload}-{index}-{variant}"
                if workload in ("shell", "shell-sustained", "subshell") or workload.startswith("powershell"):
                    row = client_round(args, baseline, variant, workload, directory)
                else:
                    threads = int(workload.split("-")[1]) if workload.startswith("concurrent-") else 32 if workload == "shortage" else 1
                    duration = args.duration if workload.startswith("concurrent-") else 0
                    saved_count, saved_warmup = args.count, args.warmup
                    if workload == "shortage": args.count, args.warmup = 1, 0
                    row = native_round(args, commands[variant], directory, threads, duration, 5 if workload == "ready" else 0)
                    args.count, args.warmup = saved_count, saved_warmup
                row.update(variant=variant, workload=workload, round=index)
                data["rounds"].append(row)
                report(args, data)
                print(workload, index, variant, statistics.median(row["samples_ms"]), flush=True)
            data["summary"][workload] = comparison([row for row in data["rounds"] if row["workload"] == workload])
            report(args, data)
    finally:
        report(args, data)


if __name__ == "__main__":
    main()
