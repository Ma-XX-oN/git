"""Measure shell validation and native environment changes on one Windows host.

Run from the repository root with --before-git EXE --after-git EXE --output DIR.
Both binaries must use identical compiler flags and the same native DLLs on PATH.
The three variants run in ABCCBA order using three workers and full context.
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
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pool_size_ab_benchmark as bench


def digest(path):
    """Return the SHA-256 identity of one measured source or executable."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    """Run serial matched rounds and retain raw samples plus comparison controls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-git", type=Path, required=True)
    parser.add_argument("--after-git", type=Path, required=True)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--bash", default=shutil.which("bash"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert os.name == "nt" and args.bash, "native Windows Python and Bash required"
    root = Path.cwd()
    source = root / "experiments/issue-809"
    args.output.mkdir(parents=True, exist_ok=True)
    bench.OUT = args.output
    environment = os.environ.copy()
    for name in ("GIT809_TRACE", "GIT_TRACE_GIT809_PRELOAD", "GIT809_NATIVE_TRACE_DIR",
                 "GIT809_TIMING_LOG", "GIT809_ABLATION_CWD", "GIT809_ABLATION_ENV",
                 "GIT_TRACE", "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF"):
        environment.pop(name, None)
    environment.update(GIT809_PLATFORM="windows",
                       GIT809_MANAGER_PYTHON=sys.executable, MSYS2_ARG_CONV_EXCL="*")
    report = {
        "sha": environment.get("GITHUB_SHA"), "baseline_sha": args.baseline_sha,
        "run_id": environment.get("GITHUB_RUN_ID"),
        "job": environment.get("GITHUB_JOB"), "host": environment.get("COMPUTERNAME"),
        "python": sys.executable, "bash": args.bash, "cwd": str(root),
        "path": environment["PATH"], "workers": 3, "tracing": False,
        "full_cwd_environment": True, "samples_per_round": bench.N,
        "round_order": ["A", "B", "C", "C", "B", "A"],
        "variants": {"A": "baseline native + regex validation",
                     "B": "baseline native + pattern validation",
                     "C": "changed-only native + pattern validation"},
        "source_sha256": {name: digest(source / name) for name in (
            "git-manager.sh", "git-ipc-request.sh", "windows_git_ipc_pool.py")},
        "build_flags": {label: (exe.parent / "GIT-CFLAGS").read_text(encoding="utf-8")
                        if (exe.parent / "GIT-CFLAGS").exists() else None
                        for label, exe in (("before", args.before_git),
                                           ("after", args.after_git))},
        "rounds": [],
    }
    with tempfile.TemporaryDirectory(prefix="git809-matched-") as directory:
        temp = Path(directory)
        for label, binary in (("base", args.before_git), ("test", args.after_git)):
            (temp / label).mkdir()
            shutil.copy2(binary, temp / label / "git.exe")
        for label in ("slow", "fast"):
            (temp / label).mkdir()
            for name in ("git-manager.sh", "git-ipc-request.sh", "windows_git_ipc_pool.py"):
                shutil.copy2(source / name, temp / label / name)
        control = temp / "slow/git-manager.sh"
        text = control.read_text(encoding="utf-8")
        patterns = ('  [[ $fd == [3-9] && $marker == [0-9] &&\n'
                    '     $retries == [1-9]*([0-9]) ]] || return 2')
        regex = ('  [[ $fd =~ ^[3-9]$ && $marker =~ ^[0-9]$ &&\n'
                 '     $retries =~ ^[1-9][0-9]*$ ]] || return 2')
        assert text.count(patterns) == 1, "validation control no longer matches"
        with control.open("w", encoding="utf-8", newline="\n") as file:
            file.write(text.replace(patterns, regex))
        report["control_sha256"] = digest(control)
        report["binary_sha256"] = {label: digest(temp / label / "git.exe")
                                    for label in ("base", "test")}
        try:
            for index, variant in enumerate(report["round_order"], 1):
                binary = temp / ("test" if variant == "C" else "base") / "git.exe"
                client = temp / ("slow" if variant == "A" else "fast") / "git-manager.sh"
                env = environment.copy()
                env["GIT809_WORKER_GIT"] = str(binary)
                # Existing benchmark verifies READY counts, allocation and output.
                result = bench.run_case(args.bash, str(client), str(binary),
                                        env, 3, index, True)
                result["variant"] = variant
                report["rounds"].append(result)
                print("MATCHED_ROUND", variant, index,
                      result["ipc"]["median_ms"], flush=True)
            report["medians_ms"] = {
                variant: statistics.median(value for row in report["rounds"]
                                           if row["variant"] == variant
                                           for value in row["ipc"]["samples_ms"])
                for variant in report["variants"]}
            medians = report["medians_ms"]
            report["reductions_ms"] = {"shell_A_to_B": medians["A"] - medians["B"],
                                        "native_B_to_C": medians["B"] - medians["C"],
                                        "combined_A_to_C": medians["A"] - medians["C"]}
            print("MATCHED_SUMMARY", json.dumps(report["medians_ms"]),
                  json.dumps(report["reductions_ms"]), flush=True)
        finally:
            with (args.output / "matched-results.json").open(
                    "w", encoding="utf-8", newline="\n") as file:
                json.dump(report, file, indent=2)
                file.write("\n")


if __name__ == "__main__":
    main()
