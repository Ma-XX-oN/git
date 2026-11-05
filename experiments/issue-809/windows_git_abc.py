#!/usr/bin/env python3
"""Issue 809: A/B/C Windows Git binary linking and version-path experiment.

All modifications are temporary in the GitHub Actions checkout.
A: ordinary Git SDK dynamic link; B: explicit archives for zlib/iconv;
C: unchanged A linkage with a narrowly gated --version fast path.
"""
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path.cwd()
OUT = ROOT / "git809-abc-results"
BASE_MAKE = [
  "make", "-j4", "NO_RUST=YesPlease", "NO_CURL=YesPlease",
  "NO_GETTEXT=YesPlease", "git.exe"
]


def execute(args, *, timeout=1800, env=None, check=True, cwd=ROOT):
  p = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     timeout=timeout, check=False)
  if check and p.returncode:
    raise RuntimeError("failed command " + repr(args[:2]) +
                       ", code " + str(p.returncode) + ", stderr " +
                       p.stderr.decode("utf-8", "replace")[-2500:])
  return p


def prepare():
  if os.name != "nt":
    raise RuntimeError("Windows SDK required")
  OUT.mkdir(exist_ok=True)
  source = ROOT / "common-main.c"
  maker = ROOT / "Makefile"
  if not source.is_file() or not maker.is_file():
    raise RuntimeError("missing Git checkout")
  return source, maker


def build_variant(name):
  result = execute(BASE_MAKE, check=False)
  git = ROOT / "git.exe"
  if result.returncode or not git.is_file():
    raise RuntimeError("Build " + name + " failed: " +
                       result.stderr.decode("utf-8", "replace")[-5000:])
  folder = OUT / name
  folder.mkdir(exist_ok=True)
  copy = folder / "git.exe"
  shutil.copy2(git, copy)
  if not copy.is_file() or copy.stat().st_size == 0:
    raise AssertionError("missing output binary")
  return copy


def check_imports(path):
  result = execute(["objdump", "-p", str(path)])
  dlls = re.findall(r"^\s*DLL Name:\s*(\S+)", result.stdout.decode("utf-8", "replace"),
                    flags=re.MULTILINE)
  if not dlls:
    raise AssertionError("PE import listing unexpectedly empty")
  return sorted(set(x.lower() for x in dlls))


def static_lib(name):
  result = execute(["gcc", "-print-file-name=" + name])
  value = result.stdout.decode("utf-8", "replace").strip()
  candidate = Path(value)
  if not candidate.is_file() or not candidate.name.endswith(".a"):
    return None
  # Import archives must not be reported as full static linkage.
  inspection = execute(["nm", "-g", str(candidate)], check=False, timeout=90)
  return str(candidate.resolve()) if inspection.returncode == 0 else None


def patch_unique(path, before, after):
  original = path.read_text(encoding="utf-8")
  if original.count(before) != 1:
    raise RuntimeError("unexpected Makefile/source marker in " + str(path) +
                       " occurrences=" + str(original.count(before)))
  path.write_text(original.replace(before, after, 1), encoding="utf-8")


def build_B(maker):
  zlib = static_lib("libz.a")
  iconv = static_lib("libiconv.a")
  info = {"zlib_archive": zlib, "iconv_archive": iconv}
  if not zlib or not iconv:
    info["status"] = "UNAVAILABLE: full static archives not both present"
    return None, info
  original = maker.read_text(encoding="utf-8")
  try:
    patch_unique(maker, "\tEXTLIBS += -lz\n", "\tEXTLIBS += " + zlib + "\n")
    #  Git Makefile uses one native linker -liconv site for NEEDS_LIBICONV.
    patch_unique(maker, "\t\tEXTLIBS += $(ICONV_LINK) -liconv\n",
                 "\t\tEXTLIBS += $(ICONV_LINK) " + iconv + "\n")
    bin_path = build_variant("B_static_zlib_iconv")
  finally:
    maker.write_text(original, encoding="utf-8")
  info["status"] = "built; PE imports must prove removal"
  return bin_path, info


def build_C(source):
  original = source.read_text(encoding="utf-8")
  # Version-only shortcut still calls the exact normal cmd_main and common_exit.
  # Other Git commands continue to run init_git unmodified.
  before = "\tinit_git(argv);\n\tresult = cmd_main(argc, argv);"
  after = (
    '\tif (argc == 2 && !strcmp(argv[1], "--version")) {\n'
    '\t\tresult = cmd_main(argc, argv);\n'
    '\t\texit(result);\n'
    '\t}\n' + before
  )
  try:
    patch_unique(source, before, after)
    return build_variant("C_version_early_dispatch")
  finally:
    source.write_text(original, encoding="utf-8")


