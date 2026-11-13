"""Read-only subprocess and filesystem-count tracer for native test diagnosis.

Activate by putting this directory on PYTHONPATH and setting
PRELOAD_PROCESS_TRACE_DIR to an existing directory. No fixture changes.
Traced durations are diagnostics, not uninstrumented performance acceptance.
"""
import atexit
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import time

_DIR = os.environ.get("PRELOAD_PROCESS_TRACE_DIR")
if _DIR:
  _BASE = subprocess.Popen
  _EVENTS = []
  _AUDIT = Counter()
  _START = time.perf_counter_ns()
  _CPU_START = time.process_time_ns()
  _PID = os.getpid()

  def _kind(args):
    if isinstance(args, (str, bytes, os.PathLike)):
      return "shell"
    if not args:
      return "unknown"
    first = str(args[0]).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if first in ("git", "git.exe"):
      return "git"
    if first in ("python", "python.exe", "python3", "python3.exe") or first.startswith("python3."):
      return "python"
    return first[:80]

  class ProfiledPopen(_BASE):
    def __init__(self, *args, **kwargs):
      start = time.perf_counter_ns()
      command = args[0] if args else kwargs.get("args")
      self._trace_started = start
      self._trace_kind = _kind(command)
      self._trace_finished = False
      try:
        super().__init__(*args, **kwargs)
      except BaseException:
        _EVENTS.append((self._trace_kind, -1, time.perf_counter_ns() - start))
        raise

    def _trace_end(self):
      if not self._trace_finished and self.returncode is not None:
        self._trace_finished = True
        _EVENTS.append((self._trace_kind, int(self.returncode),
                        time.perf_counter_ns() - self._trace_started))

    def wait(self, *args, **kwargs):
      try:
        return super().wait(*args, **kwargs)
      finally:
        self._trace_end()

    def poll(self, *args, **kwargs):
      try:
        return super().poll(*args, **kwargs)
      finally:
        self._trace_end()

    def communicate(self, *args, **kwargs):
      try:
        return super().communicate(*args, **kwargs)
      finally:
        self._trace_end()

    def __exit__(self, *args, **kwargs):
      try:
        return super().__exit__(*args, **kwargs)
      finally:
        self._trace_end()

  subprocess.Popen = ProfiledPopen

  def _audit(event, args):
    if event in ("os.mkdir", "os.remove", "os.rmdir", "os.rename",
                 "os.scandir", "os.listdir", "os.chdir", "os.chmod",
                 "os.link", "os.symlink", "os.utime", "os.truncate"):
      _AUDIT[event] += 1

  sys.addaudithook(_audit)

  def _save():
    try:
      root = Path(_DIR)
      path = root / ("proc-%d-%d.json" % (_PID, time.time_ns()))
      payload = {
        "schema": 1,
        "pid": _PID,
        "python_version": sys.version.split()[0],
        "wall_ms": (time.perf_counter_ns() - _START) / 1e6,
        "cpu_ms": (time.process_time_ns() - _CPU_START) / 1e6,
        "events": [[name, code, delta / 1e6] for name, code, delta in _EVENTS],
        "audit_counts": dict(_AUDIT),
      }
      path.write_text(json.dumps(payload, separators=(",", ":")) + "\n",
                      encoding="utf-8")
    except Exception:
      # The diagnostic must never change the test's exit status.
      pass

  atexit.register(_save)
