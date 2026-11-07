#!/usr/bin/env python3
"""#809 Unix: actual prestarted Git worker vs direct source-built Git."""
from __future__ import annotations
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

from unix_git_worker_build import build

N = 120
OUT = Path("git809-bash-pipe-results")
SCRIPT = r'''
  source "$1" 3 || exit 31
  trap 'rc=$?; if (( rc != 0 )); then printf "UNIX_MANAGER_DIAGNOSTIC rc=%s\n" "$rc" >&2; cat "$_GIT809_UNIX_MANAGER_LOG" >&2; fi' EXIT
  gitbin=$2
  expected=$("$gitbin" --version) || exit 32
  actual=$(git-ipc --version) || exit 33
  [[ $actual == "$expected" ]] || exit 34
  for ((i=0;i<120;i++)); do
    if (( i % 2 == 0 )); then
      a=$EPOCHREALTIME
      "$gitbin" --version >/dev/null || exit 41
      b=$EPOCHREALTIME
      c=$EPOCHREALTIME
      git-ipc --version >/dev/null || exit 42
      d=$EPOCHREALTIME
    else
      c=$EPOCHREALTIME
      git-ipc --version >/dev/null || exit 43
      d=$EPOCHREALTIME
      a=$EPOCHREALTIME
      "$gitbin" --version >/dev/null || exit 44
      b=$EPOCHREALTIME
    fi
    printf 'PAIR %s %s %s %s %s\n' "$i" "$a" "$b" "$c" "$d"
  done
  expected=$("$gitbin" help -a) || exit 45
  actual=$(git-ipc help -a) || exit 46
  [[ $actual == "$expected" ]] || exit 47
  shopt -s expand_aliases
  eval 'git --version' || exit 48
  printf 'ALIAS_PASS\n'
  printf 'MANAGER_BEGIN\n'
  cat "$_GIT809_UNIX_MANAGER_LOG"
  printf 'MANAGER_END\n'
'''

def stats(values):
    assert len(values) == N, f"expected {N} samples, got {len(values)}"
    sorted_values = sorted(values)
    return {
        "median_ms": statistics.median(sorted_values),
        "p95_ms": sorted_values[math.ceil(.95 * N) - 1],
        "min_ms": sorted_values[0],
        "max_ms": sorted_values[-1],
        "samples_ms": values,
    }

def main():
    if os.name == "nt":
        raise RuntimeError("Unix worker benchmark cannot run on Windows")
    OUT.mkdir(exist_ok=True)
    bash = shutil.which("bash")
    if not bash:
        raise RuntimeError("Bash not found")
    probe = subprocess.run(
        [bash, "--noprofile", "--norc", "-c",
         '[[ $EPOCHREALTIME =~ ^[0-9]+\\.[0-9]+$ ]]'],
        capture_output=True)
    if probe.returncode:
        raise RuntimeError("Bash lacks EPOCHREALTIME")
    root = Path.cwd()
    git, build_command = build(root)
    report = {
        "sha": os.getenv("GITHUB_SHA"), "runner_os": os.getenv("RUNNER_OS"),
        "git_binary": str(git), "build_command": build_command,
        "samples": N, "startup_included": False,
        "method": "one persistent Bash; direct Git and four-FIFO prestarted Git paired",
    }
    try:
        env = os.environ.copy()
        env["GIT809_WORKER_GIT"] = str(git)
        env["GIT809_MANAGER_PYTHON"] = sys.executable
        proc = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", SCRIPT, "benchmark",
             str(root / "experiments/issue-809/git-manager-unix.sh"),
             str(git)],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=180,
            env=env)
        report["bash_rc"] = proc.returncode
        report["stdout"] = proc.stdout.decode("utf-8", "replace")
        report["stderr"] = proc.stderr.decode("utf-8", "replace")
        if proc.returncode:
            raise AssertionError("Unix Bash benchmark failed rc=" +
                                 str(proc.returncode) + "\n" + report["stderr"][-2500:])
        direct, ipc = [], []
        for line in report["stdout"].splitlines():
            fields = line.split(" ")
            if fields[0] == "PAIR" and len(fields) == 6:
                _, index, a, b, c, d = fields
                if int(index) != len(direct):
                    raise AssertionError("pair index sequence mismatch")
                direct.append(float((Decimal(b) - Decimal(a)) * 1000))
                ipc.append(float((Decimal(d) - Decimal(c)) * 1000))
        report["direct"] = stats(direct)
        report["ipc"] = stats(ipc)
        if "ALIAS_PASS" not in report["stdout"]:
            raise AssertionError("alias invocation not verified")
        manager = report["stdout"].split("MANAGER_BEGIN\n", 1)[1].split(
            "\nMANAGER_END", 1)[0]
        allocated = [row for row in manager.splitlines()
                     if row.startswith("ALLOCATED ")]
        if len(allocated) != N + 3:
            raise AssertionError(f"expected {N+3} allocations, got {len(allocated)}")
        unique_pids = set(allocated)
        if len(unique_pids) != len(allocated):
            raise AssertionError("worker PID reused")
        report["allocations"] = len(allocated)
        report["speedup"] = report["direct"]["median_ms"] / report["ipc"]["median_ms"]
        print("UNIX_IPC_BENCHMARK " + json.dumps({
            "sha": report["sha"], "runner_os": report["runner_os"],
            "direct": {k: v for k, v in report["direct"].items() if k != "samples_ms"},
            "ipc": {k: v for k, v in report["ipc"].items() if k != "samples_ms"},
            "allocations": report["allocations"], "speedup": report["speedup"],
        }), flush=True)
    finally:
        target = OUT / ("unix-ipc-" + (os.getenv("RUNNER_OS") or sys.platform).lower() + ".json")
        target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("UNIX_IPC_REPORT " + str(target), flush=True)

if __name__ == "__main__":
    main()
