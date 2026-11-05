#!/usr/bin/env python3
"""Git #809: Trace2 lifecycle stage boundaries on native OS runners.

All trace-file content is transient; exported results contain timings only.
Trace2 instrumentation is not a zero-overhead measurement and is compared
against an uninstrumented version of the exact same operations.
"""
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import tempfile
import time

def invoke(args, env=None):
  start = time.perf_counter_ns()
  p = subprocess.run(args, check=False, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, env=env, timeout=20)
  ms = (time.perf_counter_ns() - start) / 1e6
  if p.returncode:
    raise RuntimeError("Git command returned " + str(p.returncode))
  return ms

def summarize(vals):
  seq = sorted(vals)
  return {"median_ms": statistics.median(seq),
          "p95_ms": seq[min(len(seq)-1, int(len(seq)*0.95))],
          "min_ms": seq[0], "max_ms": seq[-1],
          "samples_ms": vals}

def traced(args, env, path):
  ms = invoke(args, env)
  records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
  # The process clock starts before Trace2 emits the start event.
  # The start event occurs after common initialisation/trace2 setup.
  named = {}
  for event in records:
    kind = event.get("event")
    if kind in ("start", "exit", "atexit"):
      named.setdefault(kind, []).append(event)
  if any(len(named.get(key, [])) != 1 for key in ("start", "exit", "atexit")):
    raise RuntimeError("expected exactly one Trace2 start, exit and atexit event")
  begin = float(named["start"][0]["t_abs"]) * 1000
  exit = float(named["exit"][0]["t_abs"]) * 1000
  atexit = float(named["atexit"][0]["t_abs"]) * 1000
  if not 0 <= begin <= exit <= atexit:
    raise RuntimeError("invalid Trace2 time ordering")
  return {"wall_ms": ms, "pre_trace_start_ms": begin,
          "trace_start_to_exit_ms": exit-begin,
          "trace_exit_to_atexit_ms": atexit-exit,
          "external_minus_trace_lifetime_ms": ms-atexit,
          "trace_lifetime_ms": atexit,
          "trace_events": len(records)}

def main():
  git = shutil.which("git")
  if not git:
    raise RuntimeError("Git not installed")
  git = str(Path(git).resolve())
  with tempfile.TemporaryDirectory(prefix="git809-lifecycle-") as td:
    root = Path(td)
    repo = root / "fixture"
    repo.mkdir()
    invoke([git, "-C", str(repo), "init", "-q"])
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    invoke([git, "-C", str(repo), "add", "tracked.txt"])
    invoke([git, "-C", str(repo), "-c", "user.name=Git809",
            "-c", "user.email=git809@example.invalid", "commit", "-qm", "fixture"])
    commands = {
      "git_version": [git, "--version"],
      "git_rev_parse": [git, "-C", str(repo), "rev-parse", "HEAD"],
      "git_status": [git, "-C", str(repo), "status", "--porcelain"],
      "git_log": [git, "-C", str(repo), "log", "-1", "--format=%H"]
    }
    rows = {}
    for label, cmd in commands.items():
      for _ in range(4):
        invoke(cmd)
      normal = [invoke(cmd) for _ in range(60)]
      trace_data = []
      for i in range(40):
        f = root / ("trace-%s-%d.json" % (label, i))
        env = os.environ.copy()
        env["GIT_TRACE2_EVENT"] = str(f)
        trace_data.append(traced(cmd, env, f))
        f.unlink()
      rows[label] = {
        "untraced_wall": summarize(normal),
        "traced": {key: summarize([x[key] for x in trace_data])
                   for key in trace_data[0]}
      }
  output = {"experiment": "Git-809-lifecycle-trace2",
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "git_binary": git, "source_sha": os.environ.get("GITHUB_SHA", ""),
            "platform": platform.platform(),
            "method": "60 untraced and 40 traced repetitions per Git command; Trace2 start/exit/atexit events; tracing incurs overhead; external residual combines loader/launch and final teardown.",
            "results": rows}
  dest = Path("git809-results")
  dest.mkdir(exist_ok=True)
  (dest / (platform.system().lower() + "-lifecycle.json")).write_text(
    json.dumps(output, indent=2) + "\n", encoding="utf-8")
  brief = {label: {"untraced": value["untraced_wall"]["median_ms"],
                   "traced": {key: value["traced"][key]["median_ms"]
                              for key in value["traced"]}}
           for label, value in rows.items()}
  print(json.dumps({"source": output["source_sha"], "platform": output["platform"],
                    "stages_median_ms": brief}, indent=2))

if __name__ == "__main__":
  main()
