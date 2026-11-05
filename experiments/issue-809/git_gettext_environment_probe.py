#!/usr/bin/env python3
"""Issue 809: isolate Git gettext setup costs through the existing env API.

Uses a SINGLE normal Git executable on ONE native Windows runner, so
linker, dependency imports, runtime and source identity remain unchanged.
No Git production source modifications or patching.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import time

ROOT = Path.cwd()

def run(argv, env=None, timeout=30):
  p = subprocess.run(argv, env=env, cwd=ROOT, check=False,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     stdin=subprocess.DEVNULL, timeout=timeout)
  if p.returncode:
    raise RuntimeError("Git command failed rc=" + str(p.returncode) +
                       ", stderr=" + p.stderr.decode("utf-8","replace")[-300:])
  return p

def mark(name, values):
  seq=sorted(values)
  return {"median_ms":statistics.median(seq),
          "p95_ms":seq[int((len(seq)-1)*.95)],
          "samples_ms":values}

def main():
  if os.name != "nt":
    raise RuntimeError("native Windows Git SDK required")
  binary=(ROOT/"git.exe").resolve()
  if not binary.is_file():
    raise RuntimeError("build native Git executable before environment test")
  with tempfile.TemporaryDirectory(prefix="git809-gettextenv-") as td:
    root=Path(td)
    absent=root/"nonexistent"
    if absent.exists():
      raise RuntimeError("missing path unexpectedly exists")
    empty=root/"existing-empty-locale"
    empty.mkdir()
    source_dir=root/"repo"
    source_dir.mkdir()
    run([str(binary), "-C", str(source_dir), "init", "-q"])
    (source_dir/"one.txt").write_text("fixture\n",encoding="utf-8")
    run([str(binary), "-C", str(source_dir), "add", "one.txt"])
    run([str(binary), "-C", str(source_dir), "-c", "user.name=Git809",
         "-c", "user.email=git809@example.invalid", "commit", "-qm", "fixture"])
    cases={
      "default_unset": None,
      "explicit_empty": "",
      "explicit_nonexistent": str(absent),
      "explicit_existing_directory": str(empty)
    }
    commands={
      "version":[str(binary),"--version"],
      "rev_parse":[str(binary),"-C",str(source_dir),"rev-parse","HEAD"],
      "status":[str(binary),"-C",str(source_dir),"status","--porcelain"]
    }
    original_env=os.environ.copy()
    original_env.pop("GIT_TEXTDOMAINDIR",None)
    samples={(case,cmd):[] for case in cases for cmd in commands}
    ref={cmd:run(argv,original_env).stdout for cmd,argv in commands.items()}
    for i in range(124):
      order=list(samples)
      if i%2:
        order.reverse()
      for case,cmd in order:
        env=original_env.copy()
        if cases[case] is not None:
          env["GIT_TEXTDOMAINDIR"]=cases[case]
        t0=time.perf_counter_ns()
        p=run(commands[cmd],env)
        elapsed=(time.perf_counter_ns()-t0)/1e6
        if p.stdout!=ref[cmd]:
          raise RuntimeError("unexpected output difference: "+case+"/"+cmd)
        if i>=4:
          samples[(case,cmd)].append(elapsed)
    results={}
    for (case,cmd),vals in samples.items():
      results.setdefault(case,{})[cmd]=mark(case,vals)
  import pefile
  img=pefile.PE(str(binary),fast_load=True)
  try:
    img.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    libs=sorted({entry.dll.decode("ascii","replace").lower()
                  for entry in img.DIRECTORY_ENTRY_IMPORT})
  finally:
    img.close()
  if "libintl-8.dll" not in libs:
    raise RuntimeError("normal Git binary lacks expected gettext DLL import")
  doc={
    "experiment":"Git809-gettext-env-single-binary",
    "sha":os.environ.get("GITHUB_SHA",""),
    "git_binary_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),
    "dll_imports":libs,
    "case_semantics":{"default_unset":"Git compiled default locale path",
                      "explicit_empty":"skip directory lookup via empty env path",
                      "explicit_nonexistent":"check a known absent absolute path",
                      "explicit_existing_directory":"check existing dir and initialize gettext"},
    "method":"same native Git executable, 4 environment settings, 3 commands, 120 interleaved samples per case",
    "results":results
  }
  out=ROOT/"git809-gettext-env-results"
  out.mkdir(exist_ok=True)
  (out/"windows-gettext-env.json").write_text(json.dumps(doc,indent=2)+"\n",encoding="utf-8")
  print("SUMMARY",json.dumps({"sha":doc["sha"],"binary_sha256":doc["git_binary_sha256"],
   "medians":{case:{op:round(v["median_ms"],4) for op,v in vals.items()}
              for case,vals in results.items()}},indent=2))

if __name__=="__main__":
  main()
