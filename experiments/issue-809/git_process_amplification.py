#!/usr/bin/env python3
"""Git #809: major fixed cost from thousands of independent Git processes.

Same repository, exact same object requests/answers and Git binary.
Compare one-shot Git process per request vs Git's existing batch protocol.
No Git production code modifications.
"""
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

COUNT = 250
ROUNDS = 7

def execute(cmd, *, cwd, input=None):
  start=time.perf_counter_ns()
  p=subprocess.run(cmd, cwd=cwd, input=input,stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE,check=False,timeout=40)
  duration=(time.perf_counter_ns()-start)/1e6
  if p.returncode:
    raise RuntimeError("failed command: "+repr(cmd)+" "+p.stderr.decode("utf-8","replace")[-200:])
  return p.stdout,duration

def summary(arr):
  return {"median_ms":statistics.median(arr),
          "min_ms":min(arr),"max_ms":max(arr),"samples_ms":arr}

def main():
  git=shutil.which("git")
  if not git:raise RuntimeError("Git missing")
  git=str(Path(git).resolve())
  with tempfile.TemporaryDirectory(prefix="git809-batch-") as temp:
    root=Path(temp)
    execute([git,"init","-q","repo"],cwd=root)
    repo=root/"repo"
    for n in range(COUNT):
      (repo/("object-%03d.txt"%n)).write_text("Unique Git 809 payload %d\\n"%n,encoding="utf-8")
    execute([git,"add","-A"],cwd=repo)
    execute([git,"-c","user.name=Probe","-c","user.email=probe@example.invalid",
             "commit","-qm","fixture"],cwd=repo)
    listed,_=execute([git,"ls-files","--stage"],cwd=repo)
    oids=[line.split()[1] for line in listed.splitlines()]
    if len(oids)!=COUNT or len(set(oids))!=COUNT:
      raise RuntimeError("fixture object ids not unique")
    baseline,_=execute([git,"cat-file","-t",oids[0].decode("ascii")],cwd=repo)
    if baseline!=b"blob\n":raise RuntimeError("unexpected baseline object type")
    requests=b"".join(oid+b"\n" for oid in oids)
    batch_expected=baseline*COUNT
    separate=[]
    batched=[]
    # Warm cold caches then alternate round order.
    for i in range(ROUNDS+1):
      types = ("separate","batch") if i%2==0 else ("batch","separate")
      for mode in types:
        start=time.perf_counter_ns()
        if mode=="separate":
          for oid in oids:
            out,_=execute([git,"cat-file","-t",oid.decode("ascii")],cwd=repo)
            if out!=baseline:raise RuntimeError("object type changed")
        else:
          out,_=execute([git,"cat-file","--batch-check=%(objecttype)"],
                        cwd=repo,input=requests)
          if out!=batch_expected:raise RuntimeError("batched object result not byte-identical")
        elapsed=(time.perf_counter_ns()-start)/1e6
        if i>0:
          (separate if mode=="separate" else batched).append(elapsed)
    result={
      "experiment":"Git809-unique-objects-process-lifetime-amplification",
      "git_binary":git,
      "git_binary_sha256":hashlib.sha256(Path(git).read_bytes()).hexdigest(),
      "git_version":execute([git,"--version"],cwd=repo)[0].decode("utf-8","replace").strip(),
      "sha":os.environ.get("GITHUB_SHA",""),
      "platform":platform.platform(),
      "objects_per_round":COUNT,
      "rounds":ROUNDS,
      "semantic_constraint":"Both modes request 250 distinct blob IDs and byte-identical answers; this eliminates repeated-HEAD caching as a sole explanation; not every Git operation has a batch API.",
      "measurements":{
        "separate_process_per_request":summary(separate),
        "one_git_batch_process":summary(batched)
      },
      "median_speedup":statistics.median(separate)/statistics.median(batched)
    }
  path=Path("git809-batch-results")
  path.mkdir(exist_ok=True)
  (path/(platform.system().lower()+"-unique.json")).write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
  print("RESULT",json.dumps({"platform":result["platform"],"git_version":result["git_version"],
   "processes_per_round":COUNT,"separate_ms":round(statistics.median(separate),4),
   "single_batch_ms":round(statistics.median(batched),4),
   "speedup_x":round(result["median_speedup"],2)},indent=2))

if __name__=="__main__":
  main()
