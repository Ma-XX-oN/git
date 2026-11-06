#!/usr/bin/env python3
"""#809: trace MSYS2 Bash open() of a live native Win32 named pipe."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

OUT = Path("git809-bash-pipe-results")
def main():
  OUT.mkdir(exist_ok=True)
  gcc = shutil.which("gcc")
  bash = shutil.which("bash")
  strace = shutil.which("strace")
  report = {"source_sha":os.getenv("GITHUB_SHA"),"gcc":gcc,"bash":bash,
            "strace":strace,"cases":[]}
  try:
    exe=(OUT/"git809-trace-server.exe").resolve()
    build=subprocess.run([gcc,"-std=c11","-O2","-Wall","-Wextra","-Werror",
      "-o",str(exe),"experiments/issue-809/windows_bash_pipe_probe.c"],
      capture_output=True,timeout=90,check=False)
    if build.returncode:
      raise RuntimeError("build: "+repr(build.stderr))
    for mode in ("in","out","duplex"):
      token=uuid.uuid4().hex[:12]
      pipe=r"\\.\pipe\git809-bash-"+token
      server=subprocess.Popen([str(exe),token,mode],stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
      case={"mode":mode,"pipe":pipe}
      try:
        if server.stdout.readline().strip()!=b"READY":
          raise RuntimeError("server not ready")
        script={"in":'exec 3>"$1"; printf "PING\\n" >&3',
                "out":'exec 3<"$1"; IFS= read -r -t 3 line <&3',
                "duplex":'exec 3<>"$1"; printf "PING\\n" >&3'}[mode]
        cmd=[bash,"--noprofile","--norc","-c",script,"trace",pipe]
        tracefile=OUT/("msys-bash-"+mode+".strace")
        if strace:
          cmd=[strace,"-o",str(tracefile),"-f"]+cmd
        p=subprocess.run(cmd,capture_output=True,timeout=8,check=False)
        case.update(client_rc=p.returncode,stderr=p.stderr.decode("utf-8","replace"),
                    stdout=p.stdout.decode("utf-8","replace"))
        if tracefile.exists():
          full=tracefile.read_text(encoding="utf-8",errors="replace")
          lines=full.splitlines()
          selected=[(i+1,line) for i,line in enumerate(lines)
                    if any(marker in line.lower()
                    for marker in ("git809-bash","namedpipe","ebusy","errno 16",
                    "open(", "fhandler_base", "fhandler_disk_file",
                    "ntcreatefile","createfile","resource busy"))]
          case["trace_lines"]=len(lines)
          case["trace_matches"]=selected[-110:]
          case["trace_tail"]=lines[-30:]
      except Exception as exc:
        case["error"]=str(exc)
      finally:
        if server.poll() is None: server.kill()
        out,err=server.communicate(timeout=3)
        case.update(server_rc=server.returncode,
                    server_stdout=out.decode("utf-8","replace"),
                    server_stderr=err.decode("utf-8","replace"))
        report["cases"].append(case)
        print("CASE",json.dumps(case,ensure_ascii=False),flush=True)
  finally:
    path=OUT/"msys-pipe-open-trace-report.json"
    path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print("REPORT",path,flush=True)

if __name__=="__main__":
  main()
