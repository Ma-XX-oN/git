#!/usr/bin/env python3
"""Capture a short native Git filesystem trace with Windows ETW.

This diagnostic does not modify the Git implementation or RepoWorkflow tests.
The manifest records traced PIDs, UTC timestamps and exact binary trace hashes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time


def command(argv, cwd=None, timeout=30):
  return subprocess.run(
    argv, cwd=cwd, capture_output=True, text=True, errors="replace",
    timeout=timeout, check=False,
  )


def git_call(args, root):
  utc_start = time.time_ns()
  started = time.perf_counter_ns()
  child = subprocess.Popen(
    args, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    stdin=subprocess.DEVNULL,
  )
  launched = time.perf_counter_ns()
  try:
    stdout, stderr = child.communicate(timeout=25)
  except subprocess.TimeoutExpired:
    child.kill()
    child.communicate()
    raise
  finished = time.perf_counter_ns()
  utc_end = time.time_ns()
  if child.returncode:
    raise RuntimeError(
      f"Git exit {child.returncode} for {args!r}, stderr={stderr!r}"
    )
  return {
    "pid": child.pid,
    "command": [str(v) for v in args[1:]],
    "start_utc_ns": utc_start,
    "end_utc_ns": utc_end,
    "start_perf_ns": started,
    "launch_perf_ns": launched,
    "end_perf_ns": finished,
    "duration_ms": (finished - started) / 1e6,
    "stdout_bytes": len(stdout),
    "stderr_bytes": len(stderr),
  }


def create_fixture(git, root):
  root.mkdir()
  for args in (
    [git, "init", "-q", "-b", "benchmark"],
    [git, "config", "user.name", "File I/O Trace"],
    [git, "config", "user.email", "file-io@example.invalid"],
  ):
    result = command(args, cwd=root)
    if result.returncode:
      raise RuntimeError(f"fixture setup failed: {args!r}, {result.stderr}")
  for index in range(100):
    file = root / f"file-{index:04d}.txt"
    file.write_bytes((f"file {index}\n" * 32).encode("ascii"))
  for args in ([git, "add", "."], [git, "commit", "-qm", "baseline"]):
    result = command(args, cwd=root)
    if result.returncode:
      raise RuntimeError(f"fixture commit failed: {args!r}, {result.stderr}")


def start_etw(output, manifest):
  wpr = shutil.which("wpr.exe") or shutil.which("wpr")
  if not wpr:
    manifest["trace_error"] = "wpr.exe unavailable"
    return None
  manifest["wpr_path"] = wpr
  profiles = command([wpr, "-profiles"], timeout=30)
  manifest["wpr_profiles"] = {
    "exit": profiles.returncode,
    "output": profiles.stdout[-8000:],
    "error": profiles.stderr[-2000:],
  }
  result = command([wpr, "-start", "FileIO", "-start", "DiskIO", "-filemode"],
                   timeout=45)
  manifest["trace_start"] = {
    "exit": result.returncode,
    "output": result.stdout[-8000:],
    "error": result.stderr[-8000:],
  }
  if result.returncode:
    manifest["trace_error"] = "WPR could not start FileIO/DiskIO profiles"
    return None
  return wpr, output / "file-io-trace.etl"


def stop_etw(session, manifest):
  wpr, path = session
  result = command([wpr, "-stop", str(path)], timeout=120)
  manifest["trace_stop"] = {
    "exit": result.returncode,
    "output": result.stdout[-8000:],
    "error": result.stderr[-8000:],
  }
  if result.returncode or not path.is_file() or path.stat().st_size == 0:
    manifest["trace_error"] = "WPR did not produce a nonempty ETL file"
    return False
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    for block in iter(lambda: stream.read(1048576), b""):
      digest.update(block)
  manifest["trace_file"] = {
    "name": path.name,
    "bytes": path.stat().st_size,
    "sha256": digest.hexdigest(),
    "format": "binary ETW ETL, FileIO and DiskIO",
  }
  return True


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--git", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  git = args.git.resolve(strict=True)
  output = args.output.resolve()
  output.mkdir(parents=True, exist_ok=True)
  manifest = {
    "schema": 1,
    "platform": platform.platform(),
    "python": sys.version,
    "git": str(git),
    "git_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
    "revision": os.environ.get("GITHUB_SHA"),
    "workload": "100 tracked files; 12 status and 12 rev-parse calls after warmup",
    "operations": [],
  }
  captured = False
  try:
    with tempfile.TemporaryDirectory(prefix="git-fileio-") as temp:
      root = Path(temp) / "fixture"
      create_fixture(str(git), root)
      for _ in range(3):
        git_call([str(git), "status", "--porcelain"], root)
        git_call([str(git), "rev-parse", "HEAD"], root)
      if os.name != "nt":
        manifest["trace_error"] = "This ETW diagnostic is Windows-only"
      else:
        session = start_etw(output, manifest)
        if session:
          try:
            for _ in range(12):
              manifest["operations"].append(
                git_call([str(git), "status", "--porcelain"], root)
              )
              manifest["operations"].append(
                git_call([str(git), "rev-parse", "HEAD"], root)
              )
          finally:
            captured = stop_etw(session, manifest)
  except Exception as error:
    manifest["exception"] = repr(error)
  manifest["complete"] = bool(captured)
  (output / "trace-manifest.json").write_text(
    json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
  )
  print("FILEIO_TRACE", json.dumps({
    "complete": captured,
    "calls": len(manifest["operations"]),
    "trace": manifest.get("trace_file"),
    "error": manifest.get("trace_error"),
    "exception": manifest.get("exception"),
  }), flush=True)
  return 0 if captured else 2


if __name__ == "__main__":
  raise SystemExit(main())
