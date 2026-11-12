#!/usr/bin/env python3
"""Check READY dispatch, shortage isolation, mailbox ordering and DONE handling.

Run with python -m unittest discover -s preload/tests -p test_manager_dispatch.py.
Transport calls are mocked so the Unix dispatch checks also run on Windows.
"""
import os
import queue
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preload/manager"))
import manager_unix as unix
import pool as preload_pool


class DispatchTests(unittest.TestCase):
  """Exercise real mailbox threads with deterministic request barriers."""

  def setUp(self):
    """Create two mailboxes without requiring native FIFO support."""
    with patch.object(unix.os, "mkfifo", create=True), \
         patch.object(unix.os, "O_NONBLOCK", 0, create=True), \
         patch.object(unix.os, "open", return_value=10):
      self.mailboxes = [unix.Mailbox(Path("session"), n) for n in range(2)]
    self.replies = queue.Queue()
    for n, mailbox in enumerate(self.mailboxes):
      mailbox.deliver = Mock(side_effect=lambda text, worker=None, n=n:
                             self.replies.put((n, text)) or True)
    self.server = unix.Server.__new__(unix.Server)
    self.server.directory = Path("session")
    self.server.mailboxes = self.mailboxes
    self.server.manager = Mock()
    self.worker = Mock(pid=123)
    self.server.manager.allocate.return_value = self.worker

  def tearDown(self):
    """Stop the daemon consumers after every queued operation finishes."""
    for mailbox in self.mailboxes:
      mailbox.tasks.put(None)

  def test_ready_replies_on_dispatch_thread_before_replenishing(self):
    """A READY worker bypasses the mailbox thread and is replaced after reply."""
    caller = threading.get_ident()
    observed = []
    self.mailboxes[0].deliver.side_effect = lambda *args, **kwargs: observed.append(
        ("reply", threading.get_ident())) or True
    self.server.manager.replenish.side_effect = lambda name: observed.append(
        ("refill", threading.get_ident()))
    self.server.handle("GET git 0")
    self.assertEqual(observed, [("reply", caller), ("refill", caller)])
    self.server.manager.allocate.assert_called_once_with("git", wait=False)
    self.assertEqual(self.mailboxes[0].outstanding, 0)

  def test_shortage_does_not_block_another_mailbox_or_overtake(self):
    """A waiting request preserves its mailbox order and leaves others runnable."""
    entered, release = threading.Event(), threading.Event()
    order = []

    def allocate(name, wait=True):
      """Hold the first shortage until the independent mailbox has replied."""
      if name == "slow":
        if not wait:
          return None
        entered.set()
        if not release.wait(5):
          raise AssertionError("shortage not released")
      order.append(name)
      return self.worker

    self.server.manager.allocate.side_effect = allocate
    try:
      self.server.handle("GET slow 0")
      self.assertTrue(entered.wait(5))
      self.server.handle("GET later 0")
      self.server.handle("GET independent 1")
      self.assertEqual(self.replies.get(timeout=5)[0], 1)
      self.assertEqual(order, ["independent"])
    finally:
      release.set()
    self.assertEqual(self.replies.get(timeout=5)[0], 0)
    self.assertEqual(self.replies.get(timeout=5)[0], 0)
    self.assertEqual(order, ["independent", "slow", "later"])

  def test_failed_delivery_reaps_worker_and_replenishes(self):
    """A reclaimed mailbox cannot leak its allocated worker."""
    self.mailboxes[0].deliver.side_effect = None
    self.mailboxes[0].deliver.return_value = False
    self.server.handle("GET git 0")
    self.worker.kill.assert_called_once_with()
    self.worker.wait.assert_called_once_with(timeout=5)
    self.server.manager.replenish.assert_called_once_with("git")

  def test_worker_reply_is_eight_hex_bytes_without_newline(self):
    """Encode a worker PID as one atomic fixed-width reply and retain ownership."""
    mailbox = self.mailboxes[0]
    with patch.object(Path, "exists", return_value=True), \
         patch.object(unix.os, "write") as write:
      unix.Mailbox.deliver(mailbox, "0000007b", worker=self.worker)
    write.assert_called_once_with(10, b"0000007b")
    self.assertIs(mailbox.pending, self.worker)

  def test_get_error_has_zero_pid_header_and_diagnostic(self):
    """An allocation failure uses the fixed header without losing its reason."""
    self.server.manager.allocate.return_value = "ERROR not preloaded: missing"
    self.server.handle("GET missing 0")
    self.mailboxes[0].deliver.assert_called_once_with(
        "00000000ERROR not preloaded: missing")

  def test_nonblocking_shortage_does_not_grow_pool(self):
    """An empty fast-path attempt leaves growth to the queued blocking request."""
    manager = preload_pool.Manager.__new__(preload_pool.Manager)
    manager.changed = threading.Condition()
    manager.closing = threading.Event()
    manager.allocated = {}
    pool = Mock()
    pool.take.return_value = None
    manager.pools = {"git": pool}
    self.assertIsNone(manager.allocate("git", wait=False))
    pool.grow.assert_not_called()
    self.assertEqual(manager.allocate("missing", wait=False),
                     "ERROR not preloaded: missing")

  def test_nonblocking_ready_transfers_worker_without_growing(self):
    """An immediate allocation transfers the same worker and leaves refill to caller."""
    manager = preload_pool.Manager.__new__(preload_pool.Manager)
    manager.changed = threading.Condition()
    manager.closing = threading.Event()
    manager.allocated = {}
    pool = Mock()
    pool.take.return_value = self.worker
    manager.pools = {"git": pool}
    self.assertIs(manager.allocate("git", wait=False), self.worker)
    pool.grow.assert_not_called()
    pool.request_spawn.assert_not_called()
    # Retained until DONE, so DONE STATUS can report a death mid-command.
    self.assertIs(manager.allocated[123], self.worker)

  def test_done_status_runs_on_mailbox_thread(self):
    """DONE <pid> <n> STATUS replies through the caller's mailbox."""
    self.server.manager.finish.return_value = "SIGNAL 11"
    self.server.handle("DONE 123 1 STATUS")
    self.assertEqual(self.replies.get(timeout=5), (1, "SIGNAL 11"))
    self.server.manager.finish.assert_called_once_with("123", report=True)

  def test_done_without_status_has_no_reply(self):
    """DONE <pid> ends the call on the dispatch thread without any mailbox."""
    self.server.handle("DONE 123")
    self.server.manager.finish.assert_called_once_with("123")
    for mailbox in self.mailboxes:
      mailbox.deliver.assert_not_called()

  def test_preload_finishes_before_same_mailbox_get(self):
    """GET cannot bypass an earlier PRELOAD still executing on its mailbox."""
    entered, release = threading.Event(), threading.Event()

    def preload(name, path):
      """Keep registration outstanding until its ordering has been checked."""
      entered.set()
      if not release.wait(5):
        raise AssertionError("PRELOAD not released")
      return "OK"

    self.server.manager.preload.side_effect = preload
    try:
      self.server.handle("PRELOAD git 0 /git")
      self.assertTrue(entered.wait(5))
      self.server.handle("GET git 0")
      self.server.manager.allocate.assert_not_called()
    finally:
      release.set()
    self.assertEqual(self.replies.get(timeout=5), (0, "OK"))
    self.assertEqual(self.replies.get(timeout=5)[0], 0)
    self.server.manager.allocate.assert_called_once_with("git", wait=True)


