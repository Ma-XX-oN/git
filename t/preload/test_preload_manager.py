#!/usr/bin/env python3
"""Exercise the generic preload manager's request protocol on this platform.

Windows: per-call duplex connections, including Git Bash's throwaway
connection held open or closed without data. Linux/macOS: request FIFO,
polled reply files, a program that hangs without READY, and a reply to a
caller that has exited. Both: concurrent calls through the Bash library.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from acceptance import shell_path
from test_shell_output import stop_owned_tree

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = os.name == "nt"

if WINDOWS:
  import ctypes as C
  from ctypes import wintypes as W
  K = C.WinDLL("kernel32", use_last_error=True)
  K.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
  K.CreateFileW.restype = W.HANDLE
  K.WriteFile.argtypes = [W.HANDLE, W.LPCVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID]
  K.WriteFile.restype = W.BOOL
  K.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID]
  K.ReadFile.restype = W.BOOL
  K.CloseHandle.argtypes = [W.HANDLE]
  K.CloseHandle.restype = W.BOOL
  K.WaitNamedPipeW.argtypes = [W.LPCWSTR, W.DWORD]
  K.WaitNamedPipeW.restype = W.BOOL
  INVALID = C.c_void_p(-1).value


class Session:
  """A manager process owned by this test.

  Parameters
  ----------
  directory : Path
      Empty private session directory.
  """

  def __init__(self, directory):
    self.directory = directory
    self.pipe_dir = rf"\\.\pipe\preload-test-{uuid.uuid4().hex}"
    script = ROOT / "contrib/preload" / (
      "preload_manager_windows.py" if WINDOWS else "preload_manager_unix.py")
    command = [sys.executable, str(script), "--owner-pid", str(os.getpid()),
               "--directory", str(directory)]
    if WINDOWS:
      command += ["--pipe-dir", self.pipe_dir]
    self.log = open(directory / "manager.log", "wb")
    self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while not (directory / "ready.json").exists():
      assert self.process.poll() is None, self.read_log()
      assert time.monotonic() < deadline, "manager did not become ready"
      time.sleep(.02)
    self.sequence = 0

  def read_log(self):
    """Return the manager log text."""
    self.log.flush()
    return (self.directory / "manager.log").read_text(encoding="utf-8", errors="replace")

  def connect(self):
    """Open one connection to the Windows request pipe.

    Returns
    -------
    int
        Owned pipe handle.
    """
    address = self.pipe_dir + "\\request"
    for _ in range(200):
      handle = K.CreateFileW(address, 0xc0000000, 0, None, 3, 0, None)
      if handle != INVALID:
        return handle
      K.WaitNamedPipeW(address, 50)
    raise C.WinError(C.get_last_error())

  def request_on(self, handle, line):
    """Send one request on an open Windows connection and read its reply line."""
    data = (line + "\n").encode("utf-8")
    count = W.DWORD()
    assert K.WriteFile(handle, data, len(data), C.byref(count), None)
    reply = bytearray()
    buffer = C.create_string_buffer(4096)
    while b"\n" not in reply:
      assert K.ReadFile(handle, buffer, 4096, C.byref(count), None), C.get_last_error()
      reply.extend(buffer.raw[:count.value])
    return reply[:reply.index(b"\n")].decode("utf-8")

  def request(self, line, pid=None):
    """Send one request and return its reply line.

    Parameters
    ----------
    line : str
        Request without newline. On Linux/macOS, `{token}` is replaced by the
        call token `<pid>.<seq>` (this process unless pid is given).
    pid : int, optional
        Unix caller PID that receives the reply; no wait if it is not ours.

    Returns
    -------
    str or None
        Reply line, or None when the reply is addressed to another process.
    """
    if WINDOWS:
      handle = self.connect()
      try:
        return self.request_on(handle, line)
      finally:
        K.CloseHandle(handle)
    caller = os.getpid() if pid is None else pid
    self.sequence += 1
    token = f"{caller}.{self.sequence}"
    with open(self.directory / "request", "w", encoding="utf-8") as fifo:
      fifo.write(line.format(token=token) + "\n")
    if caller != os.getpid():
      return None
    reply_file = self.directory / f"reply.{token}"
    deadline = time.monotonic() + 15
    while not reply_file.exists():
      assert time.monotonic() < deadline, "no reply; log:\n" + self.read_log()
      time.sleep(.001)
    reply = reply_file.read_text(encoding="utf-8").rstrip("\n")
    with open(self.directory / "request", "w", encoding="utf-8") as fifo:
      fifo.write(f"ACK {token}\n")
    return reply

  def stop(self):
    """Stop the manager and require a clean exit."""
    assert self.request("STOP" if WINDOWS else "STOP {token}") == "OK"
    assert self.process.wait(timeout=15) == 0, self.read_log()
    assert not (self.directory / "ready.json").exists()
    self.log.close()


def pid_alive(pid):
  """Return whether a native process exists (Windows: may be a zombie handle)."""
  if WINDOWS:
    result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                            capture_output=True, text=True)
    return str(pid) in result.stdout
  try:
    os.kill(pid, 0)
    return True
  except ProcessLookupError:
    return False


def check_protocol(git, work):
  """PRELOAD, GET and error replies, plus platform-specific connection cases."""
  directory = work / "protocol"
  directory.mkdir()
  session = Session(directory)
  preload = "PRELOAD git " + (str(git) if WINDOWS else "{token} " + str(git))
  assert session.request(preload) == "OK", session.read_log()
  reply = session.request("GET git" if WINDOWS else "GET git {token}")
  assert "worker-" in reply, reply
  print("PRELOAD_MANAGER_PASS get", flush=True)
  missing = session.request("GET nope" if WINDOWS else "GET nope {token}")
  assert missing == "ERROR not preloaded: nope", missing
  print("PRELOAD_MANAGER_PASS unknown-command", flush=True)
  if WINDOWS:
    unsupported = session.request(r"PRELOAD whoami C:\Windows\System32\whoami.exe")
    assert unsupported == "ERROR whoami doesn't support --preload-worker", unsupported
    # Git Bash's throwaway connection held open while the real one is served.
    probe = session.connect()
    try:
      real = session.connect()
      try:
        assert "worker-" in session.request_on(real, "GET git")
      finally:
        K.CloseHandle(real)
    finally:
      K.CloseHandle(probe)
    print("PRELOAD_MANAGER_PASS probe-held-open", flush=True)
    # A throwaway connection that closes without data.
    K.CloseHandle(session.connect())
    assert "worker-" in session.request("GET git")
    print("PRELOAD_MANAGER_PASS probe-closed", flush=True)
  else:
    hang = work / "hang-tool"
    hang.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    hang.chmod(0o755)
    started = time.monotonic()
    unsupported = session.request("PRELOAD hang-tool {token} " + str(hang))
    elapsed = time.monotonic() - started
    assert unsupported == "ERROR hang-tool doesn't support --preload-worker", unsupported
    assert elapsed < 5, elapsed
    print(f"PRELOAD_MANAGER_PASS hang-without-ready-killed {elapsed:.2f}s", flush=True)
    # A caller that exited before its reply: the worker must not leak.
    dead = subprocess.Popen(["true"])
    dead.wait()
    session.request("GET git {token}", pid=dead.pid)
    deadline = time.monotonic() + 10
    allocated = None
    while time.monotonic() < deadline and allocated is None:
      lines = [line for line in session.read_log().splitlines()
               if line.startswith("ALLOCATED git ")]
      if len(lines) >= 2:
        allocated = int(lines[-1].split()[-1])
      time.sleep(.05)
    assert allocated, session.read_log()
    while time.monotonic() < deadline and pid_alive(allocated):
      time.sleep(.05)
    assert not pid_alive(allocated), "worker for an exited caller leaked"
    print("PRELOAD_MANAGER_PASS exited-caller-worker-killed", flush=True)
  session.stop()
  print("PRELOAD_MANAGER_PASS stop", flush=True)


SHELL = r'''
source "$1" || exit 10
preload "$2" || exit 11
expected=$("$2" --version) || exit 12
for (( i = 0; i < 8; i++ )); do
  ( actual=$(git --version) && [[ $actual == "$expected" ]] ) &
  pids+=($!)
done
for pid in "${pids[@]}"; do wait "$pid" || exit 13; done
wait   # The manager is not a job of this shell; this must not block.
printf 'CONCURRENT_PASS\n'
'''


def check_concurrent_shell(git, output):
  """Concurrent calls from background subshells through the Bash library."""
  env = os.environ.copy()
  env["PRELOAD_PYTHON"] = sys.executable
  command = [shutil.which("bash"), "--noprofile", "--norc", "-c", SHELL, "concurrent",
             shell_path(ROOT / "contrib/preload/preload-manager.sh"), shell_path(git)]
  process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=not WINDOWS)
  try:
    out, err = process.communicate(timeout=120)
  except subprocess.TimeoutExpired:
    stop_owned_tree(process)
    raise
  (output / "concurrent.stderr").write_bytes(err)
  assert process.returncode == 0 and out == b"CONCURRENT_PASS\n", (process.returncode, out, err)
  print("PRELOAD_MANAGER_PASS concurrent-shell", flush=True)


def main():
  """Run every check for this platform."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--git", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  git = args.git.resolve()
  output = args.output.resolve()
  output.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(prefix="preload-manager-test-") as work:
    try:
      check_protocol(git, Path(work))
    finally:
      log = Path(work) / "protocol" / "manager.log"
      if log.exists():
        shutil.copy2(log, output / "protocol-manager.log")
  check_concurrent_shell(git, output)
  print("PRELOAD_MANAGER_TESTS_PASS", flush=True)


if __name__ == "__main__":
  main()
