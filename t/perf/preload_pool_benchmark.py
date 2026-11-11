#!/usr/bin/env python3
"""Paired direct versus preloaded --version latency through preload-manager.sh.

Each round sources the library, preloads the checked-out Git (3 initial
workers, growing on demand) and runs 120 sequential paired direct and
preloaded --version commands, alternating which runs first.
The actual Git executable is built from the checked-out source; no historical
source-injection build script or retired worker startup flag is used.
Each OS uses one Git build and one native CI runner.
Startup, manager creation and initial worker preparation are excluded from
per-command timings but reported separately.
Tracing is controlled by caller-supplied environment variables. Leave tracing
and phase timers unset for performance measurements.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

N = 120
RUN_ORDER = (3, 3, 3, 3, 3)
OUT = Path("git-preload-bash-pipe-results")

SHELL = r"""
  set -u
  count=$1
  manager_script=$2
  gitbin=$3
  # Directly timed binary; defaults to the worker build, may be unmodified Git.
  directbin=${4:-$gitbin}
  source "$manager_script" || exit 31
  preload "$gitbin" || exit 31
  manager_log=$PRELOAD_DIR/manager.log
  _pool_ab_diagnostics() {
    local exit_code=$?
    if (( exit_code != 0 )); then
      printf 'POOL_AB_FAILURE workers=%s rc=%s bash_pid=%s\\n' "$count" "$exit_code" "$BASHPID" >&2
      [[ -f $manager_log ]] && cat "$manager_log" >&2
    fi
  }
  trap _pool_ab_diagnostics EXIT
  expected=$("$gitbin" --version) || exit 33
  actual=$(git --version) || exit 34
  [[ $actual == "$expected" ]] || exit 35
  "$directbin" --version >/dev/null || exit 36
  printf 'READY_CHECK %s\n' "$count"
  for ((i=0;i<120;i++)); do
    if (( i % 2 == 0 )); then
      a=$EPOCHREALTIME
      "$directbin" --version >/dev/null || exit 41
      b=$EPOCHREALTIME
      c=$EPOCHREALTIME
      git --version >/dev/null || exit 42
      d=$EPOCHREALTIME
    else
      c=$EPOCHREALTIME
      git --version >/dev/null || exit 43
      d=$EPOCHREALTIME
      a=$EPOCHREALTIME
      "$directbin" --version >/dev/null || exit 44
      b=$EPOCHREALTIME
    fi
    printf 'PAIR %s %s %s %s %s\n' "$i" "$a" "$b" "$c" "$d"
  done
  printf 'MANAGER_BEGIN\n'
  cat "$manager_log"
  printf 'MANAGER_END\n'
