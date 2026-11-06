#!/usr/bin/env python3
"""#809 independent native Windows four unidirectional named-pipe smoke test.

No Git production code changes. Bash client uses only shell builtins.
Each request is a counted u32 little-endian argc + NUL-terminated argv.
Output channels belong to the same manager request and include exit status.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

OUT=Path("git809-bash-pipe-results")
SCRIPT=r'''
  request=$1; stdout_pipe=$2; stderr_pipe=$3; status_pipe=$4; mode=$5
  exec 3>>"$request" || exit 21
  if [[ $mode == success ]]; then
    printf '\x02\x00\x00\x00git\x00success\x00' >&3 || exit 22
  else
    printf '\x02\x00\x00\x00git\x00failure\x00' >&3 || exit 22
  fi
  exec 3>&-
  exec 4<"$stdout_pipe" || exit 23
  # Check Git Bash's zero-timeout readiness on the Win32 named-pipe handle.
  wait_ready() {
    local fd=$1 deadline=$((SECONDS+5))
    until IFS= read -r -t 0 -u "$fd"; do
      (( SECONDS < deadline )) || return 1
    done
  }
  wait_ready 4 || exit 67
  if [[ $mode == success ]]; then
    IFS= read -r -d '' prefix <&4 || exit 24
    IFS= read -r suffix <&4 || exit 25
    [[ $prefix == OUT && $suffix == X ]] || exit 26
  else
    IFS= read -r stdout_line <&4 || exit 27
    [[ $stdout_line == FAIL ]] || exit 28
  fi
  wait_ready 4 || exit 68
  exec 4<&-
  exec 5<"$stderr_pipe" || exit 29
  wait_ready 5 || exit 69
  IFS= read -r stderr_line <&5 || exit 30
  wait_ready 5 || exit 70
  exec 5<&-
  if [[ $mode == success ]]; then
    [[ $stderr_line == ERR ]] || exit 31
  else
    [[ $stderr_line == BAD ]] || exit 32
  fi
  exec 6<"$status_pipe" || exit 33
  IFS= read -r exit_line <&6 || exit 34
  exec 6<&-
  if [[ $mode == success ]]; then
    [[ $exit_line == 0 ]] || exit 35
  else
    [[ $exit_line == 37 ]] || exit 36
  fi
  printf 'FOUR_CHANNELS_PASS:%s\n' "$mode"
'''
def main():
  if os.name!="nt": raise RuntimeError("native Windows required")
  OUT.mkdir(exist_ok=True)
  gcc=shutil.which("gcc")
  bash=shutil.which("bash")
  if not gcc or not bash: raise RuntimeError("Git SDK GCC/Bash missing")
  exe=(OUT/"git809-four-channels.exe").resolve()
  cmd=[gcc,"-std=c11","-O2","-Wall","-Wextra","-Werror","-o",
       str(exe),"experiments/issue-809/windows_bash_four_channels.c"]
  build=subprocess.run(cmd,capture_output=True,timeout=90)
  if build.returncode:
    raise RuntimeError("compile failed: "+repr(build.stderr))
  report={"source_sha":os.getenv("GITHUB_SHA"),"cases":[],"protocol":
    "u32 little endian argc followed by two NUL-terminated argv entries",
    "channels":["request","stdout","stderr","status"]}
  try:
    for mode in ("success","failure"):
      token=uuid.uuid4().hex[:12]
      pipes=[r"\\.\pipe\git809-four-"+kind+"-"+token
             for kind in ("request","stdout","stderr","status")]
      server=subprocess.Popen([str(exe),token],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
      case={"mode":mode,"request_token":token}
      try:
        line=server.stdout.readline()
        if line.strip()!=b"READY":
          raise RuntimeError("manager not ready: "+repr(line))
        p=subprocess.run([bash,"--noprofile","--norc","-c",SCRIPT,
            "four-channels",*pipes,mode],
            stdin=subprocess.DEVNULL,capture_output=True,timeout=20)
        case.update(client_rc=p.returncode,
          client_stdout=p.stdout.decode("utf-8","replace"),
          client_stderr=p.stderr.decode("utf-8","replace"))
        out,err=server.communicate(timeout=12)
        case.update(manager_rc=server.returncode,
          manager_stdout=out.decode("utf-8","replace"),
          manager_stderr=err.decode("utf-8","replace"))
        expected=f"FOUR_CHANNELS_PASS:{mode}\n".encode()
        case["passed"]=(p.returncode==0 and p.stdout==expected and
          p.stderr==b"" and server.returncode==0 and
          b"DONE mode=" in out)
      except Exception as exc:
        case["error"]=str(exc)
        case["passed"]=False
      finally:
        if server.poll() is None:
          server.kill()
        try:
          out,err=server.communicate(timeout=3)
          case.setdefault("manager_rc",server.returncode)
          case.setdefault("manager_stderr",err.decode("utf-8","replace"))
        except subprocess.TimeoutExpired:
          server.kill()
        report["cases"].append(case)
        print("FOUR",json.dumps(case,ensure_ascii=False),flush=True)
    if not all(case["passed"] for case in report["cases"]):
      raise AssertionError("four-channel correlated request/response failed")
  finally:
    path=OUT/"four-channel-correlated-results.json"
    path.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print("REPORT",path,flush=True)
if __name__=="__main__": main()
