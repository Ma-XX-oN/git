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


def run_case(name,marker_index=None,marker=b"T",drop_probe=False,request_id=None):
  address=r"\\.\pipe\git809-select-test-"+uuid.uuid4().hex
  result=[]
  def server():
    try:
      selected=manager.select_transport(address,request_id)
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
    expected=(str(int(request_id.rsplit("-",1)[1])%10).encode("ascii")
              if request_id else b"T")
    if marker_index is not None and marker==expected:
      assert result[0][0]=="selected",(name,result)
      # Selection consumes only T, preserving the following application byte.
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


def test_duplicate_markers(name="duplicate-markers", first=b"T", second=b"T", request_id=None):
  """Force both markers to be pending before selection examines either."""
  address=r"\\.\pipe\git809-select-test-"+uuid.uuid4().hex
  peek=K.PeekNamedPipe
  go=threading.Event()
  clients=[]
  result=[]
  def held_peek(*args):
    if not go.wait(5):
      raise TimeoutError("test marker gate")
    return peek(*args)
  def server():
    try:
      result.append(("selected",manager.select_transport(address,request_id)))
    except Exception as error:
      result.append(("error",error))
  K.PeekNamedPipe=held_peek
  try:
    t=threading.Thread(target=server,daemon=True)
    t.start()
    clients.append(connect(address))
    clients.append(connect(address))
    write(clients[0],first)
    write(clients[1],second)
    go.set()
    t.join(timeout=5)
    assert not t.is_alive(),"duplicate marker server stuck"
    assert len(result)==1 and result[0][0]=="error",result
    assert isinstance(result[0][1],RuntimeError),result
    assert "two markers" in str(result[0][1]),result
    print("GIT809_SELECT_TRANSPORT_PASS",name,flush=True)
  finally:
    print("GIT809_DUPLICATE_CLEANUP_BEGIN",name,flush=True)
    go.set()
    K.PeekNamedPipe=peek
    for h in clients:
      print("GIT809_DUPLICATE_CLOSE_HANDLE",name,h,flush=True)
      K.CloseHandle(h)
    print("GIT809_DUPLICATE_CLEANUP_DONE",name,flush=True)


run_case("first-instance-marker",0)
run_case("second-instance-marker-probe-held",1)
run_case("disconnected-probe-other-instance-selected",1,drop_probe=True)
run_case("invalid-marker",1,b"X")
run_case("missing-marker-timeout")
run_case("rotating-marker-zero",0,b"0",request_id="123-10")
run_case("rotating-marker-nine",1,b"9",request_id="123-19")
run_case("rotating-marker-mismatch",1,b"8",request_id="123-19")
test_duplicate_markers()
test_duplicate_markers("matching-digit-double-marker",b"7",b"7","123-7")
test_duplicate_markers("mixed-digit-double-marker",b"7",b"8","123-7")

print("GIT809_SELECTOR_SCRIPT_END",flush=True)
