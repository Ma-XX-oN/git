"""Check startup environment inheritance and clean manager replacement on re-source."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

from acceptance import shell_path
from test_shell_output import stop_owned_tree

SHELL = r'''
set -u
# No arguments; report the current assertion phase and exit status on failure.
report_exit() {
  local status=$?
  if (( status )); then
    printf 'ENVIRONMENT_RESTART_FAILURE phase=%s status=%s\n' "$phase" "$status" >&2
  fi
}
phase=startup
trap report_exit EXIT
# Argument 1: the replaced manager's job PID. Fail if it is still running.
# The session directory is reused and recreated, so it only holds the new
# manager's files.
check_retired_manager() {
  ! kill -0 "$1" 2>/dev/null
}
# A noninteractive runner may have no default editor after GIT_EDITOR is unset.
export EDITOR='fallback editor'
export GIT_EDITOR='initial editor'
source "$1" || exit 10
preload "$GIT_PRELOAD_WORKER_GIT" || exit 10
cp "$PRELOAD_DIR/ready.json" "$2/first.json" || exit 11
first_job=$_PRELOAD_MANAGER_JOB
phase=initial-value
actual=$(git var GIT_EDITOR) || exit 12
[[ $actual == 'initial editor' ]] || exit 13
export GIT_EDITOR='refreshed editor'
phase=frozen-value
actual=$(git var GIT_EDITOR) || exit 14
[[ $actual == 'initial editor' ]] || exit 15
phase=restart
source "$1" || exit 16
check_retired_manager "$first_job" || exit 17
cp "$PRELOAD_DIR/ready.json" "$2/second.json" || exit 18
second_job=$_PRELOAD_MANAGER_JOB
phase=refreshed-value
actual=$(git var GIT_EDITOR) || exit 19
[[ $actual == 'refreshed editor' ]] || exit 20
unset GIT_EDITOR
phase=unset-reference
expected=$("$GIT_PRELOAD_WORKER_GIT" var GIT_EDITOR) || exit 21
phase=unset-restart
source "$1" || exit 22
check_retired_manager "$second_job" || exit 23
actual=$(git var GIT_EDITOR) || exit 24
[[ $actual == "$expected" ]] || exit 25
phase=subshell
(
  export GIT_EDITOR='subshell editor'
  source "$1" || exit 26
  actual=$(git var GIT_EDITOR) || exit 27
  [[ $actual == 'subshell editor' ]] || exit 28
) || exit 29
phase=parent-after-subshell
actual=$(git var GIT_EDITOR) || exit 30
[[ $actual == "$expected" ]] || exit 31
phase=child-process
# A separate Bash process reuses this session through the exported function.
bash --noprofile --norc -c '
  [[ $(type -t git) == function ]] || exit 1
  actual=$(git var GIT_EDITOR) || exit 2
  [[ $actual == "$1" ]] || exit 3' child "$expected" || exit 35
phase=invalid-preload
if preload "$2/no-such-command" 2>/dev/null; then exit 32; fi
actual=$(git var GIT_EDITOR) || exit 33
[[ $actual == "$expected" ]] || exit 34
printf 'ENVIRONMENT_RESTART_PASS\n'
'''


def main():
    """Validate frozen session values, refresh, cleanup, child reuse and owner isolation.

    Returns
    -------
    None
        Prints success after real native workers pass; failure preserves logs
        in the supplied output directory and timeout terminates the owned tree.
    """
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GIT_PRELOAD_WORKER_GIT"] = str(args.git.resolve())
    env["PRELOAD_PYTHON"] = sys.executable
    command = [shutil.which("bash"), "--noprofile", "--norc", "-c", SHELL,
               "environment-restart", shell_path(root / "contrib/preload/preload-manager.sh"),
               shell_path(output)]
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=os.name != "nt")
    try:
        out, err = process.communicate(timeout=40)
    except subprocess.TimeoutExpired:
        stop_owned_tree(process)
        raise
    (output / "stdout.log").write_bytes(out)
    (output / "stderr.log").write_bytes(err)
    assert process.returncode == 0, (process.returncode, out, err)
    assert out == b"ENVIRONMENT_RESTART_PASS\n", out
    print("PRELOAD_SHELL_ENVIRONMENT_PASS startup restart unset subshell child invalid cleanup", flush=True)


if __name__ == "__main__":
    main()
