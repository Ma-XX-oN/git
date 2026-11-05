#!/usr/bin/env python3
"""Git #809 temporary source-phase fast-path controls (Windows only).

Patches ONLY the ephemeral Actions checkout, never commits Git sources.
Source-marker assertions prevent silent mismatches. Result is an
instrumented diagnostic binary, NOT a replacement for installed Git.
"""
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time

def patch_once(path, before, after):
  old = path.read_text(encoding="utf-8")
  if old.count(before) != 1:
    raise RuntimeError(str(path) + ": expected unique instrumentation marker, found " + str(old.count(before)))
  new = old.replace(before, after, 1)
  if new == old:
    raise AssertionError("no code change made")
  path.write_text(new, encoding="utf-8")

def run(command, env=None, timeout=1800, check=True):
  return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        env=env, timeout=timeout, check=check)

def stats(samples):
  a=sorted(samples)
  return {"n":len(a), "median_ms":statistics.median(a),
          "p95_ms":a[int((len(a)-1)*.95)], "samples_ms":samples}

def benchmark(name, args, expected_output, n=120):
  measured=[]
  for i in range(4+n):
    t=time.perf_counter_ns()
    result=run(args,timeout=20)
    ms=(time.perf_counter_ns()-t)/1e6
    if result.stdout != expected_output:
      raise RuntimeError(name + " output mismatch: " + repr(result.stdout[:100]))
    if i>=4:
      measured.append(ms)
  return stats(measured)

def main():
  if os.name != "nt":
    raise SystemExit("Windows-only source probe")
  root = Path.cwd()
  mingw = root / "compat" / "mingw.c"
  common = root / "common-main.c"
  if not mingw.is_file() or not common.is_file():
    raise RuntimeError("missing Git source files; execute from Git source root")
  before = ("int wmain(int argc, const wchar_t **wargv)\n"
            "{\n\tint i, exit_status;\n\tsize_t maxlen;\n\tchar *buffer, **save;\n"
            "\tconst char **argv;\n\n\ttrace2_initialize_clock();")
  after = before.replace("\n\ttrace2_initialize_clock();",
    '\n#ifdef GIT809_PROBE\n\tif (argc == 2 && !wcscmp(wargv[1], L"--git809-early-wmain"))\n\t\treturn 0;\n#endif\n\ttrace2_initialize_clock();')
  patch_once(mingw, before, after)
  before = "\t/* invoke the real main() using our utf8 version of argv. */\n\texit_status = main(argc, argv);"
  after = ('#ifdef GIT809_PROBE\n\tif (argc == 2 && !strcmp(argv[1], "--git809-after-wmain"))\n\t\treturn 0;\n#endif\n' + before)
  patch_once(mingw, before, after)
  before = "\tint result;\n\n\tinit_git(argv);\n\tresult = cmd_main(argc, argv);"
  after = ('\tint result;\n\n'
           '#ifdef GIT809_PROBE\n'
           '\tif (argc == 2 && !strcmp(argv[1], "--git809-before-init"))\n'
           '\t\treturn 0;\n#endif\n'
           '\tinit_git(argv);\n'
           '#ifdef GIT809_PROBE\n'
           '\tif (argc == 2 && !strcmp(argv[1], "--git809-after-init"))\n'
           '\t\treturn 0;\n'
           '\tif (argc == 2 && !strcmp(argv[1], "--git809-after-init-exit"))\n'
           '\t\texit(0);\n#endif\n'
           '\tresult = cmd_main(argc, argv);')
  patch_once(common, before, after)
  target=root/"git.exe"
  command=["make","-j4","NO_RUST=YesPlease","CFLAGS_APPEND=-DGIT809_PROBE","git.exe"]
  build=run(command,timeout=1800,check=False)
  if build.returncode or not target.is_file():
    print("build stdout tail:",build.stdout.decode("utf-8","replace")[-4500:])
    print("build stderr tail:",build.stderr.decode("utf-8","replace")[-4500:])
    raise RuntimeError("Git source instrumentation build failed with rc="+str(build.returncode))
  # Validate every instrumented early-exit and the actual Git version output.
  base=str(target.resolve())
  cases={
    "loader_and_crt_to_wmain":([base,"--git809-early-wmain"],b""),
    "through_windows_wmain_setup":([base,"--git809-after-wmain"],b""),
    "through_call_to_main":([base,"--git809-before-init"],b""),
    "through_common_git_init":([base,"--git809-after-init"],b""),
    "through_common_init_with_git_exit":([base,"--git809-after-init-exit"],b""),
  }
  output={}
  for name,(argv,expected) in cases.items():
    output[name]=benchmark(name,argv,expected)
  version_args=[base,"--version"]
  version=run(version_args,timeout=30)
  if not version.stdout.startswith(b"git version "):
    raise RuntimeError("git version failed in source built binary")
  output["full_git_version"]=benchmark("full_git_version",version_args,version.stdout)
  dest=Path("git809-source-results")
  dest.mkdir(exist_ok=True)
  record={
    "experiment":"Git-809-ephemeral-compiled-source-stage-bypass",
    "source_sha":os.environ.get("GITHUB_SHA",""),
    "compiler_command":command,
    "note":"Each early exit bypasses more Git source; measurements include launcher, Windows loader, CRT, and relevant return-path teardown. Diagnostic binary is NOT equivalent to installed Git for Windows binary. Timings are non-additive and instrumented return paths differ from normal Git exit.",
    "stages":output}
  (dest/"windows-source-phases.json").write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
  print(json.dumps({k:{"median_ms":v["median_ms"],"p95_ms":v["p95_ms"]}
                    for k,v in output.items()},indent=2))

if __name__=="__main__":
  main()
