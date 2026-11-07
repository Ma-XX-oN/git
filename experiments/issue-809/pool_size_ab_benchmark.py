#!/usr/bin/env python3
"""Issue 809 matched-runner comparison: 1, 2, 3, 4 and 5 workers.

This changes only the configured number of preinitialized workers. Every
configuration runs 120 sequential paired direct and IPC --version commands.
Round order 1,2,3,4,5,5,4,3,2,1 reduces time-order bias. Each OS uses one Git build and one native CI runner.
Startup, manager creation and initial worker preparation are excluded from
per-command timings but reported separately.
"""
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
import time

N = 500
RUN_ORDER = (2, 2, 2, 2, 2, 2, 2, 2)
OUT = Path("git809-bash-pipe-results")

SHELL = r"""
  set -u
  count=$1
  manager_script=$2
  gitbin=$3
  source "$manager_script" "$count" || exit 31
  _pool_ab_diagnostics() {
    local exit_code=$?
    if (( exit_code != 0 )); then
      printf 'POOL_AB_FAILURE workers=%s rc=%s bash_pid=%s\\n' "$count" "$exit_code" "$BASHPID" >&2
      if [[ $GIT809_PLATFORM == windows ]]; then
        [[ -n ${_GIT809_MANAGER_LOG:-} && -f $_GIT809_MANAGER_LOG ]] && cat "$_GIT809_MANAGER_LOG" >&2
      else
        [[ -n ${_GIT809_UNIX_MANAGER_LOG:-} && -f $_GIT809_UNIX_MANAGER_LOG ]] && cat "$_GIT809_UNIX_MANAGER_LOG" >&2
        [[ -n ${_GIT809_UNIX_MANAGER_PID:-} ]] &&
          kill -0 "$_GIT809_UNIX_MANAGER_PID" 2>/dev/null &&
          printf 'POOL_AB_MANAGER_ALIVE pid=%s\\n' "$_GIT809_UNIX_MANAGER_PID" >&2
      fi
    fi
  }
  trap _pool_ab_diagnostics EXIT
  if [[ $GIT809_PLATFORM == windows ]]; then
    [[ ${_GIT809_MANAGER_WORKERS:-} == "$count" ]] || exit 32
    export GIT809_DRAIN_MODE=chunk100
    manager_log=$_GIT809_MANAGER_LOG
      printf 'MANAGER_LOG_NATIVE %s\n' "$(cygpath -w "$manager_log")"
  else
    manager_log=$_GIT809_UNIX_MANAGER_LOG
  fi
  expected=$("$gitbin" --version) || exit 33
  actual=$(git-ipc --version) || exit 34
  [[ $actual == "$expected" ]] || exit 35
  printf 'READY_CHECK %s\n' "$count"
  for ((i=0;i<500;i++)); do
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
  printf 'MANAGER_BEGIN\n'
  cat "$manager_log"
  printf 'MANAGER_END\n'
"""

def summarize(values):
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
    return float((Decimal(b) - Decimal(a)) * 1000)

def prepare(root, windows):
    if windows:
        from windows_real_git_worker_build import build
        return build(root)
    from unix_git_worker_build import build
    return build(root)

