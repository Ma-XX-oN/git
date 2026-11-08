#!/usr/bin/env python3
"""RWF 809 Windows-only pair selection regression checks."""
import ctypes as C
from ctypes import wintypes as W
import importlib.util
import os
from pathlib import Path
import queue
import threading
import time
import uuid

if os.name != "nt":
  raise RuntimeError("Windows native named-pipe test only")

spec=importlib.util.spec_from_file_location(
  "git809_manager",Path(__file__).with_name("windows_git_ipc_pool.py"))
manager=importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)
K=manager.K
K.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.LPVOID,W.DWORD,W.DWORD,W.HANDLE]
K.CreateFileW.restype=W.HANDLE
K.WaitNamedPipeW.argtypes=[W.LPCWSTR,W.DWORD]
K.WaitNamedPipeW.restype=W.BOOL
K.PeekNamedPipe.argtypes=[W.HANDLE,W.LPVOID,W.DWORD,C.POINTER(W.DWORD),
                         C.POINTER(W.DWORD),C.POINTER(W.DWORD)]
K.PeekNamedPipe.restype=W.BOOL


def connect(address):
  deadline=time.monotonic()+5
  while time.monotonic()<deadline:
    if K.WaitNamedPipeW(address,100):
      h=K.CreateFileW(address,0xc0000000,0,None,3,0,None)
      if h!=manager.INVALID:
        return h
    time.sleep(.001)
  raise TimeoutError("no available test pipe instance")


def write(h,data):
  n=W.DWORD()
  if not K.WriteFile(h,data,len(data),C.byref(n),None):
    raise OSError(C.get_last_error(),"WriteFile(test)")
  assert n.value==len(data)


def select_for_test(address, marker, result, release):
  """Exercise the production pair owner, then read one application byte."""
  port = manager.CompletionPort()
  pair = manager.TransportPair(port, address, marker)
  try:
    while pair.advance() is None:
      port.wait(max(0, int((pair.deadline - time.monotonic()) * 1000) + 1))
    result.append(("selected", pair.selected))
    assert release.wait(5), "test client did not release transport"
    pair.selected.issue("read")
    while pair.selected.pending:
      port.wait(5000)
    assert pair.selected.take() == 1
    assert pair.selected.buffer.raw == b"Z"
  except Exception as error:
    result.append(("error", error))
  finally:
    pair.close()
    port.close()


def run_case(name, marker_index=None, marker=b"1", drop_probe=False):
  """Verify either candidate, abandoned probes, invalid bytes, and timeout."""
  address = r"\\.\pipe\git809-select-test-" + uuid.uuid4().hex
  result = []
  release = threading.Event()
  thread = threading.Thread(target=select_for_test,
    args=(address, 1, result, release), daemon=True)
  thread.start()
  clients = []
  try:
    clients.append(connect(address))
    clients.append(connect(address))
    if drop_probe:
      probe = 1 - marker_index
      K.CloseHandle(clients[probe])
      clients[probe] = None
    if marker_index is not None:
      write(clients[marker_index], marker)
    if marker_index is not None and marker == b"1":
      # Selection consumes only the digit, preserving application data.
      write(clients[marker_index], b"Z")
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), (name, "server stuck")
    if marker_index is not None and marker == b"1":
      assert len(result) == 1 and result[0][0] == "selected", (name, result)
    else:
      assert len(result) == 1 and result[0][0] == "error", (name, result)
      expected = TimeoutError if marker_index is None else RuntimeError
      assert isinstance(result[0][1], expected), (name, result)
    print("GIT809_SELECT_TRANSPORT_PASS", name, flush=True)
  finally:
    release.set()
    for handle in clients:
      if handle is not None:
        K.CloseHandle(handle)


