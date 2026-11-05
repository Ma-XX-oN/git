#!/usr/bin/env python3
"""Issue 809: same-run Git for Windows one-factor-at-a-time build matrix.

Changes made to Git's Makefile are temporary and restored, and only the
Actions checkout is built. No Git production source is committed or changed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import tempfile
import time

ROOT = Path.cwd()
RESULTS = ROOT / "git809-build-matrix-results"
OPTIONS = {
  "A_default": [],
  "B_no_rust": ["NO_RUST=YesPlease"],
  "C_no_gettext": ["NO_GETTEXT=YesPlease"],
  "D_no_curl": ["NO_CURL=YesPlease"],
  "E_all_three_off": ["NO_RUST=YesPlease", "NO_GETTEXT=YesPlease",
                      "NO_CURL=YesPlease"],
}
N = 120


def command(args, *, cwd=ROOT, timeout=1800, verify=True):
  proc = subprocess.run(args, cwd=cwd, check=False, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=timeout)
  if verify and proc.returncode != 0:
    raise RuntimeError("exit=" + str(proc.returncode) +
                       " argv_head=" + repr(args[:2]) + " stderr=" +
                       proc.stderr.decode("utf-8", "replace")[-1600:])
  return proc


def digest(path):
  h = hashlib.sha256()
  with path.open("rb") as stream:
    for chunk in iter(lambda: stream.read(65536), b""):
      h.update(chunk)
  return h.hexdigest()


def imports(path):
  import pefile
  pe = pefile.PE(str(path), fast_load=True)
  try:
    pe.parse_data_directories(
      directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    dlls = [x.dll.decode("ascii", "replace").lower()
            for x in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]
  finally:
    pe.close()
  if not dlls:
    raise RuntimeError("no PE DLL imports found")
  return sorted(set(dlls))


def archive_info(name):
  output = {"link_option": name}
  p = command(["gcc", "-print-file-name=" + name])
  candidate = Path(p.stdout.decode("utf-8", "replace").strip())
  if not candidate.is_file():
    output["status"] = "not-found"
    return output
  output.update({"status": "found", "path": str(candidate),
                 "size": candidate.stat().st_size,
                 "sha256": digest(candidate)})
  members = command(["ar", "t", str(candidate)], timeout=120, verify=False)
  if members.returncode == 0:
    member_names = members.stdout.decode("utf-8", "replace").splitlines()
    output["archive_member_count"] = len(member_names)
    output["archive_member_prefixes"] = member_names[:12]
  else:
    output["archive_list_error"] = members.stderr.decode("utf-8", "replace")[-500:]
  return output


def build(label, options, makefile_patch=None):
  info = {"options": options.copy(), "status": "started"}
  started = time.perf_counter()
  clean = command(["make", "clean"], timeout=180, verify=False)
  if clean.returncode:
    raise RuntimeError("make clean failed " +
                       clean.stderr.decode("utf-8", "replace")[-1400:])
  target = ROOT / "git.exe"
  if target.exists():
    raise RuntimeError("git.exe remained after make clean")
  makefile = ROOT / "Makefile"
  original = makefile.read_text(encoding="utf-8")
  try:
    if makefile_patch:
      for before, after in makefile_patch:
        if original.count(before) != 1:
          raise RuntimeError("Makefile patch marker missing/not unique: " + repr(before))
        current = makefile.read_text(encoding="utf-8")
        if current.count(before) != 1:
          raise RuntimeError("Makefile patch marker missing after previous edit")
        makefile.write_text(current.replace(before, after, 1), encoding="utf-8")
    args = ["make", "-j4", *options, "git.exe"]
    proc = command(args, timeout=2400, verify=False)
    info.update({"make_command": args, "build_seconds": round(time.perf_counter() - started, 2),
                 "make_stdout_tail": proc.stdout.decode("utf-8", "replace")[-1500:],
                 "make_stderr_tail": proc.stderr.decode("utf-8", "replace")[-1500:]})
    if proc.returncode or not target.is_file():
      info["status"] = "build-failed"
      info["returncode"] = proc.returncode
      return None, info
    dest = RESULTS / label
    dest.mkdir(exist_ok=True, parents=True)
    copy = dest / "git.exe"
    shutil.copy2(target, copy)
    info.update({"status": "built", "binary": str(copy), "size_bytes": copy.stat().st_size,
                 "sha256": digest(copy), "dll_imports": imports(copy)})
    return copy, info
  finally:
    if makefile.read_text(encoding="utf-8") != original:
      makefile.write_text(original, encoding="utf-8")


def static_link_recipe():
  z = archive_info("libz.a")
  ic = archive_info("libiconv.a")
  info = {"zlib": z, "iconv": ic}
  if any(x["status"] != "found" for x in (z, ic)):
    info["status"] = "static-archive-not-found"
    return None, info
  # Explicit archive filenames; require observed DLL import removal to
  # establish true static link. Import archives do not count.
  patches = [
    ("\tEXTLIBS += -lz\n", "\tEXTLIBS += " + z["path"].replace("\\", "/") + "\n"),
    ("\t\tEXTLIBS += $(ICONV_LINK) -liconv\n",
     "\t\tEXTLIBS += $(ICONV_LINK) " + ic["path"].replace("\\", "/") + "\n"),
  ]
  info["status"] = "archives-identified"
  return patches, info


def make_fixture(git, workspace):
  repo = workspace / "repo"
  repo.mkdir()
  def invoke(*items):
    command([str(git), "-C", str(repo), *items], timeout=45)
  invoke("init", "-q")
  (repo / "tracked.txt").write_text("Git809 fixture\n", encoding="utf-8")
  invoke("add", "tracked.txt")
  invoke("-c", "user.name=Git809", "-c", "user.email=git809@example.invalid",
         "commit", "-qm", "fixture")
  return repo


def cases(git, repo):
  return {
    "version": [str(git), "--version"],
    "version_build_options": [str(git), "--version", "--build-options"],
    "rev_parse": [str(git), "-C", str(repo), "rev-parse", "HEAD"],
    "status": [str(git), "-C", str(repo), "status", "--porcelain"],
    "log": [str(git), "-C", str(repo), "log", "-1", "--format=%H"],
  }


def measure(binaries, report):
  baseline = binaries.get("A_default")
  if baseline is None:
    report["measurement_status"] = "no-default-baseline"
    return
  with tempfile.TemporaryDirectory(prefix="git809-matrix-") as td:
    repo = make_fixture(baseline, Path(td))
    all_cases = {label: cases(exe, repo) for label, exe in binaries.items()}
    expected = {k: command(argv, timeout=30).stdout
                for k, argv in all_cases["A_default"].items()}
    compatible = {}
    for label, commands in all_cases.items():
      ok = True
      for name, argv in commands.items():
        p = command(argv, timeout=30, verify=False)
        # The --build-options command legitimately reports feature settings.
        strict = name != "version_build_options"
        eq = p.returncode == 0 and (not strict or p.stdout == expected[name])
        report["builds"][label].setdefault("checks", {})[name] = {
          "pass": eq, "exit_code": p.returncode,
          "stdout_equal_to_default": p.stdout == expected[name],
        }
        ok &= eq
      if ok:
        compatible[label] = commands
      else:
        report["builds"][label]["status"] = "behavior-mismatch"
    values = {(label, name): [] for label in compatible for name in expected}
    # Alternate traversal to minimize sample order bias.
    for i in range(N + 4):
      keys = list(values)
      if i % 2:
        keys.reverse()
      for label, name in keys:
        t0 = time.perf_counter_ns()
        p = command(compatible[label][name], timeout=30, verify=False)
        dt_ms = (time.perf_counter_ns() - t0) / 1_000_000
        if p.returncode:
          raise RuntimeError("benchmark command failed " + label + "/" + name)
        if name != "version_build_options" and p.stdout != expected[name]:
          raise RuntimeError("benchmark output mismatch " + label + "/" + name)
        if i >= 4:
          values[(label, name)].append(dt_ms)
    report["measurement_status"] = "completed"
    report["measurements"] = {}
    for (label, name), durations in values.items():
      s = sorted(durations)
      report["measurements"].setdefault(label, {})[name] = {
        "n": len(s), "median_ms": statistics.median(s),
        "p95_ms": s[int(.95 * (len(s)-1))],
        "samples_ms": durations,
      }


def main():
  if os.name != "nt":
    raise RuntimeError("This experiment requires the Git for Windows SDK")
  RESULTS.mkdir(exist_ok=True)
  revision = command(["git", "rev-parse", "HEAD"]).stdout.decode().strip()
  report = {"experiment": "Git809-Windows-one-factor-at-a-time",
            "source_sha": revision, "github_sha": os.environ.get("GITHUB_SHA", ""),
            "method": "Same Windows runner, same Git source/toolchain; clean build per variant; "
                      "120 interleaved full subprocess lifetimes per operation.",
            "builds": {}, "measurements": {}}
  binaries = {}
  try:
    for label, flags in OPTIONS.items():
      try:
        binary, details = build(label, flags)
        report["builds"][label] = details
        if binary is not None:
          binaries[label] = binary
      except Exception as error:
        report["builds"][label] = {"status": "build-error",
                                   "error": type(error).__name__ + ": " + str(error)}
    recipe, static = static_link_recipe()
    report["static_archive_analysis"] = static
    if recipe:
      try:
        binary, details = build("F_static_zlib_iconv", OPTIONS["E_all_three_off"], recipe)
        report["builds"]["F_static_zlib_iconv"] = details
        if binary is not None:
          prev = report["builds"].get("E_all_three_off", {})
          removed = sorted(set(prev.get("dll_imports", [])) -
                           set(details.get("dll_imports", [])))
          report["static_archive_analysis"]["removed_dll_imports"] = removed
          if {"zlib1.dll", "libiconv-2.dll"}.isdisjoint(details["dll_imports"]):
            report["static_archive_analysis"]["status"] = "confirmed-static-target-dependencies"
            binaries["F_static_zlib_iconv"] = binary
          else:
            report["static_archive_analysis"]["status"] = "not-static-imports-still-present"
        else:
          report["static_archive_analysis"]["status"] = "static-build-failed"
      except Exception as error:
        report["builds"]["F_static_zlib_iconv"] = {
          "status": "build-error", "error": type(error).__name__ + ": " + str(error)}
        report["static_archive_analysis"]["status"] = "static-build-error"
    measure(binaries, report)
  finally:
    out = RESULTS / "windows-feature-matrix.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("RESULTS", out)
    print("SUMMARY", json.dumps({
      "sha": revision,
      "builds": {label: {"status": d.get("status"), "options": d.get("options"),
                          "dll_imports": d.get("dll_imports"),
                          "error": d.get("error"),
                          "make_stderr_tail": d.get("make_stderr_tail", "")[-900:]}
                 for label, d in report["builds"].items()},
      "static": report.get("static_archive_analysis"),
      "measurement_status": report.get("measurement_status"),
      "medians": {k: {op: round(v["median_ms"], 4) for op, v in val.items()}
                  for k, val in report.get("measurements", {}).items()},
    }, indent=2), flush=True)


if __name__ == "__main__":
  main()
