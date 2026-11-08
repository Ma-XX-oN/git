#!/usr/bin/env python3
"""#809 shell-owned Windows worker allocator; no Git command-data relay.

One allocation-only duplex pipe returns the native PID of an already-READY
native Git --preload worker. A replacement is initiated immediately on allocation,
independently of command execution, pipe consumption and worker termination.
"""
from __future__ import annotations
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

if os.name != "nt":
  raise RuntimeError("Windows native Git worker manager required")
K=C.WinDLL("kernel32",use_last_error=True)
K.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD]
K.OpenProcess.restype=W.HANDLE
K.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD]
K.WaitForSingleObject.restype=W.DWORD
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
K.PeekNamedPipe.argtypes=[W.HANDLE,W.LPVOID,W.DWORD,C.POINTER(W.DWORD),C.POINTER(W.DWORD),C.POINTER(W.DWORD)]
K.PeekNamedPipe.restype=W.BOOL
K.FlushFileBuffers.argtypes=[W.HANDLE]
K.FlushFileBuffers.restype=W.BOOL
K.CloseHandle.argtypes=[W.HANDLE]
K.CloseHandle.restype=W.BOOL
K.GetCurrentProcess.restype=W.HANDLE
K.GetProcessHandleCount.argtypes=[W.HANDLE,C.POINTER(W.DWORD)]
K.GetProcessHandleCount.restype=W.BOOL
INVALID=C.c_void_p(-1).value
WAIT_TIMEOUT=258
SYNCHRONIZE=0x00100000
PIPE_ACCESS_DUPLEX=3
FILE_FLAG_FIRST_PIPE_INSTANCE=0x00080000
PIPE_TYPE_BYTE=0
PIPE_WAIT=0
ERROR_PIPE_CONNECTED=535
ERROR_NO_DATA=232
POLL_SECONDS=10
class Pool:
  def __init__(self, git:Path, target:int):
    self.git=git
    self.target=target
    self.ready=queue.Queue()
    self.children=[]
    self.lock=threading.Lock()
    self.closing=threading.Event()
    self.errors=[]
    self.allocations=0
    self.owner_winpid=None

  def reap_completed(self):
    """Release completed worker process objects while retaining live workers."""
    with self.lock:
      self.children=[child for child in self.children if child.poll() is None]

  def spawn(self):
    if self.closing.is_set():
      return
    try:
      worker_env=os.environ.copy()
      if self.owner_winpid is None:
        raise RuntimeError("owner_winpid not initialized")
      worker_env["GIT809_PRELOAD_OWNER_WINPID"]=str(self.owner_winpid)
      p=subprocess.Popen([str(self.git),"--preload"],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        env=worker_env,bufsize=0)
    except OSError as error:
      with self.lock:
        self.errors.append("worker spawn failed: "+repr(error))
      print("WORKER_SPAWN_FAILED "+repr(error),flush=True)
      return
    with self.lock:
      self.children.append(p)
    try:
      line=p.stdout.readline()
    finally:
      # READY is the only startup-pipe message. The four native named pipes
      # carry the command; retaining each startup pipe leaks a manager handle.
      p.stdout.close()
    if line != ("READY "+str(p.pid)+"\n").encode("ascii"):
      with self.lock:
        self.errors.append("worker readiness failed: pid="+str(p.pid)+" response="+repr(line))
      if p.poll() is None:
        p.kill()
      return
    if self.closing.is_set():
      if p.poll() is None:
        p.kill()
      return
    self.reap_completed()
    self.ready.put(p)
    print("WORKER_READY "+str(p.pid),flush=True)

  def alloc(self):
    try:
      p=self.ready.get(timeout=5)
    except queue.Empty:
      return "WAIT\n"
    # The client owns this worker after allocation. Retain only workers the
    # manager is still offering; the native worker watches the shell itself.
    with self.lock:
      if p not in self.children:
        raise RuntimeError("allocated worker missing from ready tracking")
      self.children.remove(p)
    if p.poll() is not None:
      threading.Thread(target=self.spawn,daemon=True).start()
      return "WAIT\n"
    # Start replacement before replying to client. Never join this thread.
    threading.Thread(target=self.spawn,daemon=True).start()
    self.allocations+=1
    if self.allocations % 100 == 0:
      handles=W.DWORD()
      if K.GetProcessHandleCount(K.GetCurrentProcess(),C.byref(handles)):
        with self.lock:
          retained=len(self.children)
        print(f"ALLOC_RESOURCE allocations={self.allocations} "
              f"manager_handles={handles.value} tracked_workers={retained}",
              flush=True)
    print("ALLOCATED "+str(p.pid),flush=True)
    return str(p.pid)+"\n"

  def close(self):
    self.closing.set()
    with self.lock:
      children=list(self.children)
    for p in children:
      if p.poll() is None:
        p.kill()
    for p in children:
      try:
        p.wait(timeout=3)
      except subprocess.TimeoutExpired:
        p.kill()
    for p in children:
      if p.stdout:
        p.stdout.close()

