#!/usr/bin/env python3
"""Git #809: isolate gettext runtime stages in one identically linked Git.

Temporary source instrumentation only in the GitHub Actions checkout.
Every trial uses one binary, the same existing locale directory, and
the same ordinary Git --version output. No production code modifications.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import time
import hashlib

ROOT=Path.cwd()
STAGES=["none","dir","bind","messages","time","charset","textdomain","full"]

def patch():
  source=ROOT/"gettext.c"
  original=source.read_text(encoding="utf-8")
  key='void git_setup_gettext(void)\n{\n\tconst char *podir = getenv(GIT_TEXT_DOMAIN_DIR_ENVIRONMENT);\n\tchar *p = NULL;\n'
  repl=key+(
    '\tconst char *stage = getenv("GIT809_GETTEXT_STAGE");\n'
    '\tif (stage && !strcmp(stage, "none"))\n\t\treturn;\n')
  if original.count(key)!=1:
    raise RuntimeError("gettext entry patch marker not unique")
  changed=original.replace(key,repl,1)
  pairs=[
    ('\tbindtextdomain("git", podir);',
     '\tif (stage && !strcmp(stage, "dir")) { free(p); return; }\n'
     '\tbindtextdomain("git", podir);'),
    ('\tsetlocale(LC_MESSAGES, "");',
     '\tif (stage && !strcmp(stage, "bind")) { free(p); return; }\n'
     '\tsetlocale(LC_MESSAGES, "");'),
    ('\tsetlocale(LC_TIME, "");',
     '\tif (stage && !strcmp(stage, "messages")) { free(p); return; }\n'
     '\tsetlocale(LC_TIME, "");'),
    ('\tinit_gettext_charset("git");',
     '\tif (stage && !strcmp(stage, "time")) { free(p); return; }\n'
     '\tinit_gettext_charset("git");'),
    ('\ttextdomain("git");',
     '\tif (stage && !strcmp(stage, "charset")) { free(p); return; }\n'
     '\ttextdomain("git");'),
    ('\tgit_gettext_enabled = 1;',
     '\tif (stage && !strcmp(stage, "textdomain")) { free(p); return; }\n'
     '\tgit_gettext_enabled = 1;')
  ]
  for old,new in pairs:
    if changed.count(old)!=1:
      raise RuntimeError("gettext stage marker not unique: "+old)
    changed=changed.replace(old,new,1)
  source.write_text(changed,encoding="utf-8")
  return hashlib.sha256(original.encode("utf-8")).hexdigest()

def run(args,env=None):
  p=subprocess.run(args,env=env,check=False,cwd=ROOT,
                   stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE,timeout=30)
  if p.returncode!=0:
    raise RuntimeError("Git failed rc="+str(p.returncode)+
                       " stderr="+p.stderr.decode("utf-8","replace")[-400:])
  return p

def median(values):
  sorted_values=sorted(values)
  return {"median_ms":statistics.median(sorted_values),
          "p95_ms":sorted_values[int((len(values)-1)*.95)],
          "samples_ms":values}

def main():
  if os.name!="nt":
    raise RuntimeError("Windows native diagnostic")
  blob=patch()
  proc=subprocess.run(["make","-j4","git.exe"],check=False,
    cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=1800)
  if proc.returncode!=0 or not (ROOT/"git.exe").is_file():
    raise RuntimeError("instrumented Git build failed: "+
      proc.stderr.decode("utf-8","replace")[-2500:])
  exe=str((ROOT/"git.exe").resolve())
  with tempfile.TemporaryDirectory(prefix="git809-gettext-stages-") as td:
    existing=Path(td)/"locale"
    existing.mkdir()
    cases={x:[] for x in STAGES}
    base_env=os.environ.copy()
    base_env["GIT_TEXTDOMAINDIR"]=str(existing)
    for i in range(124):
      names=list(cases)
      if i%2:
        names.reverse()
      for name in names:
        env=base_env.copy()
        env["GIT809_GETTEXT_STAGE"]=name
        start=time.perf_counter_ns()
        p=run([exe,"--version"],env)
        ms=(time.perf_counter_ns()-start)/1e6
        if not p.stdout.startswith(b"git version "):
          raise RuntimeError("unexpected Git version output")
        if i>=4:
          cases[name].append(ms)
  doc={"experiment":"Git809-Windows-gettext-internal-stage",
       "sha":os.environ.get("GITHUB_SHA",""),
       "unmodified_gettext_c_sha256":blob,
       "binary_sha256":hashlib.sha256(Path(exe).read_bytes()).hexdigest(),
       "method":"one temporary instrumented native Git executable, one existing empty locale path, 120 interleaved launch-to-termination samples per stage",
       "stages":{k:median(v) for k,v in cases.items()},
       "limitations":"Early return stages are diagnostic and intentionally do not finish gettext initialization. Source patch is only in disposable CI worktree."}
  output=ROOT/"git809-gettext-stage-results"
  output.mkdir(exist_ok=True)
  (output/"windows-gettext-stages.json").write_text(json.dumps(doc,indent=2)+"\n",encoding="utf-8")
  print("SUMMARY",json.dumps({k:round(v["median_ms"],4) for k,v in doc["stages"].items()},indent=2))
if __name__=="__main__":
  main()