"""

def summarize(values):
    """Retain raw latency samples and compute median and percentile values."""
    ordered = sorted(values)
    assert len(ordered) == N
    return {
        "median_ms": statistics.median(ordered),
        "p95_ms": ordered[math.ceil(.95 * N) - 1],
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
        "samples_ms": values,
    }

def delta(a, b):
    """Convert two decimal wall-clock timestamps to elapsed milliseconds."""
    return float((Decimal(b) - Decimal(a)) * 1000)

def prepare(root, windows):
    """Resolve the executable built by this checkout."""
    git = (root / ("git.exe" if windows else "git")).resolve(strict=True)
    return git, "native source-built Git from workflow checkout"

def run_case(bash, script, git_shell, environment, workers, round_index, windows,
             direct_git=None):
    """Run and validate one serial paired direct and IPC benchmark round.

    direct_git selects the directly timed executable, for example unmodified
    upstream Git; it defaults to git_shell, the build serving IPC workers.
    """
    started = time.perf_counter()
    process = subprocess.Popen(
        [bash, "--noprofile", "--norc", "-c", SHELL,
         "pool-ab", str(workers), script, git_shell, direct_git or git_shell],
        env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(timeout=180 if windows else 240)
    except subprocess.TimeoutExpired as exc:
        if windows:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           capture_output=True, timeout=15, check=False)
        if process.poll() is None:
            process.kill()
        process.communicate()
        stdout = (exc.stdout or b"").decode("utf-8", "replace")
        stderr = (exc.stderr or b"").decode("utf-8", "replace")
        complete = [row for row in stdout.splitlines() if row.startswith("PAIR ")]
        raise AssertionError(
            f"pool={workers} round={round_index} timeout; "
            f"pairs={len(complete)} last={complete[-1] if complete else None}; "
            f"stderr={stderr[-1200:]}") from exc
    elapsed = time.perf_counter() - started
    output = out.decode("utf-8", "strict")
    errors = err.decode("utf-8", "strict")
    if process.returncode:
        complete = [row for row in output.splitlines() if row.startswith("PAIR ")]
        raise AssertionError(
            f"pool={workers} round={round_index} rc={process.returncode} "
            f"pairs={len(complete)} stderr={errors[-2000:]}")
    rows = output.splitlines()
    assert f"READY_CHECK {workers}" in rows, "worker ready/response missing"
    direct, ipc = [], []
    for row in rows:
        fields = row.split(" ")
        if fields[0] != "PAIR":
            continue
        assert len(fields) == 6, repr(row)
        _, index, a, b, c, d = fields
        assert int(index) == len(direct), (index, len(direct))
        direct.append(delta(a,b))
        ipc.append(delta(c,d))
    manager = output.split("MANAGER_BEGIN\n", 1)[1].split("\nMANAGER_END", 1)[0]
    allocations = [x for x in manager.splitlines() if x.startswith("ALLOCATED ")]
    # one verification invocation plus 120 timed commands
    assert len(allocations) == N + 1, (workers, len(allocations), manager[-1200:])
    ready = [x for x in manager.splitlines() if x.startswith("WORKER_READY ")]
    assert len(ready) >= workers, "initial pool incomplete"
    # Numeric PIDs can be reused after exit; each allocation needs a READY event.
    allocated_by_pid = Counter(x.split()[-1] for x in allocations)
    ready_by_pid = Counter(x.split()[-1] for x in ready)
    assert all(ready_by_pid[pid] >= count for pid, count in allocated_by_pid.items()), (
        "worker allocation lacks corresponding READY event", allocated_by_pid, ready_by_pid)
    return {
        "workers": workers, "round": round_index,
        "direct_git": direct_git or git_shell,
        "direct": summarize(direct), "ipc": summarize(ipc),
        "allocated": len(allocations), "ready_events": len(ready),
        "elapsed_wall_seconds": elapsed,
        "startup_included_in_samples": False,
    }

def main():
    """Parse command-line options and run the requested validation or service."""
    root = Path.cwd()
    OUT.mkdir(exist_ok=True)
    windows = os.name == "nt"
    bash = shutil.which("bash")
    assert bash is not None, "Bash required"
    check = subprocess.run([bash, "--noprofile", "--norc", "-c",
                            '[[ $EPOCHREALTIME =~ ^[0-9]+\\.[0-9]+$ ]]'])
    assert check.returncode == 0, "Bash EPOCHREALTIME required"
    git, flags = prepare(root, windows)
    native = str(git)
    if windows:
        git_shell = subprocess.run(
            ["cygpath", "-u", native], check=True,
            capture_output=True, text=True, encoding="utf-8").stdout.strip()
        script = subprocess.run(
            ["cygpath", "-u", str(root / "contrib/preload/preload-manager.sh")],
            check=True, capture_output=True, text=True,
            encoding="utf-8").stdout.strip()
    else:
        git_shell = native
        script = str(root / "contrib/preload/preload-manager.sh")
    env = os.environ.copy()
    env["PRELOAD_PYTHON"] = env.get("PRELOAD_PYTHON") or sys.executable
    report = {
        "sha": env.get("GITHUB_SHA"),
        "os": env.get("RUNNER_OS", sys.platform),
        "git": native,
        "build_flags": flags,
        "samples_per_case": N,
        "round_order": RUN_ORDER,
        "reader_timeout_ms": 100,
        "workload": "120 paired direct/IPC --version operations per round",
        "source_injection": False,
        "initial_pool_size": 3,
        "manager_present_in_both_configurations": True,
        "results": [],
    }
    output_path = OUT / ("preload-paired-" + report["os"].lower() + ".json")
    try:
        for round_index, workers in enumerate(RUN_ORDER, 1):
            result = run_case(bash, script, git_shell, env, workers, round_index, windows)
            report["results"].append(result)
            print("POOL_SIZE_AB " + json.dumps({
                "sha": report["sha"], "os": report["os"],
                "round": round_index, "workers": workers,
                "direct_median_ms": result["direct"]["median_ms"],
                "ipc_median_ms": result["ipc"]["median_ms"],
                "ipc_p95_ms": result["ipc"]["p95_ms"],
                "allocated": result["allocated"],
                "wall_seconds": result["elapsed_wall_seconds"],
            }), flush=True)
    finally:
        output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("POOL_SIZE_AB_ARTIFACT " + str(output_path), flush=True)

if __name__ == "__main__":
    main()
