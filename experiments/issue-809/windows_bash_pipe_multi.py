#!/usr/bin/env python3
"""RWF #809: determine whether MSYS path probing consumes first pipe instance."""
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
  exe=(OUT/"git809-multi-server.exe").resolve()
  b=subprocess.run([gcc,"-std=c11","-O2","-Wall","-Wextra","-Werror",
                    "-o",str(exe),
                    "experiments/issue-809/windows_bash_pipe_probe.c"],
                    capture_output=True,timeout=90)
  if b.returncode: raise RuntimeError(b.stderr.decode("utf-8","replace"))
  report={"sha":os.getenv("GITHUB_SHA"),"cases":[],"instances_per_case":3}
  for kind in ("windows_native","proc_nt_namespace"):
    for mode in ("in","out","duplex"):
      token=uuid.uuid4().hex[:12]
      name="git809-bash-"+token
      path=(r"\\.\pipe"+"\\"+name) if kind=="windows_native" else (
          "/proc/sys/Device/NamedPipe/"+name)
      servers=[]
      entry={"path":path,"kind":kind,"mode":mode}
      try:
        for i in range(3):
          p=subprocess.Popen([str(exe),token,mode],stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE)
          servers.append(p)
          line=p.stdout.readline()
          if line.strip()!=b"READY": raise RuntimeError("server not ready: "+repr(line))
        script={"in":'exec 3>"$1" || exit 21; printf "PING\\n" >&3 || exit 22; exec 3>&-; echo WRITE_OK',
                "out":'exec 3<"$1" || exit 21; IFS= read -r -t 3 reply <&3 || exit 23; [[ $reply == PONG ]] || exit 24; echo READ_OK',
                "duplex":'exec 3<>"$1" || exit 21; printf "PING\\n" >&3 || exit 22; IFS= read -r -t 3 reply <&3 || exit 23; [[ $reply == PONG ]] || exit 24; echo DUPLEX_OK'}[mode]
        c=subprocess.run([bash,"--noprofile","--norc","-c",script,
                          "bash-probe",path],capture_output=True,timeout=8)
        entry["client_rc"]=c.returncode
        entry["client_stdout"]=c.stdout.decode("utf-8","replace")
        entry["client_stderr"]=c.stderr.decode("utf-8","replace")
      except Exception as err:
        entry["error"]=str(err)
      finally:
        entry["servers"]=[]
        for i,p in enumerate(servers):
          if p.poll() is None: p.kill()
          out,err=p.communicate(timeout=3)
          entry["servers"].append({"index":i,"rc":p.returncode,
            "stdout":out.decode("utf-8","replace"),
            "stderr":err.decode("utf-8","replace")})
        report["cases"].append(entry)
        print("MULTI",json.dumps(entry,ensure_ascii=False),flush=True)
  path=OUT/"multi-instance-pipe-probe.json"
  path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
  print("REPORT",str(path),flush=True)
if __name__=="__main__": main()
