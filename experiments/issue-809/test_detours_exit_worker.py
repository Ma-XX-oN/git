#!/usr/bin/env python3
"""Exercise a real Windows Git --preload worker launched under Microsoft Detours."""
import ctypes as C
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

from test_windows_worker_lifecycle import K, INVALID, read_to_eof, write

if os.name != "nt":
  raise RuntimeError("Windows-native Detours diagnostic only")
if len(sys.argv) != 5:
  raise SystemExit("usage: test_detours_exit_worker.py LAUNCHER DLL GIT LOGDIR")

launcher, dll, git, log_directory = sys.argv[1:]
root = Path(log_directory).resolve()
root.mkdir(parents=True, exist_ok=True)
expected = subprocess.check_output([git, "--version"], timeout=15)
K.CreateFileW.restype = C.c_void_p


def read_ready(stream, results):
  results.put(stream.readline())


def run_case(case, args, legacy_crt=False):
  log = root / f"{case}.exit-detours.log"
  fallback = root / f"{case}.exit-fallback.log"
  native = root / f"{case}.native-trace.log"
  env = os.environ.copy()
  env["GIT809_PRELOAD_OWNER_WINPID"] = str(os.getpid())
  env["GIT809_DETOURS_LOG"] = str(log)
  env["GIT809_DETOURS_FALLBACK"] = str(fallback)
  if legacy_crt:
    env["GIT809_DETOURS_LEGACY_CRT"] = "1"
  env["GIT_TRACE_GIT809_PRELOAD"] = str(native)
  process = subprocess.Popen([launcher, dll, git, "--preload"],
                             env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  handles = {}
  outcome = "unknown"
  try:
    ready = queue.Queue()
    thread = threading.Thread(target=read_ready,
                              args=(process.stdout, ready), daemon=True)
    thread.start()
    try:
      line = ready.get(timeout=15)
    except queue.Empty as error:
      raise AssertionError("injected worker never published READY") from error
    match = re.fullmatch(rb"READY ([1-9][0-9]*)\r?\n", line)
    assert match, ("unexpected worker READY", line)
    pid = int(match.group(1))
    for channel in ("stdout", "stderr", "exit-code", "stdin", "param-env-cwd"):
      address = rf"\\.\pipe\{pid}-{channel}"
      deadline = time.monotonic() + 5
      handle = INVALID
      while time.monotonic() < deadline:
        handle = K.CreateFileW(address, 0xC0000000, 0, None, 3, 0, None)
        if handle != INVALID:
          break
        time.sleep(.003)
      assert handle != INVALID, ("cannot connect", channel, C.get_last_error())
      handles[channel] = handle
      write(handle, b"1")
    import struct
    frame = struct.pack("<I", len(args) + 1) + b"git\0"
    frame += b"".join(x.encode("utf-8") + b"\0" for x in args) + b"end\0"
    write(handles["param-env-cwd"], frame)
    stdout = read_to_eof(handles["stdout"])
    stderr = read_to_eof(handles["stderr"])
    exit_status = read_to_eof(handles["exit-code"])
    launcher_status = process.wait(timeout=8)
    print("GIT809_DETOURS_PROCESS_EXIT", case,
          "launcher_status", launcher_status,
          "status_bytes", repr(exit_status),
          "stdout_bytes", len(stdout), flush=True)
    outcome = (stdout, stderr, exit_status)
    expected_result = expected if args == ["--version"] else b""
    assert stdout == expected_result, (case, stdout, expected_result)
    assert not stderr, (case, stderr)
    assert exit_status == b"EXIT 0\n", (case, exit_status)
    assert launcher_status == 0, (case, launcher_status)
    print("GIT809_DETOURS_WORKER_PASS", case, "legacy", legacy_crt, flush=True)
  finally:
    for handle in handles.values():
      K.CloseHandle(handle)
    if process.poll() is None:
      process.kill()
    process.wait(timeout=5)
    for path in (log, fallback, native):
      if path.exists():
        print("GIT809_DIAGNOSTIC_BEGIN", path.name, flush=True)
        print(path.read_text(encoding="utf-8", errors="replace")[-10000:],
              flush=True)
        print("GIT809_DIAGNOSTIC_END", path.name, flush=True)
    if log.exists():
      content = log.read_text(encoding="utf-8", errors="replace")
      assert "path=HOOK_READY" in content, "Detours did not attach"
    else:
      assert fallback.exists(), "both Detours diagnostic destinations unavailable"


# 240 independent processes on the same native Windows runner.
# All must publish a complete status and terminate cleanly, with and without
# the intentionally incorrect legacy CRT Detours configuration.
for attempt in range(120):
  run_case(f"git-version-ucrt-{attempt:03d}", ["--version"])
  run_case(f"git-version-legacy-{attempt:03d}", ["--version"], legacy_crt=True)
