#!/usr/bin/env python3
"""Check READY dispatch, shortage isolation and mailbox request ordering.

Run with python -m unittest discover -s t/preload -p test_manager_dispatch.py.
Transport calls are mocked so the Unix dispatch checks also run on Windows.
"""
import queue
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "contrib/preload"))
import preload_manager_unix as unix
import preload_pool


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

  def test_nonblocking_shortage_does_not_grow_pool(self):
    """An empty fast-path attempt leaves growth to the queued blocking request."""
    manager = preload_pool.Manager.__new__(preload_pool.Manager)
    manager.changed = threading.Condition()
    manager.closing = threading.Event()
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
    pool = Mock()
    pool.take.return_value = self.worker
    manager.pools = {"git": pool}
    self.assertIs(manager.allocate("git", wait=False), self.worker)
    pool.grow.assert_not_called()
    pool.request_spawn.assert_not_called()

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


if __name__ == "__main__":
  unittest.main()
