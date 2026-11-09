#!/usr/bin/env python3
"""Shell-owned Windows worker allocator; no Git command-data relay.

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

class OVERLAPPED(C.Structure):
  _fields_=[("Internal",C.c_size_t),("InternalHigh",C.c_size_t),
            ("Offset",W.DWORD),("OffsetHigh",W.DWORD),("hEvent",W.HANDLE)]

K.CancelIoEx.argtypes=[W.HANDLE,C.POINTER(OVERLAPPED)]
K.CancelIoEx.restype=W.BOOL

K.CreateIoCompletionPort.argtypes = [W.HANDLE, W.HANDLE, C.c_size_t, W.DWORD]
K.CreateIoCompletionPort.restype = W.HANDLE
K.GetQueuedCompletionStatus.argtypes = [
  W.HANDLE, C.POINTER(W.DWORD), C.POINTER(C.c_size_t),
  C.POINTER(C.POINTER(OVERLAPPED)), W.DWORD]
K.GetQueuedCompletionStatus.restype = W.BOOL
K.PostQueuedCompletionStatus.argtypes = [
  W.HANDLE, W.DWORD, C.c_size_t, C.POINTER(OVERLAPPED)]
K.PostQueuedCompletionStatus.restype = W.BOOL


class CompletionPort:
  """Own the allocator's completion queue and its live pipe operations."""

  def __init__(self):
    """Create a queue that can wait for any number of pipe instances."""
    self.handle = K.CreateIoCompletionPort(INVALID, None, 0, 1)
    if not self.handle:
      raise C.WinError(C.get_last_error())
    self.pipes = {}

  def wake(self):
    """Wake the allocator when a worker becomes ready or shutdown starts."""
    if not K.PostQueuedCompletionStatus(self.handle, 0, 0, None):
      raise C.WinError(C.get_last_error())

  def wait(self, milliseconds):
    """Dispatch one completion, returning False when the wait expires."""
    count = W.DWORD()
    key = C.c_size_t()
    overlap = C.POINTER(OVERLAPPED)()
    ok = K.GetQueuedCompletionStatus(
      self.handle, C.byref(count), C.byref(key), C.byref(overlap), milliseconds)
    error = 0 if ok else C.get_last_error()
    if not overlap:
      if error == WAIT_TIMEOUT:
        return False
      if error:
        raise C.WinError(error)
      return True  # Worker readiness/shutdown notification.
    pipe = self.pipes[key.value]
    assert pipe.pending and C.addressof(overlap.contents) == C.addressof(pipe.overlap)
    pipe.pending = False
    pipe.result = count.value if ok else -1
    if error not in (0, ERROR_BROKEN_PIPE, ERROR_NO_DATA, 233, ERROR_OPERATION_ABORTED):
      pipe.error = error
    return True

  def drain(self):
    """Dispatch all completions already queued, without a timer wait."""
    while self.wait(0):
      pass

  def close(self):
    """Close the queue after every pipe operation has been drained."""
    assert not self.pipes
    K.CloseHandle(self.handle)


