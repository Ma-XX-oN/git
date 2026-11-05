#!/usr/bin/env python3
"""Issue 809: prepare and verify Git gettext-preserving static acceptance.

Mutates only disposable CI checkout Makefile. Does not change Git source.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path.cwd()

def archive():
  p = subprocess.run(["gcc", "-print-file-name=libintl.a"], check=True,
                     capture_output=True, text=True)
  path = Path(p.stdout.strip())
  if not path.is_file() or not path.name == "libintl.a":
    raise RuntimeError("real static libintl archive unavailable")
  members = subprocess.run(["ar", "t", str(path)], check=True,
                           capture_output=True, text=True).stdout.splitlines()
  if len(members) < 2:
    raise RuntimeError("static libintl archive lacks object members")
  return path

def prepare():
  lib = archive()
  file = ROOT / "Makefile"
  old = file.read_text(encoding="utf-8")
  marker = "\tEXTLIBS += -lintl\n"
  if old.count(marker) != 1:
    raise RuntimeError("Makefile libintl link site not unique")
  replacement = "\tEXTLIBS += " + str(lib).replace("\\", "/") + "\n"
  file.write_text(old.replace(marker, replacement, 1), encoding="utf-8")
  print("STATIC_INTL_ARCHIVE", lib)
  print("STATIC_INTL_SHA256", hashlib.sha256(lib.read_bytes()).hexdigest())

def verify(mode):
  import pefile
  exe = ROOT / "git.exe"
  if not exe.is_file():
    raise RuntimeError("git.exe was not built")
  image = pefile.PE(str(exe), fast_load=True)
  try:
    image.parse_data_directories(
      directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    imports = {x.dll.decode("ascii", "replace").lower()
               for x in image.DIRECTORY_ENTRY_IMPORT}
  finally:
    image.close()
  has_intl = "libintl-8.dll" in imports
  if mode == "static" and has_intl:
    raise RuntimeError("static libintl build still imports libintl DLL")
  if mode == "dynamic" and not has_intl:
    raise RuntimeError("dynamic baseline unexpectedly lacks libintl")
  out = subprocess.run([str(exe), "--version"], check=True,
                       capture_output=True)
  if not out.stdout.startswith(b"git version "):
    raise RuntimeError("invalid Git version command output")
  evidence = {
    "mode": mode, "binary_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
    "imported_dlls": sorted(imports), "gettext_enabled":True,
    "git_version": out.stdout.decode("utf-8", "replace").strip(),
    "source_revision": os.environ.get("GITHUB_SHA", ""),
  }
  path = ROOT / ("git809-acceptance-" + mode + ".json")
  path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
  print(json.dumps(evidence, indent=2))

if __name__ == "__main__":
  a = argparse.ArgumentParser()
  a.add_argument("operation", choices=["prepare", "verify"])
  a.add_argument("--mode", choices=["dynamic", "static"])
  opts = a.parse_args()
  if opts.operation == "prepare":
    prepare()
  else:
    if not opts.mode:
      raise SystemExit("--mode is required for verify")
    verify(opts.mode)
