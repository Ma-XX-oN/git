#!/usr/bin/env python3
"""RWF #809 same-host native Win32 versus actual Git filesystem API calls.

Both binaries run against one shared 8,000-file committed fixture. Only the
tracked-file Git API phases are compared. Missing or unmatched flags fail closed.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

from benchmark_workload import create_fixture

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "preload" / "perf"))
from trace_fileio import start_etw, stop_etw

FILES = 8000
ROUNDS = 5
PHASE = re.compile(r"^RWF809_PHASE ([A-Za-z0-9_]+) ([0-9]+) ([0-9.]+)$")
NATIVE = re.compile(
    r"^RWF809_NATIVE mode=(attrs|open) threads=(1|4) count=([0-9]+) "
    r"api_ms=([0-9.]+) wall_ms=([0-9.]+)$")


def sha256(path):
  digest = hashlib.sha256()
  with path.open("rb") as f:
    for block in iter(lambda: f.read(1048576), b""):
      digest.update(block)
  return digest.hexdigest()


def run(argv, cwd, mode, capture):
  begin_utc = time.time_ns()
  begin = time.perf_counter_ns()
  process = subprocess.Popen([str(v) for v in argv], cwd=cwd,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
  try:
    stdout, stderr = process.communicate(timeout=180)
  except subprocess.TimeoutExpired:
    process.kill()
    process.communicate()
    raise RuntimeError(f"{mode} timed out")
  end = time.perf_counter_ns()
  end_utc = time.time_ns()
  if process.returncode:
    raise RuntimeError(f"{mode} exit={process.returncode} argv={argv!r} "
                       f"stderr={stderr[:4000]!r}")
  rec = {
      "mode": mode, "pid": process.pid,
      "start_utc_ns": begin_utc, "end_utc_ns": end_utc,
      "wall_ms": round((end - begin) / 1e6, 6),
      "stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
      "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
      "argv": [str(x) for x in argv],
  }
  if mode == "git":
    phases = {}
    unexpected = []
    for line in stderr.decode("utf-8", "replace").splitlines():
      parsed = PHASE.fullmatch(line)
      if parsed:
        key, count, ms = parsed.groups()
        if key in phases:
          raise AssertionError(f"duplicate Git phase {key}")
        phases[key] = {"count": int(count), "api_ms": float(ms)}
      else:
        unexpected.append(line)
    if unexpected:
      raise AssertionError(f"unexpected Git stderr: {unexpected[:5]!r}")
    for key in ("tracked_open_CreateFileW",
                "tracked_open_CreateFileW_exact_flags",
                "tracked_lstat_GetFileAttributesExW"):
      if key not in phases or phases[key]["count"] < FILES:
        raise AssertionError(f"missing tracked-file phase {key}: {phases}")
    if phases.get("tracked_open_CreateFileW_other_flags", {}).get("count", 0):
      raise AssertionError(f"Git open flags differ: {phases}")
    if (phases["tracked_open_CreateFileW"]["count"] !=
        phases["tracked_open_CreateFileW_exact_flags"]["count"]):
      raise AssertionError("Git tracked-file opens differ in native parameters")
    names = stdout.decode("utf-8", "strict").splitlines()
    expected = [f"folder-{i // 1000:02d}/tracked-{i:06d}.txt"
                for i in range(FILES)]
    if sorted(names) != expected:
      raise AssertionError(f"Git grep returned {len(names)} entries, expected exact fixture")
    rec["phases"] = phases
    rec["tracked_name_count"] = len(names)
  else:
    match = NATIVE.fullmatch(stdout.decode("ascii").strip())
    if not match:
      raise AssertionError(f"invalid native Win32 output: {stdout!r} {stderr!r}")
    opname, threads, count, api_ms, wall_ms = match.groups()
    if int(count) != FILES or stderr:
      raise AssertionError("direct Win32 comparator did not complete exact fixture")
    rec.update({"operation": opname, "threads": int(threads),
                "count": int(count), "api_ms": float(api_ms),
                "inner_wall_ms": float(wall_ms)})
  if capture is not None:
    capture.append({"mode": mode, "pid": rec["pid"],
                    "start_utc_ns": begin_utc, "end_utc_ns": end_utc,
                    "argv": rec["argv"]})
  return rec


def main():
  if len(sys.argv) != 4:
    raise SystemExit("usage: same_run_probe.py GIT.EXE DIRECT.EXE OUT_DIR")
  git, direct, out = (Path(x).resolve() for x in sys.argv[1:])
  if os.name != "nt" or not git.is_file() or not direct.is_file():
    raise RuntimeError("native Windows and both built executables are required")
  fixture = out.parent / (out.name + "-fixture")
  if out.exists() or fixture.exists():
    raise RuntimeError("refusing to overwrite a previous result or fixture")
  out.mkdir(parents=True)
  manifest = {
      "source_sha": os.getenv("GITHUB_SHA"), "platform": platform.platform(),
      "git_sha256": sha256(git), "direct_sha256": sha256(direct),
      "fixture_files": FILES, "repeats": ROUNDS,
      "open_args": {
        "access": "GENERIC_READ",
        "share": "FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE",
        "security": "SECURITY_ATTRIBUTES {sizeof, NULL, TRUE}",
        "disposition": "OPEN_EXISTING", "flags": "FILE_ATTRIBUTE_NORMAL",
        "template": "NULL",
      },
      "attrs_args": "GetFileExInfoStandard",
      "results": [], "etw_operations": [],
  }
  report = out / "report.json"
  manifest["fixture"] = create_fixture(str(git), fixture, FILES)

  def checkpoint():
    report.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

  def measure(kind, threads, period, captures):
    if kind == "git":
      argv = [str(git), "-c", f"grep.threads={threads}",
              "grep", "-l", "SEARCH_TOKEN_809"]
      result = run(argv, fixture, "git", captures)
      result["threads"] = threads
      result["operation"] = "grep"
      result["open_api_us_per_call"] = round(
          1000 * result["phases"]["tracked_open_CreateFileW"]["api_ms"] /
          result["phases"]["tracked_open_CreateFileW"]["count"], 6)
      result["attrs_api_us_per_call"] = round(
          1000 * result["phases"]["tracked_lstat_GetFileAttributesExW"]["api_ms"] /
          result["phases"]["tracked_lstat_GetFileAttributesExW"]["count"], 6)
    else:
      result = run([str(direct), kind, str(threads)], fixture, "native", captures)
      result["api_us_per_call"] = round(1000 * result["api_ms"] / FILES, 6)
    result["trial_phase"] = period
    return result

  try:
    for threads in (1, 4):
      for _ in range(2):
        for kind in ("git", "attrs", "open"):
          measure(kind, threads, "warmup", None)

    for period in ("untraced", "etw"):
      session = None
      if period == "etw":
        session = start_etw(out, manifest)
        if not session:
          raise RuntimeError("FileIO/DiskIO ETW could not start: " +
                             str(manifest.get("trace_error")))
      complete = False
      try:
        for trial in range(ROUNDS):
          for threads in ((1, 4) if trial % 2 == 0 else (4, 1)):
            for kind in (("git", "attrs", "open") if trial % 2 == 0
                         else ("open", "attrs", "git")):
              outcome = measure(
                  kind, threads, period,
                  manifest["etw_operations"] if period == "etw" else None)
              outcome["trial"] = trial
              manifest["results"].append(outcome)
              print("RWF809_SAME_RUN", period, trial, kind, threads,
                    outcome.get("open_api_us_per_call"),
                    outcome.get("attrs_api_us_per_call"),
                    outcome.get("api_us_per_call"), flush=True)
          checkpoint()
      finally:
        if session:
          complete = stop_etw(session, manifest)
      if period == "etw" and not complete:
        raise RuntimeError("ETW trace did not complete: " +
                           str(manifest.get("trace_error")))
    if len(manifest["results"]) != ROUNDS * 2 * 3 * 2:
      raise AssertionError("not all same-run comparisons completed")
    if len(manifest["etw_operations"]) != ROUNDS * 2 * 3:
      raise AssertionError("ETW PID/window manifest incomplete")
    manifest["complete"] = True
    print("RWF809_SAME_RUN_PASS", manifest["source_sha"],
          len(manifest["results"]), manifest["trace_file"]["sha256"], flush=True)
  except BaseException as error:
    manifest["error"] = repr(error)
    raise
  finally:
    checkpoint()


if __name__ == "__main__":
  main()