def allocation_server(owner_pid:int,pool:Pool):
  address=r"\\.\pipe\git809-allocate-"+str(owner_pid)
  first=True
  print("ALLOCATOR_START "+address,flush=True)
  while not pool.closing.is_set():
    handles=[]
    connected=queue.Queue()
    threads=[]
    def accept(h,index):
      print(f"ALLOC_API_ENTER ns={time.perf_counter_ns()} tid={threading.get_native_id()} op=ConnectNamedPipe handle={h} instance={index}",flush=True)
      ok=bool(K.ConnectNamedPipe(h,None))
      err=0 if ok else C.get_last_error()
      print(f"ALLOC_API_RETURN ns={time.perf_counter_ns()} tid={threading.get_native_id()} op=ConnectNamedPipe handle={h} instance={index} ok={ok} error={err}",flush=True)
      connected.put((index,ok or err in (ERROR_PIPE_CONNECTED,ERROR_NO_DATA),err))
    try:
      for index in range(2):
        print(f"ALLOC_API_ENTER ns={time.perf_counter_ns()} tid={threading.get_native_id()} op=CreateNamedPipe instance={index}",flush=True)
        h=K.CreateNamedPipeW(address,
          PIPE_ACCESS_DUPLEX|(FILE_FLAG_FIRST_PIPE_INSTANCE if first and index==0 else 0),
          PIPE_TYPE_BYTE|PIPE_WAIT,2,256,256,5000,None)
        print(f"ALLOC_API_RETURN ns={time.perf_counter_ns()} op=CreateNamedPipe instance={index} handle={h} error={C.get_last_error() if h==INVALID else 0}",flush=True)
        if h==INVALID:
          raise OSError(C.get_last_error(),"CreateNamedPipe(allocation)")
        handles.append(h)
      first=False
      for index,h in enumerate(handles):
        t=threading.Thread(target=accept,args=(h,index),daemon=True)
        t.start()
        threads.append(t)
      first_index,first_ok,first_error=connected.get(timeout=15)
      second_index,second_ok,second_error=connected.get(timeout=15)
      if not first_ok or not second_ok or first_index==second_index:
        raise OSError(first_error or second_error,"ConnectNamedPipe(allocation)")
      # Git Bash opens two connections for one shell descriptor. The first
      # is a probe, but it is NOT guaranteed to disconnect before the real
      # data-bearing connection writes. A state-based (4,3) assumption failed
      # on native Windows at request 2625 with persistent states (4,4).
      # Identify the data instance by request bytes instead of probe closure.
      for _ in range(5000):
        available=[]
        for h in handles:
          pending=W.DWORD()
          if K.PeekNamedPipe(h,None,0,None,C.byref(pending),None):
            available.append(pending.value)
          else:
            error=C.get_last_error()
            if error not in (109,ERROR_NO_DATA,233):
              raise OSError(error,"PeekNamedPipe(allocation)")
            available.append(0)
        written=[index for index,nbytes in enumerate(available) if nbytes]
        if len(written)==1:
          data_index=written[0]
          probe_index=1-data_index
          break
        if len(written)>1:
          raise RuntimeError(f"ambiguous allocation request writers: {available}")
        time.sleep(.001)
      else:
        raise TimeoutError(f"allocation request bytes never arrived: {available}")
      h=handles[data_index]
      print("ALLOCATOR_CONNECTED probe="+str(probe_index)+" data="+str(data_index)+
            " errors="+str((first_error,second_error)),flush=True)
      buf=C.create_string_buffer(64)
      count=W.DWORD()
      print(f"ALLOC_API_ENTER ns={time.perf_counter_ns()} tid={threading.get_native_id()} op=ReadFile handle={h}",flush=True)
      read_ok=bool(K.ReadFile(h,buf,64,C.byref(count),None))
      read_error=0 if read_ok else C.get_last_error()
      print(f"ALLOC_API_RETURN ns={time.perf_counter_ns()} op=ReadFile handle={h} ok={read_ok} bytes={count.value} error={read_error}",flush=True)
      if read_ok and buf.raw[:count.value]==b"GET\n":
        answer=pool.alloc().encode("ascii")
      else:
        answer=b"INVALID\n"
      n=W.DWORD()
      print(f"ALLOC_API_ENTER ns={time.perf_counter_ns()} op=WriteFile handle={h} bytes={len(answer)}",flush=True)
      write_ok=bool(K.WriteFile(h,answer,len(answer),C.byref(n),None))
      write_error=0 if write_ok else C.get_last_error()
      print(f"ALLOC_API_RETURN ns={time.perf_counter_ns()} op=WriteFile handle={h} ok={write_ok} bytes={n.value} error={write_error}",flush=True)
      if not write_ok or n.value!=len(answer):
        raise OSError(write_error,"WriteFile(allocation)")
      print(f"ALLOC_API_ENTER ns={time.perf_counter_ns()} op=FlushFileBuffers handle={h}",flush=True)
      flush_ok=bool(K.FlushFileBuffers(h))
      flush_error=0 if flush_ok else C.get_last_error()
      print(f"ALLOC_API_RETURN ns={time.perf_counter_ns()} op=FlushFileBuffers handle={h} ok={flush_ok} error={flush_error}",flush=True)
      if not flush_ok:
        raise OSError(flush_error,"FlushFileBuffers(allocation)")
    finally:
      for h in handles:
        print(f"ALLOC_API_ENTER ns={time.perf_counter_ns()} op=DisconnectNamedPipe handle={h}",flush=True)
        disconnected=bool(K.DisconnectNamedPipe(h))
        print(f"ALLOC_API_RETURN ns={time.perf_counter_ns()} op=DisconnectNamedPipe handle={h} ok={disconnected} error={0 if disconnected else C.get_last_error()}",flush=True)
      for t in threads:
        t.join(timeout=5)
        if t.is_alive():
          raise RuntimeError("allocation connection waiter did not stop")
      for h in handles:
        K.CloseHandle(h)

