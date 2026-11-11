#!/usr/bin/env python3
"""Generic preload manager for Linux and macOS.

Clients write one line to the FIFO `<directory>/request`. `<token>` is
`<pid>.<seq>`: the caller's $BASHPID and a per-call counter.

  PRELOAD <name> <token> <executable-path>  ->  OK | ERROR <reason>
  GET <name> <token>                        ->  <directory>/worker-<pid> | ERROR <reason>
  STOP <token>                              ->  OK; the manager exits after that
                                                caller's ACK (at most 5 s later)
  ACK <token>                               ->  (no reply) delete reply.<token>

A reply is written to a temporary file and renamed to `<directory>/reply.<token>`,
so a caller that polls for it never reads a partial reply. The per-call token
keeps an earlier reply that has not been deleted yet from matching. The
manager holds the write end of the FIFO `<directory>/bell` open without ever
writing to it, so a caller can sleep in `read -t` on it between checks.
(Bash's `read` is not interrupted by a trapped signal, so signals cannot be
used to wake it.)
"""
from __future__ import annotations
import argparse
import errno
import json
import os
from pathlib import Path
import re
import select
import sys
import threading
import time

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


TOKEN = re.compile(r"^([0-9]+)\.[0-9]+$")
STOP_ACK_SECONDS = 5


class Server:
  """Dispatch request lines and deliver replies through files.

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
    # After replying to STOP, keep serving until that caller's ACK arrives
    # (or STOP_ACK_SECONDS pass, or the caller exits), so the reply is never
    # deleted before it is read and the request FIFO stays open for the ACK.
    self.stop_token = None
    self.stop_deadline = None

  def reply(self, token, text):
    """Publish one reply atomically.

    Parameters
    ----------
    token : str
        Caller's `<pid>.<seq>` call token.
    text : str
        Reply line without newline.

    Returns
    -------
    bool
        False if the caller no longer exists; no reply file is left.
    """
    pid = int(TOKEN.match(token).group(1))
    if not owner_alive(pid):
      return False
    path = self.directory / ("reply." + token)
    temporary = self.directory / ("reply-tmp." + token)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
      os.write(fd, (text + "\n").encode("utf-8"))
    finally:
      os.close(fd)
    os.replace(temporary, path)
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
    if command == "ACK" and len(words) == 2 and TOKEN.match(words[1]):
      (self.directory / ("reply." + words[1])).unlink(missing_ok=True)
      if words[1] == self.stop_token:
        self.stopped.set()
    elif command == "GET" and len(words) == 3 and TOKEN.match(words[2]):
      threading.Thread(target=self._get, args=(words[1], words[2]),
                       daemon=True).start()
    elif command == "PRELOAD" and len(words) == 4 and TOKEN.match(words[2]):
      threading.Thread(target=self._preload, daemon=True,
                       args=(words[1], words[2], words[3])).start()
    elif command == "STOP" and len(words) == 2 and TOKEN.match(words[1]):
      log("MANAGER_STOP_REQUEST")
      if self.reply(words[1], "OK"):
        self.stop_token = words[1]
        self.stop_deadline = time.monotonic() + STOP_ACK_SECONDS
      else:
        self.stopped.set()  # The caller is gone; nobody will ACK.
    else:
      log("INVALID_REQUEST " + repr(line))

  def _get(self, name, token):
    """Allocate one worker for a caller."""
    result = self.manager.allocate(name)
    if isinstance(result, str):
      self.reply(token, result)
    elif not self.reply(token, str(self.directory / ("worker-" + str(result.pid)))):
      # The caller exited and never learned of this worker; reap it too, so
      # it does not remain as a zombie.
      result.kill()
      result.wait(timeout=5)

  def _preload(self, name, token, path):
    """Create a command's pool for a caller."""
    self.reply(token, self.manager.preload(name, path))


def main():
  """Run the manager until STOP or the owner process exits."""
  parser = argparse.ArgumentParser()
  parser.add_argument("--owner-pid", required=True, type=int)
  parser.add_argument("--directory", required=True, type=Path)
  args = parser.parse_args()
  # Keep the client's spelling: replies are checked against $PRELOAD_DIR, and
  # resolving symlinks would turn macOS /var/folders into /private/var/folders.
  directory = Path(os.path.abspath(args.directory))
  if not directory.is_dir():
    parser.error("session directory does not exist: " + str(directory))
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
      if server.stop_token and time.monotonic() >= server.stop_deadline:
        log("STOP_ACK_TIMEOUT")
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
