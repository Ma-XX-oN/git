#!/usr/bin/env python3
"""Exercise real pool shutdown while worker startup has not registered its child."""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch


class Worker:
  """Minimal subprocess fixture owned by the pool under test."""

  def __init__(self):
    """Start alive with the same READY stream shape as a native worker."""
    self.pid = 987654
    self.returncode = None
    self.stdout = io.BytesIO(f"READY {self.pid}\n".encode())

  def poll(self):
    """Return the fixture's process status, or None while alive."""
    return self.returncode

  def kill(self):
    """Record pool-initiated process termination."""
    self.returncode = -1

  def wait(self, timeout=None):
    """Return the terminated status without performing operating-system waits.

    Parameters
    ----------
    timeout : float, optional
        Pool-supplied deadline in seconds; fixture termination is immediate.

    Returns
    -------
    int
        Terminated status; an alive process indicates missing pool cleanup.
    """
    assert self.returncode is not None, "pool waited without terminating worker"
    return self.returncode


def check_close_during_spawn(pools):
  """Hold process startup across the shutdown snapshot and check ownership.

  Parameters
  ----------
  pools : module
      Production preload_pool module, loaded without starting a manager.
  """
  entered, release, finished = threading.Event(), threading.Event(), threading.Event()
  worker = Worker()
  failures = []

  def popen(*args, **kwargs):
    """Hold the supplied subprocess arguments until the test releases startup."""
    entered.set()
    assert release.wait(5), "fixture startup was not released"
    return worker

  with tempfile.TemporaryDirectory(prefix="preload-manager-lifecycle-") as directory:
    pool = pools.Pool("git", Path("unused"), Path(directory), {}, lambda: None)

    def close():
      """Record shutdown completion or its exception for the main test thread."""
      try:
        pool.close()
      except Exception as error:
        failures.append(error)
      finally:
        finished.set()

    with patch.object(pools.subprocess, "Popen", side_effect=popen):
      starter = threading.Thread(target=pool.spawn)
      closer = threading.Thread(target=close)
      starter.start()
      try:
        assert entered.wait(5), "pool did not enter process startup"
        closer.start()
        assert not finished.wait(.05), "pool closed before in-flight startup completed"
      finally:
        release.set()
        starter.join(5)
        if closer.ident is not None:
          closer.join(5)
      assert not starter.is_alive() and not closer.is_alive(), "pool shutdown stuck"
      assert not failures, failures
      assert worker.poll() is not None, "pool leaked a late-starting worker"
      assert worker.stdout.closed, "pool retained the startup stream"
  print("PRELOAD_MANAGER_LIFECYCLE_PASS close-during-spawn", flush=True)


class GatedStream:
  """READY stream that is withheld until the test opens its gate."""

  def __init__(self, line, gate):
    """Hold one READY line behind gate (a threading.Event)."""
    self.line = line
    self.gate = gate
    self.closed = False

  def readline(self):
    """Return the READY line once the gate opens."""
    assert self.gate.wait(10), "gate never opened"
    return self.line

  def close(self):
    """Record that the pool released the startup stream."""
    self.closed = True


def check_growth_on_shortage(pools):
  """A request that finds no READY worker grows the pool by one.

  Parameters
  ----------
  pools : module
      Production preload_pool module.
  """
  started = []
  gate = threading.Event()
  gate.set()  # The initial workers report READY at once.

  def popen(*args, **kwargs):
    """Start a fixture worker whose READY line waits for the gate."""
    worker = Worker()
    worker.pid = 1000 + len(started)
    worker.stdout = GatedStream(f"READY {worker.pid}\n".encode(), gate)
    started.append(worker)
    return worker

  with tempfile.TemporaryDirectory(prefix="preload-manager-growth-") as directory:
    manager = pools.Manager(Path(directory), {})
    with patch.object(pools.subprocess, "Popen", side_effect=popen), \
         patch.object(pools.Path, "is_file", return_value=True):
      assert manager.preload("git", "unused") == "OK"
      pool = manager.pools["git"]
      gate.clear()  # Replacements now stay unREADY: the next request is short.
      taken = [manager.allocate("git") for _ in range(pools.INITIAL_WORKERS)]
      assert all(not isinstance(worker, str) for worker in taken), taken
      result = []
      waiter = threading.Thread(target=lambda: result.append(manager.allocate("git")))
      waiter.start()
      for _ in range(500):
        if pool.target > pools.INITIAL_WORKERS:
          break
        threading.Event().wait(.01)
      gate.set()
      waiter.join(10)
      assert result and not isinstance(result[0], str), result
      assert pool.target == pools.INITIAL_WORKERS + 1, pool.target
      manager.close()
  print("PRELOAD_MANAGER_LIFECYCLE_PASS growth target=" + str(pool.target), flush=True)


def check_prepared_resource_cleanup(pools):
  """Release prepared resources on idle death, replacement and shutdown.

  Parameters
  ----------
  pools : module
      Production preload_pool module; real descriptors model owned handles.
  """
  prepared = []

  def prepare(worker):
    """Give the fixture worker an owned descriptor before it becomes READY."""
    worker.resource = os.open(os.devnull, os.O_RDONLY)
    prepared.append(worker)

  def release(worker):
    """Close the fixture's resource once, matching the Windows handle callback."""
    descriptor = getattr(worker, "resource", None)
    worker.resource = None
    if descriptor is not None:
      os.close(descriptor)

  with tempfile.TemporaryDirectory(prefix="preload-prepared-") as directory:
    pool = pools.Pool("git", Path("unused"), Path(directory), {}, lambda: None,
                      prepare=prepare, release=release)
    try:
      with patch.object(pools.subprocess, "Popen", side_effect=lambda *a, **kw: Worker()):
        pool.spawn()
      pool.close()
      assert prepared[0].resource is None, "pool shutdown leaked prepared resource"
      manager = pools.Manager(Path(directory), {}, prepare, release)
      try:
        with patch.object(pools.subprocess, "Popen", side_effect=lambda *a, **kw: Worker()), \
             patch.object(pools.Path, "is_file", return_value=True):
          assert manager.preload("git", "unused") == "OK"
          previous = manager.pools["git"]
          old_workers = list(previous.children)
          dead = previous.ready.queue[0]
          dead.returncode = 0
          with patch.object(previous, "request_spawn"):
            allocated = previous.take()
          assert dead.resource is None, "discarded idle worker leaked resource"
          assert allocated.resource is not None, "pool closed an allocated resource"
          release(allocated)  # Ownership transferred to the server.
          allocated.kill()
          assert manager.preload("git", "unused") == "OK"
          assert all(worker.resource is None for worker in old_workers), \
              "pool replacement leaked resources"
      finally:
        manager.close()
        manager.close()  # Release callbacks must tolerate repeated shutdown.
      assert all(worker.resource is None for worker in prepared), "manager leaked resource"
    finally:
      for worker in prepared:
        release(worker)
  print("PRELOAD_MANAGER_LIFECYCLE_PASS prepared-resource-shutdown-death-replacement-transfer",
        flush=True)


def main():
  """Run the lifecycle checks against the shared pool implementation."""
  root = Path(__file__).resolve().parents[2] / "contrib/preload"
  spec = importlib.util.spec_from_file_location("preload_pool", root / "preload_pool.py")
  pools = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(pools)
  check_close_during_spawn(pools)
  check_growth_on_shortage(pools)
  check_prepared_resource_cleanup(pools)


if __name__ == "__main__":
  main()
