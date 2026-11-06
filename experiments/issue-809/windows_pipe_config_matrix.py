#!/usr/bin/env python3
"""#809 native Windows controlled pipe-configuration compatibility test.

Reproduce Python relay and C worker CreateNamedPipe calls without compiling Git.
Each case receives a fresh pipe name, fresh instances and an independent
Git Bash open attempt. No manager, Git command or performance benchmark runs.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import os
import shutil
import subprocess
import sys
import uuid

if os.name!="nt":
  raise RuntimeError("Windows native Python required")
K=C.WinDLL("kernel32",use_last_error=True)
K.CreateNamedPipeW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.LPVOID]
K.CreateNamedPipeW.restype=W.HANDLE
K.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.LPVOID,W.DWORD,W.DWORD,W.HANDLE]
K.CreateFileW.restype=W.HANDLE
K.CloseHandle.argtypes=[W.HANDLE]
K.CloseHandle.restype=W.BOOL
INVALID=C.c_void_p(-1).value
PIPE_ACCESS_OUTBOUND=2
GENERIC_READ=0x80000000
OPEN_EXISTING=3

def run_case(bash,actual:int,limit:int,buffers:int):
  assert 1<=actual<=limit<=255
  name=r"\\.\pipe\git809-config-"+uuid.uuid4().hex
  handles=[]
  try:
    for _ in range(actual):
      h=K.CreateNamedPipeW(name,PIPE_ACCESS_OUTBOUND,0,limit,
        buffers,buffers,5000,None)
      if h==INVALID:
        raise OSError(C.get_last_error(),"CreateNamedPipeW")
      handles.append(h)
    # Two separate identical servers. Native probe consumes a connection;
    # Bash uses another fresh server with exactly same configuration.
    native=K.CreateFileW(name,GENERIC_READ,0,None,OPEN_EXISTING,0,None)
    if native==INVALID:
      raise OSError(C.get_last_error(),"native CreateFileW")
    K.CloseHandle(native)
    # First instance is now occupied; never let that affect Bash's trial.
    for h in handles:
      K.CloseHandle(h)
    handles=[]
    name=r"\\.\pipe\git809-config-"+uuid.uuid4().hex
    for _ in range(actual):
      h=K.CreateNamedPipeW(name,PIPE_ACCESS_OUTBOUND,0,limit,
        buffers,buffers,5000,None)
      if h==INVALID:
        raise OSError(C.get_last_error(),"CreateNamedPipeW for Bash")
      handles.append(h)
    script='exec 4<"$1" || exit 61; printf "BASH_OPEN_PASS\\n"; exec 4<&-'
    p=subprocess.run([bash,"--noprofile","--norc","-c",script,
      "pipeprobe",name],capture_output=True,timeout=8,check=False)
    record={"actual_instances":actual,"max_instances":limit,
      "buffers":buffers,"native_open":"PASS","bash_rc":p.returncode,
      "bash_stdout":p.stdout.decode("utf-8","replace"),
      "bash_stderr":p.stderr.decode("utf-8","replace")}
    print("PIPE_CASE "+repr(record),flush=True)
    return p.returncode==0 and b"BASH_OPEN_PASS" in p.stdout
  finally:
    for h in handles:
      K.CloseHandle(h)

def main():
  bash=shutil.which("bash")
  if not bash:raise RuntimeError("Git Bash missing")
  modes=[
    (1,1,65536),   # C worker exactly
    (1,255,65536), # only C maximum changed
    (2,255,65536), # two real waiting instances
    (8,255,65536), # eight real instances with C buffers
    (1,255,4096),  # Python buffers but one instance
    (8,255,4096),  # Python relay exact instance/buffer configuration
  ]
  results=[run_case(bash,*mode) for mode in modes]
  print("PIPE_MATRIX "+repr(list(zip(modes,results))),flush=True)
  if not results[-1]:
    raise AssertionError("Original Python relay configuration failed: environment/path mismatch")
  if results[0]:
    raise AssertionError("Original C single-instance configuration no longer reproduces failure")
  return 0

if __name__=="__main__":
  sys.exit(main())
