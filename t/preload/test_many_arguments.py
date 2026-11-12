#!/usr/bin/env python3
"""Verify that a preloaded call accepts as many arguments as direct execution.

The worker sizes argv from the request's argument count, so neither the old
31-argument limit nor a one-byte count may truncate or reject the call.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

from acceptance import shell_path

COUNTS = (31, 32, 255, 256, 300)

SHELL = r'''
source "$1" || exit 1
preload "$GIT_PRELOAD_WORKER_GIT" || exit 2
for count in $2; do
  args=()
  for ((i = 0; i < count - 2; i++)); do args+=("arg $i"); done
  expected=$(command git rev-parse --sq-quote "${args[@]}"); direct=$?
  actual=$(git rev-parse --sq-quote "${args[@]}"); preloaded=$?
  if (( direct != preloaded )) || [[ $actual != "$expected" ]]; then
    printf 'MISMATCH count=%s direct=%s preloaded=%s\n' "$count" "$direct" "$preloaded"
    exit 3
  fi
  printf 'PRELOAD_MANY_ARGUMENTS_PASS %s\n' "$count"
done
_preload_stop
'''


def main():
  """Compare preloaded and direct output for argument counts across the old limits."""
  parser = argparse.ArgumentParser()
  parser.add_argument("--git", required=True, type=Path)
  args = parser.parse_args()
  root = Path(__file__).resolve().parents[2]
  environment = os.environ.copy()
  environment["GIT_PRELOAD_WORKER_GIT"] = str(args.git.resolve())
  environment["PRELOAD_PYTHON"] = sys.executable
  # `command git` must run the same binary as the preloaded function.
  environment["PATH"] = str(args.git.resolve().parent) + os.pathsep + environment["PATH"]
  # Each count is the number of arguments after `git`, including `rev-parse
  # --sq-quote`; 31 was the old limit and 255 the largest one-byte count.
  result = subprocess.run(
    [shutil.which("bash"), "--noprofile", "--norc", "-c", SHELL, "many-arguments",
     shell_path(root / "contrib/preload/preload-manager.sh"),
     " ".join(str(count) for count in COUNTS)],
    capture_output=True, text=True, env=environment, timeout=120)
  sys.stdout.write(result.stdout)
  if result.returncode:
    sys.stderr.write(result.stderr)
    sys.exit(result.returncode)
  print("PRELOAD_MANY_ARGUMENTS_TESTS_PASS", flush=True)


if __name__ == "__main__":
  main()
