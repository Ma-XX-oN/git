#!/usr/bin/env python3
"""Windows RWF 809: time native pipe open/transfer/close through one Git Bash.

Creates exactly two native server instances per unique pathname and records
their connections. The Bash process is persistent. Server setup, thread setup,
and Bash startup are excluded from each measured request.
"""
import argparse
import ctypes as c
from ctypes import wintypes as w
import json
import os
import queue
import statistics
import subprocess
import threading
import time
import uuid

if os.name != "nt":
  raise SystemExit("native Windows Python required")
K = c.WinDLL("kernel32", use_last_error=True)
K.CreateNamedPipeW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.LPVOID]
K.CreateNamedPipeW.restype = w.HANDLE
K.ConnectNamedPipe.argtypes = [w.HANDLE,w.LPVOID]
K.ConnectNamedPipe.restype = w.BOOL
K.ReadFile.argtypes = [w.HANDLE,w.LPVOID,w.DWORD,c.POINTER(w.DWORD),w.LPVOID]
K.ReadFile.restype = w.BOOL
K.WriteFile.argtypes = [w.HANDLE,w.LPCVOID,w.DWORD,c.POINTER(w.DWORD),w.LPVOID]
K.WriteFile.restype = w.BOOL
K.FlushFileBuffers.argtypes = [w.HANDLE]
K.FlushFileBuffers.restype = w.BOOL
K.CloseHandle.argtypes = [w.HANDLE]
K.CloseHandle.restype = w.BOOL
K.DisconnectNamedPipe.argtypes = [w.HANDLE]
K.DisconnectNamedPipe.restype = w.BOOL
BAD = c.c_void_p(-1).value
PAYLOAD = b"RWF809_OK\n"
SCRIPTS = {
  "read": r'''
printf 'READY\n'
while IFS= read -r name; do
  [[ $name == STOP ]] && break
  if exec 9<"$name" && IFS= read -r answer <&9; then
    exec 9<&-
    printf 'OK:%s\n' "$answer"
  else
    exec 9<&- 2>/dev/null || :
    printf 'FAIL\n'
  fi
done
''',
  "write": r'''
printf 'READY\n'
while IFS= read -r name; do
  [[ $name == STOP ]] && break
  if exec 9>>"$name" && printf 'RWF809_OK\n' >&9; then
    exec 9>&-
    printf 'OK\n'
  else
    exec 9>&- 2>/dev/null || :
    printf 'FAIL\n'
  fi
done
''',
}

def source(pipe, q):
  for line in pipe:
    q.put(line)

def make_server(name, direction):
  handles, threads, records = [], [], []
  states = [{"index": i, "stage": "created"} for i in range(2)]
  for idx in range(2):
    handle = K.CreateNamedPipeW(name, 2 if direction == "read" else 1,
                                0, 2, 4096, 4096, 5000, None)
    if handle == BAD:
      raise OSError(c.get_last_error(), "CreateNamedPipeW")
    handles.append(handle)
  def serve(idx, h):
    rec = {"index":idx, "connected":False, "data":b""}
    try:
      states[idx]["stage"] = "connecting"
      ok = K.ConnectNamedPipe(h, None)
      err = c.get_last_error() if not ok else 0
      if not ok and err != 535:
        rec["error"] = f"ConnectNamedPipe:{err}"
        return
      rec["connected"] = True
      states[idx]["stage"] = "connected"
      n = w.DWORD()
      if direction == "read":
        states[idx]["stage"] = "writing"
        buf = c.create_string_buffer(PAYLOAD)
        if K.WriteFile(h, buf, len(PAYLOAD), c.byref(n), None):
          rec["data"] = PAYLOAD[:n.value]
          states[idx]["stage"] = "flushing"
          if not K.FlushFileBuffers(h):
            rec["flush_error"] = c.get_last_error()
        else:
          rec["error"] = f"WriteFile:{c.get_last_error()}"
      else:
        states[idx]["stage"] = "reading"
        buf = c.create_string_buffer(64)
        if K.ReadFile(h, buf, len(buf), c.byref(n), None):
          rec["data"] = buf.raw[:n.value]
        else:
          rec["error"] = f"ReadFile:{c.get_last_error()}"
      states[idx]["stage"] = "disconnecting"
      K.DisconnectNamedPipe(h)
    finally:
      states[idx]["stage"] = "finished"
      records.append(rec)
  for idx, handle in enumerate(handles):
    t = threading.Thread(target=serve, args=(idx,handle), daemon=True)
    t.start()
    threads.append(t)
  return handles, threads, records, states

