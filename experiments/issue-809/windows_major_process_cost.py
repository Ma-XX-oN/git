#!/usr/bin/env python3
"""Git #809: major OS process-lifetime and packaging-path cost attribution.

Never changes Defender configuration or native binaries. Runs on one hosted
Windows instance and compares exact binary copies, production SDK Git,
same-source full-feature Git and a compiler-matched no-op process.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import tempfile
import time

class FILETIME(ctypes.Structure):
  _fields_=[("low",ctypes.c_uint32),("high",ctypes.c_uint32)]

def filetime_ms(value):
  return ((value.high << 32)|value.low)/10000

def process_cpu_and_lifetime(handle):
  dll=ctypes.WinDLL("kernel32",use_last_error=True)
  dll.GetProcessTimes.argtypes=[ctypes.c_void_p]+[ctypes.POINTER(FILETIME)]*4
  dll.GetProcessTimes.restype=ctypes.c_int
  start,end,kernel,user=(FILETIME() for _ in range(4))
  if not dll.GetProcessTimes(handle,ctypes.byref(start),ctypes.byref(end),
                             ctypes.byref(kernel),ctypes.byref(user)):
    raise ctypes.WinError(ctypes.get_last_error())
  return {"child_cpu_ms":filetime_ms(user)+filetime_ms(kernel),
          "child_os_lifetime_ms":filetime_ms(end)-filetime_ms(start)}

def measure(args,expected):
  t0=time.perf_counter_ns()
  with subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL) as proc:
    t1=time.perf_counter_ns()
    out,err=proc.communicate(timeout=20)
    t2=time.perf_counter_ns()
    if proc.returncode or out!=expected:
      raise RuntimeError("unexpected output/return code: "+repr(args)+
                         " rc="+str(proc.returncode)+" stderr="+
                         err.decode("utf-8","replace")[:250])
    kernel=process_cpu_and_lifetime(int(proc._handle))
  return {"parent_popen_ms":(t1-t0)/1e6,
          "post_popen_wall_ms":(t2-t1)/1e6,
          "total_wall_ms":(t2-t0)/1e6,**kernel}

def sha(path):
  h=hashlib.sha256()
  with Path(path).open("rb") as f:
    for part in iter(lambda:f.read(65536),b""):
      h.update(part)
  return h.hexdigest()

def summary(samples):
  return {field:{"median":statistics.median(r[field] for r in samples),
                 "p95":sorted(r[field] for r in samples)[int(.95*(len(samples)-1))]}
          for field in samples[0]}

def main():
  if os.name!="nt":
    raise RuntimeError("native Windows experiment only")
  compiled=(Path.cwd()/"git.exe").resolve()
  installed=Path(shutil.which("git") or "").resolve()
  if not installed.is_file() or not compiled.is_file():
    raise RuntimeError("installed and full-source Git both required")
  if installed==compiled:
    raise RuntimeError("control aliases compiled Git")
  directory=Path("git809-major-cost")
  directory.mkdir(exist_ok=True)
  with tempfile.TemporaryDirectory(prefix="git809-source-vs-installed-") as temp:
    root=Path(temp)
    copies={}
    for name, original in (("installed_git",installed),("source_git",compiled)):
      dst=root/(name+".exe")
      shutil.copy2(original,dst)
      copies[name]=dst
    minimum=(root/"minimal_wmain.exe")
    source=Path("experiments/issue-809/minimal_wmain.c")
    proc=subprocess.run(["gcc","-O2","-municode","-o",str(minimum),str(source)],
                        stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if proc.returncode:
      raise RuntimeError("control compile failed: "+proc.stderr.decode("utf-8","replace"))
    binaries={
      "installed_git_original":installed,
      "installed_git_relocated_copy":copies["installed_git"],
      "source_git_original":compiled,
      "source_git_relocated_copy":copies["source_git"],
      "minimal_gcc_ucrt_process":minimum
    }
    run_args={name:([str(path),"--version"] if "git" in name else [str(path)])
              for name,path in binaries.items()}
    expected={}
    for name,args in run_args.items():
      p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL,check=False,timeout=20)
      if p.returncode:
        raise RuntimeError("control failed: "+name+" "+p.stderr.decode("utf-8","replace")[:200])
      if "git" in name and not p.stdout.startswith(b"git version "):
        raise RuntimeError("Git version invalid: "+name)
      expected[name]=p.stdout
    if expected["installed_git_original"]!=expected["installed_git_relocated_copy"]:
      raise RuntimeError("copy does not preserve installed Git version output")
    if expected["source_git_original"]!=expected["source_git_relocated_copy"]:
      raise RuntimeError("copy does not preserve built Git version output")
    samples={name:[] for name in binaries}
    first_sample={}
    for name,args in run_args.items():
      first_sample[name]=measure(args,expected[name])
    for i in range(154):
      keys=list(binaries)
      if i%2:keys.reverse()
      for name in keys:
        p=measure(run_args[name],expected[name])
        if i>=4:samples[name].append(p)
    binary_data={name:{"path":str(path),"size_bytes":path.stat().st_size,
                       "sha256":sha(path),"version_stdout":expected[name].decode("utf-8","replace").strip()}
                 for name,path in binaries.items()}
  result={
    "experiment":"Git809-main-cost-installed-vs-built-path-and-kernel-lifetime",
    "git_source_sha":os.environ.get("GITHUB_SHA",""),
    "platform":platform.platform(),
    "method":"one Windows runner; 150 interleaved samples per executable; full OS child process and parent launch/wait timings",
    "notes":"Relocated binaries can resolve DLLs and resource paths differently; control checks stdout; cannot independently assign difference to antivirus without ETW.",
    "binaries":binary_data,
    "first_launch":first_sample,
    "summaries":{name:summary(vals) for name,vals in samples.items()},
    "samples":samples}
  (directory/"windows-major-cost.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
  print("RESULT",json.dumps({"git_source_sha":result["git_source_sha"],
    "binary_sha256":{name:data["sha256"] for name,data in binary_data.items()},
    "summary_medians":{name:{key:round(val["median"],4) for key,val in stats.items()}
                       for name,stats in result["summaries"].items()}},indent=2))

if __name__=="__main__":
  main()
