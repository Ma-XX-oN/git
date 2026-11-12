"""Check supported Bash output and retain opt-in Windows backpressure diagnostics."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

from acceptance import preserve_manager_log, shell_path

CASES = {"small-mixed": (4096, 4096), "stdout-large": (1048576, 0),
         "stderr-large": (0, 1048576), "out-first": (1048576, 1048576),
         "err-first": (1048576, 1048576)}

SHELL = r'''
source "$1" || exit 100
preload "$GIT_PRELOAD_WORKER_GIT" || exit 100
printf '%s\n' "$PRELOAD_DIR/manager.log" >"$2/manager-path.txt"
for order in "${@:3}"; do
  case $order in
    small-mixed) output_alias='!printf "%04096d" 0; printf "%04096d" 0 >&2; exit 7' ;;
    stdout-large) output_alias='!printf "%01048576d" 0; exit 7' ;;
    stderr-large) output_alias='!printf "%01048576d" 0 >&2; exit 7' ;;
    out-first) output_alias='!printf "%01048576d" 0; printf "%01048576d" 0 >&2; exit 7' ;;
    err-first) output_alias='!printf "%01048576d" 0 >&2; printf "%01048576d" 0; exit 7' ;;
    *) exit 2 ;;
  esac
  git -c "alias.preload-output=$output_alias" preload-output \
    >"$2/$order.stdout" 2>"$2/$order.stderr"
  [[ $? == 7 ]] || exit 32
done
'''


def stop_owned_tree(process):
    """Terminate only the reproduction's owned process tree after a timeout.

    Parameters
    ----------
    process : subprocess.Popen
        Test shell; on POSIX it owns a newly created process group.

    Returns
    -------
    None
        Waits for termination; an operating-system cleanup failure raises.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True, timeout=15, check=True)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=15)


def main():
    """Run the output/status assertions, preserving failure evidence on disk.

    Returns
    -------
    None
        Prints success only after selected cases and status 7 pass. Default
        Windows coverage skips its documented large-stderr limitation; --case
        explicitly exercises it. Timeout or wrong bytes raises after cleanup.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", choices=CASES,
                        help="Select a case explicitly, including known Windows limitations.")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["GIT_PRELOAD_WORKER_GIT"] = str(args.git.resolve())
    env["PRELOAD_PYTHON"] = sys.executable
    cases = args.case or (list(CASES) if os.name != "nt" else ["small-mixed", "stdout-large"])
    command = [shutil.which("bash"), "--noprofile", "--norc", "-c", SHELL,
               "preload-output", shell_path(root / "preload/client/manager.sh"),
               shell_path(output)] + cases
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=os.name != "nt")
    try:
        # POSIX runs all five cases (over 6 MiB through Bash builtins).
        # Keep Windows opt-in deadlock diagnostics bounded at 30 seconds.
        out, err = process.communicate(timeout=30 if os.name == "nt" else 120)
    except subprocess.TimeoutExpired:
        try:
            preserve_manager_log(output)
        finally:
            stop_owned_tree(process)
        raise
    (output / "client.stdout").write_bytes(out)
    (output / "client.stderr").write_bytes(err)
    assert process.returncode == 0, (process.returncode, err)
    for order in cases:
        for stream, size in zip(("stdout", "stderr"), CASES[order]):
            actual = (output / f"{order}.{stream}").read_bytes()
            assert actual == b"0" * size, (order, stream, len(actual), size)
    if os.name == "nt" and not args.case:
        print("PRELOAD_SHELL_OUTPUT_SKIP large-stderr cases: documented Windows limitation", flush=True)
    print("PRELOAD_SHELL_OUTPUT_PASS", ",".join(cases), flush=True)


if __name__ == "__main__":
    main()
