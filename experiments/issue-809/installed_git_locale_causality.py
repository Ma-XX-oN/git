#!/usr/bin/env python3
"""Issue 809: prove or reject default installed Git locale initialization cost.

Same exact Git for Windows executable, Windows runner, PATH, and commands.
Only GIT_TEXTDOMAINDIR varies. Native full process start-to-termination
wall times with interleaved 150-sample distributions.
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

def execute(args, env):
  start=time.perf_counter_ns()
  p=subprocess.run(args,env=env,stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                   timeout=20,check=False)
  duration=(time.perf_counter_ns()-start)/1e6
  if p.returncode:
    raise RuntimeError("Git failed rc="+str(p.returncode)+" "+p.stderr.decode("utf-8","replace")[:300])
  return p.stdout, duration

def summary(a):
  data=sorted(a)
  return {"median_ms":statistics.median(data),
          "p95_ms":data[int((len(data)-1)*.95)],
          "samples_ms":a}

def main():
  if os.name!="nt":
    raise RuntimeError("native Windows required")
  git=Path(shutil.which("git") or "").resolve()
  if not git.is_file():
    raise RuntimeError("installed Git absent")
  data=git.read_bytes()
  with tempfile.TemporaryDirectory(prefix="git809-locale-default-") as td:
    root=Path(td)
    present=root/"existing-empty-locale"
    present.mkdir()
    missing=root/"missing-locale"
    if missing.exists():
      raise AssertionError("missing path exists")
    prefix=git.parent.parent
    distro_dir=prefix/"share"/"locale"
    repo=root/"repository"
    repo.mkdir()
    base_env=os.environ.copy()
    # Preserve the actual installed environment for the default variant.
    commands={
      "version":[str(git),"--version"],
    }
    scenarios={
      "actual_installed_default":None,
      "force_missing_locale_path":str(missing),
      "force_existing_empty_locale_path":str(present),
    }
    if distro_dir.is_dir():
      scenarios["force_installed_locale_path"]=str(distro_dir)
    baseline,_=execute(commands["version"],base_env)
    if not baseline.startswith(b"git version "):
      raise RuntimeError("Git version output invalid")
    samples={key:[] for key in scenarios}
    warmup={}
    for name,path in scenarios.items():
      env=base_env.copy()
      if path is not None:
        env["GIT_TEXTDOMAINDIR"]=path
      output,ms=execute(commands["version"],env)
      if output!=baseline:
        raise RuntimeError("version output mismatch: "+name)
      warmup[name]=ms
    for i in range(154):
      names=list(samples)
      if i%2:names.reverse()
      for name in names:
        env=base_env.copy()
        if scenarios[name] is not None:
          env["GIT_TEXTDOMAINDIR"]=scenarios[name]
        output,ms=execute(commands["version"],env)
        if output!=baseline:
          raise RuntimeError("byte-exact Git output mismatch: "+name)
        if i>=4:samples[name].append(ms)
    result={
      "experiment":"Git809-installed-git-locale-path-causality",
      "source_sha":os.environ.get("GITHUB_SHA",""),
      "platform":platform.platform(),
      "git_binary_path":str(git),
      "git_binary_sha256":hashlib.sha256(data).hexdigest(),
      "git_version":baseline.decode("utf-8","replace").strip(),
      "installed_locale_directory":str(distro_dir),
      "installed_locale_directory_exists":distro_dir.is_dir(),
      "original_GIT_TEXTDOMAINDIR":base_env.get("GIT_TEXTDOMAINDIR"),
      "original_LANG":base_env.get("LANG"),
      "original_LC_ALL":base_env.get("LC_ALL"),
      "original_LC_CTYPE":base_env.get("LC_CTYPE"),
      "method":"same native installed Git executable; 150 interleaved samples per env, no Git source changes, whole launch-to-exit",
      "scenarios":scenarios,
      "first_invocation_ms":warmup,
      "results":{k:summary(v) for k,v in samples.items()}}
  folder=Path("git809-installed-locale-results")
  folder.mkdir(exist_ok=True)
  (folder/"windows-installed-locale.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
  print("SUMMARY",json.dumps({
    "git_binary_sha256":result["git_binary_sha256"],
    "installed_locale_directory_exists":result["installed_locale_directory_exists"],
    "original_GIT_TEXTDOMAINDIR":result["original_GIT_TEXTDOMAINDIR"],
    "median_ms":{k:round(v["median_ms"],4) for k,v in result["results"].items()}},indent=2))

if __name__=="__main__":
  main()
