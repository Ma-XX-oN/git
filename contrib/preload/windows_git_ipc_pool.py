#!/usr/bin/env python3
"""Shell-owned Windows worker allocator; no Git command-data relay.

Two permanent allocation channels carry serial GET lines and worker PID
replies. A marker selects the application connection on each duplex pipe.
Replacement startup follows reply publication and proceeds independently of
command execution, pipe consumption and worker termination.
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
POLL_SECONDS=0.25
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
    """Dispatch one completion or expire the supplied wait.

    Parameters
    ----------
    milliseconds : int
        Wait duration in milliseconds; zero polls and 0xffffffff waits indefinitely.

    Returns
    -------
    bool
        True for a dispatched completion or wakeup, False for timeout.
    """
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
    """Publish an instance and associate it with the allocator queue.

    Parameters
    ----------
    port : CompletionPort
        Borrowed live queue; must outlive this pipe and cancellation completion.
    address : str
        Full native named-pipe address to create.
    instances : int
        Maximum server instances permitted for the name, from 1 through 255.
    first : bool
        Require exclusive first ownership of this name when True.
    """
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
    """Submit one operation and retain its storage through completion dequeue.

    Parameters
    ----------
    operation : {'connect', 'read', 'write'}
        Operation to start; the preceding operation must have completed.
    data : bytes, optional
        Write payload, copied into pipe-owned storage before submission.
    size : int
        Read buffer capacity in bytes; used only for a read operation.
    """
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
    """Consume an operation result, raising an unexpected Win32 error.

    Returns
    -------
    int or None
        Completed byte count, -1 for disconnection, or None before completion.
        The stored result is cleared; read bytes remain in the owned buffer.
    """
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
    """Publish both candidates for one private logical channel.

    Parameters
    ----------
    port : CompletionPort
        Borrowed live queue, retained until both candidate pipes are released.
    address : str
        Full native address shared by the two candidate instances.
    marker : int
        Expected decimal digit from zero through nine; consumed before application bytes.
    """
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
    """Inspect both marker results before selecting either candidate.

    Returns
    -------
    Pipe or None
        Borrowed selected pipe, or None while identification is pending.
        This pair keeps ownership until close; duplicate or invalid markers raise.
    """
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
    """Track READY workers until allocation transfers ownership to a client.

    Parameters
    ----------
    git : Path
        Native worker executable; retained for replacement startup.
    target : int
        Requested pool size, from one through 32.
    directory : Path
        Existing private session directory used by every worker.
    """
    self.git=git
    self.directory=directory
    self.target=target
    self.ready=queue.Queue()
    self.children=[]
    self.pending=None
    self.lock=threading.Lock()
    self.spawn_done=threading.Condition(self.lock)
    self.spawning=0
    self.closing=threading.Event()
    self.errors=[]
    self.allocations=0
    self.owner_pid=None
    self.notify=None

  def spawn(self):
    """Prepare one replacement and notify the allocator when it is READY."""
    with self.spawn_done:
      if self.closing.is_set():
        return
      self.spawning+=1
    try:
      self._spawn_worker()
    finally:
      with self.spawn_done:
        self.spawning-=1
        self.spawn_done.notify_all()

  def _spawn_worker(self):
    """Register each created child, including one started across shutdown."""
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
      stopping=self.closing.is_set()
    if stopping:
      p.kill()
      p.wait(timeout=3)
      p.stdout.close()
      return
    try:
      line=p.stdout.readline()
    finally:
      # READY is the only startup-pipe message. The five native named pipes
      # carry the command; retaining each startup pipe leaks a manager handle.
      p.stdout.close()
    if self.closing.is_set():
      if p.poll() is None:
        p.kill()
      return
    if line != ("READY "+str(p.pid)+"\n").encode("ascii"):
      with self.lock:
        self.errors.append("worker readiness failed: pid="+str(p.pid)+" response="+repr(line))
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
    """Reserve a live worker without waiting.

    Returns
    -------
    str or None
        Worker PID line, or None for shortage. The pool retains ownership until
        commit_allocation() follows full reply publication.
    """
    while True:
      try:
        p=self.ready.get_nowait()
      except queue.Empty:
        return None
      with self.lock:
        if p not in self.children:
          raise RuntimeError("allocated worker missing from ready tracking")
      if p.poll() is None:
        with self.lock:
          assert self.pending is None,"allocation calls must be serial"
          self.pending=p
        break
      with self.lock:
        self.children.remove(p)
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

  def commit_allocation(self):
    """Relinquish the reserved worker after its full PID reply is published."""
    with self.lock:
      assert self.pending is not None,"no allocation reply to commit"
      self.children.remove(self.pending)
      self.pending=None

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
    with self.spawn_done:
      self.spawn_done.wait_for(lambda:self.spawning==0)
      children=list(self.children)
    for p in children:
      try:
        p.wait(timeout=3)
      except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=3)
    for p in children:
      if p.stdout:
        p.stdout.close()
      try:
        (self.directory / ("worker-" + str(p.pid))).rmdir()
      except FileNotFoundError:
        pass

def connect_allocation_pairs(port, request, reply, closing):
  """Identify both persistent application connections.

  Parameters
  ----------
  port : CompletionPort
      Borrowed event queue for both pairs.
  request, reply : TransportPair
      Borrowed pairs, already created before either connection is accepted.
  closing : threading.Event
      Manager shutdown flag.

  Returns
  -------
  bool
      True once both application handles are selected, False on shutdown.
  """
  while not closing.is_set() and (
      request.selected is None or reply.selected is None):
    port.drain()
    request.advance()
    reply.advance()
    if request.selected is None or reply.selected is None:
      deadline=min(pair.deadline for pair in (request,reply)
                   if pair.selected is None)
      port.wait(max(1,int((deadline-time.monotonic())*1000)))
  return not closing.is_set()


def wait_operation(port, pipe, closing):
  """Wait for an operation while dispatching readiness and shutdown notifications.

  Parameters
  ----------
  port : CompletionPort
      Borrowed live event queue.
  pipe : Pipe
      Borrowed pipe with a submitted operation.
  closing : threading.Event
      Manager shutdown flag; pending operations remain owned for cleanup.

  Returns
  -------
  int or None
      Consumed byte count, -1 for disconnection, or None on shutdown.
  """
  while pipe.result is None and not closing.is_set():
    port.wait(0xffffffff)
  return None if closing.is_set() else pipe.take()


def read_allocation_request(port, inbound, closing):
  """Read one bounded serial GET or STOP line without changing GET's fast path.

  Parameters
  ----------
  port : CompletionPort
      Borrowed event queue.
  inbound : Pipe
      Borrowed selected request channel; no prior read may remain pending.
  closing : threading.Event
      Manager shutdown flag, set after a complete STOP request.

  Returns
  -------
  bool
      True for a complete GET line, False for STOP or shutdown; malformed input raises.
  """
  frame=bytearray()
  commands=(b"GET\n",b"STOP\n")
  while len(frame)<5:
    inbound.issue("read",size=4-len(frame) if len(frame)<4 else 1)
    count=wait_operation(port,inbound,closing)
    if count is None:
      return False
    if count<=0:
      raise ConnectionError("persistent GET channel closed")
    frame.extend(inbound.buffer.raw[:count])
    if not any(command.startswith(frame) for command in commands):
      raise ValueError("invalid persistent allocation line")
    if frame==b"GET\n":
      return True
    if frame==b"STOP\n":
      print("MANAGER_STOP_REQUEST",flush=True)
      closing.set()
      return False
  raise ValueError("invalid persistent allocation line")


def wait_ready_worker(port, pool):
  """Allocate immediately or await a READY notification within five seconds.

  Parameters
  ----------
  port : CompletionPort
      Borrowed queue receiving the pool's readiness and shutdown notifications.
  pool : Pool
      Borrowed live worker pool.

  Returns
  -------
  str or None
      Owned-by-client worker PID line, or None for shortage/shutdown.
      The caller checks the closing flag before publishing a shortage reply.
  """
  deadline=time.monotonic()+5
  while not pool.closing.is_set():
    answer=pool.alloc()
    if answer is not None or time.monotonic()>=deadline:
      return answer
    port.wait(max(1,int((deadline-time.monotonic())*1000)))
  return None


def send_allocation_reply(port, outbound, pool, answer):
  """Publish one PID or shortage reply before initiating replacement startup.

  Parameters
  ----------
  port : CompletionPort
      Borrowed event queue.
  outbound : Pipe
      Borrowed selected reply channel with no pending write.
  pool : Pool
      Borrowed live pool; replacement follows successful reply submission.
  answer : str or None
      Allocated PID plus newline, or None to send WAIT.

  Returns
  -------
  bool
      True after full write completion, False on shutdown; short writes raise.
  """
  payload=(answer or "WAIT\n").encode("ascii")
  assert payload.endswith(b"\n")
  outbound.issue("write",data=payload)
  if answer is not None:
    pool.replenish()
  count=wait_operation(port,outbound,pool.closing)
  if count is None:
    return False
  if count!=len(payload):
    raise ConnectionError("short persistent PID reply")
  if answer is not None:
    pool.commit_allocation()
  return True


def allocation_server(owner_pid:int,pool:Pool):
  """Serve serial GET and PID lines over two permanent named pipes.

  Parameters
  ----------
  owner_pid : int
      Native owner PID identifying the allocation pipe names.
  pool : Pool
      Borrowed live pool; its closing event ends this server. Readiness
      notifications use this server's queue until final cleanup.
  """
  port=CompletionPort()
  pool.notify=port.wake
  request=None
  reply=None
  try:
    # Both pairs exist before the shell opens either endpoint.
    request=TransportPair(port,r"\\.\pipe\git-preload-allocate-req-"+str(owner_pid),0)
    reply=TransportPair(port,r"\\.\pipe\git-preload-allocate-reply-"+str(owner_pid),0)
    print(f"ALLOCATOR_START persistent owner={owner_pid}",flush=True)
    if not connect_allocation_pairs(port,request,reply,pool.closing):
      return
    while read_allocation_request(port,request.selected,pool.closing):
      answer=wait_ready_worker(port,pool)
      if pool.closing.is_set():
        break
      if not send_allocation_reply(port,reply.selected,pool,answer):
        break
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
    while not pool.closing.is_set() and K.WaitForSingleObject(owner,int(POLL_SECONDS*1000))==WAIT_TIMEOUT:
      if not allocator.is_alive() and not pool.closing.is_set():
        raise RuntimeError("allocation server unexpectedly stopped")
      if pool.errors:
        raise RuntimeError("worker initialization failed: "+repr(pool.errors))
    print("MANAGER_STOP" if pool.closing.is_set() else "OWNER_EXIT "+str(args.owner_pid),flush=True)
  finally:
    ready_file.unlink(missing_ok=True)
    pool.close()
    if allocator is not None:
      allocator.join()
    K.CloseHandle(owner)

if __name__=="__main__":
  sys.exit(main())
