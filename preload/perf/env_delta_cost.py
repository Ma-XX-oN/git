#!/usr/bin/env python3
"""Measure the per-call cost of env-add/env-del fields through the C++ manager.

Runs paired ABBA rounds of manager-backed Windows calls of
`git var GIT_EDITOR`: arm A sends no environment fields, arm B sends four
env-add assignments and one env-del name (a typical RepoWorkflow delta).
Each sample is the client-observed wall time from connecting to the manager
to reading the worker's exit record. Writes raw samples and a bootstrap 95%
confidence interval of the median difference (B - A) as JSON.

Usage: env_delta_cost.py --git PATH --output NEW_DIRECTORY [--rounds N] [--calls N]
"""
import argparse
import ctypes as C
import json
import os
from pathlib import Path
import random
import statistics
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_manager import Session, ROOT, WINDOWS  # noqa: E402

if WINDOWS:
  from test_windows_worker_lifecycle import K, W, INVALID, read_to_eof, write  # noqa: E402

DELTA = (b"env-del\0GIT_PAGER\n\0env-add\0GIT_INDEX_FILE=.git/preload-index\n"
     b"GIT_AUTHOR_NAME=Preload Bench\nGIT_AUTHOR_EMAIL=bench@example.com\n"
     b"GIT_EDITOR=delta-editor\n\0")


def call(session, context):
  """Run one manager-backed `git var GIT_EDITOR`; return (seconds, stdout).

  Parameters
  ----------
  session : Session
      Borrowed session with a registered git pool.
  context : bytes
      Extra context fields sent after cwd; empty for none.
  """
  start = time.perf_counter()
  request = session.connect()
  handles = {}
  try:
    frame = struct.pack("<I", 2) + b"git\0var\0GIT_EDITOR\0"
    frame += b"cwd\0" + str(ROOT).encode("utf-8") + b"\n\0" + context + b"end\0"
    write(request, b"GET git 1\n" + frame)
    reply, count = C.create_string_buffer(8), W.DWORD()
    assert K.ReadFile(request, reply, 8, C.byref(count), None) and count.value == 8
    pid = int(reply.raw, 16)
    assert pid, "allocation failed"
    for channel in ("stdin", "stdout", "stderr", "exit-code"):
      handle = K.CreateFileW(rf"{session.pipe_dir}\worker-{pid}\{channel}",
                   0xc0000000, 0, None, 3, 0, None)
      assert handle != INVALID, (channel, C.get_last_error())
      handles[channel] = handle
      write(handle, b"1")
    ack = C.create_string_buffer(1)
    assert K.ReadFile(handles["stdin"], ack, 1, C.byref(count), None) and ack.raw == b"\x06"
    K.CloseHandle(handles.pop("stdin"))
    output = read_to_eof(handles["stdout"])  # Tiny output: no stderr drain needed first.
    errors = read_to_eof(handles["stderr"])
    status = read_to_eof(handles["exit-code"])
    elapsed = time.perf_counter() - start
    assert status == b"EXIT 0\n" and not errors, (status, errors)
    return elapsed, output
  finally:
    for handle in handles.values():
      K.CloseHandle(handle)
    K.CloseHandle(request)


def bootstrap(a, b, resamples=2000, seed=809):
  """Return the median difference b - a and its bootstrap 95% interval, in us."""
  rng = random.Random(seed)
  diffs = sorted(statistics.median(rng.choices(b, k=len(b))) -
          statistics.median(rng.choices(a, k=len(a))) for _ in range(resamples))
  scale = 1e6
  return {"median_diff_us": (statistics.median(b) - statistics.median(a)) * scale,
      "ci95_us": [diffs[int(.025 * resamples)] * scale,
            diffs[int(.975 * resamples) - 1] * scale]}


def main():
  """Run ABBA rounds and write raw samples plus the summary."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--git", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  parser.add_argument("--rounds", type=int, default=20)
  parser.add_argument("--calls", type=int, default=50)
  args = parser.parse_args()
  if not WINDOWS:
    parser.error("Windows named-pipe benchmark")
  git = args.git.resolve(strict=True)
  args.output.mkdir(parents=True, exist_ok=False)
  environment = {**os.environ, "GIT_EDITOR": "inherited-editor"}
  session = Session(args.output, environment=environment)
  samples = {"none": [], "delta": []}
  try:
    assert session.request(f"PRELOAD git {git}") == "OK", session.read_log()
    for arm, context in (("none", b""), ("delta", DELTA)):  # Warm both arms.
      for _ in range(10):
        call(session, context)
    assert call(session, b"")[1] == b"inherited-editor\n"
    assert call(session, DELTA)[1] == b"delta-editor\n"
    for round_index in range(args.rounds):
      order = ("none", "delta", "delta", "none") if round_index % 2 == 0 else \
          ("delta", "none", "none", "delta")
      for arm in order:
        context = DELTA if arm == "delta" else b""
        for _ in range(args.calls // 2):
          samples[arm].append(call(session, context)[0])
  finally:
    if session.process.poll() is None:
      session.stop()
  summary = {"git": str(git), "rounds": args.rounds, "calls_per_arm": len(samples["none"]),
         "median_none_ms": statistics.median(samples["none"]) * 1e3,
         "median_delta_ms": statistics.median(samples["delta"]) * 1e3,
         **bootstrap(samples["none"], samples["delta"])}
  (args.output / "env-delta-cost.json").write_text(
    json.dumps({"summary": summary, "samples_s": samples}, indent=2), encoding="utf-8")
  print(json.dumps(summary, indent=2))


if __name__ == "__main__":
  main()