class Pipe:
  """Own one pipe handle, OVERLAPPED, buffer, and pending operation."""

  def __init__(self, port, address, instances=255, first=False):
    """Publish an instance and associate it with the allocator queue."""
    self.port = port
    self.handle = K.CreateNamedPipeW(
      address, PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED |
      (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0),
      PIPE_TYPE_BYTE | PIPE_WAIT, instances, 256, 256, 2000, None)
    if self.handle == INVALID:
      raise C.WinError(C.get_last_error())
    self.overlap = OVERLAPPED()
    self.buffer = None
    self.pending = False
    self.result = None
    self.error = None
    self.phase = "connect"
    self.marker = None
    if not K.CreateIoCompletionPort(self.handle, port.handle, id(self), 0):
      error = C.get_last_error()
      K.CloseHandle(self.handle)
      raise C.WinError(error)
    port.pipes[id(self)] = self
    try:
      self.issue("connect")
    except BaseException:
      self.close()
      raise

  def issue(self, operation, data=None, size=1):
    """Submit one operation; retain its storage until completion is dequeued."""
    assert not self.pending
    self.overlap = OVERLAPPED()
    self.result = None
    self.error = None
    count = W.DWORD()
    if operation == "connect":
      ok = K.ConnectNamedPipe(self.handle, C.byref(self.overlap))
    elif operation == "read":
      self.buffer = C.create_string_buffer(size)
      ok = K.ReadFile(self.handle, self.buffer, size, C.byref(count),
                      C.byref(self.overlap))
    elif operation == "write":
      self.buffer = C.create_string_buffer(data)
      ok = K.WriteFile(self.handle, self.buffer, len(data), C.byref(count),
                       C.byref(self.overlap))
    else:
      raise ValueError("invalid pipe operation")
    error = 0 if ok else C.get_last_error()
    if ok or error == ERROR_IO_PENDING:
      # Even immediate success queues a completion. Consume it before reuse.
      self.pending = True
    elif operation == "connect" and error == ERROR_PIPE_CONNECTED:
      self.result = 0
    elif error in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, 233):
      self.result = -1
    else:
      raise C.WinError(error)

  def take(self):
    """Consume an operation result, raising an unexpected Win32 error."""
    if self.error is not None:
      raise C.WinError(self.error)
    result, self.result = self.result, None
    return result

  def reconnect(self):
    """Reuse a disconnected probe instance without creating a third instance."""
    assert not self.pending
    if not K.DisconnectNamedPipe(self.handle) and C.get_last_error() not in (
        ERROR_NO_DATA, 233):
      raise C.WinError(C.get_last_error())
    self.phase = "connect"
    self.issue("connect")

  def close(self):
    """Cancel and drain pending I/O before releasing its handle and storage."""
    if self.handle == INVALID:
      return
    if self.pending:
      if not K.CancelIoEx(self.handle, C.byref(self.overlap)):
        error = C.get_last_error()
        if error != ERROR_NOT_FOUND:
          raise C.WinError(error)
      # The OVERLAPPED storage must outlive cancellation completion.
      while self.pending:
        self.port.wait(0xffffffff)
    K.DisconnectNamedPipe(self.handle)
    K.CloseHandle(self.handle)
    self.handle = INVALID
    del self.port.pipes[id(self)]


class TransportPair:
  """Select one marked transport; share this selector with production tests."""

  def __init__(self, port, address, marker):
    """Publish both candidates for one private logical channel."""
    if marker not in range(10):
      raise ValueError("invalid decimal reply marker")
    self.port = port
    self.address = address
    self.expected = ord("0") + marker
    self.pipes = []
    self.selected = None
    self.deadline = time.monotonic() + 2
    try:
      for index in range(2):
        self.pipes.append(Pipe(port, address, instances=2, first=index == 0))
    except BaseException:
      self.close()
      raise

  def advance(self):
    """Inspect both marker results before selecting either candidate."""
    while True:
      for pipe in self.pipes:
        if pipe.result is None:
          continue
        count = pipe.take()
        if count < 0:
          pipe.reconnect()
        elif pipe.phase == "connect":
          pipe.phase = "marker"
          pipe.issue("read")
        elif pipe.phase == "marker":
          if count != 1:
            raise RuntimeError("invalid reply marker length")
          pipe.marker = pipe.buffer.raw[0]
          pipe.phase = "marked"
      self.port.drain()
      if not any(pipe.result is not None for pipe in self.pipes):
        break
    marked = [pipe for pipe in self.pipes if pipe.phase == "marked"]
    if len(marked) > 1:
      first, second = marked
      print(f"ALLOCATOR_DUPLICATE_MARKERS reply={self.address} "
            f"first={first.marker} (0x{first.marker:02x}) "
            f"second={second.marker} (0x{second.marker:02x}) "
            f"expected={self.expected} (0x{self.expected:02x})", flush=True)
      raise RuntimeError("two markers on one reply channel")
    if marked:
      chosen = marked[0]
      if chosen.marker != self.expected:
        raise RuntimeError("bad reply transport marker")
      for other in self.pipes:
        if other is not chosen:
          other.close()
      self.pipes = [chosen]
      self.selected = chosen
    elif time.monotonic() >= self.deadline:
      raise TimeoutError("reply marker timeout")
    return self.selected

  def close(self):
    """Release both candidates, including pending accepts and marker reads."""
    for pipe in self.pipes:
      pipe.close()
    self.pipes.clear()