def test_duplicate_markers():
  """Both markers exist before the production selector reads either."""
  address = r"\\.\pipe\git809-select-test-" + uuid.uuid4().hex
  original_issue = manager.Pipe.issue
  original_create = K.CreateNamedPipeW
  instances_ready = threading.Event()
  instances_created = 0
  go = threading.Event()
  clients = []
  result = []

  def tracked_create(*args):
    """Publish the fixture gate after both real pipe instances exist."""
    nonlocal instances_created
    handle = original_create(*args)
    if handle != manager.INVALID:
      instances_created += 1
      if instances_created == 2:
        instances_ready.set()
    return handle

  def held_marker_read(pipe, operation, data=None, size=1):
    """Make both marker writes precede the selector's first read."""
    if operation == "read" and not go.wait(5):
      raise TimeoutError("test marker-read gate")
    return original_issue(pipe, operation, data, size)

  manager.Pipe.issue = held_marker_read
  K.CreateNamedPipeW = tracked_create
  try:
    thread = threading.Thread(target=select_for_test,
      args=(address, 1, result, go), daemon=True)
    thread.start()
    assert instances_ready.wait(5), "server did not create both instances"
    clients.append(connect(address))
    clients.append(connect(address))
    write(clients[0], b"1")
    write(clients[1], b"1")
    go.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "duplicate marker server stuck"
    assert len(result) == 1 and result[0][0] == "error", result
    assert isinstance(result[0][1], RuntimeError), result
    assert "two markers" in str(result[0][1]), result
    print("GIT809_SELECT_TRANSPORT_PASS duplicate-markers", flush=True)
  finally:
    go.set()
    manager.Pipe.issue = original_issue
    K.CreateNamedPipeW = original_create
    for handle in clients:
      K.CloseHandle(handle)


def test_allocator_isolation():
  """Exercise production replies with held probes, slow readers, and shortages."""
  class ReadyPool:
    """Supply deterministic worker IDs without starting Git subprocesses."""

    def __init__(self):
      """Initialize the same allocator-facing interface as the real pool."""
      self.target = 1
      self.ready = queue.Queue()
      self.closing = threading.Event()
      self.lock = threading.Lock()
      self.errors = []
      self.notify = None

    def alloc(self):
      """Return an ID immediately, or None when no worker is ready."""
      try:
        return self.ready.get_nowait()
      except queue.Empty:
        return None

  pool = ReadyPool()
  owner = 90000000 + int(uuid.uuid4().hex[:6], 16)
  address = rf"\\.\pipe\git809-allocate-req-{owner}"
  thread = threading.Thread(target=manager.allocation_server,
                            args=(owner, pool), daemon=True)
  clients = []

  def request(sequence):
    """Open real GET/reply channels, retaining GET through reply consumption."""
    get = connect(address)
    clients.append(get)
    write(get, f"{sequence % 10}GET 123-{sequence}\n".encode())
    reply = connect(rf"\\.\pipe\git809-allocate-reply-{owner}-123-{sequence}")
    clients.append(reply)
    write(reply, str(sequence % 10).encode())
    return reply

  def read_reply(handle):
    """Read a reply with a fixture deadline instead of blocking the test."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
      available = W.DWORD()
      assert K.PeekNamedPipe(handle, None, 0, None, C.byref(available), None)
      if available.value:
        buffer = C.create_string_buffer(available.value)
        count = W.DWORD()
        assert K.ReadFile(handle, buffer, len(buffer), C.byref(count), None)
        return buffer.raw[:count.value]
      time.sleep(.001)
    raise TimeoutError("unrelated reply was stalled")

  thread.start()
  try:
    # More pending connections than WaitForMultipleObjects can accept.
    for _ in range(80):
      clients.append(connect(address))
    pool.ready.put("1001\n")
    slow = request(1)
    pool.ready.put("1002\n")
    fast = request(2)
    assert read_reply(fast) == b"1002\n"
    assert read_reply(slow) == b"1001\n"
    # Empty-pool waits must still allow another client's reply to connect.
    first = request(3)
    second = request(4)
    pool.ready.put("1003\n")
    pool.ready.put("1004\n")
    with pool.lock:
      pool.notify()
    assert {read_reply(first), read_reply(second)} == {b"1003\n", b"1004\n"}
    assert not pool.errors, pool.errors
    print("GIT809_ALLOCATOR_PASS slow-reader-shortage-80-probes", flush=True)
  finally:
    for handle in clients:
      K.CloseHandle(handle)
    pool.closing.set()
    with pool.lock:
      if pool.notify:
        pool.notify()
    thread.join(timeout=5)
    assert not thread.is_alive(), "allocator cleanup stuck"
    assert not pool.errors, pool.errors


run_case("first-instance-marker", 0)
run_case("second-instance-marker-probe-held", 1)
run_case("disconnected-probe-other-instance-selected", 1, drop_probe=True)
run_case("invalid-marker", 1, b"X")
run_case("nul-marker-is-invalid", 1, b"\0")
run_case("missing-marker-timeout")
test_duplicate_markers()
test_allocator_isolation()
