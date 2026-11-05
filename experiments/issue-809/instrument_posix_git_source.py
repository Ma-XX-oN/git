#!/usr/bin/env python3
"""Git #809: source-built POSIX stage controls equivalent to Windows.

Source edits exist only in this throwaway CI checkout. The tested Git source
is identical across OS; only platform entrypoints and system toolchains differ.
"""
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import time


def patch_once(path, old, new):
  content = path.read_text(encoding="utf-8")
  if content.count(old) != 1:
    raise RuntimeError("source marker mismatch: " + str(path) + " count=" + str(content.count(old)))
  path.write_text(content.replace(old, new, 1), encoding="utf-8")


def execute(args, timeout=900, check=True):
  p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     check=False, timeout=timeout)
  if check and p.returncode != 0:
    raise RuntimeError("command failed: " + str(args[0]) + ", exit=" +
                       str(p.returncode) + ", stderr=" +
                       p.stderr.decode("utf-8", "replace")[-1800:])
  return p


def summarize(samples):
  ordered = sorted(samples)
  return {"n": len(samples), "median_ms": statistics.median(ordered),
          "p95_ms": ordered[int((len(samples)-1)*0.95)],
          "samples_ms": samples}


def main():
  if os.name == "nt":
    raise SystemExit("POSIX main() entry control only; Windows uses wmain() controls")
  root = Path.cwd()
  source = root / "common-main.c"
  if not source.is_file():
    raise RuntimeError("not running from Git source root")
  original = ("\tint result;\n\n\tinit_git(argv);\n\tresult = cmd_main(argc, argv);")
  patched = ("\tint result;\n\n"
             "#ifdef GIT809_PROBE\n"
             "\tif (argc == 2 && !strcmp(argv[1], \"--git809-before-init\"))\n"
             "\t\treturn 0;\n"
             "#endif\n"
             "\tinit_git(argv);\n"
             "#ifdef GIT809_PROBE\n"
             "\tif (argc == 2 && !strcmp(argv[1], \"--git809-after-init\"))\n"
             "\t\treturn 0;\n"
             "\tif (argc == 2 && !strcmp(argv[1], \"--git809-after-init-exit\"))\n"
             "\t\texit(0);\n"
             "#endif\n"
             "\tresult = cmd_main(argc, argv);")
  patch_once(source, original, patched)

  # Match Windows probe's build feature selection while allowing native CC.
  make = ["make", "-j4", "NO_RUST=YesPlease",
          "NO_CURL=YesPlease", "NO_GETTEXT=YesPlease",
          "CFLAGS_APPEND=-DGIT809_PROBE", "git"]
  build = execute(make, timeout=1800, check=False)
  binary = root / "git"
  if build.returncode != 0 or not binary.is_file():
    print(build.stdout.decode("utf-8", "replace")[-3500:], flush=True)
    print(build.stderr.decode("utf-8", "replace")[-3500:], flush=True)
    raise RuntimeError("instrumented native Git build failed")

  compiler = shutil.which(os.environ.get("CC", "cc"))
  if not compiler:
    raise RuntimeError("no native C compiler available")
  minimal = root / "experiments" / "issue-809" / "minimal_posix_main"
  control_source = root / "experiments" / "issue-809" / "minimal_posix_main.c"
  execute([compiler, "-O2", "-o", str(minimal), str(control_source)])
  probe = str(binary.resolve())
  cases = {
    "minimal_native_main": ([str(minimal)], b""),
    "before_common_init_git": ([probe, "--git809-before-init"], b""),
    "after_common_init_git_return": ([probe, "--git809-after-init"], b""),
    "after_common_init_git_exit": ([probe, "--git809-after-init-exit"], b""),
  }
  version = execute([probe, "--version"])
  if not version.stdout.startswith(b"git version "):
    raise AssertionError("source-built Git version output does not match")
  cases["full_git_version"] = ([probe, "--version"], version.stdout)

  samples = {key: [] for key in cases}
  for iteration in range(124):
    keys = list(cases)
    if iteration % 2:
      keys.reverse()
    for key in keys:
      args, expected = cases[key]
      start = time.perf_counter_ns()
      p = execute(args, timeout=20)
      elapsed = (time.perf_counter_ns() - start) / 1e6
      if p.stdout != expected:
        raise AssertionError("output mismatch in " + key)
      if iteration >= 4:
        samples[key].append(elapsed)

  data = {
    "experiment": "Git809-POSIX-source-phases",
    "git_source": os.environ.get("GITHUB_SHA", ""),
    "platform": platform.platform(),
    "compiler": compiler,
    "make_command": make,
    "case_order": "interleaved, reversed on odd iterations",
    "note": "POSIX common-main.c entry corresponds to Windows after wmain setup. Windows-only wmain stages have no POSIX counterpart. Same source revision, different platform libraries and toolchains.",
    "cases": {key: summarize(value) for key, value in samples.items()}
  }
  directory = root / "git809-source-results"
  directory.mkdir(exist_ok=True)
  dest = directory / (platform.system().lower() + "-source-phases.json")
  dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
  print(json.dumps({key: {"median_ms": val["median_ms"],
                           "p95_ms": val["p95_ms"]}
                    for key, val in data["cases"].items()}, indent=2))


if __name__ == "__main__":
  main()
