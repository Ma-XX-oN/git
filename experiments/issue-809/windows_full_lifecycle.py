#!/usr/bin/env python3
"""Git #809: Windows OS-process-creation to OS-process-exit timeline.

Time boundaries come from GetProcessTimes (OS process creation/termination)
and Git Trace2 (initialization complete / exit / atexit). Tracing adds
overhead; untraced controls are recorded separately.
"""
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import tempfile
import time

class Filetime(ctypes.Structure):
  _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

EPOCH_1601_TO_1970_100NS = 116444736000000000

def filetime_unix_ms(t):
  return ((((t.high << 32) | t.low) - EPOCH_1601_TO_1970_100NS) / 10000.0)

def os_process_times(handle):
  kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
  kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(Filetime)] * 4
  kernel32.GetProcessTimes.restype = ctypes.c_int
  creation, end, cpu_kernel, cpu_user = (Filetime() for _ in range(4))
  if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(end),
                                  ctypes.byref(cpu_kernel), ctypes.byref(cpu_user)):
    raise ctypes.WinError(ctypes.get_last_error())
  return filetime_unix_ms(creation), filetime_unix_ms(end)

def json_event_unix_ms(event):
  stamp = event["time"].replace("Z", "+00:00")
  return dt.datetime.fromisoformat(stamp).timestamp() * 1000

def invoke(command, trace_path=None):
  env = os.environ.copy()
  env.pop("GIT_TRACE2_EVENT", None)
  if trace_path is not None:
    env["GIT_TRACE2_EVENT"] = str(trace_path)
  before = time.perf_counter_ns()
  with subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
    after_spawn = time.perf_counter_ns()
    stdout, stderr = proc.communicate(timeout=30)
    after_exit = time.perf_counter_ns()
    if proc.returncode:
      raise RuntimeError("git failure " + str(proc.returncode) + ": " +
                         stderr.decode("utf-8", "replace")[:200])
    created_ms, terminated_ms = os_process_times(int(proc._handle))
  out = {"total_wall_ms": (after_exit-before)/1e6,
         "parent_popen_ms": (after_spawn-before)/1e6,
         "child_os_lifetime_ms": terminated_ms-created_ms}
  if trace_path is not None:
    events = [json.loads(x) for x in trace_path.read_text(encoding="utf-8").splitlines()
              if x.strip()]
    key = {k: [x for x in events if x.get("event") == k]
           for k in ("start", "exit", "atexit")}
    if any(len(items) != 1 for items in key.values()):
      raise RuntimeError("Trace2 event count invalid: " +
                         str({k:len(v) for k,v in key.items()}))
    start, exit_event, atexit = [json_event_unix_ms(key[k][0])
                                 for k in ("start", "exit", "atexit")]
    phases = {
      "os_create_to_trace_start_ms": start-created_ms,
      "trace_start_to_exit_ms": exit_event-start,
      "git_exit_to_atexit_ms": atexit-exit_event,
      "trace_atexit_to_os_exit_ms": terminated_ms-atexit,
    }
    if any(x < -3 for x in phases.values()):
      raise RuntimeError("OS/Trace clock mismatch: " + str(phases))
    out.update(phases)
    out["trace_events"] = len(events)
    trace_path.unlink()
  return out

def summarize(raw):
  return {k: {"median_ms": statistics.median(x[k] for x in raw),
              "p95_ms": sorted(x[k] for x in raw)[min(len(raw)-1,int(len(raw)*.95))]}
          for k in raw[0]}

def main():
  if os.name != "nt":
    raise SystemExit("This native process timeline runs only on Windows")
  git = shutil.which("git")
  if not git:
    raise RuntimeError("Git missing")
  with tempfile.TemporaryDirectory(prefix="git809-timeline-") as temp:
    root = Path(temp)
    repo = root / "repo"
    repo.mkdir()
    subprocess.run([git, "-C", str(repo), "init", "-q"], check=True)
    (repo / "one.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run([git, "-C", str(repo), "add", "one.txt"], check=True)
    subprocess.run([git, "-C", str(repo), "-c", "user.name=Probe",
                    "-c", "user.email=probe@example.invalid",
                    "commit", "-qm", "fixture"], check=True)
    cases = {"version": [git, "--version"],
             "rev_parse": [git, "-C", str(repo), "rev-parse", "HEAD"],
             "status": [git, "-C", str(repo), "status", "--porcelain"],
             "log": [git, "-C", str(repo), "log", "-1", "--format=%H"]}
    results = {}
    for name, args in cases.items():
      for i in range(4):
        invoke(args)
      plain = [invoke(args) for i in range(60)]
      traced = [invoke(args, root / (name + "-" + str(i) + ".event"))
                for i in range(40)]
      results[name] = {"untraced": summarize(plain),
                       "traced": summarize(traced),
                       "raw_untraced": plain, "raw_traced": traced}
  output = {"experiment": "Git809-Windows-full-process-lifecycle",
            "source_sha": os.environ.get("GITHUB_SHA", ""),
            "git_path": git, "n_untraced":60, "n_traced":40,
            "limitations": "GetProcessTimes timestamps and Trace2 event UTC timestamps are OS-clock based; event recording introduces overhead; stages include trace setup and cleanup overhead. Measured shutdown includes time from Git exit to atexit and after atexit until OS process termination. No changes to Git source.",
            "results": results}
  dest = Path("git809-results")
  dest.mkdir(exist_ok=True)
  (dest / "windows-full-lifecycle.json").write_text(
    json.dumps(output, indent=2) + "\n", encoding="utf-8")
  print(json.dumps({k: {"untraced":v["untraced"], "traced":v["traced"]}
                    for k,v in results.items()}, indent=2))

if __name__ == "__main__":
  main()
