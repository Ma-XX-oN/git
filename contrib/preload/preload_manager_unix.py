#!/usr/bin/env python3
"""Generic preload manager for Linux and macOS.

Replies are delivered through mailboxes: SLOTS FIFOs `<directory>/slot.<n>`.
For each request a client first claims a free mailbox by atomically creating
`<directory>/slot.<n>.lock` (Bash `noclobber`) containing its $BASHPID, then
writes one line to the FIFO `<directory>/request`:

  PRELOAD <name> <n> <executable-path>  ->  OK | ERROR <reason>
  GET <name> <n>                        ->  <directory>/worker-<pid> | ERROR <reason>
  STOP <n>                              ->  OK; the manager exits after that
                                            caller's FREE (at most 5 s later)
  FREE <n>                              ->  (no reply) release mailbox <n>

The reply line is written into `slot.<n>`, where the client is blocked in
`read`, so it wakes immediately: no polling. The manager holds both ends of
every mailbox, so a client's open never blocks and, if the manager dies, the
client's read ends with EOF instead of hanging. FREE drains the mailbox and
removes its lock; a lock whose $BASHPID has exited is reclaimed the same way.
"""
from __future__ import annotations
import argparse
import errno
import json
import os
from pathlib import Path
import select
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preload_pool  # noqa: E402
from preload_pool import log  # noqa: E402

SLOTS = 32
STOP_FREE_SECONDS = 5


def owner_alive(pid):
  """Check whether a process still exists.

  Parameters
  ----------
  pid : int
      Process ID.

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


class Mailbox:
  """One reply FIFO with both ends held by the manager.

  Parameters
  ----------
  directory : Path
      Session directory.
  index : int
      Mailbox number.
  """

  def __init__(self, directory, index):
    self.path = directory / f"slot.{index}"
    self.lock = directory / f"slot.{index}.lock"
    os.mkfifo(self.path, 0o600)
    # The reader lets the writer open without blocking and keeps the FIFO
    # from ever lacking a reader; it is only used to drain stale replies.
    self.reader = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
    self.writer = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)

  def owner(self):
    """Return the claiming client's PID, or None if unclaimed or unreadable."""
    try:
      text = self.lock.read_text(encoding="ascii").strip()
    except (FileNotFoundError, UnicodeDecodeError):
      return None
    return int(text) if text.isdigit() else None

  def deliver(self, text):
    """Write one reply line into the mailbox.

    Parameters
    ----------
    text : str
        Reply line without newline; short enough to be written atomically.

    Returns
    -------
    bool
        False if the claiming client no longer exists (nothing is written).
    """
    pid = self.owner()
    if pid is None or not owner_alive(pid):
      return False
    os.write(self.writer, (text + "\n").encode("utf-8"))
    return True

  def release(self):
    """Drain any unread reply and remove the lock, freeing the mailbox."""
    while True:
      try:
        if not os.read(self.reader, 4096):
          break
      except BlockingIOError:
        break
    self.lock.unlink(missing_ok=True)

  def close(self):
    """Close both ends."""
    os.close(self.writer)
    os.close(self.reader)


class Server:
  """Dispatch request lines and deliver replies through mailboxes.

  Parameters
  ----------
  directory : Path
      Private session directory holding the FIFOs.
  manager : preload_pool.Manager
      Pools serving PRELOAD and GET requests.
  """

  def __init__(self, directory, manager):
    self.directory = directory
    self.manager = manager
    self.mailboxes = [Mailbox(directory, index) for index in range(SLOTS)]
    self.stopped = threading.Event()
    # After replying to STOP, keep serving until that caller's FREE arrives
    # (or STOP_FREE_SECONDS pass, or the caller exits), so the request FIFO
    # stays open for it.
    self.stop_slot = None
    self.stop_deadline = None

  def mailbox(self, word):
    """Return the mailbox named by a request word, or None if invalid."""
    if word.isdigit() and int(word) < SLOTS:
      return self.mailboxes[int(word)]
    return None

  def handle(self, line):
    """Execute one request line; slow requests run on their own thread.

    Parameters
    ----------
    line : str
        Request line without newline.
    """
    words = line.split(" ", 3)  # The executable path may contain spaces.
    command = words[0]
    if command == "FREE" and len(words) == 2 and self.mailbox(words[1]):
      self.mailbox(words[1]).release()
      if words[1] == self.stop_slot:
        self.stopped.set()
    elif command == "GET" and len(words) == 3 and self.mailbox(words[2]):
      threading.Thread(target=self._get, args=(words[1], self.mailbox(words[2])),
                       daemon=True).start()
    elif command == "PRELOAD" and len(words) == 4 and self.mailbox(words[2]):
      threading.Thread(target=self._preload, daemon=True,
                       args=(words[1], self.mailbox(words[2]), words[3])).start()
    elif command == "STOP" and len(words) == 2 and self.mailbox(words[1]):
      log("MANAGER_STOP_REQUEST")
      if self.mailbox(words[1]).deliver("OK"):
        self.stop_slot = words[1]
        self.stop_deadline = time.monotonic() + STOP_FREE_SECONDS
      else:
        self.stopped.set()  # The caller is gone; nobody will FREE.
    else:
      log("INVALID_REQUEST " + repr(line))

  def _get(self, name, mailbox):
    """Allocate one worker for a caller."""
    result = self.manager.allocate(name)
    if isinstance(result, str):
      mailbox.deliver(result)
    elif not mailbox.deliver(str(self.directory / ("worker-" + str(result.pid)))):
      # The caller exited and never learned of this worker; reap it too, so
      # it does not remain as a zombie.
      result.kill()
      result.wait(timeout=5)

  def _preload(self, name, mailbox, path):
    """Create a command's pool for a caller."""
    mailbox.deliver(self.manager.preload(name, path))

  def reclaim(self):
    """Free mailboxes whose claiming client has exited."""
    for mailbox in self.mailboxes:
      pid = mailbox.owner()
      if pid is not None and not owner_alive(pid):
        log(f"MAILBOX_RECLAIMED {mailbox.path.name} pid={pid}")
        mailbox.release()

  def close(self):
    """Close every mailbox."""
    for mailbox in self.mailboxes:
      mailbox.close()


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
  request = directory / "request"
  ready = directory / "ready.json"
  os.mkfifo(request, 0o600)
  # Hold a writer so the reader never sees EOF between clients.
  request_fd = os.open(request, os.O_RDONLY | os.O_NONBLOCK)
  request_guard = os.open(request, os.O_WRONLY | os.O_NONBLOCK)
  server = Server(directory, manager)
  incoming = bytearray()
  next_reclaim = 0.0
  try:
    temporary = directory / "ready.tmp"
    temporary.write_text(json.dumps({
      "state": "READY", "manager_pid": os.getpid(),
      "owner_pid": args.owner_pid, "slots": SLOTS}) + "\n", encoding="utf-8")
    temporary.replace(ready)
    log("READY " + str(request))
    while not server.stopped.is_set():
      if not owner_alive(args.owner_pid):
        log(f"OWNER_EXIT {args.owner_pid}")
        break
      now = time.monotonic()
      if server.stop_slot and now >= server.stop_deadline:
        log("STOP_FREE_TIMEOUT")
        break
      if now >= next_reclaim:
        server.reclaim()
        next_reclaim = now + 0.25
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
    server.close()
    os.close(request_guard)
    os.close(request_fd)
    preload_pool.cleanup_session(directory)


if __name__ == "__main__":
  sys.exit(main())
