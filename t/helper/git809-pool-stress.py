#!/usr/bin/env python3
"""Windows native IPC pool sizes 1..5 on one runner, with watchdog timeouts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SHELL_TEST = r'''
set -euo pipefail
workers="$1"
iterations="$2"
source experiments/issue-809/git-manager.sh "$workers"
for ((n=1;n<=iterations;n++)); do
  case $((n % 10)) in
  0)
    if git-ipc nonexistent-git809-command >stress-out.tmp 2>stress-err.tmp; then
      printf 'ERROR: unknown command unexpectedly succeeded, pool=%s seq=%s\n' "$workers" "$n" >&2
      exit 61
    fi
    [[ -s stress-err.tmp ]] || {
      printf 'ERROR: missing stderr pool=%s seq=%s\n' "$workers" "$n" >&2
      exit 62
    }
    ;;
  1)
    output="$(GIT_EDITOR=git809-stress git-ipc var GIT_EDITOR)"
    [[ "$output" == git809-stress ]] || {
      printf 'ERROR: environment mismatch pool=%s seq=%s value=%q\n' "$workers" "$n" "$output" >&2
      exit 63
    }
    [[ ! \${GIT_EDITOR+x} ]] || {
      printf 'ERROR: parent environment changed pool=%s seq=%s\n' "$workers" "$n" >&2
      exit 64
    }
    ;;
  *)
    output="$(git-ipc --version)"
    [[ "$output" == git\ version\ * ]] || {
      printf 'ERROR: git version mismatch pool=%s seq=%s value=%q\n' "$workers" "$n" "$output" >&2
      exit 65
    }
    ;;
  esac
  if (( n % 25 == 0 )); then
    printf 'GIT809_STRESS_PROGRESS workers=%s complete=%s\n' "$workers" "$n"
  fi
done
printf 'GIT809_STRESS_PASS workers=%s operations=%s\n' "$workers" "$iterations"
'''

def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: git809-pool-stress.py WINDOWS_BASH.EXE ITERATIONS")
    bash = Path(sys.argv[1]).resolve(strict=True)
    count = int(sys.argv[2])
    assert 100 <= count <= 1000
    for key in ("GIT809_WORKER_GIT", "GIT809_MANAGER_PYTHON"):
        assert os.environ.get(key), f"missing required {key}"
    report = []
    for workers in range(1, 6):
        start = time.perf_counter()
        try:
            result = subprocess.run(
                [str(bash), "--noprofile", "--norc", "-c",
                 SHELL_TEST, "git809-stress", str(workers), str(count)],
                cwd=Path.cwd(), env=os.environ.copy(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=300, check=False)
        except subprocess.TimeoutExpired as exc:
            print(f"GIT809_STRESS_STALL workers={workers} timeout_s=300",
                  flush=True)
            print(f"stdout={exc.stdout!r}\nstderr={exc.stderr!r}", flush=True)
            return 70
        elapsed = time.perf_counter() - start
        output = result.stdout.decode("utf-8", "strict")
        errors = result.stderr.decode("utf-8", "strict")
        print(output, end="", flush=True)
        if result.returncode or f"GIT809_STRESS_PASS workers={workers}" not in output:
            print(f"GIT809_STRESS_FAIL workers={workers} exit={result.returncode}"
                  f" elapsed_s={elapsed:.3f} stderr={errors!r}", flush=True)
            return 71
        report.append({"workers": workers, "operations": count,
                       "elapsed_s": round(elapsed, 3), "exit": result.returncode})
        print(f"GIT809_STRESS_RESULT workers={workers} operations={count}"
              f" elapsed_s={elapsed:.3f}", flush=True)
    assert [r["workers"] for r in report] == [1, 2, 3, 4, 5]
    assert all(r["operations"] == count and r["exit"] == 0 for r in report)
    print("GIT809_STRESS_COMPLETE " + json.dumps(report), flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
