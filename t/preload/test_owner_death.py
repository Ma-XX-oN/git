#!/usr/bin/env python3
"""Verify that allocation does not detach a worker from its shell owner."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from acceptance import shell_path

SHELL = r'''
source "$1" || exit 1
preload "$GIT_PRELOAD_WORKER_GIT" || exit 1
# Allocate a worker without running a command, so the client owns it idle.
_preload_request GET git || exit 2
if [[ ${OSTYPE:-} == msys* || ${OSTYPE:-} == cygwin* ]]; then
  allocated=${_PRELOAD_REPLY##*\\worker-}   # Native worker PID.
else
  allocated=$_PRELOAD_REPLY                 # Worker FIFO directory.
fi
[[ $_PRELOAD_REPLY == *worker-* ]] || exit 3
printf '%s\n' "$allocated"
while :; do sleep 1; done
'''


def main():
    """Kill the fixture owner and verify native worker termination and cleanup."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--git", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["GIT_PRELOAD_WORKER_GIT"] = str(args.git.resolve())
    environment["PRELOAD_PYTHON"] = sys.executable
    child = subprocess.Popen([shutil.which("bash"), "--noprofile", "--norc", "-c", SHELL,
                              "owner-death", shell_path(root / "contrib/preload/preload-manager.sh")],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    handle = None
    try:
        allocated = child.stdout.readline().decode("utf-8").strip()
        assert allocated and allocated != "WAIT", allocated
        if os.name == "nt":
            import ctypes as C
            from ctypes import wintypes as W
            kernel = C.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
            kernel.OpenProcess.restype = W.HANDLE
            kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
            kernel.WaitForSingleObject.restype = W.DWORD
            kernel.CloseHandle.argtypes = [W.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, int(allocated))
            assert handle, C.get_last_error()
            assert kernel.WaitForSingleObject(handle, 0) == 258
        else:
            assert Path(allocated).is_dir(), allocated
        child.kill()
        child.wait(timeout=5)
        if os.name == "nt":
            assert kernel.WaitForSingleObject(handle, 5000) == 0, allocated
        else:
            deadline = time.monotonic() + 15
            while Path(allocated).exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert not Path(allocated).exists(), "abandoned worker retained FIFO endpoints"
        print("PRELOAD_OWNER_DEATH_PASS", allocated, flush=True)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()
        if handle:
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