def benchmark(bash, direction, iterations):
  p = subprocess.Popen([bash,"--noprofile","--norc","-c",SCRIPTS[direction]],
                       stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE,bufsize=0)
  assert p.stdin and p.stdout and p.stderr
  replies, errors = queue.Queue(), queue.Queue()
  threading.Thread(target=source,args=(p.stdout,replies),daemon=True).start()
  threading.Thread(target=source,args=(p.stderr,errors),daemon=True).start()
  timings = []
  try:
    if replies.get(timeout=10).strip() != b"READY":
      raise RuntimeError("Bash startup handshake incorrect")
    for index in range(iterations):
      name = r"\\.\pipe\git809-open-red-" + uuid.uuid4().hex
      handles, threads, records, states = make_server(name, direction)
      try:
        began = time.perf_counter_ns()
        if any(ord(ch) < 32 for ch in name):
          raise ValueError("control character in native pipe path")
        p.stdin.write((name + "\n").encode("utf-8"))
        p.stdin.flush()
        got = replies.get(timeout=8).strip().decode("utf-8", "replace")
        ms = (time.perf_counter_ns() - began) / 1000000
        if index == 0:
          print("FIRST_TRIAL " + json.dumps({"direction": direction, "reply": got,
                "request_ms": ms, "states_before_join": states}, sort_keys=True), flush=True)
        for t in threads:
          t.join(timeout=1)
        if any(t.is_alive() for t in threads):
          safe = [{**r, "data": r["data"].hex()} for r in records]
          raise RuntimeError(f"{index}: blocked native server after reply={got!r}, "
                             f"request_ms={ms:.3f}, states={states}, records={safe}")
        connects = sum(r["connected"] for r in records)
        payloads = sum(r["data"] == PAYLOAD for r in records)
        expected = "OK:RWF809_OK" if direction == "read" else "OK"
        if (got,connects,payloads)!=(expected,2,1):
          safe = [{**r,"data":r["data"].hex()} for r in records]
          raise RuntimeError(f"{index}: reply={got!r}, connections={connects}, payloads={payloads}; {safe}")
        timings.append(ms)
      finally:
        if all(not t.is_alive() for t in threads):
          for h in handles:
            if not K.CloseHandle(h):
              raise OSError(c.get_last_error(),"CloseHandle")
    ordered = sorted(timings)
    report = {"direction":direction,"iterations":iterations,
              "persistent_bash_processes":1,"pipe_instances_each":2,
              "confirmed_native_connections":iterations*2,
              "median_ms":statistics.median(ordered),
              "p95_ms":ordered[(len(ordered)*95+99)//100-1],
              "mean_ms":statistics.fmean(ordered),
              "min_ms":ordered[0],"max_ms":ordered[-1]}
    print("PIPE_OPEN_LATENCY "+json.dumps(report,sort_keys=True),flush=True)
    return report
  finally:
    try:
      p.stdin.write(b"STOP\n")
      p.stdin.flush()
    except (BrokenPipeError,OSError):
      pass
    try:
      p.wait(timeout=2)
    except subprocess.TimeoutExpired:
      p.kill()
      p.wait(timeout=2)
    p.stdin.close()
    p.stdout.close()
    p.stderr.close()
    if p.returncode != 0:
      print("BASH_STDERR="+repr(list(errors.queue)[:10]),flush=True)

def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("--bash",required=True)
  parser.add_argument("--iterations",type=int,default=50)
  parser.add_argument("--out",required=True)
  args = parser.parse_args()
  if not os.path.isfile(args.bash):
    parser.error("Git Bash executable missing")
  if not 2 <= args.iterations <= 500:
    parser.error("iterations out of range")
  cases = [benchmark(args.bash,mode,args.iterations) for mode in ("read","write")]
  with open(args.out,"w",encoding="utf-8") as f:
    json.dump({"sha":os.environ.get("GITHUB_SHA"),"bash":args.bash,
               "cases":cases},f,indent=2,sort_keys=True)
    f.write("\n")

if __name__=="__main__":
  main()
