#!/usr/bin/env python3
"""#809 paired native Git vs already-running Bash IPC timing.

Experimental: each native Git worker and its Python manager are prestarted
and READY before the ONE Bash benchmark shell begins. Counts do not include
pool creation/replenishment, and do include Bash named-pipe opens and I/O.
"""
from __future__ import annotations
from decimal import Decimal
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import uuid

N=10
SCRIPT=r'''
  gitbin=$1; shift
  direct_rc=0
  run_direct() {
    direct_begin=$EPOCHREALTIME
    "$gitbin" --version >/dev/null 2>/dev/null
    direct_rc=$?
    direct_end=$EPOCHREALTIME
    [[ $direct_rc == 0 ]] || return 51
  }
  run_ipc() {
    request=$1; pout=$2; perr=$3; pstatus=$4
    ipc_begin=$EPOCHREALTIME
    exec 3>>"$request" || return 52
    printf '\x02\x00\x00\x00git\x00--version\x00' >&3 || return 53
    exec 3>&-
    exec 4<"$pout" || return 54
    IFS= read -r stdout_line <&4 || return 55
    exec 4<&-
    [[ $stdout_line == 'git version '* ]] || return 56
    exec 5<"$perr" || return 57
    if IFS= read -r stderr_line <&5; then return 58; fi
    exec 5<&-
    exec 6<"$pstatus" || return 59
    IFS= read -r status <&6 || return 60
    exec 6<&-
    [[ $status == 'EXIT 0' ]] || return 61
    ipc_end=$EPOCHREALTIME
  }
  i=0
  while (( $# == 4 || $# > 4 )); do
    request=$1; pout=$2; perr=$3; pstatus=$4; shift 4
    if (( i % 2 == 0 )); then
      run_direct || exit $?
      run_ipc "$request" "$pout" "$perr" "$pstatus" || exit $?
    else
      run_ipc "$request" "$pout" "$perr" "$pstatus" || exit $?
      run_direct || exit $?
    fi
    printf 'PAIR %s %s %s %s %s\n' "$i" "$direct_begin" "$direct_end" "$ipc_begin" "$ipc_end"
    ((i+=1))
  done
'''
def percentiles(samples):
  seq=sorted(samples)
  return {"n":len(seq),"median_ms":statistics.median(seq),
          "p95_ms":seq[int((len(seq)-1)*.95)],"samples_ms":samples}
def measure(root:Path, git:Path, bash:str):
  script=root/"experiments"/"issue-809"/"windows_real_git_manager.py"
  cygpath=subprocess.run(["cygpath","-u",str(git)],capture_output=True,check=True,
                         text=True,encoding="utf-8").stdout.strip()
  managers=[]
  tokens=[]
  report={"source_sha":os.environ.get("GITHUB_SHA"),
          "git_binary":str(git),"bash":bash,"samples":N,
          "comparison":"one persistent Git Bash process, interleaved direct Git vs 4-channel IPC",
          "limitations":["Workers/managers prestarted; replenishment cost excluded",
                         "Only git --version, no repository-mutating workload",
                         "Per-case process/pipe scheduling and Windows runner variability",
                         "Python manager remains alive but no per-command client executable"],
          "cases":[]}
  try:
    for _ in range(N):
      token=uuid.uuid4().hex[:12]
      p=subprocess.Popen([sys.executable,str(script),str(git),token],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
      line=p.stdout.readline()
      if line.strip()!=b"READY":
        raise RuntimeError("prestart failed: "+repr(line))
      managers.append(p);tokens.append(token)
    argv=[bash,"--noprofile","--norc","-c",SCRIPT,"paired",cygpath]
    for token in tokens:
      argv.extend(r"\\.\pipe\git809-real-"+k+"-"+token
                  for k in ("request","stdout","stderr","status"))
    p=subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,
                     timeout=90,check=False)
    report["bash_rc"]=p.returncode
    report["bash_stdout"]=p.stdout.decode("utf-8","replace")
    report["bash_stderr"]=p.stderr.decode("utf-8","replace")
    if p.returncode: raise RuntimeError("Bash paired test failed: "+repr(p.stderr))
    direct,ipc=[],[]
    for line in report["bash_stdout"].splitlines():
      fields=line.split(" ")
      if len(fields)!=6 or fields[0]!="PAIR":
        raise RuntimeError("invalid timing sample: "+repr(line))
      _,idx,ds,de,ins,ine=fields
      if int(idx)!=len(direct): raise RuntimeError("wrong sample order")
      direct.append(float((Decimal(de)-Decimal(ds))*1000))
      ipc.append(float((Decimal(ine)-Decimal(ins))*1000))
    if len(direct)!=N: raise RuntimeError("incomplete paired samples")
    for m,token in zip(managers,tokens):
      output,error=m.communicate(timeout=30)
      result=output.decode("utf-8","replace")
      lines=[x for x in result.splitlines() if x.startswith("RESULT ")]
      if m.returncode!=0 or len(lines)!=1:
        raise RuntimeError("manager failed "+token+": "+repr(result+" "+error.decode("utf-8","replace")))
      detail=json.loads(lines[0][7:])
      report["cases"].append(detail)
      if detail["worker_rc"]!=0 or not all(detail["delivered"].values()):
        raise RuntimeError("worker case mismatch: "+token)
    report["direct"]=percentiles(direct)
    report["ipc"]=percentiles(ipc)
    report["median_difference_ms"]=report["ipc"]["median_ms"]-report["direct"]["median_ms"]
    print("PERFORMANCE",json.dumps({k:report[k] for k in ("direct","ipc","median_difference_ms")}),flush=True)
  finally:
    for m in managers:
      if m.poll() is None:m.kill()
      try:m.communicate(timeout=3)
      except subprocess.TimeoutExpired:m.kill()
    dest=Path("git809-bash-pipe-results")/"real-git-persistent-bash-benchmark.json"
    dest.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print("PERFORMANCE_REPORT",dest,flush=True)
  return report
