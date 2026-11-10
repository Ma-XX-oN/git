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
# Capture the sourcing owner's discovery paths after each replacement.
# No arguments; session and ready receive borrowed shell path values.
session_paths() {
  case $OSTYPE in
    msys*|cygwin*) ready=$_GIT_PRELOAD_MANAGER_READY; session=${ready%/*} ;;
    *) session=$_GIT_PRELOAD_UNIX_DIRECTORY; ready=$session/ready.json ;;
  esac
}
# Argument 1: retired session directory. Fail if readiness or worker paths remain.
check_retired_session() {
  local entry
  [[ ! -f $1/ready.json ]] || return 1
  for entry in "$1"/worker-*; do
    [[ ! -d $entry ]] || return 1
  done
}
export GIT_EDITOR='initial editor'
source "$1" 2 || exit 10
session_paths
cp "$ready" "$2/first.json" || exit 11
first_session=$session
actual=$(git-ipc var GIT_EDITOR) || exit 12
[[ $actual == 'initial editor' ]] || exit 13
export GIT_EDITOR='refreshed editor'
actual=$(git-ipc var GIT_EDITOR) || exit 14
[[ $actual == 'initial editor' ]] || exit 15
source "$1" 4 || exit 16
check_retired_session "$first_session" || exit 17
session_paths
cp "$ready" "$2/second.json" || exit 18
second_session=$session
actual=$(git-ipc var GIT_EDITOR) || exit 19
[[ $actual == 'refreshed editor' ]] || exit 20
unset GIT_EDITOR
expected=$("$GIT_PRELOAD_WORKER_GIT" var GIT_EDITOR) || exit 21
source "$1" 3 || exit 22
check_retired_session "$second_session" || exit 23
actual=$(git-ipc var GIT_EDITOR) || exit 24
[[ $actual == "$expected" ]] || exit 25
(
  export GIT_EDITOR='subshell editor'
  source "$1" 1 || exit 26
  actual=$(git-ipc var GIT_EDITOR) || exit 27
  [[ $actual == 'subshell editor' ]] || exit 28
) || exit 29
actual=$(git-ipc var GIT_EDITOR) || exit 30
[[ $actual == "$expected" ]] || exit 31
if source "$1" 0 >/dev/null 2>&1; then exit 32; fi
actual=$(git-ipc var GIT_EDITOR) || exit 33
[[ $actual == "$expected" ]] || exit 34
printf 'ENVIRONMENT_RESTART_PASS\n'
'''


def main():
    """Validate frozen session values, refresh, cleanup, resize and owner isolation.

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
    env["GIT_PRELOAD_MANAGER_PYTHON"] = sys.executable
    env["GIT_PRELOAD_DRAIN_MODE"] = "chunk100"
    command = [shutil.which("bash"), "--noprofile", "--norc", "-c", SHELL,
               "environment-restart", shell_path(root / "contrib/preload/git-manager.sh"),
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
    print("PRELOAD_SHELL_ENVIRONMENT_PASS startup restart unset resize subshell cleanup", flush=True)


if __name__ == "__main__":
    main()
