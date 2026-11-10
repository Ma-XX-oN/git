#!/usr/bin/env python3
"""Run serial native worker acceptance and preserve raw command timings."""

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

SHELL = r'''
set -u
source "$1" 3 || exit 10
if [[ ${OSTYPE:-} == msys* || ${OSTYPE:-} == cygwin* ]]; then
  manager_log=$_GIT_PRELOAD_MANAGER_LOG
else
  manager_log=$_GIT_PRELOAD_UNIX_MANAGER_LOG
fi
printf '%s\n' "$manager_log" >"$3/manager-path.txt"
# Preserve the allocator's last events on ordinary command failure as well.
trap 'cp "$manager_log" "$3/manager.log"' EXIT
expected=$("$GIT_PRELOAD_WORKER_GIT" --version) || exit 11
actual=$(git-ipc --version) || exit 12
[[ $actual == "$expected" ]] || exit 13
git-ipc rev-parse --show-toplevel >"$3/cwd.txt" || exit 17
cd "$4/contrib/preload" || exit 19
actual=$(git-ipc rev-parse --show-prefix) || exit 20
[[ $actual == 'contrib/preload/' ]] || exit 21
cd "$4" || exit 22
for ((i=0; i<$2; i++)); do
  begin=$EPOCHREALTIME
  git-ipc --version >"$3/actual.txt" || exit 23
  end=$EPOCHREALTIME
  IFS= read -r actual <"$3/actual.txt" || exit 24
  [[ $actual == "$expected" ]] || exit 25
  printf '%s,%s,%s\n' "$i" "$begin" "$end"
  if (( i % 500 == 0 )); then printf 'PRELOAD_STRESS_PROGRESS %s\n' "$i" >&2; fi
done
'''


def shell_path(path):
    """Return a path usable by the test's native Bash interpreter."""
    if os.name == "nt":
        return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()
    return str(path)


def preserve_manager_log(output):
    """Copy the last allocator events before timeout terminates its owner."""
    metadata = output / "manager-path.txt"
    if metadata.exists():
        path = metadata.read_text(encoding="utf-8").strip()
        if os.name == "nt":
            path = subprocess.check_output(["cygpath", "-w", path], text=True).strip()
        shutil.copy2(path, output / "manager.log")


def main():
    """Validate per-call cwd, startup environment and serial command execution."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--git", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--count", type=int, default=6000)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("preload-results"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    root = args.root.resolve()
    env = os.environ.copy()
    env["GIT_PRELOAD_WORKER_GIT"] = str(args.git.resolve())
    env["GIT_PRELOAD_MANAGER_PYTHON"] = sys.executable
    env["GIT_PRELOAD_DRAIN_MODE"] = "chunk100"
    if args.trace:
        env["GIT_TRACE_PRELOAD"] = str(output / "worker-trace.log")
    command = [shutil.which("bash"), "--noprofile", "--norc", "-c", SHELL,
               "acceptance", shell_path(root / "contrib/preload/git-manager.sh"),
               str(args.count), shell_path(output), shell_path(root)]
    started = time.monotonic()
    with (output / "stress.csv").open("w", encoding="utf-8", newline="\n") as out:
        out.write("sample,begin,end\n")
        out.flush()
        with (output / "stderr.log").open("w", encoding="utf-8", newline="\n") as err:
            process = subprocess.Popen(command, env=env, cwd=root, stdout=out, stderr=err)
            try:
                result = process.wait(timeout=900)
            except subprocess.TimeoutExpired:
                try:
                    preserve_manager_log(output)
                except OSError as error:
                    print(f"PRELOAD_LOG_CAPTURE_FAILED {error}", file=sys.stderr)
                finally:
                    process.kill()
                    process.wait(timeout=15)
                raise
            if result:
                raise subprocess.CalledProcessError(result, command)
    rows = (output / "stress.csv").read_text(encoding="utf-8").splitlines()[1:]
    assert len(rows) == args.count, len(rows)
    samples = [float((Decimal(row.split(",")[2]) - Decimal(row.split(",")[1])) * 1000)
               for row in rows]
    report = {"commands": len(samples), "median_ms": statistics.median(samples),
              "sha": subprocess.check_output([str(args.git.resolve()), "rev-parse", "HEAD"],
                                              cwd=root, text=True).strip(),
              "platform": sys.platform, "git": str(args.git.resolve()),
              "context": "per-call cwd; environment inherited from manager startup", "workers": 3,
              "binary_sha256": hashlib.sha256(args.git.resolve().read_bytes()).hexdigest(),
              "tracked_worktree_diff": subprocess.check_output(
                  [str(args.git.resolve()), "diff", "--stat", "HEAD"], cwd=root, text=True),
              "trace_variables": sorted(key for key in env if key.startswith("GIT_TRACE")),
              "elapsed_seconds": time.monotonic() - started}
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("PRELOAD_ACCEPTANCE_PASS", json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
