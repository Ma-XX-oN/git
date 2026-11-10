"""Check native path-shadow caching without starting a manager or Git worker."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SHELL = r'''
set -eu
source "$1"
export PATH=/usr/bin:/c/Windows/System32 HOME='/home/preload shadow'
export LD_LIBRARY_PATH=/usr/lib ORIGINAL_PATH=/usr/bin SHELL=/usr/bin/bash
export TMPDIR=/tmp TMP=/tmp TEMP=/tmp
exec 10>"$2/first.trace"
BASH_XTRACEFD=10
set -x
_git_preload_refresh_paths
set +x
[[ $__git_env_HOME == "$(/usr/bin/cygpath -w "$HOME")" ]]
[[ $__git_env_PATH == "$(/usr/bin/cygpath -wp "$PATH")" ]]
exec 11>"$2/unchanged.trace"
BASH_XTRACEFD=11
set -x
_git_preload_refresh_paths
set +x
export HOME='/home/changed shadow'
exec 12>"$2/changed.trace"
BASH_XTRACEFD=12
set -x
_git_preload_refresh_paths
set +x
[[ $__git_env_HOME == "$(/usr/bin/cygpath -w "$HOME")" ]]
readonly HOME
_git_preload_refresh_paths
[[ $HOME == '/home/changed shadow' ]]
export TMPDIR=
_git_preload_refresh_paths
[[ ${__git_env_TMPDIR+x} && -z $__git_env_TMPDIR ]]
unset TMPDIR
_git_preload_refresh_paths
[[ ! ${__git_env_TMPDIR+x} && ! ${__git_env_source_TMPDIR+x} ]]
"$3" -c 'import os; assert "__git_env_PATH" in os.environ; assert not any(k.startswith("__git_env_source_") for k in os.environ)'
'''


def main():
    """Assert cache correctness and exact converter counts in isolated Bash.

    Returns
    -------
    None
        Prints success after startup, unchanged, changed, readonly, empty and
        unset cases pass; any shell failure or unexpected process count raises.
    """
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="preload-shadow-") as temporary:
        paths = [str(root / "contrib/preload/git-ipc-request.sh"), temporary, sys.executable]
        if os.name == "nt":
            paths = [subprocess.check_output([shutil.which("cygpath"), "-u", path],
                                             text=True).strip() for path in paths]
        subprocess.run([shutil.which("bash"), "--noprofile", "--norc", "-c", SHELL,
                        "shadow-cache"] + paths, check=True, timeout=30)
        for stage, expected in (("first", 8), ("unchanged", 0), ("changed", 1)):
            lines = (Path(temporary) / f"{stage}.trace").read_text(encoding="utf-8").splitlines()
            count = sum(line.lstrip("+").lstrip().startswith("/usr/bin/cygpath ") for line in lines)
            assert count == expected, (stage, count, expected)
    print("PRELOAD_SHELL_ENVIRONMENT_PASS startup=8 unchanged=0 changed=1", flush=True)


if __name__ == "__main__":
    main()