class Pool:
  def __init__(self, git:Path, target:int, directory:Path):
    """Track READY workers until allocation transfers ownership to a client."""
    self.git=git
    self.directory=directory
    self.target=target
    self.ready=queue.Queue()
    self.children=[]
    self.lock=threading.Lock()
    self.closing=threading.Event()
    self.errors=[]
    self.allocations=0
    self.owner_pid=None
    self.notify=None

  def spawn(self):
    """Prepare one replacement and notify the allocator when it is READY."""
    if self.closing.is_set():
      return
    try:
      worker_env=os.environ.copy()
      if self.owner_pid is None:
        raise RuntimeError("owner_pid not initialized")
      worker_env["GIT_PRELOAD_OWNER_PID"]=str(self.owner_pid)
      p=subprocess.Popen([str(self.git),"--preload-worker",str(self.directory)],
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
      # READY is the only startup-pipe message. The five native named pipes
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
    with self.lock:
      if self.notify:
        self.notify()
    print("WORKER_READY "+str(p.pid),flush=True)

  def alloc(self):
    """Transfer a live worker immediately, or return None without waiting."""
    while True:
      try:
        p=self.ready.get_nowait()
      except queue.Empty:
        return None
      # The client owns this worker after allocation. Retain only workers the
      # manager is still offering; the native worker watches the shell itself.
      with self.lock:
        if p not in self.children:
          raise RuntimeError("allocated worker missing from ready tracking")
        self.children.remove(p)
      if p.poll() is None:
        break
      self.replenish()
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

  def replenish(self):
    """Start one replacement without waiting for its READY message."""
    threading.Thread(target=self.spawn,daemon=True).start()

  def close(self):
    """Wake the allocator and terminate workers still owned by the manager."""
    self.closing.set()
    with self.lock:
      if self.notify:
        self.notify()
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
      try:
        (self.directory / ("worker-" + str(p.pid))).rmdir()
      except FileNotFoundError:
        pass

def parse_get_request(frame):
  """Validate GET and return client PID, sequence and actual marker."""
  if (len(frame)<7 or frame[0] not in b"0123456789" or
      not frame[1:].startswith(b"GET ") or not frame.endswith(b"\n")):
    raise ValueError("bad GET frame or missing digit marker")
  parts=frame[5:-1].split(b"-")
  if len(parts)!=2 or any(not p.isdigit() or len(p)>10 for p in parts):
    raise ValueError("invalid reply identity")
  client_pid,sequence=(int(p) for p in parts)
  if not 1<=client_pid<=0xffffffff or not 1<=sequence<=0xffffffff:
    raise ValueError("reply identity out of range")
  marker=sequence%10
  if frame[0]-ord("0")!=marker:
    raise ValueError("GET marker does not match request sequence")
  return client_pid,sequence,marker


def allocation_server(owner_pid:int,pool:Pool):
  """Serve serial GET lines and PID lines over two permanent named pipes."""
  port=CompletionPort()
  pool.notify=port.wake
  request=None
  reply=None
  try:
    # Both pairs exist before the shell opens either endpoint.
    request=TransportPair(port,r"\\.\pipe\git-preload-allocate-req-"+str(owner_pid),0)
    reply=TransportPair(port,r"\\.\pipe\git-preload-allocate-reply-"+str(owner_pid),0)
    print(f"ALLOCATOR_START persistent owner={owner_pid}",flush=True)

    while not pool.closing.is_set() and (
        request.selected is None or reply.selected is None):
      port.drain()
      request.advance()
      reply.advance()
      if request.selected is None or reply.selected is None:
        deadline=min(pair.deadline for pair in (request,reply)
                     if pair.selected is None)
        port.wait(max(1,int((deadline-time.monotonic())*1000)))

    if pool.closing.is_set():
      return
    inbound=request.selected
    outbound=reply.selected
    frame=bytearray()
    state="get"
    deadline=None
    answer=None
    inbound.issue("read",size=4)

    while not pool.closing.is_set():
      port.drain()

      if state=="get" and inbound.result is not None:
        count=inbound.take()
        if count<=0:
          raise ConnectionError("persistent GET channel closed")
        frame.extend(inbound.buffer.raw[:count])
        if not b"GET\n".startswith(frame):
          raise ValueError("invalid persistent GET line")
        if frame==b"GET\n":
          frame.clear()
          deadline=time.monotonic()+5
          state="allocate"
        else:
          inbound.issue("read",size=4-len(frame))

      if state=="allocate":
        answer=pool.alloc()
        if answer is not None or time.monotonic()>=deadline:
          allocated=answer is not None
          answer=(answer or "WAIT\n").encode("ascii")
          assert answer.endswith(b"\n")
          outbound.issue("write",data=answer)
          # Start replacement after submitting the reply. Never join this thread.
          if allocated:
            pool.replenish()
          state="reply"

      if state=="reply" and outbound.result is not None:
        if outbound.take()!=len(answer):
          raise ConnectionError("short persistent PID reply")
        inbound.issue("read",size=4)
        state="get"

      if state=="allocate":
        # A READY notification wakes this wait before the five-second limit.
        port.wait(max(1,int((deadline-time.monotonic())*1000)))
      elif (inbound.result is None and outbound.result is None and
            not pool.closing.is_set()):
        port.wait(0xffffffff)
  except Exception as error:
    if not pool.closing.is_set():
      with pool.lock:
        pool.errors.append("persistent allocator failed: "+repr(error))
      print("ALLOCATOR_FATAL "+repr(error),flush=True)
    raise
  finally:
    with pool.lock:
      pool.notify=None
    if reply is not None:
      reply.close()
    if request is not None:
      request.close()
    port.close()


def main():
  """Parse command-line options and run the requested validation or service."""
  parser=argparse.ArgumentParser()
  parser.add_argument("--git",required=True,type=Path)
  parser.add_argument("--workers",required=True,type=int)
  parser.add_argument("--owner-pid",required=True,type=int)
  parser.add_argument("--directory",required=True,type=Path)
  args=parser.parse_args()
  if not 1<=args.workers<=32:parser.error("workers must be 1..32")
  if not 1<=args.owner_pid<=0xffffffff:parser.error("owner PID invalid")
  git=args.git.resolve(strict=True)
  directory=args.directory.resolve(strict=True)
  ready_file=directory / "ready.json"
  owner=K.OpenProcess(SYNCHRONIZE,False,args.owner_pid)
  if not owner:
    raise OSError(C.get_last_error(),"OpenProcess(owner)")
  if K.WaitForSingleObject(owner,0)!=WAIT_TIMEOUT:
    raise RuntimeError("owner already terminated")
  pool=Pool(git,args.workers,directory)
  pool.owner_pid=args.owner_pid
  allocator=None
  try:
    for _ in range(args.workers):
      pool.spawn()
    if pool.ready.qsize()!=args.workers:
      raise RuntimeError("not all initial workers are ready: "+repr(pool.errors))
    # Ensure allocator exists before publishing READY. The allocator's
    # initialization is synchronized by an explicit event instead of sleeps.
    allocator=threading.Thread(target=allocation_server,
      args=(args.owner_pid,pool),daemon=True)
    allocator.start()
    # CreateNamedPipe happens immediately, but verify endpoint existence via
    # WaitNamedPipeW rather than assuming a started thread is ready.
    K.WaitNamedPipeW.argtypes=[W.LPCWSTR,W.DWORD]
    K.WaitNamedPipeW.restype=W.BOOL
    address=r"\\.\pipe\git-preload-allocate-req-"+str(args.owner_pid)
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
      if K.WaitNamedPipeW(address,100):
        break
      if not allocator.is_alive():
        raise RuntimeError("allocation server failed")
      time.sleep(.01)
    else:
      raise RuntimeError("allocation server startup timeout")
    record={"manager_pid":os.getpid(),"owner_pid":args.owner_pid,
      "workers":args.workers,"worker_pids":[x.pid for x in list(pool.ready.queue)],
      "poll_seconds":POLL_SECONDS,"state":"READY"}
    tmp=ready_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(record)+"\n",encoding="utf-8")
    tmp.replace(ready_file)
    print("READY "+json.dumps(record),flush=True)
    while K.WaitForSingleObject(owner,POLL_SECONDS*1000)==WAIT_TIMEOUT:
      if not allocator.is_alive():
        raise RuntimeError("allocation server unexpectedly stopped")
      if pool.errors:
        raise RuntimeError("worker initialization failed: "+repr(pool.errors))
    print("OWNER_EXIT "+str(args.owner_pid),flush=True)
  finally:
    ready_file.unlink(missing_ok=True)
    pool.close()
    if allocator is not None:
      allocator.join()
    K.CloseHandle(owner)

if __name__=="__main__":
  sys.exit(main())
