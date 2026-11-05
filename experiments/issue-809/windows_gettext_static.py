#!/usr/bin/env python3
"""Git #809: gettext-preserving native static-link attribution.

Builds all variants from the same unmodified Git source on one Windows
Git SDK runner. Every build is clean, records PE import DLLs and exact SHA.
This tests linking, not source-level removal of translation behavior.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import statistics
import sys

from windows_feature_matrix import (
  ROOT, RESULTS, archive_info, build, command, imports, make_fixture,
  measure,
)

def require_archive(name):
  result = archive_info(name)
  if result["status"] != "found":
    raise RuntimeError("required full static archive unavailable: " + name)
  if result.get("archive_member_count", 0) < 2:
    raise RuntimeError("archive has too few object members: " + name)
  return result

def replacement(archive, library_flag):
  if library_flag == "-lintl":
    return [
      ("\tEXTLIBS += -lintl\n",
       "\tEXTLIBS += " + archive["path"].replace("\\", "/") + "\n"),
  ]
  if library_flag == "-lz":
    return [
      ("\tEXTLIBS += -lz\n",
       "\tEXTLIBS += " + archive["path"].replace("\\", "/") + "\n"),
  ]
  if library_flag == "-liconv":
    return [
      ("\t\tEXTLIBS += $(ICONV_LINK) -liconv\n",
       "\t\tEXTLIBS += $(ICONV_LINK) " +
       archive["path"].replace("\\", "/") + "\n"),
  ]
  raise ValueError("unknown link flag")

def validate_library_contract(binaries, report):
  for label, exe in binaries.items():
    checks = {}
    for lang in ("C", "fr_FR.UTF-8", "de_DE.UTF-8"):
      env = os.environ.copy()
      env.update({"LANG": lang, "LC_ALL": lang})
      # Confirm smoke paths still run with native gettext linked even if
      # message catalogs are not provisioned on the hosted CI environment.
      calls = [
        [str(exe), "--version"],
        [str(exe), "--help"],
        [str(exe), "--not-a-real-command"],
      ]
      checks[lang] = []
      for argv in calls:
        p = command(argv, verify=False, timeout=30)
        checks[lang].append({"args": argv[1:],
                             "exit": p.returncode,
                             "stdout_sha": __import__("hashlib").sha256(p.stdout).hexdigest(),
                             "stderr_sha": __import__("hashlib").sha256(p.stderr).hexdigest()})
    report["builds"][label]["locale_smoke"] = checks
  baseline = report["builds"]["A_default"]["locale_smoke"]
  for label in binaries:
    if label == "A_default":
      continue
    for locale in baseline:
      for index, (a, b) in enumerate(zip(baseline[locale], report["builds"][label]["locale_smoke"][locale])):
        if a["exit"] != b["exit"] or a["stdout_sha"] != b["stdout_sha"] or a["stderr_sha"] != b["stderr_sha"]:
          raise AssertionError(label + " behavior mismatch under " + locale +
                               " case " + str(index))

def main():
  if os.name != "nt":
    raise RuntimeError("requires native Windows Git SDK")
  target = RESULTS / "windows-gettext-static.json"
  report = {"experiment": "Git809-Windows-gettext-preserving-static",
            "sha": command(["git","rev-parse","HEAD"]).stdout.decode().strip(),
            "variants": {}, "builds": {}, "archive_evidence": {},
            "notes": "Gettext remains enabled; only link mechanisms change. Translation catalogs not guaranteed in runner. Static linking accepted only if PE import disappears."}
  binaries = {}
  try:
    archives = {x: require_archive(x) for x in ("libintl.a", "libiconv.a", "libz.a")}
    report["archive_evidence"] = archives
    default = []
    configurations = [
      ("A_default", default, []),
      ("B_gettext_static_intl", default, replacement(archives["libintl.a"], "-lintl")),
      ("C_gettext_static_iconv_zlib", default, replacement(archives["libiconv.a"], "-liconv")+
       replacement(archives["libz.a"], "-lz")),
      ("D_gettext_static_all_three", default,
       replacement(archives["libintl.a"], "-lintl")+
       replacement(archives["libiconv.a"], "-liconv")+
       replacement(archives["libz.a"], "-lz")),
    ]
    for label, options, patch in configurations:
      try:
        binary, details = build(label, options, patch or None)
        report["builds"][label] = details
        if binary is not None:
          binaries[label] = binary
      except Exception as error:
        report["builds"][label] = {"status":"build-error",
                                    "error": type(error).__name__ + ": " + str(error)}
    needed = {x[0] for x in configurations}
    if set(binaries) != needed:
      raise RuntimeError("missing variants: "+repr(sorted(needed-set(binaries))))
    for label in binaries:
      report["variants"][label] = report["builds"][label]["dll_imports"]
    imports_a = set(imports(binaries["A_default"]))
    if "libintl-8.dll" not in imports_a:
      raise RuntimeError("dynamic baseline unexpectedly has no libintl DLL")
    for label in ("B_gettext_static_intl", "D_gettext_static_all_three"):
      if "libintl-8.dll" in imports(binaries[label]):
        raise RuntimeError(label + " did not statically link intl")
    for label in ("C_gettext_static_iconv_zlib", "D_gettext_static_all_three"):
      current = set(imports(binaries[label]))
      if {"libiconv-2.dll","zlib1.dll"} & current:
        raise RuntimeError(label + " not really statically linked")
    validate_library_contract(binaries, report)
    # Use the existing same-run interleaved full-lifetime probe.
    measure(binaries, report)
  finally:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2)+"\n",encoding="utf-8")
    print("SUMMARY", json.dumps({
      "sha":report["sha"],
      "builds": {k:{"status":v.get("status"),"error":v.get("error"),"imports":v.get("dll_imports"),
                   "build_failure":v.get("make_stderr_tail","")[-200:]}
                 for k,v in report["builds"].items()},
      "measurement_status":report.get("measurement_status"),
      "medians":{k:{op:round(v["median_ms"],4) for op,v in m.items()}
                 for k,m in report.get("measurements",{}).items()}
    },indent=2),flush=True)

if __name__ == "__main__":
  main()