def fixture(git, folder):
  repo = folder / "fixture"
  repo.mkdir(parents=True)
  execute([str(git), "-C", str(repo), "init", "-q"])
  (repo / "tracked.txt").write_text("same bytes\n", encoding="utf-8")
  execute([str(git), "-C", str(repo), "add", "tracked.txt"])
  execute([str(git), "-C", str(repo), "-c", "user.name=Git809",
           "-c", "user.email=git809@example.invalid",
           "commit", "-qm", "fixture"])
  return repo


def commands(git, repo):
  p = str(git)
  d = str(repo)
  return {
    "version": [p, "--version"],
    "version_build_options": [p, "--version", "--build-options"],
    "rev_parse": [p, "-C", d, "rev-parse", "HEAD"],
    "status": [p, "-C", d, "status", "--porcelain"],
    "log": [p, "-C", d, "log", "-1", "--format=%H"]
  }


def benchmark(paths, folder):
  # All variant invocations read one immutable repository fixture.
  repo = fixture(paths["A_dynamic"], folder)
  sets = {name: commands(path, repo) for name, path in paths.items()}
  reference = {name: execute(cmd).stdout for name, cmd in sets["A_dynamic"].items()}
  for variant, mapping in sets.items():
    for name, cmd in mapping.items():
      p = execute(cmd)
      if p.stdout != reference[name]:
        raise AssertionError("stdout differs: " + variant + "/" + name)
  measurements = {(variant, name): [] for variant in sets for name in reference}
  # 4 warmups and 120 paired/interleaved rounds.
  for i in range(124):
    items = list(measurements)
    if i % 2:
      items.reverse()
    for variant, name in items:
      start = time.perf_counter_ns()
      p = execute(sets[variant][name], timeout=30)
      ms = (time.perf_counter_ns() - start) / 1e6
      if p.stdout != reference[name]:
        raise AssertionError("output changed during measurement")
      if i >= 4:
        measurements[(variant, name)].append(ms)
  summary = {}
  for (variant, name), values in measurements.items():
    sorted_values = sorted(values)
    summary.setdefault(variant, {})[name] = {
      "median_ms": statistics.median(values),
      "p95_ms": sorted_values[int((len(values)-1)*0.95)],
      "samples_ms": values,
    }
  return summary


def main():
  source, maker = prepare()
  a = build_variant("A_dynamic")
  imports_a = check_imports(a)
  b, static_details = build_B(maker)
  c = build_C(source)
  # IMPORTANT: source restoration does not restore compiled common-main.o,
  # but no more builds happen in this isolated workflow after C.
  paths = {"A_dynamic": a, "C_version_early_dispatch": c}
  if b is not None:
    paths["B_static_zlib_iconv"] = b
  imports = {name: check_imports(path) for name, path in paths.items()}
  if b is not None:
    removed = sorted(set(imports_a) - set(imports["B_static_zlib_iconv"]))
    static_details["removed_dll_imports"] = removed
    if not removed:
      static_details["status"] = "STATIC LINK NOT VERIFIED: PE imports unchanged"
  data = {
    "experiment": "Git809-Windows-ABC-dynamic-static-minimal-version",
    "sha": os.environ.get("GITHUB_SHA", ""),
    "build_flags": BASE_MAKE,
    "variants": {name: {"binary_size":path.stat().st_size,
                        "dll_imports":imports[name]} for name, path in paths.items()},
    "static": static_details,
    "limitations": [
      "C early dispatch is only accepted for exact --version, all other commands use normal path.",
      "A/B use identical compiled source objects; linker changes only for B.",
      "Cross-platform binary/build configuration equivalence is not claimed.",
      "No production source modifications, test results cover representative operations only."
    ]
  }
  with tempfile.TemporaryDirectory(prefix="git809-abc-") as td:
    data["measurements"] = benchmark(paths, Path(td))
  output = OUT / "windows-abc.json"
  output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
  print("SUMMARY", json.dumps({
    "source": data["sha"], "static": data["static"],
    "dll_imports": {k: v["dll_imports"] for k, v in data["variants"].items()},
    "medians": {k:{op:round(v["median_ms"],4) for op,v in ops.items()}
               for k,ops in data["measurements"].items()}}, indent=2))


if __name__ == "__main__":
  main()
