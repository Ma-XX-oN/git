#!/usr/bin/env python3
"""#809 shell-owned Windows worker allocator; no Git command-data relay.

Two allocation-only logical channels are used: GET flows from Git Bash to
git-manager; the worker PID flows back on a per-client reply channel. The
Windows duplex pipe facility is used only for the T connection-identification
handshake. A replacement is initiated immediately on allocation, independent
of command execution, pipe consumption and worker termination.
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
K.OpenThread.argtypes=[W.DWORD,W.BOOL,W.DWORD]
K.OpenThread.restype=W.HANDLE
K.CancelSynchronousIo.argtypes=[W.HANDLE]
K.CancelSynchronousIo.restype=W.BOOL
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
FILE_FLAG_OVERLAPPED=0x40000000
ERROR_IO_PENDING=997
ERROR_OPERATION_ABORTED=995
ERROR_BROKEN_PIPE=109
ERROR_NOT_FOUND=1168
WAIT_OBJECT_0=0
WAIT_FAILED=0xffffffff

class OVERLAPPED(C.Structure):
  _fields_=[("Internal",C.c_size_t),("InternalHigh",C.c_size_t),
            ("Offset",W.DWORD),("OffsetHigh",W.DWORD),("hEvent",W.HANDLE)]

K.CreateEventW.argtypes=[W.LPVOID,W.BOOL,W.BOOL,W.LPCWSTR]
K.CreateEventW.restype=W.HANDLE
K.ResetEvent.argtypes=[W.HANDLE]
K.ResetEvent.restype=W.BOOL
K.WaitForMultipleObjects.argtypes=[W.DWORD,C.POINTER(W.HANDLE),W.BOOL,W.DWORD]
K.WaitForMultipleObjects.restype=W.DWORD
K.GetOverlappedResult.argtypes=[W.HANDLE,C.POINTER(OVERLAPPED),
                                C.POINTER(W.DWORD),W.BOOL]
K.GetOverlappedResult.restype=W.BOOL
K.CancelIoEx.argtypes=[W.HANDLE,C.POINTER(OVERLAPPED)]
K.CancelIoEx.restype=W.BOOL

def _start_async(state,operation,data=None):
  """Start one pending connect/read/write on a named-pipe instance."""
  state["pending"]=False
  K.ResetEvent(state["event"])
  state["ov"].Internal=0
  state["ov"].InternalHigh=0
  n=W.DWORD()
  if operation=="connect":
    ok=bool(K.ConnectNamedPipe(state["h"],C.byref(state["ov"])))
  elif operation=="read":
    state["buffer"]=C.create_string_buffer(1)
    ok=bool(K.ReadFile(state["h"],state["buffer"],1,C.byref(n),
                       C.byref(state["ov"])))
  else:
    assert operation=="write" and data is not None
    state["buffer"]=C.create_string_buffer(data)
    ok=bool(K.WriteFile(state["h"],state["buffer"],len(data),
                        C.byref(n),C.byref(state["ov"])))
  if ok:
    return n.value
  error=C.get_last_error()
  if operation=="connect" and error==ERROR_PIPE_CONNECTED:
    return 0
  if error==ERROR_IO_PENDING:
    state["pending"]=True
    return None
  if error in (ERROR_BROKEN_PIPE,ERROR_NO_DATA,233):
    return -1
  raise OSError(error,operation)

def _finish_async(state):
  n=W.DWORD()
  if not K.GetOverlappedResult(state["h"],C.byref(state["ov"]),
                               C.byref(n),False):
    error=C.get_last_error()
    if error in (ERROR_BROKEN_PIPE,ERROR_NO_DATA,233,ERROR_OPERATION_ABORTED):
      state["pending"]=False
      return -1
    raise OSError(error,"GetOverlappedResult")
  state["pending"]=False
  return n.value

def _close_async(state,close_pipe=True):
  if state["pending"]:
    if not K.CancelIoEx(state["h"],C.byref(state["ov"])) and (
        C.get_last_error()!=ERROR_NOT_FOUND):
      raise OSError(C.get_last_error(),"CancelIoEx")
    n=W.DWORD()
    # Keep OVERLAPPED storage and its event alive through cancellation.
    K.GetOverlappedResult(state["h"],C.byref(state["ov"]),C.byref(n),True)
    state["pending"]=False
  K.CloseHandle(state["event"])
  if close_pipe:
    K.DisconnectNamedPipe(state["h"])
    K.CloseHandle(state["h"])

def _wait_async(states,deadline):
  waiting=[x for x in states if x["pending"]]
  if not waiting:
    return None
  remaining=deadline-time.monotonic()
  if remaining<=0:
    return None
  handles=(W.HANDLE*len(waiting))(*(x["event"] for x in waiting))
  result=K.WaitForMultipleObjects(len(waiting),handles,False,
                                  max(1,int(remaining*1000)))
  if result==WAIT_TIMEOUT:
    return None
  if result==WAIT_FAILED or result>=len(waiting):
    raise OSError(C.get_last_error(),"WaitForMultipleObjects")
  return waiting[result]

def _write_reply(handle,data):
  """Write on a FILE_FLAG_OVERLAPPED handle without a helper thread."""
  event=K.CreateEventW(None,True,False,None)
  if not event:
    raise OSError(C.get_last_error(),"CreateEvent(reply write)")
  state={"h":handle,"event":event,"ov":OVERLAPPED(),
         "pending":False,"buffer":None}
  state["ov"].hEvent=event
  try:
    n=_start_async(state,"write",data)
    if n is None:
      result=K.WaitForSingleObject(event,2000)
      if result!=WAIT_OBJECT_0:
        raise TimeoutError("reply write timeout")
      n=_finish_async(state)
    if n!=len(data):
      raise OSError("short allocation reply write")
  finally:
    _close_async(state,False)
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
    # Keep READY queue and tracked process objects in the same ownership set.
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

def select_transport(address, marker):
  """Select a marked reply instance, event-driven on this thread only."""
  expected=str(marker).encode("ascii")
  if len(expected)!=1 or expected not in tuple(bytes([i]) for i in range(48,58)):
    raise ValueError("invalid decimal reply marker")
  states=[]
  selected=None
  deadline=time.monotonic()+2
  try:
    for i in range(2):
      h=K.CreateNamedPipeW(address,PIPE_ACCESS_DUPLEX|FILE_FLAG_OVERLAPPED|
        (FILE_FLAG_FIRST_PIPE_INSTANCE if i==0 else 0),
        PIPE_TYPE_BYTE|PIPE_WAIT,2,256,256,2000,None)
      if h==INVALID:
        raise OSError(C.get_last_error(),"CreateNamedPipe(reply)")
      event=K.CreateEventW(None,True,False,None)
      if not event:
        K.CloseHandle(h)
        raise OSError(C.get_last_error(),"CreateEvent(reply)")
      state={"h":h,"event":event,"ov":OVERLAPPED(),"pending":False,
             "buffer":None,"phase":"connect","marked":False}
      state["ov"].hEvent=event
      states.append(state)
      n=_start_async(state,"connect")
      if n is not None:
        state["phase"]="read"
        n=_start_async(state,"read")
        if n is not None:
          state["phase"]="done"
          state["marked"]=n==1
    while time.monotonic()<deadline:
      # Observe all completed markers before selecting either transport.
      for state in states:
        if not state["pending"] or K.WaitForSingleObject(
            state["event"],0)!=WAIT_OBJECT_0:
          continue
        n=_finish_async(state)
        if state["phase"]=="connect":
          if n<0:
            state["phase"]="done"
          else:
            state["phase"]="read"
            n=_start_async(state,"read")
            if n is not None:
              state["phase"]="done"
              state["marked"]=n==1
        elif state["phase"]=="read":
          state["phase"]="done"
          state["marked"]=n==1
      candidates=[x for x in states if x["marked"]]
      if len(candidates)>1:
        raise RuntimeError("two markers on one reply channel")
      if candidates:
        state=candidates[0]
        if state["buffer"].raw!=expected:
          raise RuntimeError("bad reply transport marker")
        selected=state
        break
      if not any(x["pending"] for x in states):
        break
      _wait_async(states,deadline)
    if selected is None:
      raise TimeoutError("reply marker timeout")
    return selected["h"]
  finally:
    for state in states:
      _close_async(state,state is not selected)


def allocation_server(owner_pid:int,pool:Pool):
  """Dynamically replenish native pipe listeners; never wait for probe EOF.

  Each Git Bash duplex open may consume an extra probe instance. A fixed
  two-instance pair can become exhausted before the actual data open, as
  demonstrated by request 1421 of the native Windows stress run. Accept
  connections independently and dispatch only those that send GET bytes.
  """
  address=r"\\.\pipe\git809-allocate-req-"+str(owner_pid)
  unlimited=255
  pending=queue.Queue()
  first=True
  backlog=min(64,max(4,2*pool.target))
  print(f"ALLOCATOR_START {address} backlog={backlog}",flush=True)

  def accept(h):
    ok=bool(K.ConnectNamedPipe(h,None))
    error=0 if ok else C.get_last_error()
    pending.put((h,ok or error in (ERROR_PIPE_CONNECTED,ERROR_NO_DATA),
                 error))

  def new_listener():
    nonlocal first
    h=K.CreateNamedPipeW(
      address,
      PIPE_ACCESS_DUPLEX|(FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0),
      PIPE_TYPE_BYTE|PIPE_WAIT,unlimited,256,256,5000,None)
    if h==INVALID:
      raise OSError(C.get_last_error(),"CreateNamedPipe(allocation)")
    first=False
    threading.Thread(target=accept,args=(h,),daemon=True).start()

  def serve_connection(h):
    try:
      # Probes carry no bytes; the actual transport must send TGET <PID>.
      frame=bytearray()
      deadline=time.monotonic()+2
      while time.monotonic()<deadline:
        available=W.DWORD()
        if not K.PeekNamedPipe(h,None,0,None,C.byref(available),None):
          error=C.get_last_error()
          if error in (109,ERROR_NO_DATA,233):
            return
          raise OSError(error,"PeekNamedPipe(request)")
        if not available.value:
          time.sleep(.001)
          continue
        buf=C.create_string_buffer(min(available.value,64))
        n=W.DWORD()
        if not K.ReadFile(h,buf,len(buf),C.byref(n),None):
          error=C.get_last_error()
          if error in (109,ERROR_NO_DATA,233):
            return
          raise OSError(error,"ReadFile(request)")
        frame.extend(buf.raw[:n.value])
        if len(frame)>64:
          raise ValueError("oversized request")
        if b"\n" in frame:
          break
      else:
        print("ALLOCATOR_IDLE_PROBE_EXPIRED",flush=True)
        return
      if len(frame)<7 or frame[0] not in b"0123456789" or not frame[1:].startswith(b"GET ") or not frame.endswith(b"\n"):
        raise ValueError("bad GET frame or missing digit marker")
      raw_id=frame[5:-1]
      parts=raw_id.split(b"-")
      if len(parts)!=2 or any(not p.isdigit() or len(p)>10 for p in parts):
        raise ValueError("invalid reply identity")
      client_pid,sequence=(int(p) for p in parts)
      if not 1<=client_pid<=0xffffffff or not 1<=sequence<=0xffffffff:
        raise ValueError("reply identity out of range")
      if frame[0]-ord("0")!=sequence%10:
        raise ValueError("GET marker does not match request sequence")
      # A unique reply pipe is required per request, not just per client.
      reply=(r"\\.\pipe\git809-allocate-reply-"+str(owner_pid)+
             "-"+str(client_pid)+"-"+str(sequence))
      response=select_transport(reply,sequence%10)
      try:
        answer=pool.alloc().encode("ascii")
        _write_reply(response,answer)
        if not K.FlushFileBuffers(response):
          error=C.get_last_error()
          if error not in (109,ERROR_NO_DATA,233):
            raise OSError(error,"FlushFileBuffers(reply)")
      finally:
        K.DisconnectNamedPipe(response)
        K.CloseHandle(response)
    except Exception as error:
      print("ALLOCATOR_CONNECTION_ERROR "+repr(error),flush=True)
    finally:
      K.DisconnectNamedPipe(h)
      K.CloseHandle(h)

  try:
    for _ in range(backlog):
      new_listener()
    while not pool.closing.is_set():
      try:
        h,ok,error=pending.get(timeout=.25)
      except queue.Empty:
        continue
      # Replenish the backlog BEFORE spending time on the accepted client.
      new_listener()
      if ok:
        threading.Thread(target=serve_connection,args=(h,),daemon=True).start()
      else:
        K.CloseHandle(h)
        print(f"ALLOCATOR_ACCEPT_ERROR {error}",flush=True)
  except Exception as error:
    with pool.lock:
      pool.errors.append("allocation listener failed: "+repr(error))
    print("ALLOCATOR_FATAL "+repr(error),flush=True)
    raise


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
    address=r"\\.\pipe\git809-allocate-req-"+str(args.owner_winpid)
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
