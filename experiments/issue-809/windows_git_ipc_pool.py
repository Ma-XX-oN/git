#!/usr/bin/env python3
"""#809 experimental persistent shell-owned pool: sequential real Git requests.

Each command consumes one already-started wmain Git worker. Replenishment
happens before subsequent command acceptance. The four independent client
channels preserve the previously verified Git Bash named-pipe transport.
"""
from __future__ import annotations
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from windows_real_git_manager import (K, INVALID, MAX_FRAME, MAX_OUTPUT,
  INSTANCES, slot_worker, send)
from windows_shell_owned_pool import connect_preinitialized, WAIT_TIMEOUT

K.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD]
K.OpenProcess.restype=W.HANDLE
K.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD]
K.WaitForSingleObject.restype=W.DWORD
K.CloseHandle.argtypes=[W.HANDLE]
K.CloseHandle.restype=W.BOOL
SYNCHRONIZE=0x00100000
POLL_SECONDS=10

def start_worker(git:Path):
  token=uuid.uuid4().hex[:16]
  child=subprocess.Popen([str(git),"--git809-worker",token],
    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  try:
    h=connect_preinitialized(child,token)
  except Exception:
    child.kill()
    child.communicate(timeout=5)
    raise
  return (child,h)

def cleanup_worker(entry):
  child,h=entry
  if h!=INVALID:
    K.CloseHandle(h)
  if child.poll() is None:
    child.kill()
  try:
    child.communicate(timeout=3)
  except subprocess.TimeoutExpired:
    child.kill()
    child.communicate(timeout=3)

def perform_round(entries,git,base,index,owner):
  token=base+"-"+str(index)
  request={"ready":threading.Event(),"data":None,"lock":threading.Lock(),
           "delivered":{"stdout":0,"stderr":0,"status":0}}
  done=threading.Event()
  output={}
  errors=[]
  for kind in ("request","stdout","stderr","status"):
    for _ in range(INSTANCES):
      t=threading.Thread(target=slot_worker,
        args=(kind,token,done,output,request,errors),daemon=True)
      t.start()
  # Wait in short intervals so owner-shell termination remains effective.
  while not request["ready"].wait(.1):
    if K.WaitForSingleObject(owner,0)!=WAIT_TIMEOUT:
      return False
  frame=request["data"]
  if not frame or len(frame)>MAX_FRAME:
    raise RuntimeError("invalid or oversized Git request frame")
  if len(frame)<8 or frame[1:4]!=b"\x00"*3 or not frame.endswith(b"\x00"):
    raise ValueError("invalid Git argument frame")
  count=frame[0]
  arguments=frame[4:].split(b"\x00")
  if not 1<=count<=32 or len(arguments)!=count+1 or arguments[0]!=b"git":
    raise ValueError("invalid Git argument count or program identity")
  for argument in arguments[:-1]:
    argument.decode("utf-8","strict")
  child,h=entries.pop(0)
  if not send(h,frame):
    raise RuntimeError("failed to forward request to ready Git worker")
  K.CloseHandle(h)
  h=INVALID
  stdout,stderr=child.communicate(timeout=30)
  if len(stdout)>MAX_OUTPUT or len(stderr)>MAX_OUTPUT:
    raise RuntimeError("Git output exceeds benchmark fixture limit")
  output.update(stdout=stdout,stderr=stderr,
                status=("EXIT "+str(child.returncode)+"\n").encode("ascii"))
  # Spawn replacement independently of the outbound data phase.
  errors_in_refill=[]
  def refill():
    try:
      entries.append(start_worker(git))
    except Exception as exc:
      errors_in_refill.append(repr(exc))
  refill_thread=threading.Thread(target=refill)
  refill_thread.start()
  done.set()
  deadline=time.monotonic()+15
  while time.monotonic()<deadline:
    if all(request["delivered"][k] for k in ("stdout","stderr","status")):
      break
    time.sleep(.01)
  refill_thread.join(timeout=15)
  if refill_thread.is_alive() or errors_in_refill:
    raise RuntimeError("worker replenishment failed: "+repr(errors_in_refill))
  if not all(request["delivered"].values()):
    raise RuntimeError("Git output delivery incomplete: "+repr(request["delivered"]))
  if errors:
    print("PIPE_DIAGNOSTICS "+json.dumps(errors),flush=True)
  print("COMMAND "+json.dumps({"index":index,"worker_rc":child.returncode,
        "worker_pid":child.pid,"stdout_hex":stdout.hex(),
        "stderr_hex":stderr.hex(),"ready_after":len(entries)}),flush=True)
  return True

def main():
  parser=argparse.ArgumentParser()
  parser.add_argument("--git",required=True,type=Path)
  parser.add_argument("--workers",required=True,type=int)
  parser.add_argument("--owner-winpid",required=True,type=int)
  parser.add_argument("--ready-file",required=True,type=Path)
  args=parser.parse_args()
  if not 1<=args.workers<=32:parser.error("workers must be 1..32")
  if not 1<=args.owner_winpid<=0xffffffff:parser.error("invalid owner WinPID")
  git=args.git.resolve(strict=True)
  owner=K.OpenProcess(SYNCHRONIZE,False,args.owner_winpid)
  if not owner:raise OSError(C.get_last_error(),"OpenProcess(owner)")
  if K.WaitForSingleObject(owner,0)!=WAIT_TIMEOUT:
    raise RuntimeError("owner already exited")
  entries=[]
  base=uuid.uuid4().hex[:16]
  try:
    for _ in range(args.workers):
      entries.append(start_worker(git))
    state={"manager_pid":os.getpid(),"owner_winpid":args.owner_winpid,
      "worker_count":args.workers,"worker_pids":[x[0].pid for x in entries],
      "poll_seconds":POLL_SECONDS,"pipe_token":base,"state":"READY"}
    args.ready_file.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.ready_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state)+"\n",encoding="utf-8")
    tmp.replace(args.ready_file)
    print("READY "+json.dumps(state),flush=True)
    index=0
    while K.WaitForSingleObject(owner,0)==WAIT_TIMEOUT:
      if not perform_round(entries,git,base,index,owner):break
      index+=1
    print("OWNER_EXIT "+str(args.owner_winpid),flush=True)
    return 0
  finally:
    args.ready_file.unlink(missing_ok=True)
    for entry in entries:
      cleanup_worker(entry)
    K.CloseHandle(owner)

if __name__=="__main__":
  if os.name!="nt":raise RuntimeError("native Windows required")
  sys.exit(main())
