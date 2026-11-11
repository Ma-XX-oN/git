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
import functools
import json
import os
import queue
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
    # The worker named by the most recent reply; killed on release if that
    # reply was never read (its client exited before collecting it).
    self.pending = None
    # One long-lived thread per mailbox serves its requests in order: handing
    # a request to a waiting thread is much cheaper than starting one, and a
    # request waiting for a worker blocks only its own mailbox.
    self.tasks = queue.Queue()
    self.dispatch_lock = threading.Lock()
    self.outstanding = 0
    threading.Thread(target=self._serve, daemon=True).start()

  def submit(self, task, immediate=None):
    """Run an idle mailbox's nonblocking attempt, or queue its task in order.

    Parameters
    ----------
    task : callable
        No-argument operation for the mailbox thread; its result is ignored.
    immediate : callable, optional
        No-argument nonblocking operation, called only with no outstanding
        task. Return True if handled, False to queue task. The dispatch lock
        prevents a queued or executing request from being overtaken.
    """
    with self.dispatch_lock:
      if self.outstanding == 0 and immediate is not None and immediate():
        return
      self.outstanding += 1
      self.tasks.put(task)

  def _serve(self):
    """Run this mailbox's requests until close() queues None."""
    while True:
      task = self.tasks.get()
      if task is None:
        return
      try:
        task()
      except Exception as error:  # Keep serving later requests.
        log(f"MAILBOX_TASK_FAILED {self.path.name} {error!r}")
      finally:
        with self.dispatch_lock:
          self.outstanding -= 1

  def owner(self):
    """Return the claiming client's PID, or None if unclaimed or unreadable."""
    try:
      text = self.lock.read_text(encoding="ascii").strip()
    except (FileNotFoundError, UnicodeDecodeError):
      return None
    return int(text) if text.isdigit() else None

  def deliver(self, text, worker=None):
    """Write one reply line into the mailbox.

    Only checks that the mailbox is still claimed (one stat); whether its
    client is alive is checked by the periodic reclaim, which kills a worker
    whose reply was never read.

    Parameters
    ----------
    text : str
        Reply line without newline; short enough to be written atomically.
    worker : subprocess.Popen, optional
        Worker named by the reply, remembered until the next reply.

    Returns
    -------
    bool
        False if the mailbox is no longer claimed (nothing is written).
    """
    if not self.lock.exists():
      return False
    os.write(self.writer, (text + "\n").encode("utf-8"))
    self.pending = worker
    return True

  def release(self):
    """Discard any unread reply and remove the lock, freeing the mailbox.

    An unread reply means its client exited without collecting it, so the
    worker it named is killed and reaped.
    """
    unread = False
    while True:
      try:
        if not os.read(self.reader, 4096):
          break
        unread = True
      except BlockingIOError:
        break
    worker, self.pending = self.pending, None
    if unread and worker is not None:
      log(f"UNCOLLECTED_WORKER_KILLED {self.path.name} pid={worker.pid}")
      worker.kill()
      worker.wait(timeout=5)
    self.lock.unlink(missing_ok=True)

  def close(self):
    """Stop the mailbox thread and close both ends."""
    self.tasks.put(None)
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
    """Execute one request line; GET and PRELOAD run on the mailbox's thread.

    An idle mailbox's GET first tries a nonblocking allocation here, avoiding
    the thread handoff when a worker is READY. Shortages remain on its thread.

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
      mailbox = self.mailbox(words[2])
      task = functools.partial(self._get, words[1], mailbox, time.time())
      mailbox.submit(task, functools.partial(task, wait=False))
    elif command == "PRELOAD" and len(words) == 4 and self.mailbox(words[2]):
      mailbox = self.mailbox(words[2])
      mailbox.submit(functools.partial(self._preload, words[1], mailbox, words[3]))
    elif command == "STOP" and len(words) == 2 and self.mailbox(words[1]):
      log("MANAGER_STOP_REQUEST")
      if self.mailbox(words[1]).deliver("OK"):
        self.stop_slot = words[1]
        self.stop_deadline = time.monotonic() + STOP_FREE_SECONDS
      else:
        self.stopped.set()  # The caller is gone; nobody will FREE.
    else:
      log("INVALID_REQUEST " + repr(line))

  def _get(self, name, mailbox, received, wait=True):
    """Allocate one worker for a caller.

    Parameters
    ----------
    name : str
        Requested command name.
    mailbox : Mailbox
        Claimed reply mailbox.
    received : float
        Wall-clock time the request line was parsed, for PRELOAD_TIMING.
    wait : bool, optional
        Whether allocation may wait for a worker; False is the dispatch fast path.

    Returns
    -------
    bool
        True when handled, including an error reply; False when no worker was
        READY in the nonblocking attempt, with no reply or replenishment.
    """
    started = time.time()
    result = self.manager.allocate(name, wait=wait)
    if result is None:
      return False
    allocated = time.time()
    if isinstance(result, str):
      mailbox.deliver(result)
      delivered = time.time()
    else:
      if not mailbox.deliver(str(self.directory / ("worker-" + str(result.pid))),
                             worker=result):
        # The mailbox was already reclaimed: its caller exited and will never
        # learn of this worker. Reap it too, so it does not remain a zombie.
        result.kill()
        result.wait(timeout=5)
      delivered = time.time()
      # Start the replacement only now, so it cannot delay the reply.
      self.manager.replenish(name)
    if preload_pool.TIMING:
      log(f"TIMING GET received={received:.6f} thread={started:.6f} "
          f"allocated={allocated:.6f} delivered={delivered:.6f}")
    return True

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
