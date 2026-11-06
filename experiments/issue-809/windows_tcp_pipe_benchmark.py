#!/usr/bin/env python3
"""#809 Windows-only: native named pipe versus loopback TCP, persistent Bash.

No Git compilation, Git processes or Python manager. Identical echo framing;
timing is within one persistent Bash process per transport, excluding process
launch and endpoint setup. Windows named pipes use Win32 CreateNamedPipeW
with a true duplex handle; TCP uses a listening 127.0.0.1 AF_INET socket.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid

COUNT=150
PAYLOAD="p"*64
BASH=r'''
set -e
endpoint=$1
kind=$2
runs=$3
payload=$4
if [[ $kind == tcp ]]; then
  exec 3<>"/dev/tcp/127.0.0.1/$endpoint" || exit 41
else
  exec 3<>"$endpoint" || exit 42
fi
for ((i=0;i<runs;i++)); do
  start=$EPOCHREALTIME
  printf 'Q%06d-%s\n' "$i" "$payload" >&3 || exit 43
  IFS= read -r answer <&3 || exit 44
  printf -v expected 'Q%06d-%s' "$i" "$payload"
  [[ $answer == "$expected" ]] || exit 45
  end=$EPOCHREALTIME
  printf 'SAMPLE %s %s %s\n' "$i" "$start" "$end"
done
exec 3<&-
exec 3>&-
'''
if os.name!="nt":
  raise RuntimeError("native Windows Python required")
K=C.WinDLL("kernel32",use_last_error=True)
K.CreateNamedPipeW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.LPVOID]
K.CreateNamedPipeW.restype=W.HANDLE
K.ConnectNamedPipe.argtypes=[W.HANDLE,W.LPVOID]
K.ConnectNamedPipe.restype=W.BOOL
K.DisconnectNamedPipe.argtypes=[W.HANDLE]
K.DisconnectNamedPipe.restype=W.BOOL
K.ReadFile.argtypes=[W.HANDLE,W.LPVOID,W.DWORD,C.POINTER(W.DWORD),W.LPVOID]
K.ReadFile.restype=W.BOOL
K.WriteFile.argtypes=[W.HANDLE,W.LPCVOID,W.DWORD,C.POINTER(W.DWORD),W.LPVOID]
K.WriteFile.restype=W.BOOL
K.CloseHandle.argtypes=[W.HANDLE]
K.CloseHandle.restype=W.BOOL
K.CancelIoEx.argtypes=[W.HANDLE,W.LPVOID]
K.CancelIoEx.restype=W.BOOL
INVALID=C.c_void_p(-1).value
ERROR_PIPE_CONNECTED=535
ERROR_BROKEN_PIPE=109

def pipe_connection(handle, metrics, stop):
  try:
    if not K.ConnectNamedPipe(handle,None) and C.get_last_error()!=ERROR_PIPE_CONNECTED:
      metrics["connect_errors"].append(C.get_last_error())
      return
    metrics["connections"]+=1
    pending=b""
    while not stop.is_set():
      buf=C.create_string_buffer(8192)
      n=W.DWORD()
      if not K.ReadFile(handle,buf,len(buf),C.byref(n),None):
        if C.get_last_error() not in (ERROR_BROKEN_PIPE,):
          metrics["read_errors"].append(C.get_last_error())
        break
      if not n.value:
        break
      pending+=buf.raw[:n.value]
      while b"\n" in pending:
        line,pending=pending.split(b"\n",1)
        data=line+b"\n"
        written=W.DWORD()
        if not K.WriteFile(handle,data,len(data),C.byref(written),None):
          metrics["write_errors"].append(C.get_last_error())
          return
        if written.value!=len(data):
          metrics["write_errors"].append("short write")
          return
        metrics["messages"]+=1
  finally:
    K.DisconnectNamedPipe(handle)

def tcp_connection(sock,metrics,stop):
  try:
    conn,_=sock.accept()
    metrics["connections"]+=1
    with conn:
      conn.settimeout(10)
      pending=b""
      while not stop.is_set():
        buf=conn.recv(8192)
        if not buf: break
        pending+=buf
        while b"\n" in pending:
          line,pending=pending.split(b"\n",1)
          conn.sendall(line+b"\n")
          metrics["messages"]+=1
  except Exception as exc:
    if not stop.is_set():
      metrics["read_errors"].append(repr(exc))

def elapsed_us(a,b):
  left=int(round(float(a)*1000000))
  right=int(round(float(b)*1000000))
  if right<left:
    raise AssertionError("nonmonotonic elapsed time")
  return right-left

def case(bash:str,mode:str):
  assert mode in ("tcp","pipe1","pipe2")
  metrics={"connections":0,"messages":0,
    "connect_errors":[],"read_errors":[],"write_errors":[]}
  stop=threading.Event()
  handles=[]
  listener=None
  threads=[]
  if mode=="tcp":
    listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    listener.bind(("127.0.0.1",0))
    listener.listen(1)
    address=str(listener.getsockname()[1])
    t=threading.Thread(target=tcp_connection,args=(listener,metrics,stop),daemon=True)
    t.start()
    threads.append(t)
  else:
    address=r"\\.\pipe\git809-roundtrip-"+uuid.uuid4().hex
    instances=1 if mode=="pipe1" else 2
    for _ in range(instances):
      h=K.CreateNamedPipeW(address,3,0,instances,65536,65536,5000,None)
      if h==INVALID:
        raise OSError(C.get_last_error(),"CreateNamedPipeW")
      handles.append(h)
    for h in handles:
      t=threading.Thread(target=pipe_connection,args=(h,metrics,stop),daemon=True)
      t.start()
      threads.append(t)
  try:
    try:
      result=subprocess.run([bash,"--noprofile","--norc","-c",
        BASH,"bench",address,"tcp" if mode=="tcp" else "pipe",str(COUNT),PAYLOAD],
        capture_output=True,timeout=15,check=False)
    except subprocess.TimeoutExpired as exc:
      print("TRANSPORT_TIMEOUT "+json.dumps({"mode":mode,
        "stdout":(exc.stdout or b"").decode("utf-8","replace")[-500:],
        "stderr":(exc.stderr or b"").decode("utf-8","replace")[-500:]}),
        flush=True)
      return {"mode":mode,"exit":"TIMEOUT","samples":0,"server":metrics}
    samples=[]
    for line in result.stdout.decode("utf-8","replace").splitlines():
      if line.startswith("SAMPLE "):
        parts=line.split()
        samples.append(elapsed_us(parts[2],parts[3]))
    record={"mode":mode,"configured_instances":0 if mode=="tcp" else len(handles),
      "samples":len(samples),"expected":COUNT,"exit":result.returncode,
      "server":metrics,"stderr":result.stderr.decode("utf-8","replace")[-700:]}
    if samples:
      sorted_us=sorted(samples)
      record.update(median_us=statistics.median(samples),
        p95_us=sorted_us[int(.95*len(samples)+.999999)-1],
        min_us=min(samples),max_us=max(samples),
        sample_us=samples)
    print("TRANSPORT_RESULT "+json.dumps(record),flush=True)
    return record
  finally:
    stop.set()
    if listener is not None:
      listener.close()
    # A blocking ConnectNamedPipe/ReadFile can outlive a failed client.
    # Cancel first; closing an active synchronous handle is not a
    # reliable way to stop its servicing thread.
    for h in handles:
      K.CancelIoEx(h,None)
    for t in threads:
      t.join(timeout=.5)
    for h,t in zip(handles,threads):
      if not t.is_alive():
        K.CloseHandle(h)
      else:
        print("PIPE_TEARDOWN_PENDING "+mode,flush=True)
        # The isolated test process releases these on exit; do not block
        # the transport comparison waiting on a failed pipe connection.

def main():
  bash=os.environ.get("GIT809_BASH","bash")
  results=[case(bash,mode) for mode in ("tcp","pipe2","pipe1")]
  output=Path("git809-bash-pipe-results")
  output.mkdir(exist_ok=True)
  (output/"windows-tcp-versus-pipe.json").write_text(
    json.dumps({"windows":sys.getwindowsversion().platform,
      "configuration":{"count":COUNT,"payload_bytes":len(PAYLOAD)+8,
        "timing":"Bash EPOCHREALTIME from preconnected request to reply"},
      "results":results},indent=2)+"\n",encoding="utf-8")
  tcp=results[0]
  if tcp["exit"] or tcp["samples"]!=COUNT or tcp["server"]["messages"]!=COUNT:
    raise AssertionError("localhost TCP did not complete exact echo acceptance")
  # Pipe modes are diagnostic; preserve their failures rather than masking them.
  return 0

if __name__=="__main__":
  sys.exit(main())