class FinishTests(unittest.TestCase):
  """Check how Manager.finish() ends calls and reports workers that died."""

  def setUp(self):
    """Create a manager without pools, threads or processes."""
    self.manager = preload_pool.Manager.__new__(preload_pool.Manager)
    self.manager.lock = threading.Lock()
    self.manager.allocated = {}

  def allocate(self, returncode):
    """Record one allocated worker whose wait() returns returncode."""
    worker = Mock(pid=77)
    worker.wait.return_value = returncode
    self.manager.allocated[77] = worker
    return worker

  def test_done_forgets_without_waiting(self):
    """DONE <pid> removes the record, needs no reply and never waits."""
    worker = self.allocate(0)
    self.assertIsNone(self.manager.finish("77"))
    self.assertEqual(self.manager.allocated, {})
    worker.wait.assert_not_called()

  def test_exit_code(self):
    """A process exit code, including an unsigned Windows NTSTATUS, is raw."""
    self.allocate(3221225477)
    self.assertEqual(self.manager.finish("77", report=True), "EXIT 3221225477")
    self.assertEqual(self.manager.allocated, {})

  def test_signal(self):
    """A POSIX signal death (negative returncode) is reported as SIGNAL."""
    self.allocate(-11)
    self.assertEqual(self.manager.finish("77", report=True), "SIGNAL 11")

  def test_still_running(self):
    """A worker that does not exit within the timeout is an error."""
    worker = self.allocate(None)
    worker.wait.side_effect = preload_pool.subprocess.TimeoutExpired("git", 1)
    self.assertEqual(self.manager.finish("77", report=True),
                     "ERROR worker still running 77")

  def test_unknown_and_invalid(self):
    """Only decimal PIDs of retained allocations are answered."""
    self.assertEqual(self.manager.finish("78", report=True), "ERROR unknown worker 78")
    self.assertEqual(self.manager.finish("x", report=True), "ERROR invalid worker PID")
    self.assertIsNone(self.manager.finish("x"))


@unittest.skipUnless(os.name == "nt", "the Windows manager imports only on Windows")
class RequestScannerTests(unittest.TestCase):
  """Check where the Windows manager stops relaying a held GET request."""

  REQUEST = (b"\x02\x00\x00\x00" + b"git\0" + b"rev-parse\0" + b"end\0" +
             b"cwd\0" + b"C:/repo\n\0" + b"end\0")

  def test_whole_request(self):
    """The end is just past the `end` tag; an `end` parameter is not it."""
    import manager_windows
    data = self.REQUEST + b"STATUS\n"
    self.assertEqual(manager_windows.RequestScanner().scan(data), len(self.REQUEST))

  def test_byte_by_byte(self):
    """Resumed scans find the same end as one complete scan."""
    import manager_windows
    scanner = manager_windows.RequestScanner()
    data = bytearray()
    ends = []
    for byte in self.REQUEST:
      data.append(byte)
      ends.append(scanner.scan(data))
    self.assertEqual(ends[:-1], [None] * (len(self.REQUEST) - 1))
    self.assertEqual(ends[-1], len(self.REQUEST))


if __name__ == "__main__":
  unittest.main()