def run_case(bash, script, git_shell, environment, workers, round_index, windows):
    started = time.perf_counter()
    process = subprocess.Popen(
        [bash, "--noprofile", "--norc", "-c", SHELL,
         "pool-ab", str(workers), script, git_shell],
        env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out_bytes, err_bytes = process.communicate(timeout=120)
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode("utf-8", "replace")
        stderr = (exc.stderr or b"").decode("utf-8", "replace")
        complete = [row for row in stdout.splitlines() if row.startswith("PAIR ")]
        native_logs = [row.split(" ", 1)[1] for row in stdout.splitlines()
                       if row.startswith("MANAGER_LOG_NATIVE ")]
        manager_trace = ""
        snapshot = ""
        if native_logs:
            try:
                manager_trace = Path(native_logs[-1]).read_text(encoding="utf-8")
            except OSError as error:
                manager_trace = repr(error)
        if windows:
            import re
            # Out-of-process inspection only after the deadline, with no
            # instrumentation added to timed IPC operations.
            ids = [process.pid]
            allocations = re.findall(r"^ALLOCATED ([0-9]+)$", manager_trace,
                                     flags=re.MULTILINE)
            if allocations:
                ids.append(int(allocations[-1]))
            managers = re.findall(r'"manager_pid":\s*([0-9]+)', manager_trace)
            if managers:
                ids.append(int(managers[-1]))
            ps = ("foreach ($i in @(" + ",".join(str(x) for x in ids) +
                  ")) { $p = Get-Process -Id $i -ErrorAction SilentlyContinue; " +
                  "if ($p) { Write-Output ('PID=' + $i); " +
                  "$p.Threads | Select-Object Id,ThreadState,WaitReason | " +
                  "Format-Table -AutoSize | Out-String -Width 200 } }")
            try:
                snap = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive",
                     "-Command", ps], capture_output=True, timeout=15,
                    check=False)
                snapshot = snap.stdout.decode("utf-8", "replace") + (
                    snap.stderr.decode("utf-8", "replace"))
            except (OSError, subprocess.TimeoutExpired) as error:
                snapshot = repr(error)
        failure_path = OUT / f"two-instance-timeout-round-{round_index}.log"
        failure_path.write_text(
            f"PID={process.pid} rounds={len(complete)}\n" +
            "PROCESS_THREAD_SNAPSHOT\n" + snapshot + "\n" +
            "MANAGER_LOG\n" + manager_trace + "\n" +
            "STDERR\n" + stderr + "\nSTDOUT\n" + stdout,
            encoding="utf-8")
        process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise AssertionError(
            f"pool={workers} round={round_index} timeout; "
            f"pairs={len(complete)} last={complete[-1] if complete else None}; "
            f"threads={snapshot[-1500:]!r} manager_tail={manager_trace[-2500:]!r} "
            f"diagnostic={failure_path}") from exc
    result = subprocess.CompletedProcess(
        process.args, process.returncode, out_bytes, err_bytes)
    elapsed = time.perf_counter() - started
    output = result.stdout.decode("utf-8", "strict")
    errors = result.stderr.decode("utf-8", "strict")
    if result.returncode:
        complete = [row for row in output.splitlines() if row.startswith("PAIR ")]
        failure_path = OUT / f"two-instance-failure-round-{round_index}.log"
        failure_path.write_text(
            "STDERR FULL\\n" + errors + "\\nSTDOUT FULL\\n" + output,
            encoding="utf-8")
        raise AssertionError(
            f"pool={workers} round={round_index} rc={result.returncode} "
            f"pairs={len(complete)} first_stderr={errors[:1800]!r} "
            f"last_stderr={errors[-1800:]!r} full_log={failure_path}")
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
    assert len(set(allocations)) == len(allocations), "worker PID allocated twice"
    ready = [x for x in manager.splitlines() if x.startswith("WORKER_READY ")]
    assert len(ready) >= workers, "initial pool incomplete"
    return {
        "workers": workers, "round": round_index,
        "direct": summarize(direct), "ipc": summarize(ipc),
        "allocated": len(allocations), "ready_events": len(ready),
        "elapsed_wall_seconds": elapsed,
        "startup_included_in_samples": False,
    }

def main():
    root = Path.cwd()
    OUT.mkdir(exist_ok=True)
    windows = os.name == "nt"
    bash = shutil.which("bash")
    assert bash is not None, "Bash required"
    check = subprocess.run([bash, "--noprofile", "--norc", "-c",
                            '[[ $EPOCHREALTIME =~ ^[0-9]+\\.[0-9]+$ ]]'])
    assert check.returncode == 0, "Bash EPOCHREALTIME required"
    if windows:
        assert "git-sdk" in bash.lower(), f"Git SDK Bash required, not {bash}"
    git, flags = prepare(root, windows)
    native = str(git)
    if windows:
        git_shell = subprocess.run(
            ["cygpath", "-u", native], check=True,
            capture_output=True, text=True, encoding="utf-8").stdout.strip()
        script = subprocess.run(
            ["cygpath", "-u", str(root / "experiments/issue-809/git-manager.sh")],
            check=True, capture_output=True, text=True,
            encoding="utf-8").stdout.strip()
    else:
        git_shell = native
        script = str(root / "experiments/issue-809/git-manager-unix.sh")
    env = os.environ.copy()
    env["GIT809_PLATFORM"] = "windows" if windows else "unix"
    if windows:
        env["GIT809_TRACE"] = "1"
    env["GIT809_WORKER_GIT"] = native
    env["GIT809_MANAGER_PYTHON"] = env.get("GIT809_MANAGER_PYTHON") or sys.executable
    report = {
        "sha": env.get("GITHUB_SHA"),
        "os": env.get("RUNNER_OS", sys.platform),
        "git": native,
        "build_flags": flags,
        "samples_per_case": N,
        "round_order": RUN_ORDER,
        "reader_timeout_ms": 100,
        "pool_sizes": (2,),
        "manager_present_in_both_configurations": True,
        "results": [],
    }
    output_path = OUT / ("pool-size-matched-1to5-" + report["os"].lower() + ".json")
    try:
        for round_index, workers in enumerate(RUN_ORDER, 1):
            if windows:
                env["GIT809_TRACE"] = "0" if round_index % 2 else "1"
            result = run_case(bash, script, git_shell, env, workers, round_index, windows)
            result["trace_enabled"] = env.get("GIT809_TRACE") == "1"
            report["results"].append(result)
            print("POOL_SIZE_AB " + json.dumps({
                "sha": report["sha"], "os": report["os"],
                "round": round_index, "workers": workers,
                "trace_enabled": result["trace_enabled"],
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