def main():
  parser=argparse.ArgumentParser()
  parser.add_argument("--git",required=True,type=Path)
  parser.add_argument("--workers",required=True,type=int)
  parser.add_argument("--owner-winpid",required=True,type=int)
  parser.add_argument("--ready-file",required=True,type=Path)
  args=parser.parse_args()
  if not 1<=args.workers<=32:parser.error("workers must be 1..32")
  if not 1<=args.owner_winpid<=0xffffffff:parser.error("owner PID invalid")
  git=args.git.resolve(strict=True)
  owner=K.OpenProcess(SYNCHRONIZE,False,args.owner_winpid)
  if not owner:
    raise OSError(C.get_last_error(),"OpenProcess(owner)")
  if K.WaitForSingleObject(owner,0)!=WAIT_TIMEOUT:
    raise RuntimeError("owner already terminated")
  pool=Pool(git,args.workers)
  pool.owner_winpid=args.owner_winpid
  try:
    for _ in range(args.workers):
      pool.spawn()
    if pool.ready.qsize()!=args.workers:
      raise RuntimeError("not all initial workers are ready: "+repr(pool.errors))
    # Ensure allocator exists before publishing READY. The allocator's
    # initialization is synchronized by an explicit event instead of sleeps.
    allocator=threading.Thread(target=allocation_server,
      args=(args.owner_winpid,pool),daemon=True)
    allocator.start()
    # CreateNamedPipe happens immediately, but verify endpoint existence via
    # WaitNamedPipeW rather than assuming a started thread is ready.
    K.WaitNamedPipeW.argtypes=[W.LPCWSTR,W.DWORD]
    K.WaitNamedPipeW.restype=W.BOOL
    address=r"\\.\pipe\git809-allocate-"+str(args.owner_winpid)
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
      if K.WaitNamedPipeW(address,100):
        break
      if not allocator.is_alive():
        raise RuntimeError("allocation server failed")
      time.sleep(.01)
    else:
      raise RuntimeError("allocation server startup timeout")
    record={"manager_pid":os.getpid(),"owner_winpid":args.owner_winpid,
      "worker_count":args.workers,"worker_pids":[x.pid for x in list(pool.ready.queue)],
      "poll_seconds":POLL_SECONDS,"state":"READY"}
    args.ready_file.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.ready_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(record)+"\n",encoding="utf-8")
    tmp.replace(args.ready_file)
    print("READY "+json.dumps(record),flush=True)
    while K.WaitForSingleObject(owner,POLL_SECONDS*1000)==WAIT_TIMEOUT:
      if not allocator.is_alive():
        raise RuntimeError("allocation server unexpectedly stopped")
      if pool.errors:
        raise RuntimeError("worker initialization failed: "+repr(pool.errors))
    print("OWNER_EXIT "+str(args.owner_winpid),flush=True)
  finally:
    args.ready_file.unlink(missing_ok=True)
    pool.close()
    K.CloseHandle(owner)

if __name__=="__main__":
  sys.exit(main())
