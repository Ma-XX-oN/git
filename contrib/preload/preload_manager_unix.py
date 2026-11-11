#!/usr/bin/env python3
"""Generic preload manager for Linux and macOS.

Clients write one line to the FIFO `<directory>/request`; `<pid>` is the
caller's $BASHPID:

  PRELOAD <name> <pid> <executable-path>   ->  OK | ERROR <reason>
  GET <name> <pid>                         ->  <directory>/worker-<pid> | ERROR <reason>
  STOP <pid>                               ->  OK, then the manager exits
  ACK <pid>                                ->  (no reply) delete reply.<pid>

A reply is written to the regular file `<directory>/reply.<pid>`, after which
the caller is sent SIGUSR1. The manager holds the write end of the FIFO
`<directory>/bell` open without ever writing to it, so a caller can block in
`read -t` on it until the signal arrives.
"""
from __future__ import annotations
import argparse
import errno
import json
import os
from pathlib import Path
import select
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preload_pool  # noqa: E402
from preload_pool import log  # noqa: E402


def owner_alive(pid):
  """Check whether the owning process still exists.

  Parameters
  ----------
  pid : int
      Owner PID.

  Returns
  -------
  bool
      False only if the process no longer exists; permission denial means alive.
  """
  try:
    os.kill(pid, 0)
    return True
  except ProcessLookupError:
    return False


class Server:
  """Dispatch request lines and deliver replies through files and signals.

  Parameters
  ----------
  directory : Path
      Private session directory holding the FIFOs and reply files.
  manager : preload_pool.Manager
      Pools serving PRELOAD and GET requests.
  """

  def __init__(self, directory, manager):
    self.directory = directory
    self.manager = manager
    self.stopped = threading.Event()

  def reply(self, pid, text):
    """Publish one reply and wake its caller.

    Parameters
    ----------
    pid : int
        Caller's $BASHPID.
    text : str
        Reply line without newline.

    Returns
    -------
    bool
        False if the caller no longer exists; the reply file is removed.
    """
    path = self.directory / ("reply." + str(pid))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
      os.write(fd, (text + "\n").encode("utf-8"))
    finally:
      os.close(fd)
    try:
      os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
      path.unlink(missing_ok=True)
      return False
    return True

  def handle(self, line):
    """Execute one request line; slow requests run on their own thread.

    Parameters
    ----------
    line : str
        Request line without newline.
    """
    words = line.split(" ", 3)  # The executable path may contain spaces.
    command = words[0]
    if command == "ACK" and len(words) == 2 and words[1].isdigit():
      (self.directory / ("reply." + words[1])).unlink(missing_ok=True)
    elif command == "GET" and len(words) == 3 and words[2].isdigit():
      threading.Thread(target=self._get, args=(words[1], int(words[2])),
                       daemon=True).start()
    elif command == "PRELOAD" and len(words) == 4 and words[2].isdigit():
      threading.Thread(target=self._preload, daemon=True,
                       args=(words[1], int(words[2]), words[3])).start()
    elif command == "STOP" and len(words) == 2 and words[1].isdigit():
      self.reply(int(words[1]), "OK")
      log("MANAGER_STOP_REQUEST")
      self.stopped.set()
    else:
      log("INVALID_REQUEST " + repr(line))

  def _get(self, name, pid):
    """Allocate one worker for a caller."""
    result = self.manager.allocate(name)
    if isinstance(result, str):
      self.reply(pid, result)
    elif not self.reply(pid, str(self.directory / ("worker-" + str(result.pid)))):
      result.kill()  # The caller exited and never learned of this worker.

  def _preload(self, name, pid, path):
    """Create a command's pool for a caller."""
    self.reply(pid, self.manager.preload(name, path))


def main():
  """Run the manager until STOP or the owner process exits."""
  parser = argparse.ArgumentParser()
  parser.add_argument("--owner-pid", required=True, type=int)
  parser.add_argument("--directory", required=True, type=Path)
  args = parser.parse_args()
  directory = args.directory.resolve(strict=True)
  manager = preload_pool.Manager(
    directory, preload_pool.worker_environment(args.owner_pid))
  server = Server(directory, manager)
  request = directory / "request"
  bell = directory / "bell"
  ready = directory / "ready.json"
  os.mkfifo(request, 0o600)
  os.mkfifo(bell, 0o600)
  # Hold a writer on each FIFO: the request writer keeps the reader from
  # seeing EOF between clients; the bell writer makes callers' reads block.
  request_fd = os.open(request, os.O_RDONLY | os.O_NONBLOCK)
  request_guard = os.open(request, os.O_WRONLY | os.O_NONBLOCK)
  bell_reader = os.open(bell, os.O_RDONLY | os.O_NONBLOCK)
  bell_writer = os.open(bell, os.O_WRONLY | os.O_NONBLOCK)
  incoming = bytearray()
  try:
    temporary = directory / "ready.tmp"
    temporary.write_text(json.dumps({
      "state": "READY", "manager_pid": os.getpid(),
      "owner_pid": args.owner_pid}) + "\n", encoding="utf-8")
    temporary.replace(ready)
    log("READY " + str(request))
    while not server.stopped.is_set():
      if not owner_alive(args.owner_pid):
        log(f"OWNER_EXIT {args.owner_pid}")
        break
      readable, _, _ = select.select([request_fd], [], [], 0.25)
      if not readable:
        continue
      try:
        data = os.read(request_fd, 4096)
      except OSError as error:
        if error.errno == errno.EAGAIN:
          continue
        raise
      incoming.extend(data)
      while b"\n" in incoming:
        line, _, remainder = incoming.partition(b"\n")
        incoming = bytearray(remainder)
        server.handle(line.decode("utf-8", "replace"))
      if len(incoming) > 4096:
        log("REQUEST_TOO_LONG")
        incoming.clear()
  finally:
    ready.unlink(missing_ok=True)
    manager.close()
    for fd in (bell_writer, bell_reader, request_guard, request_fd):
      os.close(fd)
    preload_pool.cleanup_session(directory)


if __name__ == "__main__":
  sys.exit(main())
