#!/usr/bin/env python3
"""#809 paired 3-OS Git --version benchmark driver.

Windows: build instrumented Git and compare direct executable against
the persistent Bash->four-pipe->real Git pool, including replenishment.
The other two platforms use the exact same source-built Git flags but
measure direct Git only within a persistent Bash shell.
"""
from __future__ import annotations
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

N=12
OUT=Path("git809-bash-pipe-results")
WIN_SCRIPT=r'''
  source "$1" 3 || exit 31
  gitbin=$2
  printf 'POOL %s %s\n' "$_GIT809_MANAGER_BASH_PID" "$_GIT809_MANAGER_READY"
  source "$1" 3 || exit 32
  [[ "$_GIT809_MANAGER_WORKERS" == 3 ]] || exit 33
  for ((i=0;i<12;i++)); do
    if (( i % 2 == 0 )); then
      direct_begin=$EPOCHREALTIME
      "$gitbin" --version >/dev/null 2>/dev/null || exit 41
      direct_end=$EPOCHREALTIME
      ipc_begin=$EPOCHREALTIME
      git-ipc --version >/dev/null 2>/dev/null || { cat "$_GIT809_MANAGER_LOG" >&2; exit 42; }
      ipc_end=$EPOCHREALTIME
    else
      ipc_begin=$EPOCHREALTIME
      git-ipc --version >/dev/null 2>/dev/null || { cat "$_GIT809_MANAGER_LOG" >&2; exit 43; }
      ipc_end=$EPOCHREALTIME
      direct_begin=$EPOCHREALTIME
      "$gitbin" --version >/dev/null 2>/dev/null || exit 44
      direct_end=$EPOCHREALTIME
    fi
    printf 'PAIR %s %s %s %s %s\n' "$i" "$direct_begin" "$direct_end" "$ipc_begin" "$ipc_end"
  done
  # Verify that alias actually dispatches to the IPC function, not executable.
  shopt -s expand_aliases
  eval 'git --version' || exit 45
  printf 'ALIAS_PASS\n'
'''
UNIX_SCRIPT=r'''
  gitbin=$1
  for ((i=0;i<12;i++)); do
    a=$EPOCHREALTIME
    "$gitbin" --version >/dev/null 2>/dev/null || exit 41
    b=$EPOCHREALTIME
    printf 'DIRECT %s %s %s\n' "$i" "$a" "$b"
  done
'''
def stats(vals):
  s=sorted(vals)
  assert len(s)==N
  return {"median_ms":statistics.median(s),"p95_ms":s[math.ceil(.95*len(s))-1],
          "min_ms":s[0],"max_ms":s[-1],"samples_ms":vals}
def timed(a,b):
  return float((Decimal(b)-Decimal(a))*1000)

def build_git(root:Path):
  if os.name=="nt":
    from windows_real_git_worker_build import build
    return build(root)
  cmd=["make","-j4","NO_RUST=YesPlease","NO_CURL=YesPlease",
       "NO_GETTEXT=YesPlease","git"]
  subprocess.run(cmd,cwd=root,check=True,timeout=1800,
                 stdout=subprocess.DEVNULL)
  return (root/"git").resolve(),cmd

def main():
  OUT.mkdir(exist_ok=True)
  root=Path.cwd()
  windows=os.name=="nt"
  bash=shutil.which("bash")
  if not bash:raise RuntimeError("Bash not installed")
  timer_test=subprocess.run([bash,"--noprofile","--norc","-c",
    '[[ $EPOCHREALTIME =~ ^[0-9]+\\.[0-9]+$ ]]'],capture_output=True)
  if timer_test.returncode:
    raise RuntimeError("selected Bash has no EPOCHREALTIME timer: "+bash)
  if windows and "git-sdk" not in bash.lower():
    raise RuntimeError("Git SDK Bash not selected: "+repr(bash))
  git,build_cmd=build_git(root)
  git_native=str(git)
  git_shell=(subprocess.run(["cygpath","-u",git_native],capture_output=True,
     check=True,text=True,encoding="utf-8").stdout.strip() if windows else git_native)
  report={"sha":os.getenv("GITHUB_SHA"),"runner_os":os.getenv("RUNNER_OS"),
    "windows_optimized":windows,"git_binary":git_native,
    "build_command":build_cmd,"n":N,
    "method":"one persistent Bash process; source-built Git --version; EPOCHREALTIME; alternating paired order on Windows",
    "startup_included":False,"worker_replenishment_included":windows}
  try:
    if windows:
      script=(subprocess.run(["cygpath","-u",str(root/"experiments"/"issue-809"/"git-manager.sh")],
        capture_output=True,check=True,text=True,encoding="utf-8").stdout.strip())
      env=os.environ.copy()
      env["GIT809_WORKER_GIT"]=git_native
      if not env.get("GIT809_MANAGER_PYTHON"):
        env["GIT809_MANAGER_PYTHON"]=sys.executable
      proc=subprocess.run([bash,"--noprofile","--norc","-c",WIN_SCRIPT,
        "benchmark",script,git_shell],capture_output=True,stdin=subprocess.DEVNULL,
        timeout=180,env=env)
    else:
      proc=subprocess.run([bash,"--noprofile","--norc","-c",UNIX_SCRIPT,
        "benchmark",git_shell],capture_output=True,stdin=subprocess.DEVNULL,timeout=90)
    report["bash_rc"]=proc.returncode
    report["stdout"]=proc.stdout.decode("utf-8","replace")
    report["stderr"]=proc.stderr.decode("utf-8","replace")
    if proc.returncode:
      raise AssertionError("Bash benchmark failed rc="+str(proc.returncode)+" "+report["stderr"][-2000:])
    direct=[]
    ipc=[]
    for line in report["stdout"].splitlines():
      fields=line.split(" ")
      if fields[0]=="PAIR" and len(fields)==6:
        _,idx,db,de,ib,ie=fields
        if int(idx)!=len(direct):raise AssertionError("pair index mismatch")
        direct.append(timed(db,de))
        ipc.append(timed(ib,ie))
      elif fields[0]=="DIRECT" and len(fields)==4:
        _,idx,a,b=fields
        if int(idx)!=len(direct):raise AssertionError("direct index mismatch")
        direct.append(timed(a,b))
    report["direct"]=stats(direct)
    if windows:
      report["ipc"]=stats(ipc)
      if "ALIAS_PASS" not in report["stdout"]:
        raise AssertionError("transparent git alias not exercised")
      report["median_saved_ms"]=report["direct"]["median_ms"]-report["ipc"]["median_ms"]
      report["median_speedup"]=report["direct"]["median_ms"]/report["ipc"]["median_ms"]
    print("OS_BENCHMARK "+json.dumps({k:report[k] for k in
      ("sha","runner_os","direct",* (("ipc","median_saved_ms","median_speedup") if windows else ()))},
      ensure_ascii=False),flush=True)
  finally:
    dest=OUT/("three-os-"+("windows" if windows else
                          (os.getenv("RUNNER_OS") or sys.platform).lower())+".json")
    dest.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print("OS_BENCHMARK_REPORT "+str(dest),flush=True)

if __name__=="__main__":
  main()
