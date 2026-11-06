#!/usr/bin/env python3
"""#809: test Git Bash >, >> and <> against native Windows pipe instances."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

OUT=Path("git809-bash-pipe-results")
def main():
  OUT.mkdir(exist_ok=True)
  gcc=shutil.which("gcc")
  bash=shutil.which("bash")
  exe=(OUT/"git809-flags-server.exe").resolve()
  b=subprocess.run([gcc,"-std=c11","-O2","-Wall","-Wextra","-Werror",
                    "-o",str(exe),
                    "experiments/issue-809/windows_bash_pipe_probe.c"],
                   capture_output=True,timeout=90)
  if b.returncode: raise RuntimeError(b.stderr.decode("utf-8","replace"))
  variants=[
    ("in_truncate","in",'exec 3>"$1" || exit 21; printf "PING\n" >&3 || exit 22; exec 3>&-; echo WRITE_OK'),
    ("in_append","in",'exec 3>>"$1" || exit 21; printf "PING\n" >&3 || exit 22; exec 3>&-; echo WRITE_OK'),
    ("duplex_write","duplex",'exec 3<>"$1" || exit 21; printf "PING\n" >&3 || exit 22; exec 3>&-; exec 3<&-; echo WRITE_OK'),
    ("duplex_echo","duplex",'exec 3<>"$1" || exit 21; printf "PING\n" >&3 || exit 22; IFS= read -r -t 2 response <&3 || exit 23; [[ $response == PONG ]] || exit 24; echo ECHO_OK; exec 3>&-; exec 3<&-'),
    ("out_read","out",'exec 3<"$1" || exit 21; IFS= read -r -t 2 response <&3 || exit 23; [[ $response == PONG ]] || exit 24; echo READ_OK; exec 3<&-'),
  ]
  result={"sha":os.getenv("GITHUB_SHA"),"cases":[]}
  for label, mode, script in variants:
    token=uuid.uuid4().hex[:12]
    path=r"\\.\pipe\git809-bash-"+token
    servers=[]
    rec={"variant":label,"mode":mode,"pipe":path}
    try:
      for i in range(4):
        p=subprocess.Popen([str(exe),token,mode],stdin=subprocess.DEVNULL,
          stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        servers.append(p)
        if p.stdout.readline().strip()!=b"READY":
          raise RuntimeError("server startup failed")
      p=subprocess.run([bash,"--noprofile","--norc","-c",script,"flags",path],
         capture_output=True,timeout=5,check=False)
      rec.update(client_rc=p.returncode,
        client_stdout=p.stdout.decode("utf-8","replace"),
        client_stderr=p.stderr.decode("utf-8","replace"))
    except Exception as exc:
      rec["error"]=str(exc)
    finally:
      rec["servers"]=[]
      for i,p in enumerate(servers):
        if p.poll() is None: p.kill()
        out,err=p.communicate(timeout=3)
        rec["servers"].append({"index":i,"rc":p.returncode,
          "stdout":out.decode("utf-8","replace"),
          "stderr":err.decode("utf-8","replace")})
      result["cases"].append(rec)
      print("FLAGS",json.dumps(rec,ensure_ascii=False),flush=True)
  # Confirm one Bash process can transmit and receive via separate native
  # named-pipe endpoints. This is a transport test, not a causal reply protocol.
  pair={"variant":"two_distinct_pipes", "request_received":False,
        "response_received":False}
  pair_servers=[]
  try:
    request_token=uuid.uuid4().hex[:12]
    response_token=uuid.uuid4().hex[:12]
    request_path=r"\\.\pipe\git809-bash-"+request_token
    response_path=r"\\.\pipe\git809-bash-"+response_token
    pair["request_path"]=request_path
    pair["response_path"]=response_path
    for mode,token in (("in",request_token),("out",response_token)):
      for i in range(4):
        p=subprocess.Popen([str(exe),token,mode],stdin=subprocess.DEVNULL,
          stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        pair_servers.append((mode,i,p))
        if p.stdout.readline().strip()!=b"READY":
          raise RuntimeError("pair server startup failed")
    script=('exec 3>>"$1" || exit 21; printf "PING\n" >&3 || exit 22; '
            'exec 3>&-; exec 4<"$2" || exit 23; '
            'IFS= read -r -t 2 reply <&4 || exit 24; '
            '[[ "$reply" == PONG ]] || exit 25; '
            'exec 4<&-; echo TWO_PIPE_OK')
    proc=subprocess.run([bash,"--noprofile","--norc","-c",script,
       "two-pipes",request_path,response_path],capture_output=True,
       timeout=7,check=False)
    pair.update(client_rc=proc.returncode,
       client_stdout=proc.stdout.decode("utf-8","replace"),
       client_stderr=proc.stderr.decode("utf-8","replace"),
       response_received=(proc.returncode==0 and proc.stdout==b"TWO_PIPE_OK\n"))
  except Exception as exc:
    pair["error"]=str(exc)
  finally:
    pair["servers"]=[]
    for mode,i,p in pair_servers:
      if p.poll() is None: p.kill()
      out,err=p.communicate(timeout=3)
      item={"mode":mode,"index":i,"rc":p.returncode,
        "stdout":out.decode("utf-8","replace"),
        "stderr":err.decode("utf-8","replace")}
      pair["servers"].append(item)
      if mode=="in" and p.returncode==0 and b"COMPLETE" in out:
        pair["request_received"]=True
    result["cases"].append(pair)
    print("FLAGS",json.dumps(pair,ensure_ascii=False),flush=True)
  path=OUT/"bash-open-flags.json"
  path.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
  print("REPORT",path,flush=True)
if __name__=="__main__": main()
