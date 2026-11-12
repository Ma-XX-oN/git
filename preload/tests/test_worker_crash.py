#!/usr/bin/env python3
"""Verify that a worker dying mid-command reports the status of a direct run.

A worker that crashes or is killed closes its exit-code channel without an
EXIT record; the Bash client then asks the manager (STATUS) how the process
ended. Each platform runs the same death directly and preloaded, and the
statuses must match: 139 for an access violation / SIGSEGV.

Windows terminates a worker held by a shell alias with STATUS_ACCESS_VIOLATION
(the code of a real MinGW Git crash), then releases the alias's shell; the
direct run is a native process exiting with that code. Workers see EOF on
stdin, so the alias, not stdin, keeps the worker busy. Linux/macOS use an
alias that sends SIGSEGV to the Git process running it. The pool must serve
a normal call afterwards.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from acceptance import shell_path

STATUS_ACCESS_VIOLATION = 0xC0000005
EXPECTED = 139  # 128 + SIGSEGV; MSYS maps STATUS_ACCESS_VIOLATION to SIGSEGV.

SHELL = r'''
source "$1" || exit 1
preload "$GIT_PRELOAD_WORKER_GIT" || exit 2
if [[ $OSTYPE == msys* || $OSTYPE == cygwin* ]]; then
  cygpath -w "$PRELOAD_DIR" || exit 3
  "$PRELOAD_PYTHON" -c 'import ctypes; ctypes.windll.kernel32.ExitProcess(0xC0000005)' 2>/dev/null
  printf 'direct=%s\n' "$?"
  # Release the output pipes, then wait until the harness has killed the worker.
  hold="!exec >/dev/null 2>&1 </dev/null; while test ! -e '$PRELOAD_DIR/release'; do sleep .1; done"
  git -c "alias.hold=$hold" hold
  printf 'preloaded=%s\n' "$?"
else
  printf '%s\n' "$PRELOAD_DIR"
  crash='!kill -SEGV $PPID'
  command git -c "alias.crash=$crash" crash 2>/dev/null
  printf 'direct=%s\n' "$?"
  git -c "alias.crash=$crash" crash
  printf 'preloaded=%s\n' "$?"
fi
expected=$(command git --version) && actual=$(git --version) &&
  [[ $actual == "$expected" ]] || exit 5
printf 'recovered\n'
_preload_stop
'''


def terminate(pid, code):
  """Terminate a native Windows process with a chosen exit code.

  Parameters
  ----------
  pid : int
      Native process ID.
  code : int
      Unsigned exit code the process reports.
  """
  import ctypes as C
  from ctypes import wintypes as W
  kernel = C.WinDLL("kernel32", use_last_error=True)
  kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
  kernel.OpenProcess.restype = W.HANDLE
  kernel.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
  kernel.TerminateProcess.restype = W.BOOL
  kernel.CloseHandle.argtypes = [W.HANDLE]
  handle = kernel.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
  assert handle, f"OpenProcess({pid}) failed: {C.get_last_error()}"
  try:
    assert kernel.TerminateProcess(handle, code), C.get_last_error()
  finally:
    kernel.CloseHandle(handle)


def allocated_worker(log, timeout=15):
  """Wait for the first worker allocation in a manager log.

  Parameters
  ----------
  log : Path
      Session manager.log, written live by the manager.
  timeout : float
      Seconds to wait.

  Returns
  -------
  int
      PID of the first allocated worker.
  """
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if log.exists():
      match = re.search(r"^ALLOCATED git (\d+)\r?$", log.read_text(encoding="utf-8"), re.M)
      if match:
        return int(match.group(1))
    time.sleep(.05)
  raise AssertionError("no worker allocation in " + str(log))


def expect_line(child, prefix):
  """Read the next stdout line and return the text after a required prefix."""
  line = child.stdout.readline().decode("utf-8").rstrip("\r\n")
  assert line.startswith(prefix), f"expected {prefix!r}, got {line!r}"
  return line[len(prefix):]


def main():
  """Kill a direct and a preloaded Git the same way and compare statuses."""
  parser = argparse.ArgumentParser()
  parser.add_argument("--git", required=True, type=Path)
  args = parser.parse_args()
  root = Path(__file__).resolve().parents[2]
  environment = os.environ.copy()
  environment["GIT_PRELOAD_WORKER_GIT"] = str(args.git.resolve())
  environment["PRELOAD_PYTHON"] = sys.executable
  # `command git` must run the same binary as the preloaded function.
  environment["PATH"] = str(args.git.resolve().parent) + os.pathsep + environment["PATH"]
  # A file, not a pipe: a leftover descendant cannot block reading stderr.
  errors = tempfile.TemporaryFile()
  child = subprocess.Popen(
    [shutil.which("bash"), "--noprofile", "--norc", "-c", SHELL, "worker-crash",
     shell_path(root / "preload/client/manager.sh")],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors, env=environment)
  watchdog = threading.Timer(120, child.kill)
  watchdog.start()
  session = None
  try:
    session = Path(expect_line(child, ""))
    direct = int(expect_line(child, "direct="))
    if os.name == "nt":
      terminate(allocated_worker(session / "manager.log"), STATUS_ACCESS_VIOLATION)
      (session / "release").touch()
    preloaded = int(expect_line(child, "preloaded="))
    assert direct == preloaded == EXPECTED, f"direct={direct} preloaded={preloaded}"
    expect_line(child, "recovered")
    assert child.wait(timeout=30) == 0, child.returncode
    print(f"PRELOAD_WORKER_CRASH_PASS direct={direct} preloaded={preloaded}", flush=True)
  except BaseException:
    if session is not None and session.is_dir():
      (session / "release").touch()  # End a held alias shell.
    child.kill()
    child.wait()
    errors.seek(0)
    sys.stderr.write(errors.read().decode("utf-8", "replace"))
    raise
  finally:
    watchdog.cancel()
    child.stdout.close()
    errors.close()


if __name__ == "__main__":
  main()
