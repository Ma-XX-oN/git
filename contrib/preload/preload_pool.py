"""Platform-independent worker pools for the generic preload manager.

One manager serves every preloaded command. Each command has its own Pool of
single-use `<command> --preload-worker <session-directory>` processes. A pool
starts with three READY workers, starts one replacement per allocation, and
grows by one worker whenever a request finds no READY worker.
"""
from __future__ import annotations
import os
from pathlib import Path
import queue
import subprocess
import threading

INITIAL_WORKERS = 3
MAX_WORKERS = 32
SUPPORT_TIMEOUT = 1.0
REPLACEMENT_TIMEOUT = 10.0


def log(message):
  """Write one line to the manager log (the manager's stdout).

  Parameters
  ----------
  message : str
      Single-line event text.
  """
  print(message, flush=True)


class Pool:
  """READY workers for one preloaded command.

  Parameters
  ----------
  name : str
      Generated function name, used in GET requests.
  executable : Path
      Native executable that supports --preload-worker.
  directory : Path
      Existing private session directory passed to every worker.
  environment : dict
      Complete worker environment, including PRELOAD_PID.
  notify : callable
      Called without arguments whenever a worker becomes READY or fails.
  """

  def __init__(self, name, executable, directory, environment, notify):
    self.name = name
    self.executable = executable
    self.directory = directory
    self.environment = environment
    self.notify = notify
    self.ready = queue.Queue()
    self.children = []
    self.lock = threading.Lock()
    self.spawn_done = threading.Condition(self.lock)
    self.spawning = 0
    self.target = INITIAL_WORKERS
    self.closing = threading.Event()
    self.errors = []

  def start(self):
    """Start the initial workers and require each READY within one second.

    Returns
    -------
    bool
        True when all initial workers reported READY; False otherwise, after
        killing every started worker. An unsupported command fails here.
    """
    threads = [threading.Thread(target=self.spawn, args=(SUPPORT_TIMEOUT,), daemon=True)
               for _ in range(INITIAL_WORKERS)]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join()
    if self.ready.qsize() == INITIAL_WORKERS and not self.errors:
      return True
    self.close()
    return False

  def spawn(self, timeout=REPLACEMENT_TIMEOUT):
    """Start one worker and publish it once it reports READY.

    Parameters
    ----------
    timeout : float
        Seconds allowed for the READY line; a worker that is still running
        without having sent it is killed.
    """
    with self.spawn_done:
      if self.closing.is_set():
        return
      self.spawning += 1
    try:
      self._spawn_worker(timeout)
    finally:
      with self.spawn_done:
        self.spawning -= 1
        self.spawn_done.notify_all()
      self.notify()

  def _spawn_worker(self, timeout):
    """Run one worker startup; see spawn()."""
    try:
      p = subprocess.Popen(
        [str(self.executable), "--preload-worker", str(self.directory)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=self.environment, bufsize=0)
    except OSError as error:
      with self.lock:
        self.errors.append("spawn failed: " + repr(error))
      log(f"WORKER_SPAWN_FAILED {self.name} {error!r}")
      return
    with self.lock:
      self.children.append(p)
      stopping = self.closing.is_set()
    if stopping:
      p.kill()
      p.wait(timeout=3)
      p.stdout.close()
      return
    # A program without --preload-worker support either exits (EOF) or keeps
    # running without READY; the timer kills the latter so readline returns.
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    try:
      line = p.stdout.readline()
    finally:
      timer.cancel()
      p.stdout.close()
    if line != ("READY " + str(p.pid) + "\n").encode("ascii"):
      if p.poll() is None:
        p.kill()
      p.wait(timeout=3)
      with self.lock:
        self.children.remove(p)
        self.errors.append(f"no READY from pid={p.pid} response={line!r}")
      log(f"WORKER_NOT_READY {self.name} pid={p.pid} response={line!r}")
      return
    self.ready.put(p)
    log(f"WORKER_READY {self.name} {p.pid}")

  def take(self):
    """Remove one live READY worker without waiting.

    Returns
    -------
    subprocess.Popen or None
        The allocated worker, now owned by the client, or None if none is
        READY. Each allocation starts one replacement, keeping the pool at
        its target size.
    """
    while True:
      try:
        p = self.ready.get_nowait()
      except queue.Empty:
        return None
      with self.lock:
        self.children.remove(p)
      threading.Thread(target=self.spawn, daemon=True).start()
      if p.poll() is None:
        log(f"ALLOCATED {self.name} {p.pid}")
        return p

  def grow(self):
    """Add one worker to the pool's target size, up to MAX_WORKERS.

    Called once for each request that found no READY worker, so a workload
    that outpaces the pool raises its steady-state size.
    """
    with self.lock:
      if self.target >= MAX_WORKERS:
        return
      self.target += 1
    log(f"POOL_GROW {self.name} target={self.target}")
    threading.Thread(target=self.spawn, daemon=True).start()

  def close(self):
    """Kill workers still owned by the pool and wait for pending startups."""
    self.closing.set()
    with self.lock:
      children = list(self.children)
    for p in children:
      if p.poll() is None:
        p.kill()
    with self.spawn_done:
      self.spawn_done.wait_for(lambda: self.spawning == 0)
      children = list(self.children)
    for p in children:
      try:
        p.wait(timeout=3)
      except subprocess.TimeoutExpired:
        pass
      if p.stdout:
        p.stdout.close()


class Manager:
  """Pools for every preloaded command in one session.

  Parameters
  ----------
  directory : Path
      Existing private session directory.
  environment : dict
      Worker environment shared by all pools.
  """

  def __init__(self, directory, environment):
    self.directory = directory
    self.environment = environment
    self.pools = {}
    self.lock = threading.Lock()
    self.changed = threading.Condition(self.lock)
    self.closing = threading.Event()

  def _notify(self):
    """Wake requests waiting for a READY worker."""
    with self.changed:
      self.changed.notify_all()

  def preload(self, name, executable):
    """Create (or replace) the pool for one command.

    Parameters
    ----------
    name : str
        Function name the client will request.
    executable : str
        Native path of the executable.

    Returns
    -------
    str
        Reply line without newline: OK, or ERROR followed by a reason.
    """
    path = Path(executable)
    if not path.is_file():
      return "ERROR executable not found: " + executable
    pool = Pool(name, path, self.directory, self.environment, self._notify)
    if not pool.start():
      return f"ERROR {name} doesn't support --preload-worker"
    with self.lock:
      previous = self.pools.get(name)
      self.pools[name] = pool
    if previous is not None:
      previous.close()
    log(f"POOL_READY {name} {executable}")
    return "OK"

  def allocate(self, name):
    """Return a READY worker for one command, waiting while the pool grows.

    Parameters
    ----------
    name : str
        Requested function name.

    Returns
    -------
    subprocess.Popen or str
        The allocated worker, or an ERROR reply line.
    """
    with self.changed:
      pool = self.pools.get(name)
      if pool is None:
        return "ERROR not preloaded: " + name
      grown = False
      while not self.closing.is_set():
        p = pool.take()
        if p is not None:
          return p
        if not grown:
          pool.grow()
          grown = True
        elif pool.errors and pool.spawning == 0 and pool.ready.empty():
          return "ERROR workers failing for " + name + ": " + pool.errors[-1]
        self.changed.wait(0.5)
    return "ERROR manager stopping"

  def close(self):
    """Stop every pool."""
    self.closing.set()
    self._notify()
    with self.lock:
      pools = list(self.pools.values())
    for pool in pools:
      pool.close()


def cleanup_session(directory):
  """Remove worker directories left by killed workers and manager endpoints.

  Parameters
  ----------
  directory : Path
      Private session directory. Its worker-* directories, request/bell
      FIFOs and reply files are removed; manager.log and the directory itself
      remain for the sourcing shell, which owns them.
  """
  for worker in directory.glob("worker-*"):
    for entry in worker.iterdir():
      entry.unlink(missing_ok=True)
    worker.rmdir()
  for name in ("request", "bell"):
    (directory / name).unlink(missing_ok=True)
  for reply in directory.glob("reply*.*"):  # reply.<token>, reply-tmp.<token>
    reply.unlink(missing_ok=True)


def worker_environment(owner_pid, extra=None):
  """Copy the manager's environment for workers.

  Parameters
  ----------
  owner_pid : int
      Native PID whose death ends the session; workers watch it.
  extra : dict, optional
      Additional variables (e.g. PRELOAD_WIN_DIR).

  Returns
  -------
  dict
      Environment captured at manager startup plus PRELOAD_PID.
  """
  environment = os.environ.copy()
  environment["PRELOAD_PID"] = str(owner_pid)
  environment.update(extra or {})
  return environment
