#!/usr/bin/env python3
"""RWF 809 Windows-only pair selection regression checks."""
import ctypes as C
from ctypes import wintypes as W
import importlib.util
import os
from pathlib import Path
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


def run_case(name,marker_index=None,marker=b"1",drop_probe=False):
  address=r"\\.\pipe\git809-select-test-"+uuid.uuid4().hex
  result=[]
  def server():
    try:
      selected=manager.select_transport(address,1)
      result.append(("selected",selected))
    except Exception as error:
      result.append(("error",error))
  t=threading.Thread(target=server,daemon=True)
  t.start()
  clients=[]
  try:
    clients.append(connect(address))
    clients.append(connect(address))
    if drop_probe:
      assert marker_index in (0,1)
      probe=1-marker_index
      K.CloseHandle(clients[probe])
      clients[probe]=None
    if marker_index is not None:
      write(clients[marker_index],marker)
    t.join(timeout=5)
    assert not t.is_alive(),(name,"server stuck")
    assert len(result)==1,(name,result)
    if marker_index is not None and marker==b"1":
      assert result[0][0]=="selected",(name,result)
      # Selection consumes only the digit, preserving application data.
      write(clients[marker_index],b"Z")
      byte=C.create_string_buffer(1)
      count=W.DWORD()
      assert K.ReadFile(result[0][1],byte,1,C.byref(count),None)
      assert count.value==1 and byte.raw==b"Z",(name,byte.raw)
      K.DisconnectNamedPipe(result[0][1])
      K.CloseHandle(result[0][1])
    else:
      assert result[0][0]=="error",(name,result)
      if marker_index is None:
        assert isinstance(result[0][1],TimeoutError),(name,result)
      else:
        assert isinstance(result[0][1],RuntimeError),(name,result)
    print("GIT809_SELECT_TRANSPORT_PASS",name,flush=True)
  finally:
    for h in clients:
      if h is not None:
        K.CloseHandle(h)


def test_duplicate_markers():
  """Both markers exist before the selector is allowed to read either."""
  address=r"\\.\pipe\git809-select-test-"+uuid.uuid4().hex
  original_start=manager._start_async
  original_create=K.CreateNamedPipeW
  instances_ready=threading.Event()
  instances_created=0
  go=threading.Event()
  clients=[]
  result=[]

  def tracked_create(*args):
    nonlocal instances_created
    handle=original_create(*args)
    if handle!=manager.INVALID:
      instances_created+=1
      if instances_created==2:
        instances_ready.set()
    return handle

  def held_marker_read(state,operation,data=None,size=1):
    if operation=="read" and not go.wait(5):
      raise TimeoutError("test marker-read gate")
    return original_start(state,operation,data,size)

  def server():
    try:
      result.append(("selected",manager.select_transport(address,1)))
    except Exception as error:
      result.append(("error",error))

  manager._start_async=held_marker_read
  K.CreateNamedPipeW=tracked_create
  try:
    t=threading.Thread(target=server,daemon=True)
    t.start()
    assert instances_ready.wait(5),"server did not create both pipe instances"
    clients.append(connect(address))
    clients.append(connect(address))
    write(clients[0],b"1")
    write(clients[1],b"1")
    go.set()
    t.join(timeout=5)
    assert not t.is_alive(),"duplicate marker server stuck"
    assert len(result)==1 and result[0][0]=="error",result
    assert isinstance(result[0][1],RuntimeError),result
    assert "two markers" in str(result[0][1]),result
    print("GIT809_SELECT_TRANSPORT_PASS duplicate-markers",flush=True)
  finally:
    go.set()
    manager._start_async=original_start
    K.CreateNamedPipeW=original_create
    for h in clients:
      K.CloseHandle(h)


run_case("first-instance-marker",0)
run_case("second-instance-marker-probe-held",1)
run_case("disconnected-probe-other-instance-selected",1,drop_probe=True)
run_case("invalid-marker",1,b"X")
run_case("missing-marker-timeout")
test_duplicate_markers()
