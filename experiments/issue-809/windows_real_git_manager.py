#!/usr/bin/env python3
"""#809 experimental actual Git wmain worker + Bash-only four-channel IPC."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

K=C.WinDLL("kernel32",use_last_error=True)
K.CreateNamedPipeW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.LPVOID]
K.CreateNamedPipeW.restype=W.HANDLE
K.ConnectNamedPipe.argtypes=[W.HANDLE,W.LPVOID]
K.ConnectNamedPipe.restype=W.BOOL
K.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.LPVOID,W.DWORD,W.DWORD,W.HANDLE]
K.CreateFileW.restype=W.HANDLE
K.ReadFile.argtypes=[W.HANDLE,W.LPVOID,W.DWORD,C.POINTER(W.DWORD),W.LPVOID]
K.ReadFile.restype=W.BOOL
K.WriteFile.argtypes=[W.HANDLE,W.LPCVOID,W.DWORD,C.POINTER(W.DWORD),W.LPVOID]
K.WriteFile.restype=W.BOOL
K.FlushFileBuffers.argtypes=[W.HANDLE];K.FlushFileBuffers.restype=W.BOOL
K.CloseHandle.argtypes=[W.HANDLE];K.CloseHandle.restype=W.BOOL
INVALID=C.c_void_p(-1).value
PIPE_ACCESS_INBOUND=1
PIPE_ACCESS_OUTBOUND=2
PIPE_TYPE_BYTE=0
PIPE_READMODE_BYTE=0
PIPE_WAIT=0
PIPE_UNLIMITED_INSTANCES=255
ERROR_PIPE_CONNECTED=535
ERROR_BROKEN_PIPE=109
GENERIC_WRITE=0x40000000
OPEN_EXISTING=3
MAX_FRAME=4096
MAX_OUTPUT=1024*1024
INSTANCES=8

def name(kind,token):
  return "\\\\.\\pipe\\git809-real-"+kind+"-"+token

def recv(h):
  parts=[]
  total=0
  while True:
    buf=C.create_string_buffer(4096)
    count=W.DWORD()
    ok=K.ReadFile(h,buf,len(buf),C.byref(count),None)
    if not ok:
      code=C.get_last_error()
      if code==ERROR_BROKEN_PIPE: break
      raise OSError(code,"ReadFile")
    if not count.value: break
    parts.append(buf.raw[:count.value])
    total+=count.value
    if total>MAX_FRAME: raise ValueError("request too large")
  return b"".join(parts)

def send(h,data):
  for i in range(0,len(data),4096):
    chunk=data[i:i+4096]
    count=W.DWORD()
    c=C.create_string_buffer(chunk)
    if not K.WriteFile(h,c,len(chunk),C.byref(count),None):
      return False
    if count.value!=len(chunk): return False
  if data:
    K.FlushFileBuffers(h)
  return True

def server(kind,token,ready,done,result,lock,events):
  for slot in range(INSTANCES):
    pass

def slot_worker(kind,token,event,output,request_slot,errors):
  p=name(kind,token)
  h=K.CreateNamedPipeW(p,PIPE_ACCESS_INBOUND if kind=="request"
     else PIPE_ACCESS_OUTBOUND,PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT,
     PIPE_UNLIMITED_INSTANCES,4096,4096,5000,None)
  if h==INVALID:
    errors.append((kind,"create",C.get_last_error()))
    return
  try:
    if not K.ConnectNamedPipe(h,None):
      err=C.get_last_error()
      if err!=ERROR_PIPE_CONNECTED:
        errors.append((kind,"connect",err));return
    if kind=="request":
      try:
        data=recv(h)
        if data and not request_slot["ready"].is_set():
          with request_slot["lock"]:
            if not request_slot["ready"].is_set():
              request_slot["data"]=data
              request_slot["ready"].set()
      except (ValueError,OSError) as exc:
        errors.append((kind,"read",str(exc)))
    elif event.wait(15):
      data=output.get(kind,b"")
      if send(h,data):
        with request_slot["lock"]:
          request_slot["delivered"][kind]+=1
  finally:
    K.CloseHandle(h)

def manager(git:Path,token:str):
  request={"ready":threading.Event(),"data":None,"lock":threading.Lock(),
           "delivered":{"stdout":0,"stderr":0,"status":0}}
  event=threading.Event()
  output={}
  errors=[]
  # Prestart a genuine Git binary, after Windows CRT and Git wmain setup.
  child=subprocess.Popen([str(git),"--git809-worker",token],
    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  pipe=name("git-worker",token)
  h=INVALID
  try:
    for _ in range(800):
      h=K.CreateFileW(pipe,GENERIC_WRITE,0,None,OPEN_EXISTING,0,None)
      if h!=INVALID: break
      if child.poll() is not None:
        raise RuntimeError(f"Git worker exited before ready: {child.returncode}")
      time.sleep(.01)
    if h==INVALID: raise RuntimeError("Git worker did not reach wmain IPC rendezvous")
    threads=[]
    for kind in ("request","stdout","stderr","status"):
      for _ in range(INSTANCES):
        t=threading.Thread(target=slot_worker,
          args=(kind,token,event,output,request,errors),daemon=True)
        t.start();threads.append(t)
    print("READY",flush=True)
    if not request["ready"].wait(12):
      raise RuntimeError("no Bash request received")
    frame=request["data"]
    if len(frame)>MAX_FRAME: raise ValueError("oversized request")
    if not send(h,frame): raise RuntimeError("failed writing Git worker request")
    K.CloseHandle(h);h=INVALID
    stdout,stderr=child.communicate(timeout=20)
    if len(stdout)>MAX_OUTPUT or len(stderr)>MAX_OUTPUT:
      raise RuntimeError("Git output exceeds bounded fixture limit")
    output.update(stdout=stdout,stderr=stderr,
                  status=("EXIT "+str(child.returncode)+"\n").encode("ascii"))
    event.set()
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
      if all(request["delivered"][k] for k in ("stdout","stderr","status")):
        break
      time.sleep(.01)
    print("RESULT "+json.dumps({"worker_rc":child.returncode,
      "stdout_hex":stdout.hex(),"stderr_hex":stderr.hex(),
      "output_len":len(stdout),"error_len":len(stderr),
      "delivered":request["delivered"],"pipe_errors":errors}),flush=True)
    if not all(request["delivered"][k] for k in ("stdout","stderr","status")):
      return 4
    return 0
  finally:
    if h!=INVALID: K.CloseHandle(h)
    if child.poll() is None:
      child.kill()
    try: child.communicate(timeout=2)
    except subprocess.TimeoutExpired: child.kill()

if __name__=="__main__":
  if os.name!="nt" or len(sys.argv)!=3:sys.exit(2)
  sys.exit(manager(Path(sys.argv[1]).resolve(),sys.argv[2]))
