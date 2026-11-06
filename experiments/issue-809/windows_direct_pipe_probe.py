#!/usr/bin/env python3
"""Focused Win32 and Git Bash readiness test for the PID-owned worker pipe.

Run only after building the instrumented native git.exe in this checkout.
Each connection probe uses a disposable actual Git worker so a test client
does not consume a named pipe later used by the benchmark client.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import shutil
import subprocess
import sys

K=C.WinDLL("kernel32",use_last_error=True)
K.WaitNamedPipeW.argtypes=[W.LPCWSTR,W.DWORD]
K.WaitNamedPipeW.restype=W.BOOL
K.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.LPVOID,W.DWORD,W.DWORD,W.HANDLE]
K.CreateFileW.restype=W.HANDLE
K.CloseHandle.argtypes=[W.HANDLE]
K.CloseHandle.restype=W.BOOL
INVALID=C.c_void_p(-1).value
GENERIC_READ=0x80000000
OPEN_EXISTING=3

def worker(git:Path):
  child=subprocess.Popen([str(git),"--git809-direct-worker"],
    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
    bufsize=0)
  try:
    line=child.stdout.readline()
    if line != ("READY "+str(child.pid)+"\n").encode("ascii"):
      raise AssertionError("worker READY mismatch "+repr(line)+
        " rc="+repr(child.poll()))
    return child
  except BaseException:
    child.kill()
    child.communicate(timeout=5)
    raise

def finish(child):
  if child.poll() is None:
    child.kill()
  child.communicate(timeout=5)

def probe(git:Path,bash:str,kind:str):
  child=worker(git)
  name="\\\\.\\pipe\\"+str(child.pid)+"-stdout"
  try:
    if not K.WaitNamedPipeW(name,2000):
      raise OSError(C.get_last_error(),"WaitNamedPipe stdout "+name)
    print("PIPE_AVAILABLE "+kind+" "+name,flush=True)
    if kind=="native":
      h=K.CreateFileW(name,GENERIC_READ,0,None,OPEN_EXISTING,0,None)
      if h==INVALID:
        raise OSError(C.get_last_error(),"native CreateFile "+name)
      K.CloseHandle(h)
      print("NATIVE_OPEN_PASS pid="+str(child.pid),flush=True)
    else:
      script='exec 4<"$1" || exit 61; printf "BASH_OPEN_PASS\\n"; exec 4<&-'
      proc=subprocess.run([bash,"--noprofile","--norc","-c",script,
        "probe",name],capture_output=True,timeout=8)
      print("BASH_OPEN_RESULT "+repr({"pid":child.pid,"rc":proc.returncode,
        "out":proc.stdout.decode("utf-8","replace"),
        "err":proc.stderr.decode("utf-8","replace")}),flush=True)
      if proc.returncode or b"BASH_OPEN_PASS" not in proc.stdout:
        raise AssertionError("Git Bash cannot open known-ready native pipe")
  finally:
    finish(child)

def run(git:Path):
  if sys.platform!="win32":
    raise RuntimeError("native Windows required")
  bash=shutil.which("bash")
  if not bash:
    raise RuntimeError("Git SDK bash missing")
  for kind in ("native","bash"):
    probe(git,bash,kind)

if __name__=="__main__":
  if len(sys.argv)!=2:
    raise SystemExit("usage: python windows_direct_pipe_probe.py <instrumented-git.exe>")
  run(Path(sys.argv[1]).resolve(strict=True))
