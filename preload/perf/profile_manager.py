#!/usr/bin/env python3
"""Launch the unchanged Unix manager with diagnostic wall/CPU spans.

Invoke as `python profile_manager.py OUTPUT FOCUS MANAGER [manager args]`.
FOCUS is all, root, or an operation label such as os.write.
Used as PRELOAD_PYTHON through a generated launcher. Records GET dispatch and
queued allocation attempts, nested operation spans, native thread IDs and
context switches. Writes OUTPUT at shutdown; no per-request profile file I/O.
These instrumented durations are diagnostics, not normal command latencies.
"""
import functools
import json
import os
from pathlib import Path
import queue
import statistics
import subprocess
import sys
import threading
import time

try:
  import resource
except ImportError:
  resource = None


class Profiler:
  """Collect nested spans independently on each request-handling thread."""

  def __init__(self):
    """Initialize in-memory records and per-thread active span stacks."""
    self.local = threading.local()
    self.records = []

  def switches(self):
    """Return thread context-switch counts, or None when unavailable."""
    if resource is None or not hasattr(resource, "RUSAGE_THREAD"):
      return None
    usage = resource.getrusage(resource.RUSAGE_THREAD)
    return [usage.ru_nvcsw, usage.ru_nivcsw]

  def wrap(self, name, function, root_when=None):
    """Wrap one callable without changing its return value or exceptions.

    Parameters
    ----------
    name : str
        Stable operation label written to each span.
    function : callable
        Original operation; invoked exactly once per call.
    root_when : callable, optional
        Predicate of positional arguments. Starts a record when True and
        no record is active; other calls outside a record remain unmeasured.

    Returns
    -------
    callable
        Diagnostic wrapper, preserving nesting and native thread identity.
    """
    @functools.wraps(function)
    def measured(*args, **kwargs):
      """Measure one operation while a request record is active."""
      record = getattr(self.local, "record", None)
      root = record is None and root_when is not None and root_when(args)
      if root:
        record = {"kind": name, "pid": os.getpid(),
              "tid": threading.get_native_id(), "epoch_ns": time.time_ns(),
              "mono_ns": time.monotonic_ns(),
              "switches_before": self.switches(), "events": []}
        self.local.record, self.local.stack = record, []
      if record is None:
        return function(*args, **kwargs)
      events, stack = record["events"], self.local.stack
      index = len(events)
      event = {"name": name, "parent": stack[-1] if stack else None}
      if name == "os.write":
        event.update(fd=args[0], bytes=len(args[1]))
      if name == "Server._get":
        event.update(received=args[3], wait=kwargs.get("wait", True))
      events.append(event)
      stack.append(index)
      event["start_ns"] = time.perf_counter_ns()
      cpu = time.thread_time_ns()
      event["start_cpu_ns"] = cpu
      try:
        result = function(*args, **kwargs)
        if name == "Server._get":
          event["handled"] = result
        return result
      finally:
        event["cpu_ns"] = time.thread_time_ns() - cpu
        event["end_ns"] = time.perf_counter_ns()
        stack.pop()
        if root:
          record["switches_after"] = self.switches()
          self.local.record = None
          self.records.append(record)
    return measured

  def calibrate(self):
    """Return empty-span and complete-wrapper overhead medians in ns."""
    def empty():
      """Provide an operation with no work for clock-overhead calibration."""
      return None

    call = self.wrap("calibration", empty, lambda args: True)
    outer = []
    for _ in range(1000):
      started = time.perf_counter_ns()
      call()
      outer.append(time.perf_counter_ns() - started)
    result = {"empty_wrapper_wall_ns": statistics.median(outer),
          "empty_span_wall_ns": statistics.median(
            r["events"][0]["end_ns"] - r["events"][0]["start_ns"]
            for r in self.records)}
    self.records.clear()
    return result


def install(profiler, manager, focus="all"):
  """Install diagnostic wrappers on manager operations and their callees.

  Parameters
  ----------
  profiler : Profiler
      Collector owned by this launcher.
  manager : module
      Imported production preload_manager_unix module; no source is rewritten.
  focus : str
      all for nested spans, root for request boundaries only, or one operation
      label for a lighter probe of that boundary.
  """
  targets = [
    (manager.Server, "handle", lambda args: args[1].startswith("GET ")),
    (manager.Server, "_get", lambda args: True),
    (manager.Mailbox, "submit", None), (manager.Mailbox, "deliver", None),
    (manager.preload_pool.Manager, "allocate", None),
    (manager.preload_pool.Manager, "replenish", None),
    (manager.preload_pool.Pool, "take", None),
    (manager.preload_pool.Pool, "request_spawn", None),
    (Path, "exists", None), (Path, "stat", None),
    (Path, "__truediv__", None), (Path, "__str__", None),
    (os, "stat", None), (os, "write", None), (os, "waitpid", None),
    (subprocess.Popen, "poll", None),
    (queue.Queue, "put", None), (queue.Queue, "get", None),
    (threading.Condition, "wait", None),
  ]
  for owner, attribute, root_when in targets:
    label = (owner.__name__ if hasattr(owner, "__name__") else type(owner).__name__)
    if root_when is None and focus not in ("all", label + "." + attribute):
      continue
    setattr(owner, attribute, profiler.wrap(label + "." + attribute,
                        getattr(owner, attribute), root_when))


def main():
  """Run the requested Unix manager and save raw spans even on failure."""
  output, focus = Path(sys.argv[1]), sys.argv[2]
  script = Path(sys.argv[3]).resolve(strict=True)
  if script.name != "manager_unix.py":
    raise ValueError("this launcher profiles manager_unix.py only")
  sys.path.insert(0, str(script.parent))
  import manager_unix as preload_manager_unix
  profiler = Profiler()
  calibration = profiler.calibrate()
  install(profiler, preload_manager_unix, focus)
  sys.argv = [str(script), *sys.argv[4:]]
  try:
    return preload_manager_unix.main()
  finally:
    output.write_text(json.dumps({"focus": focus, "calibration": calibration,
                    "records": profiler.records}) + "\n", encoding="utf-8")


if __name__ == "__main__":
  sys.exit(main())
