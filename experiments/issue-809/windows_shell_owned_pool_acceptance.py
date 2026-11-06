#!/usr/bin/env python3
"""#809 native Windows acceptance for sourced, shell-owned Git worker pool.

Tests idempotence, actual preinitialization, normal shell exit and abrupt
termination, without a control protocol or shell-session names on IPC pipes.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from windows_real_git_worker_build import build

K=C.WinDLL("kernel32",use_last_error=True)
K.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD]
K.OpenProcess.restype=W.HANDLE
K.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD]
K.WaitForSingleObject.restype=W.DWORD
K.CloseHandle.argtypes=[W.HANDLE]
K.CloseHandle.restype=W.BOOL
SYNCHRONIZE=0x00100000
WAIT_OBJECT_0=0
WAIT_TIMEOUT=258
OUT=Path("git809-bash-pipe-results")
SHELL=r'''
  source "$1" "$2" || exit 31
  first=$_GIT809_MANAGER_BASH_PID
  source "$1" "$2" || exit 32
  [[ "$_GIT809_MANAGER_BASH_PID" == "$first" ]] || exit 33
  [[ "$_GIT809_MANAGER_WORKERS" == "$2" ]] || exit 34
  [[ -f "$_GIT809_MANAGER_READY" ]] || exit 35
  printf 'POOL_READY %s\n' "$(cygpath -w "$_GIT809_MANAGER_READY")"
  IFS= read -r signal || exit 36
  [[ "$signal" == shutdown ]] || exit 37
'''
def alive(pid:int)->bool:
  h=K.OpenProcess(SYNCHRONIZE,False,pid)
  if not h: return False
  try:
    return K.WaitForSingleObject(h,0)==WAIT_TIMEOUT
  finally:
    K.CloseHandle(h)

def until_gone(pids:list[int],ready:Path,timeout=15):
  deadline=time.monotonic()+timeout
  while time.monotonic()<deadline:
    if not any(alive(pid) for pid in pids) and not ready.exists():
      return True
    time.sleep(0.2)
  return False

def run_case(bash:str,script:str,git:Path,python:str,case:str,count:int):
  env=os.environ.copy()
  env["GIT809_WORKER_GIT"]=str(git)
  env["GIT809_MANAGER_PYTHON"]=python
  p=subprocess.Popen([bash,"--noprofile","--norc","-c",SHELL,
                      "lifecycle",script,str(count)],
                     stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                     env=env)
  record={"case":case,"worker_target":count,"shell_winpid":p.pid}
  ready=None
  all_pids=[]
  try:
    line=p.stdout.readline().decode("utf-8","strict").strip()
    if not line.startswith("POOL_READY "):
      raise RuntimeError("Bash didn't finish sourcing: "+repr(line))
    ready=Path(line[len("POOL_READY "):])
    data=json.loads(ready.read_text(encoding="utf-8"))
    record["ready_record"]=data
    if data["owner_winpid"]!=p.pid:
      raise AssertionError("manager linked to wrong owning process: "
                           +repr((data["owner_winpid"],p.pid)))
    if data["worker_count"]!=count:
      raise AssertionError("wrong ready worker count")
    if len(set(data["worker_pids"]))!=count:
      raise AssertionError("workers not distinct")
    all_pids=[data["manager_pid"],*data["worker_pids"]]
    if len(set(all_pids))!=len(all_pids) or not all(map(alive,all_pids)):
      raise AssertionError("manager/worker not all alive at READY")
    start=time.perf_counter()
    if case=="normal_exit":
      p.stdin.write(b"shutdown\n")
      p.stdin.flush()
      stdout,stderr=p.communicate(timeout=5)
      if p.returncode!=0:
        raise RuntimeError("Bash nonzero exit "+repr((p.returncode,stderr)))
    elif case=="forced_termination":
      p.terminate()
      p.communicate(timeout=5)
    else:
      raise AssertionError("unknown test case")
    record["cleanup_success"]=until_gone(all_pids,ready)
    record["cleanup_elapsed_seconds"]=round(time.perf_counter()-start,3)
    record["remaining_alive"]=[pid for pid in all_pids if alive(pid)]
    if not record["cleanup_success"]:
      raise AssertionError("orphan manager or Git worker survived shell exit")
    record["passed"]=True
  except Exception as exc:
    record["error"]=str(exc)
    record["passed"]=False
  finally:
    if p.poll() is None:
      p.kill()
    try: p.communicate(timeout=4)
    except subprocess.TimeoutExpired: p.kill()
    print("LIFECYCLE",json.dumps(record,ensure_ascii=False),flush=True)
  return record

def main():
  if os.name!="nt": raise RuntimeError("native Windows required")
  OUT.mkdir(exist_ok=True)
  bash=shutil.which("bash")
  if not bash or "git-sdk" not in bash.lower():
    raise RuntimeError("Git SDK Bash missing: "+repr(bash))
  root=Path.cwd()
  git,build_command=build(root)
  script=subprocess.run(["cygpath","-u",
    str(root/"experiments"/"issue-809"/"git-manager.sh")],
    capture_output=True,text=True,encoding="utf-8",check=True).stdout.strip()
  results={"source_sha":os.getenv("GITHUB_SHA"),
           "git_binary":str(git),"build_command":build_command,
           "cases":[]}
  try:
    for name,count in (("normal_exit",3),("forced_termination",2)):
      results["cases"].append(run_case(bash,script,git,
                                      os.environ["GIT809_MANAGER_PYTHON"],
                                      name,count))
    if not all(x["passed"] for x in results["cases"]):
      raise AssertionError("shell-owned manager lifecycle acceptance failed")
  finally:
    path=OUT/"git809-shell-owned-pool-lifecycle.json"
    path.write_text(json.dumps(results,indent=2)+"\n",encoding="utf-8")
    print("LIFECYCLE_REPORT",path,flush=True)

if __name__=="__main__":
  main()
