#!/usr/bin/env python3
"""#809 native Windows actual Git worker via Bash-only four-channel transport."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
import uuid

from windows_real_git_worker_build import build
from windows_real_git_perf import measure

OUT=Path("git809-bash-pipe-results")
SCRIPT=r'''
  request=$1; pout=$2; perr=$3; pstatus=$4; mode=$5
  exec 3>>"$request" || exit 21
  if [[ $mode == version ]]; then
    printf '\x02\x00\x00\x00git\x00--version\x00' >&3 || exit 22
  else
    printf '\x02\x00\x00\x00git\x00git809-unknown-command\x00' >&3 || exit 22
  fi
  exec 3>&-
  exec 4<"$pout" || exit 23
  out=''
  while IFS= read -r line <&4 || [[ -n $line ]]; do
    out+="$line"
    out+=$'\n'
  done
  exec 4<&-
  exec 5<"$perr" || exit 24
  err=''
  while IFS= read -r line <&5 || [[ -n $line ]]; do
    err+="$line"
    err+=$'\n'
  done
  exec 5<&-
  exec 6<"$pstatus" || exit 25
  IFS= read -r status <&6 || exit 26
  exec 6<&-
  if [[ $mode == version ]]; then
    [[ $out == 'git version '* && -z $err && $status == 'EXIT 0' ]] || exit 27
  else
    [[ -z $out && $err == *'git809-unknown-command'* && $status == 'EXIT '* ]] || exit 28
    [[ $status != 'EXIT 0' ]] || exit 29
  fi
  printf 'REAL_GIT_WORKER_PASS:%s:%s\n' "$mode" "$status"
'''
def main():
  if os.name!="nt": raise RuntimeError("native Windows required")
  root=Path.cwd()
  OUT.mkdir(exist_ok=True)
  bash=shutil.which("bash")
  if not bash or "git-sdk" not in bash.lower():
    raise RuntimeError("Git SDK Bash not found: "+repr(bash))
  git,build_cmd=build(root)
  report={"source_sha":os.getenv("GITHUB_SHA"),"build_command":build_cmd,
          "git":str(git),"cases":[]}
  try:
    for label,args in (
      ("version",["--version"]),
      ("unknown",["git809-unknown-command"])
    ):
      direct=subprocess.run([str(git),*args],stdin=subprocess.DEVNULL,
          stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
      token=uuid.uuid4().hex[:12]
      manager=subprocess.Popen([sys.executable,
        str(root/"experiments/issue-809/windows_real_git_manager.py"),
        str(git),token],stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
      c={"mode":label,"token":token,
         "direct_rc":direct.returncode,
         "direct_stdout_hex":direct.stdout.hex(),
         "direct_stderr_hex":direct.stderr.hex()}
      try:
        line=manager.stdout.readline()
        if line.strip()!=b"READY":
          raise RuntimeError("native Git worker manager did not reach READY: "+repr(line))
        pipes=[r"\\.\pipe\git809-real-"+kind+"-"+token
               for kind in ("request","stdout","stderr","status")]
        start=time.perf_counter_ns()
        bash_result=subprocess.run([bash,"--noprofile","--norc","-c",SCRIPT,
           "real-worker",*pipes,label],capture_output=True,timeout=35,
           check=False,stdin=subprocess.DEVNULL)
        c["elapsed_bash_ms"]=(time.perf_counter_ns()-start)/1e6
        c.update(bash_rc=bash_result.returncode,
                 bash_stdout=bash_result.stdout.decode("utf-8","replace"),
                 bash_stderr=bash_result.stderr.decode("utf-8","replace"))
        manager_out,manager_err=manager.communicate(timeout=25)
        c["manager_rc"]=manager.returncode
        c["manager_stdout"]=manager_out.decode("utf-8","replace")
        c["manager_stderr"]=manager_err.decode("utf-8","replace")
        result_lines=[line for line in c["manager_stdout"].splitlines()
                      if line.startswith("RESULT ")]
        if len(result_lines)!=1:
          raise RuntimeError("missing exactly one real worker result")
        details=json.loads(result_lines[0][7:])
        c["real_worker"]=details
        c["passed"]=(bash_result.returncode==0 and manager.returncode==0 and
          details["worker_rc"]==direct.returncode and
          details["stdout_hex"]==direct.stdout.hex() and
          details["stderr_hex"]==direct.stderr.hex() and
          bash_result.stdout.startswith(f"REAL_GIT_WORKER_PASS:{label}:EXIT ".encode())
          and bash_result.stderr==b"")
      except Exception as exc:
        c["error"]=str(exc)
        c["passed"]=False
      finally:
        if manager.poll() is None:
          manager.kill()
        try: manager.communicate(timeout=4)
        except subprocess.TimeoutExpired: manager.kill()
        report["cases"].append(c)
        print("REAL_CASE",json.dumps(c,ensure_ascii=False),flush=True)
    if not all(c["passed"] for c in report["cases"]):
      raise AssertionError("real Git worker four-channel acceptance failed")
    report["performance"]=measure(root,git,bash)
  finally:
    path=OUT/"real-git-worker-four-channel.json"
    path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",
                    encoding="utf-8")
    print("REPORT",path,flush=True)
if __name__=="__main__": main()
