#!/usr/bin/env python3
"""RWF 809: can an already running Git Bash open Win32 named pipes directly?

A native Win32 C server offers one duplex named pipe per trial.
Bash uses only builtins: exec redirection, printf, read and comparison.
Neither a Git wrapper nor a native helper is spawned by the Bash client.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

OUT = Path("git809-bash-pipe-results")
SCRIPT_DUPLEX = r'''
  exec 3<>"$1" || exit 21
  printf 'PING\n' >&3 || exit 22
  IFS= read -r -t 3 reply <&3 || exit 23
  [[ $reply == PONG ]] || exit 24
  printf 'PONG_OK\n'
  exec 3>&-
  exec 3<&-
'''
SCRIPT_IN = r'''
  exec 3>"$1" || exit 21
  printf 'PING\n' >&3 || exit 22
  exec 3>&-
  printf 'WRITE_OK\n'
'''
SCRIPT_OUT = r'''
  exec 3<"$1" || exit 21
  IFS= read -r -t 3 reply <&3 || exit 23
  [[ $reply == PONG ]] || exit 24
  exec 3<&-
  printf 'READ_OK\n'
'''
TIMEOUT = 9

def main():
  if os.name != "nt":
    raise RuntimeError("Native Windows CI required")
  OUT.mkdir(exist_ok=True)
  gcc = shutil.which("gcc")
  bash = shutil.which("bash")
  if not gcc or not bash:
    raise RuntimeError(f"gcc={gcc!r} bash={bash!r}")
  exe = (OUT / "git809-bash-pipe-server.exe").resolve()
  build = subprocess.run([gcc, "-std=c11", "-O2", "-Wall", "-Wextra",
                           "-Werror", "-o", str(exe),
                           "experiments/issue-809/windows_bash_pipe_probe.c"],
                         capture_output=True, timeout=90, check=False)
  if build.returncode:
    raise RuntimeError(build.stderr.decode("utf-8", "replace"))
  # Windows named-pipe native form and two MSYS/Git Bash path translations.
  cases = [
    ("windows_native", lambda name: r"\\.\pipe\\".rstrip("\\") + "\\" + name),
    ("msys_unc", lambda name: "//./pipe/" + name),
    ("msys_dev", lambda name: "/dev/pipe/" + name),
  ]
  report = {"source_sha": os.getenv("GITHUB_SHA"),
            "gcc": gcc, "bash": bash, "cases": [],
            "goal": "Native Git Bash builtins connect/write/read a Win32 named pipe"}
  try:
    for label, pathfunc in cases:
      for mode in ("in", "out", "duplex"):
        token=uuid.uuid4().hex[:12]
        name="git809-bash-" + token
        path=pathfunc(name)
        server=subprocess.Popen([str(exe), token, mode],
                                 stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        start=time.perf_counter_ns()
        record={"case": label, "mode": mode, "bash_path": path}
        try:
          line=server.stdout.readline()
          if line.strip()!=b"READY":
            raise RuntimeError("server was not READY: "+repr(line))
          script={"in": SCRIPT_IN, "out": SCRIPT_OUT,
                  "duplex": SCRIPT_DUPLEX}[mode]
          expected={"in": b"WRITE_OK\n", "out": b"READ_OK\n",
                    "duplex": b"PONG_OK\n"}[mode]
          client=subprocess.run([bash, "--noprofile", "--norc", "-c",
                                 script, "bash-probe", path],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=TIMEOUT, check=False)
          record.update(client_rc=client.returncode,
                        client_stdout=client.stdout.decode("utf-8", "replace"),
                        client_stderr=client.stderr.decode("utf-8", "replace"),
                        bash_success=(client.returncode == 0 and
                                      client.stdout == expected))
          if record["bash_success"]:
            out,err=server.communicate(timeout=3)
            record.update(server_rc=server.returncode,
                          server_stdout=out.decode("utf-8", "replace"),
                          server_stderr=err.decode("utf-8", "replace"))
            if server.returncode!=0:
              record["bash_success"]=False
        except Exception as exc:
          record["failure"]=str(exc)
          record["bash_success"]=False
        finally:
          record["elapsed_ms"]=(time.perf_counter_ns()-start)/1e6
          if server.poll() is None:
            server.kill()
          try:
            out,err=server.communicate(timeout=2)
            record.setdefault("server_rc",server.returncode)
            record.setdefault("server_stdout",out.decode("utf-8","replace"))
            record.setdefault("server_stderr",err.decode("utf-8","replace"))
          except subprocess.TimeoutExpired:
            server.kill()
          report["cases"].append(record)
          print("CASE", json.dumps(record, ensure_ascii=False), flush=True)
  finally:
    path=OUT / "git-bash-named-pipe-probe.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n",
                    encoding="utf-8")
    print("RESULT", str(path), flush=True)
  print("SUMMARY", json.dumps({x["case"]:x["bash_success"]
                               for x in report["cases"]}),flush=True)

if __name__ == "__main__":
  main()
